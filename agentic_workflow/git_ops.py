"""Guarded Git, GitHub publication, and project-validation operations.

The Jira delivery graph calls this module only after human approval.  It creates
or reuses an agent branch, snapshots candidate changes, runs configured project
validators, and commits, pushes, and opens a draft pull request only for paths
explicitly approved by the user.  The module also rejects sensitive paths so
local configuration and repository metadata cannot be accidentally published.
"""

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List



FORBIDDEN_PATHS = (".env", ".agents/", ".claude/", ".git/", ".agentic-workflow/")


def configured_validation_commands() -> tuple[tuple[str, ...], ...]:
    """Parse ``WORKFLOW_VALIDATION_COMMANDS``; empty means validation is off."""
    configured = os.getenv("WORKFLOW_VALIDATION_COMMANDS", "").strip()
    commands = tuple(tuple(shlex.split(command)) for command in configured.split(";") if command.strip())
    if any(not command for command in commands):
        raise RuntimeError("WORKFLOW_VALIDATION_COMMANDS must contain shell-style commands separated by semicolons")
    return commands


class GitOperations:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _run(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo_root, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git command failed")
        return result.stdout.strip()

    def _run_gh(self, *args: str) -> str:
        """Run GitHub CLI commands without routing them through Git."""
        result = subprocess.run(["gh", *args], cwd=self.repo_root, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "GitHub CLI command failed")
        return result.stdout.strip()

    def _has_staged_changes(self, paths: List[str]) -> bool:
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", *paths],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        raise RuntimeError(result.stderr.strip() or "Unable to inspect staged changes")

    def create_branch(self, ticket_key: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", ticket_key.lower()).strip("-")
        branch = f"agent/{slug}"
        # A retry after a failed validation must continue from the same delivery
        # branch rather than failing before the coding agent can address it.
        existing = self._run("branch", "--list", branch)
        if existing:
            self._run("switch", branch)
        else:
            self._run("switch", "-c", branch)
        return branch

    def _changed_files(self) -> Dict[str, str]:
        """Return every tracked or untracked file currently different from HEAD."""
        changes: Dict[str, str] = {}
        for row in self._run("diff", "--name-status", "HEAD", "--").splitlines():
            columns = row.split("\t")
            if len(columns) >= 2:
                changes[columns[-1]] = columns[0][0]
        for path in self._run("ls-files", "--others", "--exclude-standard", "--").splitlines():
            if path:
                changes[path] = "A"
        return changes

    def _fingerprint(self, path: str) -> str:
        target = (self.repo_root / path).resolve()
        if not target.exists() or not target.is_file():
            return "<deleted>"
        digest = hashlib.sha256()
        with target.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def snapshot_changes(self) -> Dict[str, str]:
        """Fingerprint pre-existing working-tree changes before an agent run."""
        return {path: self._fingerprint(path) for path in self._changed_files()}

    def changes_since(self, baseline: Dict[str, str]) -> List[Dict[str, str]]:
        """List files whose current content differs from the start-of-run snapshot."""
        current = self._changed_files()
        changes = []
        for path, status in current.items():
            if self._fingerprint(path) != baseline.get(path):
                changes.append({"path": path, "status": status})
        return sorted(changes, key=lambda item: item["path"])

    def diff_for_path(self, path: str) -> str:
        target = (self.repo_root / path).resolve()
        if self.repo_root not in target.parents:
            raise ValueError("Unknown file change")
        untracked = self._run("ls-files", "--others", "--exclude-standard", "--", path)
        command = ("git", "diff", "--no-index", "--", "/dev/null", path) if untracked else ("git", "diff", "--no-ext-diff", "HEAD", "--", path)
        result = subprocess.run(command, cwd=self.repo_root, text=True, capture_output=True, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "Unable to read file diff")
        return result.stdout or "No text diff is available for this file."

    def validate_paths(self, paths: List[str]) -> None:
        if not paths:
            raise RuntimeError("No changed files selected for publication")
        for path in paths:
            target = (self.repo_root / path).resolve()
            if self.repo_root not in target.parents and target != self.repo_root:
                raise RuntimeError(f"Path escapes repository: {path}")
            if path.startswith(FORBIDDEN_PATHS):
                raise RuntimeError(f"Protected path cannot be published: {path}")

    def commit_and_push(self, ticket_key: str, summary: str, paths: List[str]) -> str:
        self.validate_paths(paths)
        self._run("add", "--", *paths)
        if self._has_staged_changes(paths):
            self._run("commit", "--only", "-m", f"{ticket_key}: {summary}", "--", *paths)
        self._run("push", "-u", "origin", "HEAD")
        return self._run("rev-parse", "HEAD")

    def create_draft_pull_request(self, ticket_key: str, summary: str, branch: str) -> str:
        """Open a reviewable draft PR for the branch that passed final approval."""
        existing = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
        title = f"{ticket_key}: {summary}"
        body = f"## Jira\n\n{ticket_key}\n\nCreated by the human-approved local delivery workflow."
        return self._run_gh(
            "pr",
            "create",
            "--draft",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        )


class ProjectValidator:
    """Runs optional validation commands after implementation.

    Validation is off unless ``WORKFLOW_VALIDATION_COMMANDS`` lists
    semicolon-separated commands; with none configured the step is skipped.
    """

    def __init__(self, repo_root: Path, commands: tuple[tuple[str, ...], ...] | None = None) -> None:
        self.repo_root = repo_root
        self.commands = commands if commands is not None else configured_validation_commands()

    def run(self) -> Dict[str, object]:
        if not self.commands:
            return {"passed": True, "skipped": True, "results": []}
        results = []
        for command in self.commands:
            run = subprocess.run(command, cwd=self.repo_root, text=True, capture_output=True, check=False)
            results.append({"command": " ".join(command), "exit_code": run.returncode, "output": (run.stdout + run.stderr)[-6000:]})
        return {"passed": all(item["exit_code"] == 0 for item in results), "results": results}


def _branch_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def ticket_branch_name(ticket_key: str, summary: str) -> str:
    """``feature/<key>-<first words of the summary>``, kept short and ref-safe."""
    words = _branch_slug(summary).split("-")[:6]
    name = "-".join([_branch_slug(ticket_key), *[word for word in words if word]])
    return "feature/" + name[:60].rstrip("-")


class WorktreeOperations:
    """Branch, worktree, commit, and PR operations for the SDLC beta flow.

    Implementation happens in a separate Git worktree next to the repository
    (``<repo>.worktrees/<branch>``), so the developer's own checkout and its
    uncommitted changes are never touched. Every write happens only after the
    graph's human approval gates.
    """

    EXCLUDED_PATHSPECS = tuple(f":(exclude){path.rstrip('/')}" for path in FORBIDDEN_PATHS if path != ".git/")

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    @staticmethod
    def _run_in(cwd: Path, *command: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(list(command), cwd=cwd, text=True, capture_output=True, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or f"{command[0]} command failed")
        return result

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return self._run_in(cwd or self.repo_root, "git", *args).stdout.strip()

    def default_base_branch(self) -> str:
        head = self._run_in(self.repo_root, "git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        name = head.stdout.strip()
        return name.removeprefix("origin/") if head.returncode == 0 and name.startswith("origin/") else "main"

    def worktree_root(self) -> Path:
        return self.repo_root.parent / f"{self.repo_root.name}.worktrees"

    def ensure_branch_worktree(self, branch: str, base_branch: str) -> Dict[str, str]:
        """Create or reuse the ticket branch and its isolated worktree."""
        fetched = self._run_in(self.repo_root, "git", "fetch", "origin", base_branch, check=False).returncode == 0
        base_ref = f"origin/{base_branch}" if fetched else base_branch
        if self._git("branch", "--list", branch):
            branch_status = "reused"
        else:
            self._git("branch", "--no-track", branch, base_ref)
            branch_status = "created"

        path = self.worktree_root() / branch.replace("/", "-")
        registered = self._git("worktree", "list", "--porcelain").splitlines()
        if f"worktree {path}" in registered:
            worktree_status = "reused"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._git("worktree", "add", str(path), branch)
            except RuntimeError as error:
                raise RuntimeError(
                    f"Could not create a worktree for {branch}. If that branch is checked out in your own "
                    f"checkout, switch away from it and retry. Git said: {error}"
                ) from error
            worktree_status = "created"
        return {
            "branch": branch,
            "base_branch": base_branch,
            "base_ref": base_ref,
            "path": str(path),
            "branch_status": branch_status,
            "worktree_status": worktree_status,
        }

    def changes(self, worktree: Path, base_ref: str) -> List[Dict[str, str]]:
        """Committed and uncommitted changes in the worktree relative to the base."""
        changes: Dict[str, str] = {}
        for row in self._git("diff", "--name-status", base_ref, "--", cwd=worktree).splitlines():
            columns = row.split("\t")
            if len(columns) >= 2:
                changes[columns[-1]] = columns[0][0]
        for path in self._git("ls-files", "--others", "--exclude-standard", "--", cwd=worktree).splitlines():
            if path:
                changes[path] = "A"
        return [{"path": path, "status": status} for path, status in sorted(changes.items()) if not path.startswith(FORBIDDEN_PATHS)]

    def diff_for_path(self, worktree: Path, base_ref: str, path: str) -> str:
        target = (worktree / path).resolve()
        if worktree.resolve() not in target.parents:
            raise ValueError("Unknown file change")
        untracked = self._git("ls-files", "--others", "--exclude-standard", "--", path, cwd=worktree)
        command = ("git", "diff", "--no-index", "--", "/dev/null", path) if untracked else ("git", "diff", "--no-ext-diff", base_ref, "--", path)
        result = self._run_in(worktree, *command, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "Unable to read file diff")
        return result.stdout or "No text diff is available for this file."

    def combined_diff(self, worktree: Path, base_ref: str, limit: int = 120_000) -> str:
        diff = "".join(self.diff_for_path(worktree, base_ref, change["path"]) for change in self.changes(worktree, base_ref))
        return diff if len(diff) <= limit else diff[:limit] + "\n[diff truncated]\n"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        rows = self._run_in(worktree, "git", "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        return any(not row[3:].strip('"').startswith(FORBIDDEN_PATHS) for row in rows if row.strip())

    def commit(self, worktree: Path, ticket_key: str, message: str, fallback_subject: str) -> Dict[str, str]:
        """Stage every non-protected change and commit with the Jira key in the subject."""
        self._git("add", "-A", "--", ".", *self.EXCLUDED_PATHSPECS, cwd=worktree)
        if self._run_in(worktree, "git", "diff", "--cached", "--quiet", check=False).returncode == 0:
            return {"status": "skipped_clean", "sha": self._git("rev-parse", "HEAD", cwd=worktree), "subject": ""}
        lines = message.strip().splitlines() or [fallback_subject]
        subject, body = lines[0].strip() or fallback_subject, "\n".join(lines[1:]).strip()
        if ticket_key not in subject:
            subject = f"{ticket_key}: {subject}"
        self._git("commit", "-m", subject, *(["-m", body] if body else []), cwd=worktree)
        return {"status": "committed", "sha": self._git("rev-parse", "HEAD", cwd=worktree), "subject": subject}

    def push_and_open_pull_request(
        self, worktree: Path, branch: str, base_branch: str, base_ref: str, title: str, body: str, draft: bool
    ) -> Dict[str, str]:
        if self._git("rev-list", "--count", f"{base_ref}..HEAD", cwd=worktree) == "0":
            return {"status": "skipped_no_commits", "url": ""}
        self._git("push", "-u", "origin", branch, cwd=worktree)
        existing = self._run_in(worktree, "gh", "pr", "view", branch, "--json", "url", "--jq", ".url", check=False)
        if existing.returncode == 0 and existing.stdout.strip():
            return {"status": "updated_existing", "url": existing.stdout.strip()}
        command = ["gh", "pr", "create", "--base", base_branch, "--head", branch, "--title", title, "--body", body]
        if draft:
            command.append("--draft")
        url = self._run_in(worktree, *command).stdout.strip()
        return {"status": "created_draft" if draft else "created_ready", "url": url}

    def remove_worktree(self, worktree: Path) -> str:
        """Remove a clean worktree; keep one with uncommitted work. The branch always stays."""
        if not worktree.exists():
            return "already_removed"
        if self.has_uncommitted_changes(worktree):
            return "kept_uncommitted_changes"
        # --force only clears ignored files; there is no tracked or untracked work left.
        self._git("worktree", "remove", "--force", str(worktree))
        self._git("worktree", "prune")
        return "removed"

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

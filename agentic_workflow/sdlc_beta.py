"""LangGraph definition for the SDLC beta flow: Jira ticket to reviewed PR.

Compared with Jira delivery, this flow checks GitHub and Jira access before
planning, implements in an isolated Git worktree instead of the developer's
checkout, assesses the Swift blast radius, and runs an agent code review before
the human publication gate. Rejecting the finished change still runs cleanup and
returns a final status report; only an approval commits, pushes, and opens a PR.
"""

import subprocess
from pathlib import Path
from typing import Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adapters import (
    ChangeReviewer,
    DeliveryPlanner,
    DetailedMcpTicketClient,
    WorktreeImplementer,
    _active_workflow_configuration,
    _require_ticket_mcp,
)
from .contracts import WorkflowState
from .git_ops import ProjectValidator, WorktreeOperations, configured_validation_commands, ticket_branch_name
from .swift_graph import assess_blast_radius


GUARDRAIL_FILES = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")
BASE_GUARDRAILS = (
    "- Stay inside the approved ticket scope; do not refactor unrelated code.\n"
    "- Never commit, push, open pull requests, alter remotes, or read secrets; the workflow does that after approval.\n"
    "- Never touch .env files, .git/, or agent configuration directories.\n"
    "- Add or update tests for changed behaviour and run the validation commands.\n"
    "- Follow the repository's existing naming, structure, and comment style."
)


def _approval(value: object, phase: str) -> Dict[str, object]:
    if not isinstance(value, dict) or value.get("action") not in {"approve", "revise", "reject"}:
        raise ValueError(f"{phase} approval must be JSON: {{'action': 'approve'|'revise'|'reject', ...}}")
    return value


def repository_guardrails(repo_root: Path) -> str:
    sections = [BASE_GUARDRAILS]
    for name in GUARDRAIL_FILES:
        source = repo_root / name
        if source.is_file():
            sections.append(f"From {name}:\n{source.read_text(errors='replace')[:8000]}")
    return "\n\n".join(sections)


def check_access(repo_root: Path) -> Dict[str, str]:
    """Fail before planning if GitHub CLI or the Jira MCP server is not usable."""
    auth = subprocess.run(["gh", "auth", "status"], cwd=repo_root, text=True, capture_output=True, check=False)
    if auth.returncode:
        raise RuntimeError("GitHub CLI is not authenticated. Run `gh auth login`, then retry. " + auth.stderr.strip()[-300:])
    remote = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=repo_root, text=True, capture_output=True, check=False,
    )
    if remote.returncode:
        raise RuntimeError("This repository has no GitHub origin that GitHub CLI can open. " + remote.stderr.strip()[-300:])
    server = _require_ticket_mcp(_active_workflow_configuration().get("provider", "codex"))
    return {"github": "authenticated", "repository": remote.stdout.strip(), "jira_mcp": server}


def build_sdlc_beta_graph(
    repo_root: Path,
    ticket_client=None,
    planner=None,
    implementer_factory=None,
    reviewer_factory=None,
    worktrees=None,
    access_check=None,
    validator_factory=None,
    report=None,
):
    """Build the SDLC beta graph for one explicit target repository.

    Optional dependencies support deterministic tests. With no overrides, nodes
    resolve the local agent, GitHub CLI, and Git adapters only when reached.
    """
    worktrees = worktrees or WorktreeOperations(repo_root)
    access_check = access_check or check_access
    validator_factory = validator_factory or (lambda path: ProjectValidator(path))

    def progress(stage: str, message: str) -> None:
        if report:
            report(stage, message)

    def worktree_path(state: WorkflowState) -> Path:
        return Path(state["worktree"]["path"])

    def repo_context(state: WorkflowState):
        progress("resolving_repository", "Resolving repository, base branch, and engineering guardrails")
        current = subprocess.run(["git", "branch", "--show-current"], cwd=repo_root, text=True, capture_output=True, check=False)
        context = {
            "repository": str(repo_root),
            "base_branch": state.get("base_branch") or worktrees.default_base_branch(),
            "current_branch": current.stdout.strip(),
            "worktree_root": str(worktrees.worktree_root()),
            "validation_commands": [" ".join(command) for command in configured_validation_commands()],
            "guardrails": repository_guardrails(repo_root),
        }
        return {"repo_context": context, "status": "checking_access"}

    def preflight_auth(state: WorkflowState):
        progress("preflight", "Checking GitHub CLI authentication and the Jira MCP server")
        return {"auth": access_check(repo_root), "status": "fetching_ticket"}

    def load_jira_ticket(state: WorkflowState):
        progress("fetching_ticket", "Fetching the Jira ticket and its acceptance criteria")
        client = ticket_client or DetailedMcpTicketClient(repo_root)
        ticket = client.fetch(state["ticket_key"])
        summary = ticket.get("summary") if isinstance(ticket, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError(
                "Atlassian returned the ticket key but no usable ticket title. "
                "Check the Jira issue summary and the Atlassian MCP connection, then retry."
            )
        ticket = {**ticket, "branch_name": ticket_branch_name(ticket["key"], summary)}
        return {"ticket": ticket, "status": "planning"}

    def create_plan(state: WorkflowState):
        progress("planning", "Creating the plan, validation approach, review focus, and PR summary")
        active_planner = planner or DeliveryPlanner(repo_root)
        brief = active_planner.create(state["ticket"], state["repo_context"], state.get("plan_feedback"))
        return {"plan": brief["plan"], "delivery_brief": brief, "plan_feedback": ""}

    def approve_plan(state: WorkflowState):
        progress("awaiting_plan_review", "Waiting for your plan decision")
        decision = _approval(interrupt({
            "phase": "plan_review",
            "ticket": state["ticket"],
            "plan": state["plan"],
            "delivery_brief": state["delivery_brief"],
        }), "plan")
        if decision["action"] == "approve":
            return Command(goto="ensure_branch", update={"plan_decision": "approve", "status": "plan_approved"})
        if decision["action"] == "revise":
            return Command(goto="create_plan", update={"plan_decision": "revise", "plan_feedback": str(decision.get("feedback", ""))})
        return Command(goto=END, update={"plan_decision": "reject", "status": "plan_rejected"})

    def ensure_branch(state: WorkflowState):
        progress("preparing_worktree", "Creating or reusing the ticket branch and its isolated worktree")
        worktree = worktrees.ensure_branch_worktree(state["ticket"]["branch_name"], state["repo_context"]["base_branch"])
        return {"worktree": worktree, "status": "implementing"}

    def implement_change(state: WorkflowState):
        progress("implementing", "Coding agent is implementing the approved plan in the worktree")
        path = worktree_path(state)
        implementer = (implementer_factory or WorktreeImplementer)(path)
        implementation = implementer.run(
            state["ticket"], state["delivery_brief"], state["worktree"], state["repo_context"]["guardrails"], state.get("change_feedback") or None
        )
        progress("validating", "Running configured validation in the worktree")
        validation = validator_factory(path).run()
        return {
            "implementation": implementation,
            "validation": validation,
            "file_changes": worktrees.changes(path, state["worktree"]["base_ref"]),
            "change_feedback": {},
            "status": "assessing_impact",
        }

    def graph_context(state: WorkflowState):
        progress("graph_context", "Assessing the blast radius of changed Swift production files")
        paths = [change["path"] for change in state.get("file_changes", [])]
        return {"graph_context": assess_blast_radius(worktree_path(state), paths), "status": "reviewing_change"}

    def review_change(state: WorkflowState):
        progress("reviewing_change", "Reviewing the change for correctness, tests, wiring, and standards")
        path = worktree_path(state)
        reviewer = (reviewer_factory or ChangeReviewer)(path)
        review = reviewer.review(
            state["ticket"],
            state["delivery_brief"],
            state["implementation"],
            worktrees.combined_diff(path, state["worktree"]["base_ref"]),
            state["graph_context"],
            state["repo_context"]["guardrails"],
        )
        return {"change_review": review, "status": "ready_for_change_review"}

    def approve_pr(state: WorkflowState):
        progress("awaiting_change_review", "Waiting for your decision on the finished change")
        decision = _approval(interrupt({
            "phase": "change_review",
            "ticket": state["ticket"],
            "worktree": state["worktree"],
            "implementation": state["implementation"],
            "validation": state["validation"],
            "file_changes": state.get("file_changes", []),
            "graph_context": state["graph_context"],
            "change_review": state["change_review"],
        }), "change")
        if decision["action"] == "revise":
            feedback = {"feedback": str(decision.get("feedback", "")), "previous_review": state["change_review"]}
            return Command(goto="implement_change", update={"change_decision": "revise", "change_feedback": feedback})
        # A rejection still commits nothing and opens no PR, but it runs cleanup and reports the outcome.
        return Command(goto="commit_changes", update={"change_decision": decision["action"]})

    def commit_changes(state: WorkflowState):
        if state.get("change_decision") != "approve":
            return {"commit": {"status": "skipped_not_approved", "sha": "", "subject": ""}}
        progress("committing", "Committing the approved change")
        ticket = state["ticket"]
        commit = worktrees.commit(
            worktree_path(state), ticket["key"], state["implementation"].get("commit_message", ""), f"{ticket['key']}: {ticket['summary']}"
        )
        return {"commit": commit, "commit_sha": commit["sha"]}

    def create_pr(state: WorkflowState):
        if state.get("change_decision") != "approve":
            return {"pr_result": {"status": "skipped_not_approved", "url": ""}}
        progress("creating_pr", "Pushing the branch and opening the pull request")
        ticket, implementation, worktree = state["ticket"], state["implementation"], state["worktree"]
        title = str(implementation.get("pr_title") or state["delivery_brief"]["pr_summary"]["title"]).strip()
        if ticket["key"] not in title:
            title = f"{ticket['key']}: {title}"
        body = str(implementation.get("pr_body") or state["delivery_brief"]["pr_summary"]["body"]).strip()
        body += f"\n\n---\nJira: [{ticket['key']}]({ticket.get('url', '')})"
        result = worktrees.push_and_open_pull_request(
            worktree_path(state), worktree["branch"], worktree["base_branch"], worktree["base_ref"], title, body,
            draft=state.get("pr_mode", "draft") != "ready",
        )
        return {"pr_result": result, "pull_request_url": result["url"]}

    def cleanup_worktree(state: WorkflowState):
        progress("cleaning_up", "Removing the temporary worktree; the branch is kept")
        return {"cleanup": {"status": worktrees.remove_worktree(worktree_path(state)), "path": state["worktree"]["path"]}}

    def finalize_result(state: WorkflowState):
        decision = state.get("change_decision")
        url = state.get("pr_result", {}).get("url", "")
        status = "published" if url else "change_rejected" if decision == "reject" else "completed_without_pr"
        result = {
            "ticket": state["ticket"]["key"],
            "ticket_url": state["ticket"].get("url", ""),
            "branch": state["worktree"]["branch"],
            "plan_approval": state.get("plan_decision", ""),
            "change_approval": decision or "",
            "authentication": state.get("auth", {}),
            "review_verdict": state.get("change_review", {}).get("verdict", ""),
            "blast_radius": state.get("graph_context", {}).get("blast_radius", ""),
            "commit": state.get("commit", {}),
            "pull_request": state.get("pr_result", {}),
            "cleanup": state.get("cleanup", {}),
        }
        progress("finalizing", "Workflow result recorded")
        return {"result": result, "status": status}

    def retry_request(state: WorkflowState):
        """Checkpoint marker used by the runtime before a stage-aware retry."""
        return {}

    def start_route(state: WorkflowState):
        return "retry_request" if state.get("retry_target") else "repo_context"

    def retry_dispatch(state: WorkflowState):
        target = state.get("retry_target")
        allowed_targets = {
            "repo_context", "preflight_auth", "load_jira_ticket", "create_plan", "ensure_branch", "implement_change",
            "graph_context", "review_change", "commit_changes", "create_pr", "cleanup_worktree",
        }
        if target not in allowed_targets:
            raise ValueError("This SDLC run cannot be retried from its current state")
        progress("retrying", f"Retrying {str(target).replace('_', ' ')}")
        return Command(goto=target, update={"status": "retrying", "retry_target": ""})

    graph = StateGraph(WorkflowState)
    for name, node in (
        ("repo_context", repo_context),
        ("preflight_auth", preflight_auth),
        ("load_jira_ticket", load_jira_ticket),
        ("create_plan", create_plan),
        ("approve_plan", approve_plan),
        ("ensure_branch", ensure_branch),
        ("implement_change", implement_change),
        ("graph_context", graph_context),
        ("review_change", review_change),
        ("approve_pr", approve_pr),
        ("commit_changes", commit_changes),
        ("create_pr", create_pr),
        ("cleanup_worktree", cleanup_worktree),
        ("finalize_result", finalize_result),
        ("retry_request", retry_request),
        ("retry_dispatch", retry_dispatch),
    ):
        graph.add_node(name, node)
    graph.add_conditional_edges(START, start_route, {"repo_context": "repo_context", "retry_request": "retry_request"})
    graph.add_edge("repo_context", "preflight_auth")
    graph.add_edge("preflight_auth", "load_jira_ticket")
    graph.add_edge("load_jira_ticket", "create_plan")
    graph.add_edge("create_plan", "approve_plan")
    graph.add_edge("ensure_branch", "implement_change")
    graph.add_edge("implement_change", "graph_context")
    graph.add_edge("graph_context", "review_change")
    graph.add_edge("review_change", "approve_pr")
    graph.add_edge("commit_changes", "create_pr")
    graph.add_edge("create_pr", "cleanup_worktree")
    graph.add_edge("cleanup_worktree", "finalize_result")
    graph.add_edge("finalize_result", END)
    graph.add_edge("retry_request", "retry_dispatch")
    return graph

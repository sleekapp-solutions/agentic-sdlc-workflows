"""Human-reviewed, read-only GitHub pull-request review workflow."""

from pathlib import Path
from typing import Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adapters import CodexPullRequestReviewer, GitHubCliPullRequestClient
from .contracts import WorkflowState


def _approval(value: object) -> Dict[str, object]:
    if not isinstance(value, dict) or value.get("action") not in {"approve", "reject"}:
        raise ValueError("PR review approval must be JSON: {'action': 'approve'|'reject'}")
    return value


def build_pr_review_graph(repo_root: Path, pull_request_client=None, reviewer=None, report=None):
    """Build a workflow that never changes code or posts review comments."""

    def progress(stage: str, message: str) -> None:
        if report:
            report(stage, message)

    def fetch_pull_request(state: WorkflowState):
        progress("fetching_pull_request", "Fetching pull request metadata and patch through GitHub CLI")
        client = pull_request_client or GitHubCliPullRequestClient(repo_root)
        number = state["pr_number"]
        return {"pull_request": client.fetch(number), "pull_request_diff": client.diff(number), "status": "reviewing_pull_request"}

    def review_pull_request(state: WorkflowState):
        progress("reviewing_pull_request", "Analyzing the pull request locally with Codex")
        active_reviewer = reviewer or CodexPullRequestReviewer(repo_root)
        return {"review": active_reviewer.review(state["pull_request"], state["pull_request_diff"])}

    def review_result(state: WorkflowState):
        progress("awaiting_pr_review", "Waiting for acknowledgement of the local code review")
        decision = _approval(interrupt({"phase": "pr_review", "pull_request": state["pull_request"], "review": state["review"]}))
        status = "pr_review_acknowledged" if decision["action"] == "approve" else "pr_review_dismissed"
        return Command(goto=END, update={"status": status})

    graph = StateGraph(WorkflowState)
    graph.add_node("fetch_pull_request", fetch_pull_request)
    graph.add_node("review_pull_request", review_pull_request)
    graph.add_node("review_result", review_result)
    graph.add_edge(START, "fetch_pull_request")
    graph.add_edge("fetch_pull_request", "review_pull_request")
    graph.add_edge("review_pull_request", "review_result")
    return graph

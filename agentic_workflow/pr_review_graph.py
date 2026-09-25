"""LangGraph definition for a human-reviewed PR review flow.

The flow fetches pull-request metadata and its patch through GitHub CLI, asks
the selected local agent for structured findings, and pauses for local human
publication approval. It never changes repository files; posting a review is
an explicit, final human-approved GitHub action.
"""

from pathlib import Path
from typing import Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adapters import GitHubCliPullRequestClient, LocalPullRequestReviewer
from .contracts import WorkflowState


def _approval(value: object) -> Dict[str, object]:
    if not isinstance(value, dict) or value.get("action") not in {"publish", "dismiss"}:
        raise ValueError("PR review decision must be JSON: {'action': 'publish'|'dismiss', ...}")
    return value


def build_pr_review_graph(repo_root: Path, pull_request_client=None, reviewer=None, report=None):
    """Build the PR review graph for one explicit repository.

    Optional dependencies are injection points for tests or alternative
    integrations. The default adapters use GitHub CLI for retrieval and final,
    explicit review publication, plus the selected local agent for analysis.
    """

    def progress(stage: str, message: str) -> None:
        if report:
            report(stage, message)

    def fetch_pull_request(state: WorkflowState):
        progress("fetching_pull_request", "Fetching pull request metadata and patch through GitHub CLI")
        client = pull_request_client or GitHubCliPullRequestClient(repo_root)
        number = state["pr_number"]
        return {"pull_request": client.fetch(number), "pull_request_diff": client.diff(number), "status": "reviewing_pull_request"}

    def review_pull_request(state: WorkflowState):
        progress("reviewing_pull_request", "Analyzing the pull request with the selected local provider")
        active_reviewer = reviewer or LocalPullRequestReviewer(repo_root)
        return {"review": active_reviewer.review(state["pull_request"], state["pull_request_diff"])}

    def review_result(state: WorkflowState):
        progress("awaiting_pr_review", "Waiting for approval to publish the pull-request review")
        decision = _approval(interrupt({"phase": "pr_review", "pull_request": state["pull_request"], "review": state["review"]}))
        if decision["action"] == "dismiss":
            return Command(goto=END, update={"status": "pr_review_dismissed"})
        event = decision.get("event")
        if event not in {"approve", "request_changes", "comment"}:
            raise ValueError("Published review event must be approve, request_changes, or comment")
        body = decision.get("body", state["review"].get("summary", ""))
        if not isinstance(body, str):
            raise ValueError("Published review body must be text")
        indexes = decision.get("finding_indexes", [])
        if not isinstance(indexes, list) or any(not isinstance(index, int) for index in indexes):
            raise ValueError("Selected review findings must be a list of indexes")
        findings = state["review"].get("findings", [])
        if any(index < 0 or index >= len(findings) for index in indexes):
            raise ValueError("A selected review finding is no longer available")
        selected_findings = [findings[index] for index in dict.fromkeys(indexes)]
        return Command(
            goto="publish_review",
            update={"review_event": event, "review_body": body, "review_findings": selected_findings},
        )

    def publish_review(state: WorkflowState):
        progress("publishing_review", "Publishing the explicitly approved pull-request review to GitHub")
        client = pull_request_client or GitHubCliPullRequestClient(repo_root)
        published_review = client.publish_review(
            state["pr_number"], state["review_event"], state["review_body"], state["review_findings"], state["pull_request"]
        )
        return {"published_review": published_review, "status": "pr_review_published"}

    def retry_request(state: WorkflowState):
        """Checkpoint marker used by the runtime before a stage-aware retry."""
        return {}

    def start_route(state: WorkflowState):
        return "retry_request" if state.get("retry_target") else "fetch_pull_request"

    def retry_dispatch(state: WorkflowState):
        target = state.get("retry_target")
        if target not in {"fetch_pull_request", "review_pull_request", "publish_review"}:
            raise ValueError("This pull-request review cannot be retried from its current state")
        progress("retrying", f"Retrying {str(target).replace('_', ' ')}")
        return Command(goto=target, update={"status": "retrying", "retry_target": ""})

    graph = StateGraph(WorkflowState)
    graph.add_node("fetch_pull_request", fetch_pull_request)
    graph.add_node("review_pull_request", review_pull_request)
    graph.add_node("review_result", review_result)
    graph.add_node("publish_review", publish_review)
    graph.add_node("retry_request", retry_request)
    graph.add_node("retry_dispatch", retry_dispatch)
    graph.add_conditional_edges(START, start_route, {"fetch_pull_request": "fetch_pull_request", "retry_request": "retry_request"})
    graph.add_edge("fetch_pull_request", "review_pull_request")
    graph.add_edge("review_pull_request", "review_result")
    graph.add_edge("publish_review", END)
    graph.add_edge("retry_request", "retry_dispatch")
    return graph

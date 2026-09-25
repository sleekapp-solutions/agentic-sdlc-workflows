"""Shared workflow state and dependency contracts.

``WorkflowState`` is the durable data passed between LangGraph nodes.  The
small abstract client classes define the ports used by the graphs, allowing
real local adapters to be swapped for fakes in tests.  These contracts describe
data and capabilities only; they do not perform network, agent, Git, or file
operations themselves.
"""

from typing import Any, Dict, List, Optional, TypedDict


class WorkflowState(TypedDict, total=False):
    ticket_key: str
    ticket: Dict[str, Any]
    plan: Dict[str, Any]
    plan_feedback: str
    branch: str
    implementation: Dict[str, Any]
    validation: Dict[str, Any]
    baseline: Dict[str, str]
    file_changes: List[Dict[str, str]]
    candidate_paths: List[str]
    commit_sha: str
    pull_request_url: str
    error: str
    status: str
    pr_number: int
    pull_request: Dict[str, Any]
    pull_request_diff: str
    review: Dict[str, Any]
    review_event: str
    review_body: str
    review_findings: List[Dict[str, Any]]
    published_review: Dict[str, Any]
    retry_target: str


class TicketClient:
    def fetch(self, ticket_key: str) -> Dict[str, Any]:
        raise NotImplementedError


class Planner:
    def create(self, ticket: Dict[str, Any], feedback: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError


class Implementer:
    def run(self, ticket: Dict[str, Any], plan: Dict[str, Any], branch: str) -> Dict[str, Any]:
        raise NotImplementedError


class PullRequestClient:
    """Read-only access to a GitHub pull request and its patch."""

    def fetch(self, number: int) -> Dict[str, Any]:
        raise NotImplementedError

    def diff(self, number: int) -> str:
        raise NotImplementedError

    def publish_review(
        self, number: int, event: str, body: str, findings: List[Dict[str, Any]], pull_request: Dict[str, Any]
    ) -> Dict[str, Any]:
        raise NotImplementedError


class PullRequestReviewer:
    """Produces a structured, local code-review report without publishing it."""

    def review(self, pull_request: Dict[str, Any], diff: str) -> Dict[str, Any]:
        raise NotImplementedError

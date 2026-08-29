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


class PullRequestReviewer:
    """Produces a structured, local code-review report without publishing it."""

    def review(self, pull_request: Dict[str, Any], diff: str) -> Dict[str, Any]:
        raise NotImplementedError

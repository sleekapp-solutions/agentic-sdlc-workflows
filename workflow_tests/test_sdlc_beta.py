import subprocess
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agentic_workflow.adapters import DetailedMcpTicketClient
from agentic_workflow.git_ops import WorktreeOperations, ticket_branch_name
from agentic_workflow.local_ui import HTML, WorkflowRuntime
from agentic_workflow.sdlc_beta import build_sdlc_beta_graph
from agentic_workflow.swift_graph import assess_blast_radius, is_swift_production_file


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    root = tmp_path / "app"
    git(tmp_path, "clone", "-q", str(remote), str(root))
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "checkout", "-q", "-b", "main")
    (root / "Sources").mkdir()
    (root / "Tests").mkdir()
    (root / "Sources/Cart.swift").write_text("struct Cart {\n    var items: [Int] = []\n}\n")
    (root / "Sources/Checkout.swift").write_text("final class Checkout {\n    let cart = Cart()\n}\n")
    (root / "Tests/CartTests.swift").write_text("class CartTests { let cart = Cart() }\n")
    (root / "AGENTS.md").write_text("Prefer value types.\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "push", "-q", "-u", "origin", "main")
    git(root, "remote", "set-head", "origin", "main")
    return root


class FakeTickets:
    def fetch(self, key):
        return {
            "key": key, "summary": "Add a cart total", "description": "Sum the items", "issue_type": "Story",
            "url": "https://jira.example/" + key, "acceptance_criteria": ["Cart exposes total"], "implementation_context": "Cart model",
        }


class FakePlanner:
    def __init__(self):
        self.feedback = []

    def create(self, ticket, context, feedback=None):
        self.feedback.append(feedback)
        return {
            "plan": {"goal": ticket["summary"], "assumptions": [], "files_to_inspect": [], "implementation_steps": ["add total"], "tests": [], "risks": []},
            "validation_approach": ["swift test"],
            "review_focus": ["Cart callers"],
            "pr_summary": {"title": f"{ticket['key']}: Add a cart total", "body": "## Summary\nAdds total."},
        }


class FakeImplementer:
    calls = []

    def __init__(self, worktree):
        self.worktree = worktree

    def run(self, ticket, brief, worktree, guardrails, feedback=None):
        FakeImplementer.calls.append({"worktree": self.worktree, "feedback": feedback, "guardrails": guardrails})
        (self.worktree / "Sources/Cart.swift").write_text(
            "struct Cart {\n    var items: [Int] = []\n    var total: Int { items.reduce(0, +) }\n}\n"
        )
        (self.worktree / ".env").write_text("SECRET=1\n")
        return {
            "change_summary": "Added Cart.total", "files_changed": ["Sources/Cart.swift"], "validation": [],
            "commit_message": "Add cart total\n\nSums the items.", "pr_title": "Add cart total", "pr_body": "## Summary\nAdds total.",
        }


class FakeReviewer:
    def __init__(self, worktree):
        pass

    def review(self, ticket, brief, implementation, diff, graph_context, guardrails):
        assert "items.reduce" in diff
        return {
            "verdict": "approve", "summary": "Looks right",
            "findings": [{"severity": "low", "category": "tests", "title": "Add a test", "body": "No test for total", "path": "Sources/Cart.swift", "line": 3}],
            "acceptance_criteria_met": [{"criterion": "Cart exposes total", "met": True}],
        }


class RecordingWorktrees(WorktreeOperations):
    """Real Git worktree, commit, and push; only `gh pr create` is recorded instead of run."""

    def __init__(self, repo_root):
        super().__init__(repo_root)
        self.pull_requests = []

    def push_and_open_pull_request(self, worktree, branch, base_branch, base_ref, title, body, draft):
        self._git("push", "-u", "origin", branch, cwd=worktree)
        self.pull_requests.append({"branch": branch, "base": base_branch, "title": title, "body": body, "draft": draft})
        return {"status": "created_draft" if draft else "created_ready", "url": "https://github.example/pr/1"}


def build(repo, access_check=None, planner=None, worktrees=None):
    FakeImplementer.calls = []
    worktrees = worktrees or RecordingWorktrees(repo)
    graph = build_sdlc_beta_graph(
        repo,
        ticket_client=FakeTickets(),
        planner=planner or FakePlanner(),
        implementer_factory=FakeImplementer,
        reviewer_factory=FakeReviewer,
        worktrees=worktrees,
        access_check=access_check or (lambda root: {"github": "authenticated", "repository": "o/r", "jira_mcp": "atlassian"}),
    ).compile(checkpointer=InMemorySaver())
    return graph, worktrees


def run_to_change_review(graph, config, repo):
    result = graph.invoke({"ticket_key": "PROJ-14", "pr_mode": "draft"}, config=config)
    assert result["__interrupt__"][0].value["phase"] == "plan_review"
    result = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert result["__interrupt__"][0].value["phase"] == "change_review"
    return result


def test_approved_change_is_committed_in_a_worktree_and_opened_as_a_draft_pr(repo):
    graph, worktrees = build(repo)
    config = {"configurable": {"thread_id": "approve"}}
    result = run_to_change_review(graph, config, repo)

    branch = "feature/proj-14-add-a-cart-total"
    worktree = Path(result["worktree"]["path"])
    assert worktree.parent == repo.parent / "app.worktrees"
    assert result["file_changes"] == [{"path": "Sources/Cart.swift", "status": "M"}]
    assert result["graph_context"]["blast_radius"] == "low"
    assert result["change_review"]["verdict"] == "approve"
    assert "Prefer value types." in FakeImplementer.calls[0]["guardrails"]
    # The developer's own checkout is untouched while the agent works.
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""

    result = graph.invoke(Command(resume={"action": "approve"}), config=config)

    assert result["status"] == "published"
    assert result["commit"]["subject"] == "PROJ-14: Add cart total"
    assert git(repo, "show", "--name-only", "--format=", branch).split() == ["Sources/Cart.swift"]
    assert git(repo.parent / "remote.git", "branch", "--list", branch).strip() == branch
    assert worktrees.pull_requests[0]["draft"] is True
    assert worktrees.pull_requests[0]["title"] == "PROJ-14: Add cart total"
    assert result["cleanup"]["status"] == "removed" and not worktree.exists()
    assert git(repo, "branch", "--list", branch).strip() == branch
    assert result["result"]["pull_request"]["url"] == "https://github.example/pr/1"
    assert result["result"]["authentication"]["jira_mcp"] == "atlassian"


def test_rejected_change_publishes_nothing_and_keeps_uncommitted_work(repo):
    graph, worktrees = build(repo)
    config = {"configurable": {"thread_id": "reject"}}
    result = run_to_change_review(graph, config, repo)
    worktree = Path(result["worktree"]["path"])

    result = graph.invoke(Command(resume={"action": "reject"}), config=config)

    assert result["status"] == "change_rejected"
    assert result["commit"]["status"] == "skipped_not_approved"
    assert result["pr_result"]["status"] == "skipped_not_approved"
    assert worktrees.pull_requests == []
    assert result["cleanup"]["status"] == "kept_uncommitted_changes" and worktree.exists()
    assert result["result"]["change_approval"] == "reject"


def test_change_revision_sends_feedback_and_previous_review_back_to_the_implementer(repo):
    graph, _ = build(repo)
    config = {"configurable": {"thread_id": "revise"}}
    run_to_change_review(graph, config, repo)

    result = graph.invoke(Command(resume={"action": "revise", "feedback": "Add a unit test"}), config=config)

    assert result["__interrupt__"][0].value["phase"] == "change_review"
    assert FakeImplementer.calls[0]["feedback"] is None
    second = FakeImplementer.calls[1]["feedback"]
    assert second["feedback"] == "Add a unit test"
    assert second["previous_review"]["findings"][0]["title"] == "Add a test"


def test_plan_revision_and_rejection_never_create_a_worktree(repo):
    planner = FakePlanner()
    graph, _ = build(repo, planner=planner)
    config = {"configurable": {"thread_id": "plan"}}
    graph.invoke({"ticket_key": "PROJ-14"}, config=config)
    graph.invoke(Command(resume={"action": "revise", "feedback": "Smaller scope"}), config=config)
    result = graph.invoke(Command(resume={"action": "reject"}), config=config)

    assert planner.feedback == [None, "Smaller scope"]
    assert result["status"] == "plan_rejected"
    assert not (repo.parent / "app.worktrees").exists()
    assert FakeImplementer.calls == []


def test_access_failure_stops_before_the_ticket_is_fetched(repo):
    def denied(root):
        raise RuntimeError("GitHub CLI is not authenticated")

    graph, _ = build(repo, access_check=denied)
    with pytest.raises(RuntimeError, match="not authenticated"):
        graph.invoke({"ticket_key": "PROJ-14"}, config={"configurable": {"thread_id": "denied"}})
    assert graph.get_state({"configurable": {"thread_id": "denied"}}).values.get("ticket") is None


def test_existing_branch_and_worktree_are_reused(repo):
    worktrees = WorktreeOperations(repo)
    first = worktrees.ensure_branch_worktree("feature/proj-1-x", "main")
    second = worktrees.ensure_branch_worktree("feature/proj-1-x", "main")
    assert (first["branch_status"], first["worktree_status"]) == ("created", "created")
    assert (second["branch_status"], second["worktree_status"]) == ("reused", "reused")
    assert first["base_ref"] == "origin/main"


def test_swift_blast_radius_ignores_tests_and_lists_dependents(repo):
    assert is_swift_production_file("Sources/Cart.swift")
    assert not is_swift_production_file("Tests/CartTests.swift")
    assert not is_swift_production_file("Sources/CartMock.swift")
    radius = assess_blast_radius(repo, ["Sources/Cart.swift", "Tests/CartTests.swift", "README.md"])
    assert radius["swift_files"] == ["Sources/Cart.swift"]
    assert radius["symbols"] == [{"file": "Sources/Cart.swift", "symbol": "Cart", "dependents": ["Sources/Checkout.swift", "Tests/CartTests.swift"]}]
    assert assess_blast_radius(repo, ["README.md"])["blast_radius"] == "not_applicable"


def test_ticket_branch_name_is_short_and_ref_safe():
    assert ticket_branch_name("PROJ-14", "Fix: crash on launch (iOS 18)!") == "feature/proj-14-fix-crash-on-launch-ios-18"
    assert len(ticket_branch_name("PROJ-1", "word " * 40)) <= len("feature/") + 60


def test_detailed_ticket_lookup_requires_acceptance_criteria(tmp_path):
    client = DetailedMcpTicketClient.__new__(DetailedMcpTicketClient)
    schema = client._schema()
    assert {"acceptance_criteria", "implementation_context", "summary"} <= set(schema["required"])
    assert set(schema["required"]) == set(schema["properties"])
    assert "acceptance criteria" in client._prompt("PROJ-14")


def test_local_runtime_starts_the_beta_flow_and_offers_it_in_the_ui(repo, tmp_path):
    def factory(repo_root, report=None):
        return build_sdlc_beta_graph(
            repo_root, ticket_client=FakeTickets(), planner=FakePlanner(), implementer_factory=FakeImplementer,
            reviewer_factory=FakeReviewer, worktrees=RecordingWorktrees(repo_root),
            access_check=lambda root: {"github": "authenticated", "repository": "o/r", "jira_mcp": "atlassian"}, report=report,
        )

    runtime = WorkflowRuntime(repo, tmp_path / "checkpoints.sqlite", sdlc_beta_graph_factory=factory)
    response = runtime.start_sdlc_beta("proj-14", pr_mode="ready", base_branch="main")
    deadline = time.monotonic() + 5
    while response["run"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        response = runtime.status(response["run_id"])

    assert response["run"]["flow"] == "sdlc_beta"
    assert response["phase"] == "plan_review"
    assert response["state"]["pr_mode"] == "ready"
    assert response["state"]["delivery_brief"]["review_focus"] == ["Cart callers"]
    assert 'value="sdlc_beta"' in HTML
    with pytest.raises(ValueError):
        runtime.start_sdlc_beta("PROJ-14", pr_mode="merge")

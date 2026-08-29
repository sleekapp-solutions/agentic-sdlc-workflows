from pathlib import Path
from io import BytesIO
import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agentic_workflow.graph import build_graph
from agentic_workflow.git_ops import GitOperations, ProjectValidator
from agentic_workflow.adapters import CodexMcpTicketClient, _codex_options, codex_workflow_configuration, use_codex_workflow_configuration
from agentic_workflow.local_ui import LocalUiApplication, WorkflowRuntime, explain_failure
from agentic_workflow.pr_review_graph import build_pr_review_graph


class FakeTickets:
    def fetch(self, key):
        return {"key": key, "summary": "A ticket", "description": "Requirements", "issue_type": "Story", "url": "https://jira.example/T-1"}


class FakePlanner:
    def create(self, ticket, feedback=None):
        return {"goal": ticket["summary"], "implementation_steps": ["change code"], "tests": ["run tests"], "feedback": feedback or ""}


class EmptySummaryTickets:
    def fetch(self, key):
        return {"key": key, "summary": "", "description": "Requirements"}


class FakeImplementer:
    def __init__(self):
        self.calls = 0

    def run(self, ticket, plan, branch):
        self.calls += 1
        return {"branch": branch}


class FakeGit:
    def __init__(self):
        self.published = []
        self.draft_pull_requests = []

    def create_branch(self, ticket_key):
        return "agent/" + ticket_key.lower()

    def candidate_paths(self):
        return ["lib/main.dart", "test/main_test.dart"]

    def changes_since(self, baseline):
        return [
            {"path": "lib/main.dart", "status": "M"},
            {"path": "test/main_test.dart", "status": "A"},
        ]

    def commit_and_push(self, key, summary, paths):
        self.published.append((key, summary, paths))
        return "deadbeef"

    def create_draft_pull_request(self, key, summary, branch):
        self.draft_pull_requests.append((key, summary, branch))
        return "https://github.com/example/memomatch/pull/42"


class PassingValidator:
    def run(self):
        return {"passed": True, "results": [{"command": "flutter test", "exit_code": 0}]}


class FakePullRequests:
    def fetch(self, number):
        return {"number": number, "title": "Protect progress state", "url": f"https://github.com/example/memomatch/pull/{number}"}

    def diff(self, number):
        return "diff --git a/lib/progress.dart b/lib/progress.dart\n+@@ -1 +1 @@\n-old\n+new\n"


class FakePullRequestReviewer:
    def review(self, pull_request, diff):
        assert pull_request["number"] == 42
        assert "+new" in diff
        return {
            "summary": "One regression needs attention.",
            "verdict": "request_changes",
            "findings": [{"severity": "high", "title": "Missing migration", "body": "Keep old progress readable.", "path": "lib/progress.dart", "line": 10}],
            "tests_to_run": ["flutter test test/progress_service_test.dart"],
        }


def build_test_graph():
    git = FakeGit()
    implementer = FakeImplementer()
    graph = build_graph(
        Path.cwd(),
        ticket_client=FakeTickets(),
        planner=FakePlanner(),
        implementer=implementer,
        git=git,
        validator=PassingValidator(),
    ).compile(checkpointer=InMemorySaver())
    return graph, git, implementer


def test_push_never_happens_without_second_approval():
    graph, git, implementer = build_test_graph()
    config = {"configurable": {"thread_id": "test-reject"}}
    plan_pause = graph.invoke({"ticket_key": "SCRUM-3"}, config=config)
    assert plan_pause["__interrupt__"]
    push_pause = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert push_pause["__interrupt__"]
    finished = graph.invoke(Command(resume={"action": "reject"}), config=config)
    assert finished["status"] == "push_rejected"
    assert implementer.calls == 1
    assert git.published == []
    assert git.draft_pull_requests == []


def test_ticket_without_a_title_stops_before_planning_or_implementation():
    graph = build_graph(
        Path.cwd(),
        ticket_client=EmptySummaryTickets(),
        planner=FakePlanner(),
        implementer=FakeImplementer(),
        git=FakeGit(),
        validator=PassingValidator(),
    ).compile(checkpointer=InMemorySaver())

    try:
        graph.invoke({"ticket_key": "MGA-38"}, config={"configurable": {"thread_id": "missing-summary"}})
    except RuntimeError as error:
        assert "no usable ticket title" in str(error)
    else:
        raise AssertionError("Expected an incomplete Jira ticket to stop the workflow")


def test_explicit_second_approval_publishes_only_selected_paths():
    graph, git, _ = build_test_graph()
    config = {"configurable": {"thread_id": "test-publish"}}
    graph.invoke({"ticket_key": "SCRUM-3"}, config=config)
    graph.invoke(Command(resume={"action": "approve"}), config=config)
    finished = graph.invoke(
        Command(resume={"action": "approve", "paths": ["lib/main.dart"]}),
        config=config,
    )
    assert finished["status"] == "published"
    assert finished["commit_sha"] == "deadbeef"
    assert finished["pull_request_url"] == "https://github.com/example/memomatch/pull/42"
    assert git.published == [("SCRUM-3", "A ticket", ["lib/main.dart"])]
    assert git.draft_pull_requests == [("SCRUM-3", "A ticket", "agent/scrum-3")]


def test_pull_request_review_stops_for_human_acknowledgement_without_writes():
    graph = build_pr_review_graph(
        Path.cwd(), pull_request_client=FakePullRequests(), reviewer=FakePullRequestReviewer()
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "test-pr-review"}}

    paused = graph.invoke({"pr_number": 42}, config=config)

    assert paused["__interrupt__"]
    assert paused["pull_request"]["title"] == "Protect progress state"
    assert paused["review"]["verdict"] == "request_changes"
    finished = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert finished["status"] == "pr_review_acknowledged"


def test_local_reviewer_ui_serves_its_browser_interface():
    app = LocalUiApplication(runtime=None)
    response = {}

    def start_response(status, headers):
        response["status"] = status
        response["headers"] = dict(headers)

    body = b"".join(
        app(
            {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "wsgi.input": BytesIO()},
            start_response,
        )
    ).decode()

    assert response["status"] == "200 OK"
    assert "Agentic SDLC Workflows" in body
    assert "Credentials remain in the local process" in body
    assert "Pull-request review" in body
    assert "Estimated payload tokens" in body
    assert "This is not a Jira-fetch failure" in body
    assert "GPT-5.6 Terra · balanced" in body
    assert "Codex model" in body
    assert "Current local defaults" in body
    assert "Reasoning effort" in body


def test_local_codex_defaults_are_read_without_exposing_other_config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\napi_key = "do-not-expose"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("WORKFLOW_CODEX_MODEL", raising=False)
    monkeypatch.delenv("WORKFLOW_CODEX_REASONING_EFFORT", raising=False)

    configuration = codex_workflow_configuration()

    assert configuration == {
        "model": "gpt-5.6-terra",
        "model_source": "CODEX_HOME/config.toml",
        "reasoning_effort": "medium",
        "reasoning_effort_source": "CODEX_HOME/config.toml",
    }


def test_fetch_failure_explains_that_planning_did_not_start():
    explanation = explain_failure("fetching_ticket", RuntimeError("Atlassian MCP unavailable"))

    assert explanation["title"] == "Jira ticket fetch failed"
    assert "before planning" in explanation["summary"]
    assert explanation["technical_detail"] == "Atlassian MCP unavailable"


def test_missing_atlassian_title_has_a_specific_recovery_message():
    explanation = explain_failure("fetching_ticket", RuntimeError("Atlassian returned the ticket key but no usable ticket title."))

    assert explanation["title"] == "Ticket title was missing from the Atlassian response"
    assert "key was found" in explanation["summary"]


def test_atlassian_ticket_schema_requires_a_nonempty_summary(tmp_path):
    class CapturingAgent:
        def run(self, prompt, schema):
            self.prompt = prompt
            self.schema = schema
            return {"key": "MGA-38", "summary": "A title", "description": "", "issue_type": "Task", "url": "https://jira.example/MGA-38"}

    client = CodexMcpTicketClient(tmp_path)
    client.agent = CapturingAgent()

    ticket = client.fetch("MGA-38")

    assert ticket["summary"] == "A title"
    assert client.agent.schema["properties"]["summary"]["minLength"] == 1
    assert "actual Jira Summary field" in client.agent.prompt


def test_ui_model_choice_is_scoped_to_one_workflow_run(tmp_path):
    configuration = WorkflowRuntime._model_configuration("gpt-5.6-terra", "medium")

    assert configuration["model_source"] == "UI selection"
    assert configuration["reasoning_effort_source"] == "UI selection"
    with use_codex_workflow_configuration(configuration):
        assert _codex_options() == [
            "--model",
            "gpt-5.6-terra",
            "--config",
            'model_reasoning_effort="medium"',
        ]


def test_fast_model_rejects_unsupported_none_effort():
    try:
        WorkflowRuntime._model_configuration("gpt-5.6-luna", "none")
    except ValueError as error:
        assert "requires low or higher" in str(error)
    else:
        raise AssertionError("Expected incompatible model/effort selection to fail")


def test_validation_commands_can_be_configured_for_non_flutter_projects(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOW_VALIDATION_COMMANDS", "npm test; npm run lint")

    validator = ProjectValidator(tmp_path)

    assert validator.commands == (("npm", "test"), ("npm", "run", "lint"))


def test_local_runtime_exposes_live_execution_events(tmp_path, monkeypatch):
    git = FakeGit()
    implementer = FakeImplementer()

    def graph_factory(repo_root, report=None):
        return build_graph(
            repo_root,
            ticket_client=FakeTickets(),
            planner=FakePlanner(),
            implementer=implementer,
            git=git,
            validator=PassingValidator(),
            report=report,
        )

    monkeypatch.setattr(GitOperations, "snapshot_changes", lambda self: {})
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("WORKFLOW_CODEX_MODEL", raising=False)
    monkeypatch.delenv("WORKFLOW_CODEX_REASONING_EFFORT", raising=False)
    runtime = WorkflowRuntime(tmp_path, tmp_path / "checkpoints.sqlite", graph_factory=graph_factory)
    response = runtime.start("SCRUM-3")
    deadline = time.monotonic() + 2
    while response["run"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        response = runtime.status(response["run_id"])

    assert response["phase"] == "plan_review"
    assert response["run"]["running"] is False
    assert response["run"]["model"]["model"] == "Codex automatic default"
    assert response["run"]["usage"]["total_tokens"] > 0
    assert [event["stage"] for event in response["run"]["events"]] == [
        "starting",
        "fetching_ticket",
        "planning",
        "awaiting_plan_review",
        "plan_review",
    ]


def test_local_runtime_starts_a_pr_review_flow(tmp_path):
    def review_graph_factory(repo_root, report=None):
        return build_pr_review_graph(
            repo_root, pull_request_client=FakePullRequests(), reviewer=FakePullRequestReviewer(), report=report
        )

    runtime = WorkflowRuntime(
        tmp_path,
        tmp_path / "checkpoints.sqlite",
        pr_review_graph_factory=review_graph_factory,
    )
    response = runtime.start_pr_review("42")
    deadline = time.monotonic() + 2
    while response["run"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        response = runtime.status(response["run_id"])

    assert response["run"]["flow"] == "pr_review"
    assert response["phase"] == "pr_review"
    assert response["state"]["review"]["findings"][0]["path"] == "lib/progress.dart"
    assert response["run"]["usage"]["total_tokens"] > 0


def test_existing_ticket_branch_is_reused_for_a_retry(monkeypatch, tmp_path):
    commands = []

    def run(self, *args):
        commands.append(args)
        return "  agent/mga-14" if args[:2] == ("branch", "--list") else ""

    monkeypatch.setattr(GitOperations, "_run", run)
    branch = GitOperations(tmp_path).create_branch("MGA-14")

    assert branch == "agent/mga-14"
    assert commands == [("branch", "--list", "agent/mga-14"), ("switch", "agent/mga-14")]


def test_draft_pull_request_uses_github_cli_not_git_subcommand(monkeypatch, tmp_path):
    commands = []

    class Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[:3] == ["gh", "pr", "view"]:
            return Result(1, stderr="no pull requests found")
        return Result(0, stdout="https://github.com/example/memomatch/pull/42\n")

    monkeypatch.setattr("agentic_workflow.git_ops.subprocess.run", run)
    url = GitOperations(tmp_path).create_draft_pull_request(
        "MGA-38",
        "Formalize typography",
        "agent/mga-38",
    )

    assert url == "https://github.com/example/memomatch/pull/42"
    assert commands[0][0][:3] == ["gh", "pr", "view"]
    assert commands[1][0][:3] == ["gh", "pr", "create"]
    assert all("git" not in command[0] for command in commands)


def test_draft_pull_request_reuses_an_existing_review(monkeypatch, tmp_path):
    commands = []

    class Result:
        returncode = 0
        stdout = "https://github.com/example/memomatch/pull/42\n"
        stderr = ""

    def run(command, **kwargs):
        commands.append(command)
        return Result()

    monkeypatch.setattr("agentic_workflow.git_ops.subprocess.run", run)
    url = GitOperations(tmp_path).create_draft_pull_request(
        "MGA-38",
        "Formalize typography",
        "agent/mga-38",
    )

    assert url == "https://github.com/example/memomatch/pull/42"
    assert commands == [["gh", "pr", "view", "agent/mga-38", "--json", "url", "--jq", ".url"]]

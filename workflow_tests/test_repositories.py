import os
import subprocess
import time

import pytest

from agentic_workflow import repositories, settings
from agentic_workflow.local_ui import WorkflowRuntime
from agentic_workflow.pr_review_graph import build_pr_review_graph


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    # Switching repositories loads configuration layers into os.environ; keep
    # them away from this checkout's .env and restore the environment afterwards.
    monkeypatch.setenv("AGENTIC_WORKFLOWS_CONFIG", str(tmp_path / "config" / ".env"))
    monkeypatch.setattr(settings, "checkout_env_path", lambda: tmp_path / "no-checkout.env")
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_a_path_inside_a_repository_resolves_to_its_root(tmp_path):
    repo = make_repo(tmp_path / "app")
    (repo / "src").mkdir()
    assert repositories.resolve_repository(str(repo / "src")) == repo
    with pytest.raises(ValueError, match="not inside a Git repository"):
        repositories.resolve_repository(str(tmp_path))
    with pytest.raises(ValueError, match="absolute path"):
        repositories.resolve_repository("app")


def test_recent_repositories_are_most_recent_first_and_deduplicated(tmp_path):
    first, second = make_repo(tmp_path / "first"), make_repo(tmp_path / "second")
    repositories.remember_repository(first)
    repositories.remember_repository(second)
    repositories.remember_repository(first)
    assert repositories.recent_repositories() == [first, second]


def test_startup_prefers_flag_then_current_checkout_then_last_used(tmp_path):
    repo, other = make_repo(tmp_path / "repo"), make_repo(tmp_path / "other")
    outside = tmp_path / "outside"
    outside.mkdir()
    assert repositories.startup_repository(other, repo) == other
    assert repositories.startup_repository(None, repo) == repo
    assert repositories.startup_repository(None, outside) == outside.resolve()
    repositories.remember_repository(other)
    assert repositories.startup_repository(None, outside) == other


def test_discovery_finds_checkouts_in_code_folders_and_skips_dependencies(tmp_path):
    home = tmp_path / "home"
    app = make_repo(home / "Desktop" / "app")
    nested = make_repo(home / "Projects" / "team" / "service")
    make_repo(home / "Desktop" / "node_modules" / "dep")
    make_repo(app / "vendored")  # Inside another checkout; not listed separately.
    assert repositories.discovered_repositories(home=home) == [app, nested]


def test_switching_repository_drops_the_previous_repository_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_VALIDATION_COMMANDS", raising=False)
    first, second = make_repo(tmp_path / "first"), make_repo(tmp_path / "second")
    (first / ".agentic-workflow.env").write_text("WORKFLOW_VALIDATION_COMMANDS=swift test\n")
    settings.switch_repository(first)
    assert os.environ["WORKFLOW_VALIDATION_COMMANDS"] == "swift test"
    settings.switch_repository(second)
    assert "WORKFLOW_VALIDATION_COMMANDS" not in os.environ


class FakePullRequests:
    def fetch(self, number):
        return {"number": number, "title": "PR", "url": "https://github.example/pr/1"}

    def diff(self, number):
        return "diff"


class FakeReviewer:
    def review(self, pull_request, diff):
        return {"summary": "ok", "verdict": "comment", "findings": [], "tests_to_run": []}


def test_runs_keep_their_repository_after_the_ui_switches(tmp_path):
    first, second = make_repo(tmp_path / "first"), make_repo(tmp_path / "second")
    used = []

    def factory(repo_root, report=None):
        used.append(repo_root)
        return build_pr_review_graph(repo_root, pull_request_client=FakePullRequests(), reviewer=FakeReviewer(), report=report)

    runtime = WorkflowRuntime(first, tmp_path / "checkpoints.sqlite", pr_review_graph_factory=factory)
    response = runtime.start_pr_review("7")
    environment = runtime.select_repository(str(second))
    deadline = time.monotonic() + 5
    while response["run"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        response = runtime.status(response["run_id"])

    assert environment["repository"]["path"] == str(second)
    assert response["run"]["repository"] == str(first)
    assert used[0] == first
    assert runtime.repositories()["repositories"][0] == {"name": "second", "path": str(second), "origin": None, "recent": True}


def test_runs_cannot_start_until_a_git_repository_is_selected(tmp_path):
    runtime = WorkflowRuntime(tmp_path, tmp_path / "checkpoints.sqlite")
    assert runtime.environment()["repository"]["is_git_repository"] is False
    with pytest.raises(ValueError, match="Select a Git repository"):
        runtime.start_sdlc_beta("PROJ-1")


def call(app, method, path, headers=None, body=b""):
    from io import BytesIO

    captured = {}
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": BytesIO(body), "CONTENT_LENGTH": str(len(body)),
        "HTTP_HOST": "127.0.0.1:8080", "SERVER_PORT": "8080", **(headers or {}),
    }
    app(environ, lambda status, response_headers: captured.update(status=status))
    return captured["status"]


def test_ui_rejects_requests_another_website_could_send(tmp_path):
    from agentic_workflow.local_ui import LocalUiApplication

    target = make_repo(tmp_path / "target")
    app = LocalUiApplication(WorkflowRuntime(make_repo(tmp_path / "app"), tmp_path / "db.sqlite"))
    body = ('{"path": "%s"}' % target).encode()
    # A cross-site form or no-cors fetch can only send text/plain without a preflight.
    assert call(app, "POST", "/api/repository", {"CONTENT_TYPE": "text/plain"}, body).startswith("403")
    assert call(app, "POST", "/api/repository", {"CONTENT_TYPE": "application/json", "HTTP_ORIGIN": "https://evil.example"}, body).startswith("403")
    # DNS rebinding arrives with the attacker's host name.
    assert call(app, "GET", "/api/environment", {"HTTP_HOST": "evil.example:8080"}).startswith("403")
    assert call(app, "POST", "/api/repository", {"CONTENT_TYPE": "application/json", "HTTP_ORIGIN": "http://127.0.0.1:8080"}, body).startswith("200")
    assert call(app, "GET", "/api/environment", {"HTTP_HOST": "localhost:8080"}).startswith("200")

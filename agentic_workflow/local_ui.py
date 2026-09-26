"""Local-only browser UI for starting and reviewing workflow runs.

The UI provides a human-friendly front end for both Jira delivery and
pull-request review with an explicit publication gate. It exposes the selected repository, provider,
safe environment diagnostics, run progress, and LangGraph approval interrupts.
The server deliberately binds to loopback by default: credentials remain in
the server process and the browser receives workflow state, never secrets.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import parse_qs, urlparse, urlunparse
from wsgiref.simple_server import make_server

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .jira_delivery import build_graph
from .git_ops import GitOperations, configured_validation_commands
from .pr_review_graph import build_pr_review_graph
from .adapters import (
    AGENT_PROVIDERS,
    TicketLookupError,
    TicketLookupResponseError,
    TicketMcpAccessError,
    TicketMcpNotConfiguredError,
    TicketMcpUnavailableError,
    TicketNotFoundError,
    agent_workflow_configuration,
    configured_mcp_servers,
    use_workflow_configuration,
)
from .settings import load_workflow_environment


TICKET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
PR_NUMBER_PATTERN = re.compile(r"^[1-9]\d*$")
MODEL_CHOICES = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
EFFORT_CHOICES = {"none", "low", "medium", "high", "xhigh", "max"}
COPILOT_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return _json_safe(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def workflow_response(run_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert LangGraph's result into state that is safe for the browser."""
    interrupts = result.get("__interrupt__", [])
    pending = [_json_safe(interrupt) for interrupt in interrupts]
    phase = pending[0].get("phase") if pending and isinstance(pending[0], dict) else None
    state = {key: _json_safe(value) for key, value in result.items() if key not in {"__interrupt__", "baseline"}}
    return {"run_id": run_id, "phase": phase, "pending": pending, "state": state}


def _estimate_tokens(value: Any) -> int:
    """A transparent payload-size estimate; it is intentionally not billing usage."""
    encoded = json.dumps(_json_safe(value), separators=(",", ":"), ensure_ascii=False)
    return math.ceil(len(encoded) / 4)


def workflow_usage_estimate(state: Dict[str, Any], flow: str) -> Dict[str, Any]:
    stages = []
    if flow == "jira_delivery":
        ticket = state.get("ticket", {})
        if ticket:
            stages.append({"name": "Ticket retrieval", "input": _estimate_tokens(state.get("ticket_key", "")), "output": _estimate_tokens(ticket)})
        if state.get("plan"):
            stages.append({"name": "Planning", "input": _estimate_tokens(ticket), "output": _estimate_tokens(state["plan"])})
        if state.get("implementation"):
            stages.append({"name": "Implementation", "input": _estimate_tokens({"ticket": ticket, "plan": state.get("plan", {})}), "output": _estimate_tokens(state["implementation"])})
    elif state.get("review"):
        stages.append({"name": "PR review", "input": _estimate_tokens({"pull_request": state.get("pull_request", {}), "patch": state.get("pull_request_diff", "")}), "output": _estimate_tokens(state["review"])})
    total_input = sum(stage["input"] for stage in stages)
    total_output = sum(stage["output"] for stage in stages)
    return {
        "kind": "payload_estimate",
        "note": "Estimated from workflow payload character counts; not billed token usage.",
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "stages": stages,
    }


def explain_failure(stage: str, error: Exception) -> Dict[str, str]:
    """Turn low-level workflow failures into an actionable local UI message."""
    error_text = str(error)
    if stage == "fetching_ticket" and isinstance(error, TicketMcpNotConfiguredError):
        return {
            "title": "Jira MCP is not configured",
            "summary": error_text,
            "next_step": "Configure the named Jira/Atlassian MCP server for the selected agent provider, then start a new run.",
            "technical_detail": "",
        }
    if stage == "fetching_ticket" and isinstance(error, TicketMcpAccessError):
        return {
            "title": "Jira access was denied",
            "summary": error_text,
            "next_step": "Reconnect the Atlassian MCP identity or grant it access to this Jira project, then start a new run.",
            "technical_detail": error.detail,
        }
    if stage == "fetching_ticket" and isinstance(error, TicketMcpUnavailableError):
        return {
            "title": "Jira lookup is temporarily unavailable",
            "summary": error_text,
            "next_step": "Check the MCP service or network connection, then retry the ticket lookup.",
            "technical_detail": error.detail,
        }
    if stage == "fetching_ticket" and isinstance(error, TicketLookupResponseError):
        return {
            "title": "Jira returned an invalid response",
            "summary": error_text,
            "next_step": "Retry once. If it repeats, check or reconnect the Jira MCP integration.",
            "technical_detail": error.detail,
        }
    if stage == "fetching_ticket" and isinstance(error, TicketNotFoundError):
        return {
            "title": "Jira ticket was not found",
            "summary": error_text,
            "next_step": "Check the ticket key or the configured Atlassian MCP identity before starting a new run.",
            "technical_detail": "",
        }
    if stage == "fetching_ticket" and "no usable ticket title" in error_text:
        return {
            "title": "Ticket title was missing from the Atlassian response",
            "summary": "The Jira issue key was found, but the workflow did not receive a usable Summary/title.",
            "next_step": "Retry the run. If it repeats, check that the Jira issue has a Summary and reconnect the Atlassian MCP integration.",
            "technical_detail": error_text,
        }
    guidance = {
        "fetching_ticket": (
            "Jira ticket fetch failed",
            "The workflow stopped before planning, so no implementation was started.",
            "Confirm the ticket key and that the selected local agent can use the Atlassian MCP connection, then try again.",
        ),
        "planning": (
            "Implementation plan could not be created",
            "The Jira ticket was fetched, but the planning agent did not complete.",
            "Review the technical detail below, then retry after checking the selected local agent.",
        ),
        "implementing": (
            "Implementation did not complete",
            "The approved plan reached the coding stage, but the coding agent stopped before validation.",
            "Review the technical detail below and retry only after resolving the reported command or workspace issue.",
        ),
        "validating": (
            "Validation could not run",
            "The coding stage completed, but the workflow could not finish its validation step.",
            "Review the technical detail below, then rerun after the local toolchain issue is resolved.",
        ),
        "publishing": (
            "Draft pull request could not be published",
            "Code review and validation completed, but publication did not finish.",
            "Review the technical detail below. The branch may already be pushed, so retry publication before rerunning implementation.",
        ),
        "publishing_review": (
            "Pull-request review could not be published",
            "The local review was ready, but GitHub did not accept the explicitly approved publication.",
            "Review the technical detail below, then retry publication before generating a new review.",
        ),
    }
    title, summary, next_step = guidance.get(
        stage,
        ("Workflow stopped", "The workflow stopped before it could reach the next stage.", "Review the technical detail below and retry when the cause is resolved."),
    )
    return {"title": title, "summary": summary, "next_step": next_step, "technical_detail": error_text}


def retry_target(flow: str, stage: str, state: Dict[str, Any]) -> str | None:
    """Return the single safe graph node to re-run after a known failed stage."""
    if flow == "jira_delivery":
        if state.get("status") == "validation_failed":
            return "validate"
        return {
            "fetching_ticket": "fetch_ticket",
            "planning": "make_plan",
            "implementing": "implement",
            "validating": "validate",
            "publishing": "publish",
        }.get(stage)
    if flow == "pr_review":
        return {
            "fetching_pull_request": "fetch_pull_request",
            "reviewing_pull_request": "review_pull_request",
            "publishing_review": "publish_review",
        }.get(stage)
    return None


def _sanitized_origin(repo_root: Path) -> str | None:
    """Show a remote identity without any embedded username or token."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return None
    origin = result.stdout.strip()
    parsed = urlparse(origin)
    if not parsed.scheme:
        return origin or None
    hostname = parsed.hostname or ""
    netloc = hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _validation_enabled() -> bool:
    try:
        return bool(configured_validation_commands())
    except RuntimeError:
        return True


def _github_cli_status() -> Dict[str, Any]:
    if not shutil.which("gh"):
        return {"available": False, "authenticated": False}
    result = subprocess.run(["gh", "auth", "status"], text=True, capture_output=True, check=False)
    return {"available": True, "authenticated": result.returncode == 0}


def _provider_environment(provider: str) -> Dict[str, Any]:
    command = "codex" if provider == "codex" else "copilot"
    configured_mcps = configured_mcp_servers(provider)
    expected_jira_mcp = os.getenv("WORKFLOW_COPILOT_TICKET_MCP_SERVER", "atlassian") if provider == "copilot" else "atlassian"
    jira_mcp_configured = any(expected_jira_mcp.lower() in name.lower() for name in configured_mcps)
    return {
        "label": "Codex CLI" if provider == "codex" else "GitHub Copilot CLI",
        "installed": bool(shutil.which(command)),
        "configured_mcps": configured_mcps,
        "expected_jira_mcp": expected_jira_mcp,
        "jira_mcp_configured": jira_mcp_configured,
    }


class WorkflowRuntime:
    """Opens the SQLite checkpointer only while a local request is executing."""

    def __init__(
        self,
        repo_root: Path,
        database: Path,
        graph_factory: Callable[[Path], Any] = build_graph,
        pr_review_graph_factory: Callable[[Path], Any] = build_pr_review_graph,
    ) -> None:
        self.repo_root = repo_root
        self.database = database
        self.graph_factory = graph_factory
        self.pr_review_graph_factory = pr_review_graph_factory
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _graph(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        return SqliteSaver.from_conn_string(str(self.database))

    def _event(self, run_id: str, stage: str, message: str) -> None:
        with self._lock:
            record = self._runs[run_id]
            record["stage"] = stage
            record["events"].append({"at": time.strftime("%H:%M:%S"), "stage": stage, "message": message})
            record["events"] = record["events"][-30:]

    def _response(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._runs.get(run_id)
            if not record:
                raise ValueError("Unknown local run ID")
            response = dict(record["response"])
            response["run"] = {
                "flow": record["flow"],
                "model": record["model"],
                "usage": record["usage"],
                "running": record["running"],
                "stage": record["stage"],
                "events": list(record["events"]),
                "error": record.get("error"),
                "failure": record.get("failure"),
                "retry": {
                    "available": bool(record.get("retry_target")),
                    "target": record.get("retry_target"),
                },
            }
            return response

    def _execute(
        self,
        run_id: str,
        input_state: Dict[str, Any],
        decision: Dict[str, Any] | None = None,
        retry_from: str | None = None,
    ) -> None:
        def report(stage: str, message: str) -> None:
            self._event(run_id, stage, message)

        graph = None
        factory = None
        config = {"configurable": {"thread_id": run_id}}
        try:
            with self._lock:
                flow = self._runs[run_id]["flow"]
                model_configuration = self._runs[run_id]["model"]
            with use_workflow_configuration(model_configuration):
                with self._graph() as checkpointer:
                    factory = self.graph_factory if flow == "jira_delivery" else self.pr_review_graph_factory
                    graph = factory(self.repo_root, report=report).compile(checkpointer=checkpointer)
                    if retry_from:
                        graph.update_state(config, {"retry_target": retry_from}, as_node="retry_request")
                        result = graph.invoke(None, config=config)
                    else:
                        result = graph.invoke(Command(resume=decision), config=config) if decision else graph.invoke(input_state, config=config)
            with self._lock:
                self._runs[run_id]["response"] = workflow_response(run_id, result)
                state = self._runs[run_id]["response"]["state"]
                self._runs[run_id]["usage"] = workflow_usage_estimate(state, self._runs[run_id]["flow"])
                self._runs[run_id]["running"] = False
            phase = self._runs[run_id]["response"].get("phase") or self._runs[run_id]["response"]["state"].get("status", "complete")
            state = self._runs[run_id]["response"]["state"]
            message = "Workflow is ready for your review" if phase.endswith("review") else "Workflow finished"
            if phase == "no_run_changes":
                message = "Ticket fetch, planning, implementation, and validation completed; no new files differ from the run start."
            elif phase == "validation_failed":
                message = "Implementation completed, but validation failed; no draft PR was created."
            with self._lock:
                self._runs[run_id]["retry_target"] = retry_target(
                    self._runs[run_id]["flow"], self._runs[run_id]["stage"], state
                )
            self._event(run_id, phase, message)
        except Exception as error:  # Keep background errors visible in the local UI.
            with self._lock:
                record = self._runs[run_id]
                failed_stage = record["stage"]
                if graph is not None and factory is not None:
                    try:
                        # The execution checkpointer's context has closed while
                        # unwinding the failed graph call. Reopen the database
                        # before recovering the last successful node state.
                        with self._graph() as recovery_checkpointer:
                            recovery_graph = factory(self.repo_root).compile(checkpointer=recovery_checkpointer)
                            checkpoint = recovery_graph.get_state(config)
                        record["response"] = workflow_response(run_id, dict(checkpoint.values))
                    except Exception:
                        pass
                record["running"] = False
                record["error"] = str(error)
                record["failure"] = explain_failure(failed_stage, error)
                record["retry_target"] = (
                    None
                    if isinstance(error, TicketLookupError) and not error.retryable
                    else retry_target(record["flow"], failed_stage, record["response"]["state"])
                )
            self._event(run_id, "failed", "Workflow stopped: " + str(error))

    def _launch(
        self,
        run_id: str,
        input_state: Dict[str, Any],
        decision: Dict[str, Any] | None = None,
        retry_from: str | None = None,
    ) -> Dict[str, Any]:
        threading.Thread(target=self._execute, args=(run_id, input_state, decision, retry_from), daemon=True).start()
        return self._response(run_id)

    @staticmethod
    def _model_configuration(provider: str | None, model: str | None, reasoning_effort: str | None) -> Dict[str, str]:
        resolved_provider = (provider or "codex").strip().lower()
        if resolved_provider not in AGENT_PROVIDERS:
            raise ValueError("Choose Codex CLI or GitHub Copilot CLI")
        if resolved_provider == "codex" and model and model not in MODEL_CHOICES:
            raise ValueError("Choose a supported Codex model")
        if resolved_provider == "copilot" and model and not COPILOT_MODEL_PATTERN.fullmatch(model):
            raise ValueError("Copilot model IDs may contain letters, numbers, dots, underscores, and hyphens only")
        if resolved_provider == "copilot" and reasoning_effort:
            raise ValueError("GitHub Copilot CLI reads reasoning effort from its local settings; clear the UI override")
        if reasoning_effort and reasoning_effort not in EFFORT_CHOICES:
            raise ValueError("Choose a supported reasoning effort")
        if resolved_provider == "codex" and model == "gpt-5.6-luna" and reasoning_effort == "none":
            raise ValueError("gpt-5.6-luna requires low or higher reasoning effort")
        return agent_workflow_configuration(
            provider=resolved_provider,
            model=model,
            reasoning_effort=reasoning_effort,
            source="UI selection",
        )

    def start(
        self,
        ticket_key: str,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Dict[str, Any]:
        ticket_key = ticket_key.strip().upper()
        if not TICKET_KEY_PATTERN.fullmatch(ticket_key):
            raise ValueError("Ticket key must look like PROJ-14")
        model_configuration = self._model_configuration(provider, model, reasoning_effort)
        run_id = f"ui-{ticket_key.lower()}-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._runs[run_id] = {
                "flow": "jira_delivery",
                "model": model_configuration,
                "usage": {"kind": "payload_estimate", "note": "Available after the first agent stage completes.", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "stages": []},
                "failure": None,
                "retry_target": None,
                "running": True,
                "stage": "starting",
                "events": [{"at": time.strftime("%H:%M:%S"), "stage": "starting", "message": "Delivery run started"}],
                "response": {"run_id": run_id, "phase": None, "pending": [], "state": {"ticket_key": ticket_key, "status": "starting"}},
            }
        baseline = GitOperations(self.repo_root).snapshot_changes()
        return self._launch(run_id, {"ticket_key": ticket_key, "baseline": baseline})

    def start_pr_review(
        self,
        pull_request_number: str,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Dict[str, Any]:
        number = pull_request_number.strip()
        if not PR_NUMBER_PATTERN.fullmatch(number):
            raise ValueError("Pull-request number must be a positive whole number")
        model_configuration = self._model_configuration(provider, model, reasoning_effort)
        run_id = f"ui-pr-{number}-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._runs[run_id] = {
                "flow": "pr_review",
                "model": model_configuration,
                "usage": {"kind": "payload_estimate", "note": "Available after the PR review completes.", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "stages": []},
                "failure": None,
                "retry_target": None,
                "running": True,
                "stage": "starting",
                "events": [{"at": time.strftime("%H:%M:%S"), "stage": "starting", "message": "Pull-request review started"}],
                "response": {"run_id": run_id, "phase": None, "pending": [], "state": {"pr_number": int(number), "status": "starting"}},
            }
        return self._launch(run_id, {"pr_number": int(number)})

    def resume(self, run_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        if decision.get("action") not in {"approve", "revise", "reject"}:
            raise ValueError("Action must be approve, revise, or reject")
        with self._lock:
            record = self._runs.get(run_id)
            if not record:
                raise ValueError("Unknown local run ID")
            if record["running"]:
                raise ValueError("This workflow is already running")
            record["running"] = True
            record["error"] = None
            record["retry_target"] = None
        self._event(run_id, "resuming", "Applying your decision")
        return self._launch(run_id, {}, decision)

    def retry(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._runs.get(run_id)
            if not record:
                raise ValueError("Unknown local run ID")
            if record["running"]:
                raise ValueError("This workflow is already running")
            target = record.get("retry_target")
            if not target:
                raise ValueError("This run has no retryable failed step")
            record["running"] = True
            record["error"] = None
            record["failure"] = None
            record["retry_target"] = None
        self._event(run_id, "retrying", f"Retrying {target.replace('_', ' ')}")
        return self._launch(run_id, {}, retry_from=target)

    def status(self, run_id: str) -> Dict[str, Any]:
        return self._response(run_id)

    def configuration(self) -> Dict[str, Any]:
        """Expose resolved, non-sensitive defaults before a workflow starts."""
        return {
            "defaults": agent_workflow_configuration(),
            "providers": {
                "codex": agent_workflow_configuration(provider="codex"),
                "copilot": agent_workflow_configuration(provider="copilot"),
            },
        }

    def environment(self) -> Dict[str, Any]:
        """Expose local setup status without returning credentials or MCP values."""
        return {
            "repository": {
                "name": self.repo_root.name,
                "path": str(self.repo_root),
                "origin": _sanitized_origin(self.repo_root),
                "is_git_repository": (self.repo_root / ".git").exists(),
            },
            "github_cli": _github_cli_status(),
            "validation_enabled": _validation_enabled(),
            "providers": {provider: _provider_environment(provider) for provider in sorted(AGENT_PROVIDERS)},
            "note": "MCP status means configured locally; it is not a live permission or connectivity check.",
        }

    def diff(self, run_id: str, path: str) -> Dict[str, str]:
        response = self._response(run_id)
        allowed = {change["path"] for change in response["state"].get("file_changes", [])}
        if path not in allowed:
            raise ValueError("That file is not part of this run")
        return {"path": path, "diff": GitOperations(self.repo_root).diff_for_path(path)}


class LocalUiApplication:
    def __init__(self, runtime: WorkflowRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _respond(start_response, status: str, body: str, content_type: str):
        encoded = body.encode("utf-8")
        start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(encoded))), ("Cache-Control", "no-store")])
        return [encoded]

    @staticmethod
    def _body(environ) -> Dict[str, Any]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        request_url = urlparse(environ.get("PATH_INFO", "/") + ("?" + environ.get("QUERY_STRING", "") if environ.get("QUERY_STRING") else ""))
        path = request_url.path
        if method == "GET" and path == "/":
            return self._respond(start_response, "200 OK", HTML, "text/html; charset=utf-8")
        try:
            if method == "GET" and path == "/api/configuration":
                response = self.runtime.configuration()
            elif method == "GET" and path == "/api/environment":
                response = self.runtime.environment()
            elif method == "POST" and path == "/api/runs":
                payload = self._body(environ)
                flow = payload.get("flow", "jira_delivery")
                provider = str(payload.get("provider", "")).strip() or None
                model = str(payload.get("model", "")).strip() or None
                reasoning_effort = str(payload.get("reasoning_effort", "")).strip() or None
                if flow == "jira_delivery":
                    response = self.runtime.start(str(payload.get("ticket_key", "")), provider, model, reasoning_effort)
                elif flow == "pr_review":
                    response = self.runtime.start_pr_review(str(payload.get("pr_number", "")), provider, model, reasoning_effort)
                else:
                    raise ValueError("Unknown workflow")
            elif method == "GET" and path.startswith("/api/runs/") and path.endswith("/diff"):
                run_id = path.removeprefix("/api/runs/").removesuffix("/diff").strip("/")
                response = self.runtime.diff(run_id, parse_qs(request_url.query).get("path", [""])[0])
            elif method == "GET" and path.startswith("/api/runs/"):
                response = self.runtime.status(path.removeprefix("/api/runs/").strip("/"))
            elif method == "POST" and path.startswith("/api/runs/") and path.endswith("/resume"):
                run_id = path.removeprefix("/api/runs/").removesuffix("/resume").strip("/")
                response = self.runtime.resume(run_id, self._body(environ))
            elif method == "POST" and path.startswith("/api/runs/") and path.endswith("/retry"):
                run_id = path.removeprefix("/api/runs/").removesuffix("/retry").strip("/")
                response = self.runtime.retry(run_id)
            else:
                return self._respond(start_response, "404 Not Found", "Not found", "text/plain; charset=utf-8")
            return self._respond(start_response, "200 OK", json.dumps(response), "application/json; charset=utf-8")
        except (ValueError, RuntimeError) as error:
            return self._respond(start_response, "400 Bad Request", json.dumps({"error": str(error)}), "application/json; charset=utf-8")


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic SDLC Workflows</title><style>
:root { color-scheme:light; --bg:#f5f7fb; --panel:#ffffff; --tile:#f8fafc; --input:#ffffff; --line:#e2e7f0; --line-strong:#c9d2e2; --ink:#111827; --muted:#5b6578; --accent:#0a7a58; --accent-bg:#0f8f67; --accent-ink:#ffffff; --accent-soft:#e3f5ee; --secondary-bg:#eef1f7; --warn:#8a5a00; --warn-bg:#fff7e6; --warn-line:#f0d49a; --danger:#b42335; --danger-bg:#fff1f2; --danger-line:#f4b8c0; --danger-ink:#7a1020; --code-bg:#f6f8fb; --add-bg:#e6f6ec; --add-ink:#0f5132; --del-bg:#fdecee; --del-ink:#842029; --hunk-bg:#efeafc; --hunk-ink:#5b3fb0; --meta-bg:#f1f3f8; --shadow:0 1px 2px rgba(16,24,40,.05),0 4px 16px rgba(16,24,40,.04) }
@media (prefers-color-scheme:dark) { :root:not([data-theme="light"]) { color-scheme:dark; --bg:#0b1120; --panel:#131b2e; --tile:#0f1729; --input:#0d1527; --line:#253152; --line-strong:#3b4a6b; --ink:#edf2ff; --muted:#9daac4; --accent:#79e8c0; --accent-bg:#79e8c0; --accent-ink:#092019; --accent-soft:#1d3d34; --secondary-bg:#22304b; --warn:#ffc56f; --warn-bg:#201b10; --warn-line:#78612f; --danger:#ff8d99; --danger-bg:#2a1520; --danger-line:#6d3444; --danger-ink:#ffdce0; --code-bg:#0a1020; --add-bg:#173d2b; --add-ink:#c9f4dc; --del-bg:#4b2028; --del-ink:#ffd5da; --hunk-bg:#1a2038; --hunk-ink:#c9b6ff; --meta-bg:#111a2e; --shadow:none } }
:root[data-theme="dark"] { color-scheme:dark; --bg:#0b1120; --panel:#131b2e; --tile:#0f1729; --input:#0d1527; --line:#253152; --line-strong:#3b4a6b; --ink:#edf2ff; --muted:#9daac4; --accent:#79e8c0; --accent-bg:#79e8c0; --accent-ink:#092019; --accent-soft:#1d3d34; --secondary-bg:#22304b; --warn:#ffc56f; --warn-bg:#201b10; --warn-line:#78612f; --danger:#ff8d99; --danger-bg:#2a1520; --danger-line:#6d3444; --danger-ink:#ffdce0; --code-bg:#0a1020; --add-bg:#173d2b; --add-ink:#c9f4dc; --del-bg:#4b2028; --del-ink:#ffd5da; --hunk-bg:#1a2038; --hunk-ink:#c9b6ff; --meta-bg:#111a2e; --shadow:none }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased }
a { color:var(--accent) } p { color:var(--muted) } h2 { font-size:1.35rem; margin:.2rem 0 1rem } h3 { font-size:1rem; margin:1.4rem 0 .5rem } h4 { font-size:.9rem; margin:1rem 0 .4rem }
.topbar { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:14px; padding:12px 24px; background:var(--panel); border-bottom:1px solid var(--line) }
.brand { font-weight:750; letter-spacing:-.01em; white-space:nowrap } .brand span { color:var(--accent) }
.repo { display:flex; align-items:baseline; gap:8px; min-width:0; color:var(--muted); font-size:.85rem } .repo strong { color:var(--ink); font-size:.92rem } .repo small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.topbar .spacer { flex:1 }
.theme-switch { position:relative; flex:0 0 auto; width:60px; height:30px; padding:0; border-radius:99px; background:var(--secondary-bg); border:1px solid var(--line-strong); color:var(--muted) } .theme-switch:hover { filter:none; border-color:var(--accent) }
.theme-switch .icon { position:absolute; z-index:1; top:50%; transform:translateY(-50%); width:22px; text-align:center; font-size:.8rem; line-height:1; transition:color .18s ease } .theme-switch .sun { left:3px } .theme-switch .moon { right:3px } .theme-switch[aria-checked="false"] .sun,.theme-switch[aria-checked="true"] .moon { color:var(--accent-ink) }
.theme-switch .knob { position:absolute; top:3px; left:3px; width:22px; height:22px; border-radius:50%; background:var(--accent-bg); box-shadow:0 1px 3px rgba(0,0,0,.25); transition:transform .18s ease } .theme-switch[aria-checked="true"] .knob { transform:translateX(30px) }
@media (prefers-reduced-motion:reduce) { .theme-switch .knob { transition:none } }
main { max-width:960px; margin:0 auto; padding:28px 24px 80px }
.card { border:1px solid var(--line); background:var(--panel); border-radius:14px; padding:22px; margin-top:18px; box-shadow:var(--shadow) }
.eyebrow { color:var(--accent); font-size:.72rem; font-weight:750; letter-spacing:.1em; text-transform:uppercase }
button { cursor:pointer; border:0; border-radius:9px; padding:11px 16px; font:inherit; font-weight:700; background:var(--accent-bg); color:var(--accent-ink) } button:hover { filter:brightness(1.06) } button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible,.flow-option:focus-within { outline:2px solid var(--accent); outline-offset:2px }
button.secondary { background:var(--secondary-bg); color:var(--ink) } button.danger { background:var(--danger-bg); color:var(--danger); border:1px solid var(--danger-line) } button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); padding:7px 11px; font-weight:600 } button:disabled { opacity:.5; cursor:not-allowed }
input,select,textarea { color:var(--ink); background:var(--input); border:1px solid var(--line-strong); border-radius:9px; padding:11px 12px; font:inherit; width:100%; min-width:0 } textarea { min-height:100px; resize:vertical } #actions:not(:empty) { margin-top:18px } #log { margin-top:8px }
.field { display:grid; gap:6px; color:var(--muted); font-size:.8rem; font-weight:650 }
.env summary { display:flex; align-items:center; gap:12px; flex-wrap:wrap; cursor:pointer; list-style:none } .env summary::-webkit-details-marker { display:none } .env { padding:14px 18px }
.env-title { font-weight:700; font-size:.9rem } .chips { display:flex; flex-wrap:wrap; gap:6px; flex:1 } .chip { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:99px; padding:3px 10px; font-size:.8rem; color:var(--ink); background:var(--tile) } .chip::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--accent) } .chip.warn { border-color:var(--warn-line); background:var(--warn-bg); color:var(--warn) } .chip.warn::before { background:var(--warn) }
.env-toggle { color:var(--muted); font-size:.8rem } .env[open] .env-toggle::after { content:" ▴" } .env:not([open]) .env-toggle::after { content:" ▾" }
.environment-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-top:14px } .environment-grid div { border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:var(--tile) } .environment-grid small { display:block; color:var(--muted); margin-bottom:3px; font-size:.78rem } .environment-grid strong { display:block; overflow-wrap:anywhere } .environment-grid ul { list-style:none; padding:0; margin:4px 0 0; font-size:.85rem } .environment-grid .ok { color:var(--accent) } .environment-grid .warn { color:var(--warn) } .environment-note { margin:10px 0 0; font-size:.8rem }
.start h1 { font-size:1.5rem; letter-spacing:-.02em; margin:.3rem 0 1rem }
.flow-picker { display:grid; grid-template-columns:1fr 1fr; gap:12px } .flow-option { position:relative; display:grid; gap:4px; padding:14px 16px; border:1.5px solid var(--line); border-radius:12px; cursor:pointer; background:var(--tile) } .flow-option input { position:absolute; opacity:0; pointer-events:none } .flow-option:has(input:checked) { border-color:var(--accent); background:var(--accent-soft) } .flow-title { font-weight:700 } .flow-desc { color:var(--muted); font-size:.85rem }
form { margin-top:16px } .start-row { display:flex; gap:10px; align-items:end } .start-row .field { flex:1 } #startButton { min-width:170px; padding:12px 18px }
.advanced { margin-top:14px; border-top:1px solid var(--line); padding-top:12px } .advanced summary { cursor:pointer; color:var(--muted); font-size:.85rem; font-weight:650 } .advanced summary span { font-weight:500; margin-left:6px } .advanced-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:12px } .default-note { grid-column:1/-1; margin:0; font-size:.82rem }
#message:empty { display:none } #message { margin:12px 0 0 }
#run { display:none; scroll-margin-top:72px } .timeline { display:flex; gap:6px; flex-wrap:wrap; margin:12px 0 14px } .step { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:99px; padding:4px 11px; color:var(--muted); font-size:.8rem } .step.done { color:var(--accent); border-color:transparent; background:var(--accent-soft) } .step.current { color:var(--ink); border-color:var(--accent); font-weight:700 } .step.skipped { opacity:.6 } .step .spinner { width:11px; height:11px; border-width:2px; border-color:var(--line-strong); border-top-color:var(--accent) }
.plan { border:1px solid var(--line); border-radius:12px; padding:16px 18px; background:var(--tile) } .plan-goal { margin:0 0 4px; color:var(--ink); font-size:1rem; font-weight:600 } .plan-section h4 { margin:14px 0 6px; color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.06em } .plan-section ol,.plan-section ul { margin:0; padding-left:1.3rem } .plan-section li { margin:3px 0 } .plan-section.risks { margin-top:14px; padding:2px 12px 10px; border-left:3px solid var(--warn); background:var(--warn-bg); border-radius:0 8px 8px 0 } .plan-section.risks h4 { color:var(--warn) } .file-chips { display:flex; flex-wrap:wrap; gap:6px } .file-chips code { padding:2px 8px; border:1px solid var(--line); border-radius:6px; background:var(--panel); font-size:.8rem }
.agent-line { margin:0 0 6px; font-size:.85rem; cursor:help } .debug { margin-top:18px } .debug summary { cursor:pointer; color:var(--muted); font-size:.8rem }
.start-again { display:none } #start.collapsed { padding:10px 14px } #start.collapsed > * { display:none } #start.collapsed > .start-again { display:inline-block; border:0; padding:4px 6px; color:var(--accent) }
pre { white-space:pre-wrap; word-break:break-word; background:var(--code-bg); border:1px solid var(--line); padding:14px; border-radius:10px; max-height:390px; overflow:auto; font-size:.84rem } #diffPanel { padding:0; white-space:pre; word-break:normal; line-height:1.55 } .diff-line { display:block; min-height:1.55em; padding:0 12px } .diff-line.meta { color:var(--muted); background:var(--meta-bg) } .diff-line.hunk { color:var(--hunk-ink); background:var(--hunk-bg) } .diff-line.added { color:var(--add-ink); background:var(--add-bg) } .diff-line.removed { color:var(--del-ink); background:var(--del-bg) }
.actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:16px } .error { color:var(--danger) }
.validation-failure { margin:16px 0; padding:16px; border:1px solid var(--danger-line); border-left:4px solid var(--danger); border-radius:10px; background:var(--danger-bg) } .validation-failure h3 { color:var(--danger-ink); margin:0 0 6px } .validation-failure p { margin:0 0 10px; color:var(--danger-ink) } .validation-failure h4 { color:var(--ink) } .validation-failure pre { margin:0; max-height:220px }
.outcome-note { margin:16px 0; padding:16px; border:1px solid var(--warn-line); border-left:4px solid var(--warn); border-radius:10px; background:var(--warn-bg) } .outcome-note h3 { color:var(--warn); margin:0 0 6px } .outcome-note p { margin:0 0 8px; color:var(--ink) }
.finding { margin:10px 0; padding:12px 14px; border:1px solid var(--line); border-radius:10px; background:var(--tile) } .finding h3 { margin:0 0 4px; font-size:.95rem } .finding p { margin:0 0 4px } .finding .where { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem }
label.path { display:flex; align-items:center; gap:9px; padding:8px 0; color:var(--ink); border-bottom:1px solid var(--line) } label.path input { width:auto } .file-diff { margin-left:auto; padding:5px 9px; background:var(--secondary-bg); color:var(--ink); font-size:.8rem; font-weight:600 }
.badge { border-radius:99px; padding:2px 8px; font-size:.7rem; font-weight:750; background:var(--accent-soft); color:var(--accent); white-space:nowrap } .badge.deleted,.badge.sev-high,.badge.sev-critical { background:var(--danger-bg); color:var(--danger) } .badge.sev-medium,.badge.sev-request_changes { background:var(--warn-bg); color:var(--warn) }
.live-progress { display:flex; align-items:center; gap:10px; border:1px solid var(--warn-line); background:var(--warn-bg); padding:10px 12px; border-radius:10px; color:var(--warn); font-weight:600 } .spinner { width:16px; height:16px; border:2.5px solid var(--warn-line); border-top-color:var(--warn); border-radius:50%; animation:spin .8s linear infinite; flex:0 0 auto } @keyframes spin { to { transform:rotate(360deg) } }
.run-info { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:16px 0 } .run-info div { border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:var(--tile) } .run-info small { display:block; color:var(--muted); font-size:.78rem; margin-bottom:2px } .run-info strong { font-variant-numeric:tabular-nums } .run-info p { grid-column:1/-1; margin:0; font-size:.8rem }
.events { list-style:none; margin:10px 0 0; padding:0; border-top:1px solid var(--line) } .events li { padding:8px 0; border-bottom:1px solid var(--line); color:var(--muted); font-size:.88rem } .events time { display:inline-block; width:74px; color:var(--ink); font-variant-numeric:tabular-nums }
.footnote { margin:28px 0 0; text-align:center; font-size:.8rem }
@media (max-width:640px) { .topbar { padding:10px 16px; gap:10px } .repo small { display:none } .repo strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap } .env summary .chips { order:3; flex-basis:100% } .env-toggle { margin-left:auto } .run-info { grid-template-columns:1fr 1fr } main { padding:18px 16px 60px } .flow-picker,.advanced-grid { grid-template-columns:1fr } .start-row { flex-direction:column; align-items:stretch } #startButton { width:100% } }
</style></head><body>
<header class="topbar"><div class="brand"><span>◆</span> Agentic SDLC</div><div class="repo" id="repoLabel"><strong>…</strong></div><div class="spacer"></div><button class="theme-switch" id="themeToggle" type="button" role="switch" aria-checked="false" aria-label="Dark mode" title="Toggle light or dark mode"><span class="icon sun" aria-hidden="true">☀︎</span><span class="icon moon" aria-hidden="true">☾</span><span class="knob" aria-hidden="true"></span></button></header>
<main>
<details class="card env" id="environment"><summary><span class="env-title">Environment</span><span class="chips" id="envChips">Checking…</span><span class="env-toggle">Details</span></summary><div class="environment-grid" id="environmentStatus" aria-live="polite">Loading environment status…</div><p class="environment-note" id="environmentNote"></p></details>
<section class="card start" id="start"><button class="ghost start-again" id="startAgain" type="button">+ Start another run</button><div class="eyebrow">Local only · you approve every step</div><h1>What do you want to do?</h1>
<div class="flow-picker" role="radiogroup" aria-label="Workflow"><label class="flow-option"><input type="radio" name="flow" value="jira_delivery" checked><span class="flow-title">Deliver a Jira ticket</span><span class="flow-desc">Plan, implement, and open a draft PR, with your approval at each step.</span></label><label class="flow-option"><input type="radio" name="flow" value="pr_review"><span class="flow-title">Review a pull request</span><span class="flow-desc">Analyze a PR and publish only the review you approve.</span></label></div>
<form id="startForm"><div class="start-row"><label class="field" id="startValueLabel"><span id="startValueText">Jira ticket key</span><input id="startValue" placeholder="PROJ-14" required autocomplete="off"></label><button id="startButton">Analyze ticket</button></div>
<details class="advanced"><summary>Agent settings<span id="agentSummary"></span></summary><div class="advanced-grid"><label class="field">Agent provider<select id="provider"><option value="codex">Codex CLI</option><option value="copilot">GitHub Copilot CLI</option></select></label><label class="field">Model override<input id="model" list="modelChoices" placeholder="Default"></label><datalist id="modelChoices"><option value="gpt-5.6-sol"></option><option value="gpt-5.6-terra"></option><option value="gpt-5.6-luna"></option></datalist><label class="field">Reasoning effort<select id="reasoningEffort"><option value="">Default</option><option value="none">None · fastest</option><option value="low">Low · efficient</option><option value="medium">Medium · balanced</option><option value="high">High · thorough</option><option value="xhigh">Extra high · complex</option><option value="max">Max · hardest tasks</option></select></label><p class="default-note" id="defaultConfiguration">Loading local agent defaults…</p></div></details></form><p id="message" role="status"></p></section>
<section class="card" id="run"><div class="eyebrow" id="phase">Running</div><h2 id="title">Workflow run</h2><div class="timeline" id="timeline"></div><div id="details"></div><div id="actions"></div><div id="log"></div></section>
<p class="footnote">Runs on this machine only. Credentials remain in the local process and are never shown here.</p>
</main><script>
let run, collapsedFor, poller, openDiffPath, providerDefaults={}, environmentData; const message=document.querySelector('#message'), runPanel=document.querySelector('#run'), details=document.querySelector('#details'), actions=document.querySelector('#actions'), providerPicker=document.querySelector('#provider'), modelPicker=document.querySelector('#model'), effortPicker=document.querySelector('#reasoningEffort'), startValue=document.querySelector('#startValue'), defaultConfiguration=document.querySelector('#defaultConfiguration'), environmentStatus=document.querySelector('#environmentStatus'), environmentNote=document.querySelector('#environmentNote'), envChips=document.querySelector('#envChips'), environmentPanel=document.querySelector('#environment');
const startCard=document.querySelector('#start'); document.querySelector('#startAgain').onclick=()=>startCard.classList.remove('collapsed');
const selectedFlow=()=>document.querySelector('input[name="flow"]:checked').value;
const themeToggle=document.querySelector('#themeToggle');
function currentTheme(){return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}
function applyTheme(theme){if(theme)document.documentElement.dataset.theme=theme;themeToggle.setAttribute('aria-checked',String(currentTheme()==='dark'))}
try{applyTheme(localStorage.getItem('theme'))}catch(error){applyTheme()}
themeToggle.onclick=()=>{const next=currentTheme()==='dark'?'light':'dark';applyTheme(next);try{localStorage.setItem('theme',next)}catch(error){}};
async function request(url, payload){ const options=payload===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}; const r=await fetch(url,options), raw=await r.text(); let data; try{data=JSON.parse(raw)}catch(error){throw Error('Server returned HTTP '+r.status+' without a JSON response')} if(!r.ok) throw Error(data.error||'Request failed'); return data }
function escapeHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function renderPlan(plan){ if(!plan||typeof plan!=='object')return '<pre>'+escapeHtml(String(plan))+'</pre>';
 const items=value=>(Array.isArray(value)?value:[value]).filter(item=>item!==null&&item!==undefined&&String(item).trim()).map(item=>typeof item==='object'?JSON.stringify(item):String(item));
 const section=(title,value,kind)=>{const list=items(value);if(!list.length)return '';if(kind==='files')return '<div class="plan-section"><h4>'+title+'</h4><div class="file-chips">'+list.map(item=>'<code>'+escapeHtml(item)+'</code>').join('')+'</div></div>';const tag=kind==='steps'?'ol':'ul';return '<div class="plan-section'+(kind==='risks'?' risks':'')+'"><h4>'+title+'</h4><'+tag+'>'+list.map(item=>'<li>'+escapeHtml(item)+'</li>').join('')+'</'+tag+'></div>'};
 const known={implementation_steps:['Steps','steps'],files_to_inspect:['Files to inspect','files'],tests:['Tests','list'],assumptions:['Assumptions','list'],risks:['Risks','risks']};
 const extra=Object.keys(plan).filter(key=>key!=='goal'&&!known[key]).map(key=>section(escapeHtml(key.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase())),plan[key],'list')).join('');
 return '<div class="plan">'+(plan.goal?'<p class="plan-goal">'+escapeHtml(plan.goal)+'</p>':'')+Object.entries(known).map(([key,[title,kind]])=>section(title,plan[key],kind)).join('')+extra+'</div>' }
function formatTokens(value){return Number(value||0).toLocaleString()}
function renderDiff(diff){return diff.split('\n').map(line=>{let kind='context';if(line.startsWith('diff ')||line.startsWith('index ')||line.startsWith('+++')||line.startsWith('---'))kind='meta';else if(line.startsWith('@@'))kind='hunk';else if(line.startsWith('+'))kind='added';else if(line.startsWith('-'))kind='removed';return '<span class="diff-line '+kind+'">'+escapeHtml(line||' ')+'</span>'}).join('')}
const timelineSteps={jira_delivery:['Ticket','Plan','Implement','Validate','Draft PR'],pr_review:['Pull request','Analyze patch','Publish review']};
function activeSteps(stage,flow,running,status){ const stepByStage=flow==='pr_review'?{starting:0,fetching_pull_request:0,reviewing_pull_request:1,awaiting_pr_review:2,pr_review:2,resuming:2,publishing_review:2}:{starting:0,fetching_ticket:0,planning:1,awaiting_plan_review:1,plan_review:1,resuming:1,preparing_workspace:2,implementing:2,validating:3,awaiting_push_review:4,push_review:4,publishing:4}; const finished=['published','pr_review_published'].includes(status); const index=finished?99:(stepByStage[stage]??-1); const validationOff=environmentData&&environmentData.validation_enabled===false; document.querySelector('#timeline').innerHTML=(timelineSteps[flow]||timelineSteps.jira_delivery).map((step,i)=>{const skipped=step==='Validate'&&validationOff; const state=i<index?'done':i===index?'current':'upcoming'; const mark=skipped&&state!=='current'?'–':state==='done'?'✓':state==='current'?(running?'<span class="spinner" aria-hidden="true"></span>':'●'):'○'; return '<span class="step '+state+(skipped?' skipped':'')+'"'+(state==='current'?' aria-current="step"':'')+'>'+mark+' '+step+(skipped?' · skipped':'')+'</span>'}).join(''); }
function stopPolling(){ if(poller){clearInterval(poller);poller=null} }
function startPolling(){ if(poller||!run?.run_id) return; poller=setInterval(async()=>{try{render(await request('/api/runs/'+run.run_id))}catch(error){stopPolling();actions.innerHTML='<p class="error">'+escapeHtml(error.message)+'</p>'}},1000) }
 function render(data){ run=data; runPanel.style.display='block'; const state=data.state||{}, runStatus=data.run||{}, flow=runStatus.flow||'jira_delivery', phase=data.phase||runStatus.stage||state.status||'complete'; const ticketTitle=String(state.ticket?.summary||'').trim(); document.querySelector('#phase').textContent=flow==='pr_review'?'Pull-request review':'Jira delivery'; document.querySelector('#title').textContent=state.ticket ? state.ticket.key+(ticketTitle?' · '+ticketTitle:'') : state.pull_request ? 'PR #'+state.pull_request.number+' · '+state.pull_request.title : state.ticket_key ? state.ticket_key : state.pr_number ? 'PR #'+state.pr_number : (flow==='pr_review'?'Pull-request review':'Delivery run'); activeSteps(runStatus.stage||phase,flow,runStatus.running,state.status); if(data.run_id!==collapsedFor){collapsedFor=data.run_id;startCard.classList.add('collapsed')}
 const plan=state.plan ? '<h3>Proposed plan</h3>'+renderPlan(state.plan) : ''; const validation=state.validation&&!state.validation.skipped ? '<h3>Validation</h3><pre>'+escapeHtml(JSON.stringify(state.validation,null,2))+'</pre>' : ''; const implementation=state.implementation ? '<h3>Implementation result</h3><pre>'+escapeHtml((state.implementation.stdout||'No implementation output was captured.')+(state.implementation.stderr?'\n\nStderr:\n'+state.implementation.stderr:''))+'</pre>' : ''; const review=state.review ? '<h3>Code-review report <span class="badge sev-'+escapeHtml(state.review.verdict)+'">'+escapeHtml(String(state.review.verdict||'').replaceAll('_',' '))+'</span></h3><p>'+escapeHtml(state.review.summary)+'</p>'+(state.review.findings||[]).map(f=>'<section class="finding"><h3><span class="badge sev-'+escapeHtml(f.severity)+'">'+escapeHtml(f.severity)+'</span> '+escapeHtml(f.title)+'</h3><p>'+escapeHtml(f.body)+'</p><p class="where">'+escapeHtml(f.path||'General')+(f.line?':'+f.line:'')+'</p></section>').join('')+'<h4>Suggested verification</h4><pre>'+escapeHtml((state.review.tests_to_run||[]).join('\n')||'No additional tests suggested.')+'</pre>' : ''; const failures=(state.validation?.results||[]).filter(result=>result.exit_code!==0); const failure=failures.length?'<section class="validation-failure" role="alert"><h3>Validation failed</h3><p>'+failures.length+' command'+(failures.length===1?'':'s')+' failed. A draft PR was not created.</p>'+failures.map(result=>'<h4>'+escapeHtml(result.command)+'</h4><pre>'+escapeHtml((result.output||'No output was captured.').split('\n').slice(-18).join('\n'))+'</pre>').join('')+'</section>':''; const noChanges=state.status==='no_run_changes'?'<section class="outcome-note" role="status"><h3>Implementation finished without new files</h3><p><strong>Ticket fetch, planning, implementation, and validation completed.</strong> This is not a Jira-fetch failure.</p><p>No tracked or untracked file differs from the snapshot taken when this run began, so the workflow cannot safely create a draft PR.</p><p>Review the implementation result below to see what the coding agent reported.</p></section>':''; const pr=state.pull_request_url ? '<p><a href="'+escapeHtml(state.pull_request_url)+'" target="_blank" rel="noreferrer">Open draft pull request</a></p>' : (state.pull_request?.url?'<p><a href="'+escapeHtml(state.pull_request.url)+'" target="_blank" rel="noreferrer">Open pull request</a></p>':''); const model=runStatus.model||{}, usage=runStatus.usage||{}, issue=runStatus.failure; const compact=n=>{n=Number(n||0);return n>=1000?(n/1000).toFixed(n>=10000?0:1)+'k':String(n)}; const agentParts=[model.provider_label||'Local CLI',model.model||'default model',(model.reasoning_effort||'default')+' effort'].concat(usage.total_tokens?['~'+compact(usage.total_tokens)+' tokens ('+compact(usage.input_tokens)+' in / '+compact(usage.output_tokens)+' out)']:[]); const sources='Model: '+(model.model_source||'local provider configuration')+'\nReasoning: '+(model.reasoning_effort_source||'local provider configuration')+(usage.total_tokens?'\nTokens are payload-size estimates, not billed usage.':''); const telemetry='<p class="agent-line" title="'+escapeHtml(sources)+'">'+agentParts.map(escapeHtml).join(' · ')+'</p>'; const technicalDetail=issue?.technical_detail?'<h4>Technical detail</h4><pre>'+escapeHtml(issue.technical_detail)+'</pre>':''; const issuePanel=issue?'<section class="validation-failure" role="alert"><h3>'+escapeHtml(issue.title)+'</h3><p>'+escapeHtml(issue.summary)+'</p><p><strong>Next step:</strong> '+escapeHtml(issue.next_step)+'</p>'+technicalDetail+'</section>':''; const events=(runStatus.events||[]).map(event=>'<li><time>'+escapeHtml(event.at)+'</time>'+escapeHtml(event.message)+'</li>').join(''); details.innerHTML=telemetry+issuePanel+failure+noChanges+plan+implementation+validation+review+pr; document.querySelector('#log').innerHTML='<h3>Activity</h3><ul class="events">'+events+'</ul><details class="debug"><summary>Debug: raw state</summary><pre>'+escapeHtml(JSON.stringify(state,null,2))+'</pre></details>'; actions.innerHTML='';
 if(runStatus.running){ startPolling(); return } stopPolling();
 if(phase==='plan_review'){ actions.innerHTML='<textarea id="feedback" placeholder="Feedback for a revised plan (optional)"></textarea><div class="actions"><button id="approve">Approve plan</button><button class="secondary" id="revise">Request revision</button><button class="danger" id="reject">Reject</button></div>'; document.querySelector('#approve').onclick=()=>resume({action:'approve'}); document.querySelector('#revise').onclick=()=>resume({action:'revise',feedback:document.querySelector('#feedback').value}); document.querySelector('#reject').onclick=()=>resume({action:'reject'}); }
 if(phase==='push_review'){ openDiffPath=null; const changes=state.file_changes||[]; if(!changes.length){actions.innerHTML='<section class="validation-failure" role="status"><h3>No files changed</h3><p>This run did not produce any files for review, so a draft PR cannot be created.</p></section>';return} const label={A:'Added',M:'Modified',D:'Deleted',R:'Renamed'}; actions.innerHTML='<h3>Review changes for the draft PR</h3><p>Select files only after inspecting their local Git diff.</p>'+changes.map(change=>'<label class="path"><input type="checkbox" value="'+escapeHtml(change.path)+'" checked><span class="badge '+(change.status==='D'?'deleted':'')+'">'+escapeHtml(label[change.status]||change.status)+'</span><span>'+escapeHtml(change.path)+'</span><button class="file-diff" type="button" data-path="'+escapeHtml(change.path)+'">View diff</button></label>').join('')+'<pre id="diffPanel" hidden></pre><div class="actions"><button id="publish">Create draft PR</button><button class="danger" id="reject">Reject publication</button></div>'; document.querySelectorAll('.file-diff').forEach(button=>button.onclick=()=>showDiff(button.dataset.path)); document.querySelector('#publish').onclick=()=>resume({action:'approve',paths:[...document.querySelectorAll('.path input:checked')].map(x=>x.value)}); document.querySelector('#reject').onclick=()=>resume({action:'reject'}); }
 if(phase==='pr_review'){const findings=state.review?.findings||[];actions.innerHTML='<h3>Publish pull-request review</h3><p>This is an external GitHub action. Confirm the decision, review body, and selected comments before publishing.</p><label class="field">Review decision<select id="reviewEvent"><option value="approve">Approve</option><option value="request_changes" '+(state.review?.verdict==='request_changes'?'selected':'')+'>Request changes</option><option value="comment" '+(state.review?.verdict==='comment'?'selected':'')+'>Comment only</option></select></label><label class="field">Review summary<textarea id="reviewBody">'+escapeHtml(state.review?.summary||'')+'</textarea></label><h4>Inline comments</h4>'+(findings.length?findings.map((finding,index)=>'<label class="path"><input type="checkbox" value="'+index+'" checked><span class="badge">'+escapeHtml(finding.severity||'finding')+'</span><span>'+escapeHtml(finding.path||'General comment')+(finding.line?' : '+finding.line:'')+' · '+escapeHtml(finding.title||finding.body||'Finding')+'</span></label>').join(''):'<p>No generated findings to post inline.</p>')+'<div class="actions"><button id="publishReview">Publish review to GitHub</button><button class="secondary" id="dismissReview">Dismiss local review</button></div>';document.querySelector('#publishReview').onclick=()=>resume({action:'publish',event:document.querySelector('#reviewEvent').value,body:document.querySelector('#reviewBody').value,finding_indexes:[...document.querySelectorAll('.path input:checked')].map(input=>Number(input.value))});document.querySelector('#dismissReview').onclick=()=>resume({action:'dismiss'}); }
 if(runStatus.retry?.available){const target=String(runStatus.retry.target||'failed step').replaceAll('_',' ');actions.innerHTML='<section class="validation-failure" role="status"><h3>Retry available</h3><p>Only <strong>'+escapeHtml(target)+'</strong> will run again. Earlier completed steps and approvals are preserved.</p><div class="actions"><button id="retry">Retry '+escapeHtml(target)+'</button></div></section>';document.querySelector('#retry').onclick=()=>retryRun()}
}
async function showDiff(path){ const panel=document.querySelector('#diffPanel'); if(openDiffPath===path){panel.hidden=true;openDiffPath=null;document.querySelectorAll('.file-diff').forEach(button=>button.textContent='View diff');return} openDiffPath=path; panel.hidden=false; document.querySelectorAll('.file-diff').forEach(button=>button.textContent=button.dataset.path===path?'Hide diff':'View diff'); panel.textContent='Loading '+path+'…'; try{const result=await request('/api/runs/'+run.run_id+'/diff?path='+encodeURIComponent(path)); panel.innerHTML=renderDiff(result.diff)}catch(error){openDiffPath=null;panel.textContent='Unable to load diff: '+error.message;document.querySelectorAll('.file-diff').forEach(button=>button.textContent='View diff')} }
async function resume(decision){ actions.innerHTML='<p>Continuing local workflow…</p>'; try{render(await request('/api/runs/'+run.run_id+'/resume',decision))}catch(e){actions.innerHTML='<p class="error">'+escapeHtml(e.message)+'</p>'} }
async function retryRun(){actions.innerHTML='<p>Retrying only the failed step…</p>';try{render(await request('/api/runs/'+run.run_id+'/retry',{}))}catch(e){actions.innerHTML='<p class="error">'+escapeHtml(e.message)+'</p>'}}
function renderEnvironment(){if(!environmentData)return;const repo=environmentData.repository||{}, provider=environmentData.providers?.[providerPicker.value]||{}, github=environmentData.github_cli||{}, mcps=(provider.configured_mcps||[]);const mcpList=mcps.length?'<ul>'+mcps.map(name=>'<li>'+escapeHtml(name)+'</li>').join('')+'</ul>':'<strong class="warn">None found</strong>';environmentStatus.innerHTML='<div><small>Target repository</small><strong>'+escapeHtml(repo.name||'Unknown')+'</strong><small>'+escapeHtml(repo.origin||repo.path||'No origin remote')+'</small></div><div><small>GitHub CLI</small><strong class="'+(github.authenticated?'ok':'warn')+'">'+(github.authenticated?'Authenticated':github.available?'Not authenticated':'Not installed')+'</strong></div><div><small>'+escapeHtml(provider.label||'Agent provider')+'</small><strong class="'+(provider.installed?'ok':'warn')+'">'+(provider.installed?'Installed':'Not installed')+'</strong></div><div><small>Configured MCPs · '+escapeHtml(provider.label||'selected provider')+'</small>'+mcpList+'</div><div><small>Jira MCP · '+escapeHtml(provider.expected_jira_mcp||'atlassian')+'</small><strong class="'+(provider.jira_mcp_configured?'ok':'warn')+'">'+(provider.jira_mcp_configured?'Configured':'Not configured')+'</strong></div>';environmentNote.textContent=environmentData.note||'';const jira=selectedFlow()==='jira_delivery';const chips=[['GitHub CLI',github.authenticated],[provider.label||'Agent CLI',provider.installed]].concat(jira?[['Jira MCP',provider.jira_mcp_configured]]:[]);envChips.innerHTML=chips.map(([label,ok])=>'<span class="chip'+(ok?'':' warn')+'">'+escapeHtml(label)+(ok?'':' · needs setup')+'</span>').join('');if(chips.some(([,ok])=>!ok))environmentPanel.open=true;document.querySelector('#repoLabel').innerHTML='<strong>'+escapeHtml(repo.name||'Unknown repository')+'</strong><small>'+escapeHtml(repo.origin||repo.path||'No origin remote')+'</small>'}
function updateStartForm(){const review=selectedFlow()==='pr_review', copilot=providerPicker.value==='copilot', noEffort=effortPicker.querySelector('option[value="none"]');noEffort.disabled=!copilot&&modelPicker.value==='gpt-5.6-luna';if(noEffort.disabled&&effortPicker.value==='none')effortPicker.value='';if(copilot)effortPicker.value='';effortPicker.disabled=copilot;startValue.placeholder=review?'42':'PROJ-14';document.querySelector('#startValueText').textContent=review?'Pull-request number':'Jira ticket key';document.querySelector('#startButton').textContent=review?'Review pull request':'Analyze ticket';const settings=providerDefaults[providerPicker.value];if(settings){modelPicker.placeholder='Default: '+(settings.model||'Automatic');document.querySelector('#agentSummary').textContent=(providerPicker.selectedOptions[0]?.text||'')+' · '+(modelPicker.value||settings.model||'default model')+(effortPicker.value?' · '+effortPicker.value:'');defaultConfiguration.textContent=settings.provider_label+' defaults · Model: '+(settings.model||'Automatic')+' ('+(settings.model_source||'runtime')+') · Reasoning: '+(settings.reasoning_effort||'Automatic')+' ('+(settings.reasoning_effort_source||'runtime')+').'+(copilot?' Reasoning effort is managed by Copilot local settings.':'')}renderEnvironment()}
async function loadDefaults(){try{const configuration=await request('/api/configuration');providerDefaults=configuration.providers||{};const selected=configuration.defaults?.provider||'codex';providerPicker.value=providerDefaults[selected]?selected:'codex';updateStartForm()}catch(error){defaultConfiguration.textContent='Local agent defaults could not be read. The selected CLI will use its automatic defaults.'}}
async function loadEnvironment(){try{environmentData=await request('/api/environment');renderEnvironment()}catch(error){environmentStatus.innerHTML='<div><strong class="warn">Environment status unavailable</strong></div>';environmentNote.textContent=error.message}}
document.querySelectorAll('input[name="flow"]').forEach(radio=>radio.onchange=updateStartForm);effortPicker.onchange=updateStartForm;providerPicker.onchange=updateStartForm;modelPicker.oninput=updateStartForm;updateStartForm();loadDefaults();loadEnvironment();
document.querySelector('#startForm').onsubmit=async e=>{e.preventDefault();const review=selectedFlow()==='pr_review', settings={provider:providerPicker.value,model:modelPicker.value,reasoning_effort:effortPicker.value};message.textContent=review?'Starting local PR review…':'Starting local delivery run…';try{render(await request('/api/runs',review?{flow:'pr_review',pr_number:startValue.value,...settings}:{flow:'jira_delivery',ticket_key:startValue.value,...settings}));message.textContent='';runPanel.scrollIntoView({behavior:'smooth',block:'start'})}catch(err){message.innerHTML='<span class="error">'+escapeHtml(err.message)+'</span>'}};
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local agentic SDLC workflow UI")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path, default=Path(".agentic-workflow/checkpoints.sqlite"))
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",), help="Loopback only for local safety")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-open", action="store_true", help="Do not open the local UI in a browser")
    args = parser.parse_args()
    repo = args.repo.resolve()
    load_workflow_environment(repo)
    database = args.database if args.database.is_absolute() else repo / args.database
    app = LocalUiApplication(WorkflowRuntime(repo, database))
    with make_server(args.host, args.port, app) as server:
        url = f"http://{args.host}:{args.port}"
        print(f"Local delivery review UI: {url}")
        if not args.no_open:
            webbrowser.open(url)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

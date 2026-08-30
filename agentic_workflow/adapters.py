import base64
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .contracts import Implementer, Planner, PullRequestClient, PullRequestReviewer, TicketClient


_RUN_CODEX_CONFIGURATION: ContextVar[Dict[str, str] | None] = ContextVar(
    "run_codex_configuration", default=None
)

AGENT_PROVIDERS = {"codex", "copilot"}


def _active_workflow_configuration() -> Dict[str, str]:
    """Return this run's provider configuration without exposing credentials."""
    return _RUN_CODEX_CONFIGURATION.get() or agent_workflow_configuration()


class JiraRestClient(TicketClient):
    """Minimal Jira Cloud client; credentials never enter graph state."""

    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()

    @classmethod
    def from_environment(cls) -> "JiraRestClient":
        required = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError("Missing Jira configuration: " + ", ".join(missing))
        return cls(os.environ["JIRA_BASE_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    def fetch(self, ticket_key: str) -> Dict[str, Any]:
        request = Request(
            f"{self.base_url}/rest/api/3/issue/{ticket_key}",
            headers={"Authorization": f"Basic {self.auth}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                issue = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Could not fetch Jira ticket {ticket_key}: HTTP {error.code}") from error
        fields = issue["fields"]
        description = fields.get("description")
        # Jira Cloud can return ADF. Preserve it as JSON instead of losing requirements.
        if isinstance(description, dict):
            description = json.dumps(description)
        return {
            "key": issue["key"],
            "summary": fields["summary"],
            "description": description or "",
            "issue_type": fields["issuetype"]["name"],
            "url": f"{self.base_url}/browse/{issue['key']}",
        }


class OpenAIPlanner(Planner):
    """Plans only; it cannot edit files or invoke shell commands."""

    def __init__(self, model: str) -> None:
        self.model = model

    @classmethod
    def from_environment(cls) -> "OpenAIPlanner":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required to generate a plan")
        return cls(os.getenv("OPENAI_MODEL", "gpt-5"))

    def create(self, ticket: Dict[str, Any], feedback: Optional[str] = None) -> Dict[str, Any]:
        from openai import OpenAI

        prompt = {
            "ticket": ticket,
            "review_feedback": feedback or "",
            "required_schema": {
                "goal": "string",
                "assumptions": ["string"],
                "files_to_inspect": ["string"],
                "implementation_steps": ["string"],
                "tests": ["string"],
                "risks": ["string"],
            },
        }
        response = OpenAI().responses.create(
            model=self.model,
            instructions=(
                "You are a software delivery planner. Return only valid JSON matching "
                "required_schema. Do not claim work is done. Prefer existing project conventions."
            ),
            input=json.dumps(prompt),
        )
        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as error:
            raise RuntimeError("Planner returned non-JSON output") from error


def _codex_environment() -> Dict[str, str]:
    """Provide Codex only the local session settings it needs, never Jira keys."""
    allowed = ("CODEX_HOME", "HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TMPDIR", "USER")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _local_codex_settings() -> Dict[str, str]:
    """Read the two non-sensitive defaults the workflow can accurately surface."""
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    config_path = codex_home / "config.toml"
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}

    return {
        key: value
        for key in ("model", "model_reasoning_effort")
        if isinstance((value := config.get(key)), str) and value.strip()
    }


def _local_copilot_settings() -> Dict[str, str]:
    """Read only non-sensitive GitHub Copilot CLI settings when they exist."""
    copilot_home = Path(os.getenv("COPILOT_HOME", str(Path.home() / ".copilot")))
    settings_path = copilot_home / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(settings, dict):
        return {}
    values = {"model": settings.get("model"), "effortLevel": settings.get("effortLevel")}
    return {key: value for key, value in values.items() if isinstance(value, str) and value.strip()}


def codex_workflow_configuration(
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    source: str = "local Codex configuration",
) -> Dict[str, str]:
    """Expose only the workflow's explicit Codex configuration, never secrets."""
    active = _RUN_CODEX_CONFIGURATION.get()
    if active is not None and model is None and reasoning_effort is None:
        return active
    local_settings = _local_codex_settings()
    configured_model = model or os.getenv("WORKFLOW_CODEX_MODEL") or local_settings.get("model")
    configured_effort = (
        reasoning_effort
        or os.getenv("WORKFLOW_CODEX_REASONING_EFFORT")
        or local_settings.get("model_reasoning_effort")
    )
    return {
        "model": configured_model or "Codex automatic default",
        "model_source": source if model else ("WORKFLOW_CODEX_MODEL" if os.getenv("WORKFLOW_CODEX_MODEL") else ("CODEX_HOME/config.toml" if local_settings.get("model") else "Codex runtime")),
        "reasoning_effort": configured_effort or "Codex automatic default",
        "reasoning_effort_source": source if reasoning_effort else ("WORKFLOW_CODEX_REASONING_EFFORT" if os.getenv("WORKFLOW_CODEX_REASONING_EFFORT") else ("CODEX_HOME/config.toml" if local_settings.get("model_reasoning_effort") else "Codex runtime")),
    }


def agent_workflow_configuration(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    source: str = "local configuration",
) -> Dict[str, str]:
    """Resolve one local CLI provider's non-sensitive run configuration."""
    active = _RUN_CODEX_CONFIGURATION.get()
    if active is not None and provider is None and model is None and reasoning_effort is None:
        return active

    resolved_provider = (provider or os.getenv("WORKFLOW_AGENT_PROVIDER", "codex")).strip().lower()
    if resolved_provider not in AGENT_PROVIDERS:
        raise ValueError("WORKFLOW_AGENT_PROVIDER must be either 'codex' or 'copilot'")
    if resolved_provider == "codex":
        return {"provider": "codex", "provider_label": "Codex CLI", **codex_workflow_configuration(model, reasoning_effort, source)}

    local_settings = _local_copilot_settings()
    configured_model = model or os.getenv("WORKFLOW_COPILOT_MODEL") or local_settings.get("model")
    configured_effort = reasoning_effort or os.getenv("WORKFLOW_COPILOT_REASONING_EFFORT") or local_settings.get("effortLevel")
    return {
        "provider": "copilot",
        "provider_label": "GitHub Copilot CLI",
        "model": configured_model or "Copilot automatic default",
        "model_source": source if model else ("WORKFLOW_COPILOT_MODEL" if os.getenv("WORKFLOW_COPILOT_MODEL") else ("COPILOT_HOME/settings.json" if local_settings.get("model") else "Copilot runtime")),
        "reasoning_effort": configured_effort or "Copilot automatic default",
        "reasoning_effort_source": source if reasoning_effort else ("WORKFLOW_COPILOT_REASONING_EFFORT" if os.getenv("WORKFLOW_COPILOT_REASONING_EFFORT") else ("COPILOT_HOME/settings.json" if local_settings.get("effortLevel") else "Copilot runtime")),
    }


@contextmanager
def use_codex_workflow_configuration(configuration: Dict[str, str]):
    """Scope a UI-selected model to one workflow thread without mutating os.environ."""
    token = _RUN_CODEX_CONFIGURATION.set(configuration)
    try:
        yield
    finally:
        _RUN_CODEX_CONFIGURATION.reset(token)


use_workflow_configuration = use_codex_workflow_configuration


def _codex_options() -> list[str]:
    """Apply explicit workflow overrides when configured by the local operator."""
    configuration = codex_workflow_configuration()
    options: list[str] = []
    if configuration["model"] != "Codex automatic default":
        options.extend(["--model", configuration["model"]])
    if configuration["reasoning_effort"] != "Codex automatic default":
        options.extend(["--config", f'model_reasoning_effort="{configuration["reasoning_effort"]}"'])
    return options


def _copilot_options() -> list[str]:
    """Apply only flags supported by Copilot CLI's programmatic interface."""
    configuration = _active_workflow_configuration()
    options: list[str] = []
    if configuration["model"] != "Copilot automatic default":
        options.append(f"--model={configuration['model']}")
    return options


class CodexJsonAgent:
    """Runs the user's already-authenticated local Codex CLI with JSON output."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="memomatch-codex-") as directory:
            temporary = Path(directory)
            output = temporary / "response.json"
            schema_path = temporary / "response-schema.json"
            schema_path.write_text(json.dumps(schema))
            result = subprocess.run(
                [
                    "codex",
                    "exec",
                    *_codex_options(),
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output),
                    prompt,
                ],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                timeout=10 * 60,
                env=_codex_environment(),
                check=False,
            )
            if result.returncode:
                raise RuntimeError(f"Local Codex request failed: {result.stderr[-1000:]}")
            try:
                return json.loads(output.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError("Local Codex did not return valid structured data") from error


def _parse_json_agent_response(raw: str, provider: str) -> Dict[str, Any]:
    """Accept JSON responses with or without a Markdown code fence."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Local {provider} did not return valid structured data") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Local {provider} did not return a JSON object")
    return value


class CopilotJsonAgent:
    """Runs GitHub Copilot CLI programmatically for structured work."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, prompt: str, schema: Dict[str, Any], *, allow_ticket_mcp: bool = False) -> Dict[str, Any]:
        schema_instruction = (
            "Return only a JSON object that conforms to this JSON Schema. Do not wrap it in Markdown.\n"
            + json.dumps(schema)
        )
        command = ["copilot", "-s", "-p", f"{prompt}\n\n{schema_instruction}", "--no-ask-user", *_copilot_options()]
        if allow_ticket_mcp:
            mcp_server = os.getenv("WORKFLOW_COPILOT_TICKET_MCP_SERVER", "atlassian").strip()
            if not mcp_server:
                raise RuntimeError("WORKFLOW_COPILOT_TICKET_MCP_SERVER must name the configured Jira MCP server")
            command.append(f"--allow-tool={mcp_server}")
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                timeout=10 * 60,
                env=_codex_environment(),
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("GitHub Copilot CLI was not found. Install it and run `copilot login` before retrying.") from error
        if result.returncode:
            raise RuntimeError(f"GitHub Copilot CLI request failed: {result.stderr[-1000:]}")
        return _parse_json_agent_response(result.stdout, "GitHub Copilot CLI")


def _structured_agent(repo_root: Path):
    """Choose the local structured-output provider for this workflow run."""
    provider = _active_workflow_configuration().get("provider", "codex")
    return CopilotJsonAgent(repo_root) if provider == "copilot" else CodexJsonAgent(repo_root)


class CodexMcpTicketClient(TicketClient):
    """Fetches Jira through the user's configured Atlassian MCP connection."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = CodexJsonAgent(repo_root)

    def fetch(self, ticket_key: str) -> Dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "summary", "description", "issue_type", "url"],
            "properties": {
                "key": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "issue_type": {"type": "string", "minLength": 1},
                "url": {"type": "string", "minLength": 1},
            },
        }
        return self.agent.run(
            (
                f"Use only the configured Atlassian MCP tool to retrieve Jira ticket {ticket_key}. "
                "Read the issue's actual Jira Summary field and place that exact non-empty value in "
                "`summary`; never use a blank placeholder or infer it from the description. "
                "Do not edit files or call any write tool. Return the requested JSON exactly."
            ),
            schema,
        )


class LocalMcpTicketClient(CodexMcpTicketClient):
    """Fetches Jira through the selected local CLI provider's configured MCP tools."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = _structured_agent(repo_root)

    def fetch(self, ticket_key: str) -> Dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "summary", "description", "issue_type", "url"],
            "properties": {
                "key": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "issue_type": {"type": "string", "minLength": 1},
                "url": {"type": "string", "minLength": 1},
            },
        }
        prompt = (
            f"Use only the configured Atlassian MCP tool to retrieve Jira ticket {ticket_key}. "
            "Read the issue's actual Jira Summary field and place that exact non-empty value in "
            "`summary`; never use a blank placeholder or infer it from the description. "
            "Do not edit files or call any write tool."
        )
        if isinstance(self.agent, CopilotJsonAgent):
            return self.agent.run(prompt, schema, allow_ticket_mcp=True)
        return self.agent.run(prompt + " Return the requested JSON exactly.", schema)


class CodexPlanner(Planner):
    """Generates a plan through the user's local Codex login instead of an API key."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = CodexJsonAgent(repo_root)

    def create(self, ticket: Dict[str, Any], feedback: Optional[str] = None) -> Dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal", "assumptions", "files_to_inspect", "implementation_steps", "tests", "risks"],
            "properties": {
                "goal": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "files_to_inspect": {"type": "array", "items": {"type": "string"}},
                "implementation_steps": {"type": "array", "items": {"type": "string"}},
                "tests": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
        }
        request = json.dumps({"ticket": ticket, "review_feedback": feedback or ""})
        return self.agent.run(
            (
                "Act as a software delivery planner for this local repository. Return only a concrete, "
                "non-implementing plan matching the JSON schema. Respect the ticket scope and feedback.\n\n"
                + request
            ),
            schema,
        )


class LocalPlanner(CodexPlanner):
    """Generates plans with the local provider selected for the current run."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = _structured_agent(repo_root)


class GitHubCliPullRequestClient(PullRequestClient):
    """Reads PR metadata and patches through the locally authenticated GitHub CLI."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["gh", *args], cwd=self.repo_root, text=True, capture_output=True,
            timeout=60, env=_codex_environment(), check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "GitHub CLI command failed")
        return result.stdout

    def fetch(self, number: int) -> Dict[str, Any]:
        fields = "number,title,body,url,state,isDraft,author,baseRefName,headRefName,additions,deletions,changedFiles,mergeable,reviewDecision"
        try:
            return json.loads(self._run("pr", "view", str(number), "--json", fields))
        except json.JSONDecodeError as error:
            raise RuntimeError("GitHub CLI did not return valid pull-request metadata") from error

    def diff(self, number: int) -> str:
        return self._run("pr", "diff", str(number), "--patch")


class CodexPullRequestReviewer(PullRequestReviewer):
    """Uses Codex in read-only mode to review a fetched pull request locally."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = CodexJsonAgent(repo_root)

    def review(self, pull_request: Dict[str, Any], diff: str) -> Dict[str, Any]:
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["summary", "verdict", "findings", "tests_to_run"],
            "properties": {
                "summary": {"type": "string"},
                "verdict": {"type": "string", "enum": ["approve", "comment", "request_changes"]},
                "findings": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["severity", "title", "body", "path", "line"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "title": {"type": "string"}, "body": {"type": "string"},
                        "path": {"type": "string"}, "line": {"type": ["integer", "null"]},
                    },
                }},
                "tests_to_run": {"type": "array", "items": {"type": "string"}},
            },
        }
        request = json.dumps({"pull_request": pull_request, "patch": diff})
        return self.agent.run(
            "Act as a careful code reviewer for this local repository. Review only the supplied pull request metadata and patch. "
            "Report only actionable correctness, regression, security, or test-coverage findings; do not invent style nits. "
            "Do not edit files, run commands, or post GitHub comments. Return the requested JSON exactly.\n\n" + request,
            schema,
        )


class LocalPullRequestReviewer(CodexPullRequestReviewer):
    """Reviews a PR using the selected local CLI provider without posting comments."""

    def __init__(self, repo_root: Path) -> None:
        self.agent = _structured_agent(repo_root)


class CommandImplementer(Implementer):
    """Runs a separately configured coding agent after plan approval only."""

    def __init__(self, command: str, repo_root: Path) -> None:
        self.command = shlex.split(command)
        self.repo_root = repo_root

    @classmethod
    def from_environment(cls, repo_root: Path) -> "CommandImplementer":
        command = os.getenv("IMPLEMENTATION_COMMAND")
        if command:
            return cls(command, repo_root)
        if _active_workflow_configuration().get("provider") == "copilot":
            return CopilotImplementer(repo_root)
        command = "codex exec --sandbox workspace-write -"
        return cls(command, repo_root)

    def run(self, ticket: Dict[str, Any], plan: Dict[str, Any], branch: str) -> Dict[str, Any]:
        payload = json.dumps(
            {
                "ticket": ticket,
                "plan": plan,
                "branch": branch,
                "safety_rules": [
                    "Implement only the approved plan.",
                    "Do not commit, push, create pull requests, alter remotes, or read secrets.",
                    "Run relevant local tests and report commands/results.",
                ],
            }
        )
        command = list(self.command)
        if command[:2] == ["codex", "exec"]:
            command[2:2] = _codex_options()
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            input=payload,
            text=True,
            capture_output=True,
            timeout=60 * 60,
            # Do not forward Jira or planner credentials to the coding agent.
            # The small allowlist keeps command discovery and local CLI sessions
            # working while minimizing accidental credential exposure.
            env={**_codex_environment(), "AGENTIC_WORKFLOW_NO_PUSH": "1"},
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Implementation command failed ({result.returncode}): {result.stderr[-2000:]}")
        return {"command": command, "stdout": result.stdout[-6000:], "stderr": result.stderr[-2000:]}


class CopilotImplementer(Implementer):
    """Uses Copilot CLI with only repository write and shell permissions."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, ticket: Dict[str, Any], plan: Dict[str, Any], branch: str) -> Dict[str, Any]:
        payload = json.dumps(
            {
                "ticket": ticket,
                "plan": plan,
                "branch": branch,
                "safety_rules": [
                    "Implement only the approved plan.",
                    "Do not commit, push, create pull requests, alter remotes, or read secrets.",
                    "Run relevant local tests and report commands/results.",
                ],
            }
        )
        prompt = (
            "Act as the implementation agent for this local repository. Follow the approved request below. "
            "You may edit repository files and run local commands, but never commit, push, create pull requests, "
            "alter remotes, or read secrets. Return a concise report of edits and test results.\n\n"
            + payload
        )
        command = [
            "copilot", "-s", "-p", prompt, "--no-ask-user",
            "--allow-tool=write,shell", *_copilot_options(),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                timeout=60 * 60,
                env={**_codex_environment(), "AGENTIC_WORKFLOW_NO_PUSH": "1"},
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("GitHub Copilot CLI was not found. Install it and run `copilot login` before retrying.") from error
        if result.returncode:
            raise RuntimeError(f"GitHub Copilot CLI implementation failed ({result.returncode}): {result.stderr[-2000:]}")
        return {"command": command, "stdout": result.stdout[-6000:], "stderr": result.stderr[-2000:]}

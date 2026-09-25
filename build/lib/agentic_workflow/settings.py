"""Layered local configuration for workflow runs.

Settings are resolved once the target repository is known, so the installed
commands behave the same from any working directory.  Precedence, highest
first:

1. The process environment.
2. ``<target repo>/.agentic-workflow.env`` — per-repository, non-secret
   workflow settings only (see ``REPOSITORY_SETTINGS``).
3. ``~/.config/agentic-workflows/.env`` (or ``$AGENTIC_WORKFLOWS_CONFIG``).
4. ``.env`` in this project's checkout, for editable installs.

A target repository's own ``.env`` is never read.
"""

import os
from pathlib import Path
from typing import Iterable, Optional

from dotenv import dotenv_values


REPOSITORY_SETTINGS_FILE = ".agentic-workflow.env"

# A repository file may be committed and shared, so it can only select
# validation and provider preferences. Commands that run in place of the agent
# and credentials stay in user-controlled configuration.
REPOSITORY_SETTINGS = frozenset(
    {
        "WORKFLOW_VALIDATION_COMMANDS",
        "WORKFLOW_AGENT_PROVIDER",
        "WORKFLOW_CODEX_MODEL",
        "WORKFLOW_CODEX_REASONING_EFFORT",
        "WORKFLOW_COPILOT_MODEL",
        "WORKFLOW_COPILOT_TICKET_MCP_SERVER",
    }
)


def user_config_path() -> Path:
    configured = os.getenv("AGENTIC_WORKFLOWS_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "agentic-workflows" / ".env"


def checkout_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _apply(values: dict, allowed: Optional[Iterable[str]] = None) -> None:
    for name, value in values.items():
        if value is None or name in os.environ:
            continue
        if allowed is not None and name not in allowed:
            continue
        os.environ[name] = value


def load_workflow_environment(
    repo: Optional[Path] = None,
    user_config: Optional[Path] = None,
    checkout_env: Optional[Path] = None,
) -> None:
    """Fill unset variables from the configuration layers, highest first."""
    if repo is not None:
        repo_file = Path(repo) / REPOSITORY_SETTINGS_FILE
        if repo_file.is_file():
            _apply(dotenv_values(repo_file), REPOSITORY_SETTINGS)
    for path in (user_config or user_config_path(), checkout_env or checkout_env_path()):
        if path.is_file():
            _apply(dotenv_values(path))

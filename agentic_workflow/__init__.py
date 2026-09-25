"""Local, human-approved agentic SDLC workflows.

This package contains the repository-specific application built on LangGraph.
It exposes two flows: Jira delivery, which pauses before implementation and
publication, and pull-request review with an explicit publication gate. Provider credentials and MCP
configuration remain outside package state; importing the package only loads
local workflow configuration when it is present.
"""

from pathlib import Path

from dotenv import load_dotenv


# Local invocations conventionally start from this project. Existing process
# environment takes precedence, and no target-repository `.env` is loaded.
load_dotenv(Path.cwd() / ".env", override=False)

"""Human-approved Jira-to-GitHub delivery workflow built with LangGraph."""
"""Local, human-approved agentic SDLC workflows."""

from pathlib import Path

from dotenv import load_dotenv


# Local invocations conventionally start from this project. Existing process
# environment takes precedence, and no target-repository `.env` is loaded.
load_dotenv(Path.cwd() / ".env", override=False)

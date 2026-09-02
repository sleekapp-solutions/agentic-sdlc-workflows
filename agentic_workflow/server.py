"""LangGraph Studio/Agent Server entrypoint for Jira delivery.

LangGraph Studio discovers the module-level compiled ``graph`` object.  The
Agent Server owns persistence in Studio/deployed environments; the terminal
CLI and local browser UI use their own SQLite checkpointers.  The target
repository is selected by ``WORKFLOW_REPO`` or, as a fallback, the current
directory.
"""
import os
from pathlib import Path

from .jira_delivery import build_graph


REPOSITORY_ROOT = Path(os.getenv("WORKFLOW_REPO", Path.cwd())).resolve()
graph = build_graph(REPOSITORY_ROOT).compile(name="jira_delivery")

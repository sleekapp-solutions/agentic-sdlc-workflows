"""Agent Server entrypoint used by LangGraph Studio.

The Agent Server owns persistence in deployed environments. The local CLI keeps
its SQLite checkpointer for terminal-only runs; Studio uses this compiled graph
and the server's thread/checkpoint APIs instead.
"""
import os
from pathlib import Path

from .graph import build_graph


REPOSITORY_ROOT = Path(os.getenv("WORKFLOW_REPO", Path.cwd())).resolve()
graph = build_graph(REPOSITORY_ROOT).compile(name="jira_delivery")

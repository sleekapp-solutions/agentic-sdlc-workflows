"""LangGraph Studio/Agent Server entrypoint for the SDLC beta flow.

LangGraph Studio discovers the module-level compiled ``graph`` object. The
repository is selected by ``WORKFLOW_REPO`` (or the current directory), while
runtime persistence is supplied by the Agent Server rather than this module.
"""

import os
from pathlib import Path

from .sdlc_beta import build_sdlc_beta_graph

from .settings import load_workflow_environment


REPOSITORY_ROOT = Path(os.getenv("WORKFLOW_REPO", Path.cwd())).resolve()
load_workflow_environment(REPOSITORY_ROOT)
graph = build_sdlc_beta_graph(REPOSITORY_ROOT).compile(name="sdlc_beta")

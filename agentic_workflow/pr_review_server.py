"""LangGraph Studio/Agent Server entrypoint for read-only PR review.

LangGraph Studio discovers the module-level compiled ``graph`` object.  The
repository is selected by ``WORKFLOW_REPO`` (or the current directory), while
runtime persistence is supplied by the Agent Server rather than this module.
The underlying graph remains read-only with respect to GitHub and local code.
"""

import os
from pathlib import Path

from .pr_review_graph import build_pr_review_graph


REPOSITORY_ROOT = Path(os.getenv("WORKFLOW_REPO", Path.cwd())).resolve()
graph = build_pr_review_graph(REPOSITORY_ROOT).compile(name="pr_review")

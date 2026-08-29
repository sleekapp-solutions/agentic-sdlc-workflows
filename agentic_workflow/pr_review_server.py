"""Agent Server entrypoint for the local, read-only pull-request review flow."""

import os
from pathlib import Path

from .pr_review_graph import build_pr_review_graph


REPOSITORY_ROOT = Path(os.getenv("WORKFLOW_REPO", Path.cwd())).resolve()
graph = build_pr_review_graph(REPOSITORY_ROOT).compile(name="pr_review")

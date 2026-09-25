"""Command-line entrypoint for resumable local SDLC workflows.

This module is the non-browser route into the Jira delivery and pull-request
review graphs.  It selects an explicit target repository, stores local
LangGraph checkpoints in that repository, starts a run, and resumes approval
interrupts from JSON decisions.  It deliberately delegates all business logic
to the graph and adapter modules.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .jira_delivery import build_graph
from .pr_review_graph import build_pr_review_graph
from .project_profiles import resolve_validation
from .settings import load_workflow_environment


def _json_default(value: Any) -> str:
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-approved Jira delivery and pull-request review workflows")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Git repository to change")
    parser.add_argument("--database", type=Path, default=Path(".agentic-workflow/checkpoints.sqlite"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="Fetch and plan a Jira ticket")
    start.add_argument("ticket", help="Jira issue key, e.g. SCRUM-3")
    start.add_argument("--run-id", required=True, help="Stable ID used to resume this run")
    review_pr = subparsers.add_parser("review-pr", help="Review an existing GitHub pull request locally")
    review_pr.add_argument("number", type=int, help="GitHub pull-request number")
    review_pr.add_argument("--run-id", required=True, help="Stable ID used to resume this run")
    resume = subparsers.add_parser("resume", help="Resume a paused approval")
    resume.add_argument("--run-id", required=True, help="Run ID returned from start")
    resume.add_argument("--flow", choices=("jira_delivery", "pr_review"), default="jira_delivery", help="Workflow that owns the paused run")
    resume.add_argument("--decision", required=True, help="JSON, e.g. '{\"action\":\"approve\"}'")
    args = parser.parse_args()

    repo = args.repo.resolve()
    load_workflow_environment(repo)
    database = args.database if args.database.is_absolute() else repo / args.database
    database.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(database)) as checkpointer:
        config = {"configurable": {"thread_id": args.run_id}}
        if args.command == "start":
            try:
                resolve_validation(repo)
            except RuntimeError as error:
                parser.error(str(error))
            graph = build_graph(repo).compile(checkpointer=checkpointer)
            result = graph.invoke({"ticket_key": args.ticket}, config=config)
        elif args.command == "review-pr":
            graph = build_pr_review_graph(repo).compile(checkpointer=checkpointer)
            result = graph.invoke({"pr_number": args.number}, config=config)
        else:
            graph = (build_graph(repo) if args.flow == "jira_delivery" else build_pr_review_graph(repo)).compile(checkpointer=checkpointer)
            try:
                decision = json.loads(args.decision)
            except json.JSONDecodeError as error:
                parser.error(f"--decision must be valid JSON: {error.msg}")
            result = graph.invoke(Command(resume=decision), config=config)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())

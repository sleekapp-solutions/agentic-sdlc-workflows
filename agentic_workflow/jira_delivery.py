"""LangGraph definition for the human-approved Jira-to-draft-PR flow.

The graph turns a Jira issue into a structured plan, pauses for plan approval,
lets the selected local agent implement only the approved work, validates the
repository, and pauses again before any commit, push, or draft PR.  This module
defines the state transitions and approval boundaries; concrete Jira, agent,
Git, and validation integrations are injected or resolved through adapters.
"""

from pathlib import Path
from typing import Dict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .adapters import CommandImplementer, LocalMcpTicketClient, LocalPlanner
from .contracts import WorkflowState
from .git_ops import GitOperations, ProjectValidator


def _approval(value: object, phase: str) -> Dict[str, object]:
    if not isinstance(value, dict) or value.get("action") not in {"approve", "revise", "reject"}:
        raise ValueError(f"{phase} approval must be JSON: {{'action': 'approve'|'revise'|'reject', ...}}")
    return value


def build_graph(repo_root: Path, ticket_client=None, planner=None, implementer=None, git=None, validator=None, report=None):
    """Build the Jira delivery graph for one explicit target repository.

    Optional dependencies support deterministic tests and custom integrations.
    With no overrides, nodes lazily resolve the local MCP, agent, Git, and
    validator adapters only when that stage is reached.
    """
    # Keep external credentials out of module import/startup. Studio can then
    # render the graph before a developer configures a real Jira/LLM session.
    # Each dependency is resolved only in the node that needs it.
    git = git or GitOperations(repo_root)
    validator = validator or ProjectValidator(repo_root)

    def progress(stage: str, message: str) -> None:
        if report:
            report(stage, message)

    def fetch_ticket(state: WorkflowState):
        progress("fetching_ticket", "Fetching Jira ticket through the selected provider's Atlassian MCP connection")
        client = ticket_client or LocalMcpTicketClient(repo_root)
        ticket = client.fetch(state["ticket_key"])
        summary = ticket.get("summary") if isinstance(ticket, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError(
                "Atlassian returned the ticket key but no usable ticket title. "
                "Check the Jira issue summary and the Atlassian MCP connection, then retry."
            )
        return {"ticket": ticket, "status": "planning"}

    def make_plan(state: WorkflowState):
        progress("planning", "Creating the implementation plan")
        active_planner = planner or LocalPlanner(repo_root)
        return {"plan": active_planner.create(state["ticket"], state.get("plan_feedback")), "plan_feedback": ""}

    def review_plan(state: WorkflowState):
        progress("awaiting_plan_review", "Waiting for your plan decision")
        decision = _approval(interrupt({"phase": "plan_review", "ticket": state["ticket"], "plan": state["plan"]}), "plan")
        if decision["action"] == "approve":
            return Command(goto="prepare_workspace", update={"status": "plan_approved"})
        if decision["action"] == "revise":
            return Command(goto="make_plan", update={"plan_feedback": str(decision.get("feedback", ""))})
        return Command(goto=END, update={"status": "plan_rejected"})

    def prepare_workspace(state: WorkflowState):
        progress("preparing_workspace", "Creating an isolated delivery branch")
        return {"branch": git.create_branch(state["ticket_key"]), "status": "implementing"}

    def implement(state: WorkflowState):
        progress("implementing", "Coding agent is implementing the approved plan")
        active_implementer = implementer or CommandImplementer.from_environment(repo_root)
        return {"implementation": active_implementer.run(state["ticket"], state["plan"], state["branch"])}

    def validate(state: WorkflowState):
        progress("validating", "Running configured project validation")
        validation = validator.run()
        if not validation["passed"]:
            return {"validation": validation, "status": "validation_failed"}
        file_changes = git.changes_since(state.get("baseline", {}))
        if not file_changes:
            return Command(
                goto=END,
                update={
                    "validation": validation,
                    "file_changes": [],
                    "candidate_paths": [],
                    "status": "no_run_changes",
                },
            )
        return {
            "validation": validation,
            "file_changes": file_changes,
            "candidate_paths": [change["path"] for change in file_changes],
            "status": "ready_for_push_review",
        }

    def review_push(state: WorkflowState):
        if not state["validation"]["passed"]:
            return Command(goto=END, update={"status": "validation_failed"})
        progress("awaiting_push_review", "Waiting for approval to create the draft PR")
        decision = _approval(interrupt({
            "phase": "push_review",
            "ticket": state["ticket"],
            "branch": state["branch"],
            "validation": state["validation"],
            "candidate_paths": state["candidate_paths"],
        }), "push")
        if decision["action"] == "approve":
            paths = decision.get("paths", state["candidate_paths"])
            if not isinstance(paths, list):
                raise ValueError("Approved paths must be a list")
            return Command(goto="publish", update={"candidate_paths": paths})
        return Command(goto=END, update={"status": "push_rejected"})

    def publish(state: WorkflowState):
        progress("publishing", "Creating the approved draft pull request")
        sha = git.commit_and_push(state["ticket_key"], state["ticket"]["summary"], state["candidate_paths"])
        pull_request_url = git.create_draft_pull_request(
            state["ticket_key"], state["ticket"]["summary"], state["branch"]
        )
        return {"commit_sha": sha, "pull_request_url": pull_request_url, "status": "published"}

    def retry_request(state: WorkflowState):
        """Checkpoint marker used by the runtime before a stage-aware retry."""
        return {}

    def start_route(state: WorkflowState):
        return "retry_request" if state.get("retry_target") else "fetch_ticket"

    def retry_dispatch(state: WorkflowState):
        target = state.get("retry_target")
        allowed_targets = {"fetch_ticket", "make_plan", "implement", "validate", "publish"}
        if target not in allowed_targets:
            raise ValueError("This delivery run cannot be retried from its current state")
        progress("retrying", f"Retrying {str(target).replace('_', ' ')}")
        return Command(goto=target, update={"status": "retrying", "retry_target": ""})

    graph = StateGraph(WorkflowState)
    graph.add_node("fetch_ticket", fetch_ticket)
    graph.add_node("make_plan", make_plan)
    graph.add_node("review_plan", review_plan)
    graph.add_node("prepare_workspace", prepare_workspace)
    graph.add_node("implement", implement)
    graph.add_node("validate", validate)
    graph.add_node("review_push", review_push)
    graph.add_node("publish", publish)
    graph.add_node("retry_request", retry_request)
    graph.add_node("retry_dispatch", retry_dispatch)
    graph.add_conditional_edges(START, start_route, {"fetch_ticket": "fetch_ticket", "retry_request": "retry_request"})
    graph.add_edge("fetch_ticket", "make_plan")
    graph.add_edge("make_plan", "review_plan")
    graph.add_edge("prepare_workspace", "implement")
    graph.add_edge("implement", "validate")
    graph.add_edge("validate", "review_push")
    graph.add_edge("publish", END)
    graph.add_edge("retry_request", "retry_dispatch")
    return graph

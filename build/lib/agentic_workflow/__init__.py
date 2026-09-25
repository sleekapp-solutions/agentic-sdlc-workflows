"""Local, human-approved agentic SDLC workflows.

This package contains the repository-specific application built on LangGraph.
It exposes two flows: Jira delivery, which pauses before implementation and
publication, and pull-request review with an explicit publication gate. Provider credentials and MCP
configuration remain outside package state. Entrypoints load layered local
configuration (see :mod:`agentic_workflow.settings`) once the target
repository is known; importing the package reads no configuration.
"""

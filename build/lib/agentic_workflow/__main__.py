"""Make ``python -m agentic_workflow`` behave like the workflow CLI.

The implementation lives in :mod:`agentic_workflow.cli`; this small module is
only the Python module-entrypoint and intentionally contains no workflow logic.
"""

from .cli import main

raise SystemExit(main())

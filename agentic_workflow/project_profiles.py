"""Detect a target repository's validation commands from its project files.

Explicit ``WORKFLOW_VALIDATION_COMMANDS`` always wins. Otherwise the first
matching profile below supplies conservative analyze/test commands based on
marker files at the repository root. A repository with no recognised marker
must configure its commands explicitly, so a run never validates against a
toolchain the project does not use.
"""

import json
import os
import re
import shlex
from pathlib import Path
from typing import Dict, Optional, Tuple

Commands = Tuple[Tuple[str, ...], ...]

# npm's scaffolded placeholder test script always fails; treat it as absent.
_NPM_PLACEHOLDER_TEST = 'echo "Error: no test specified" && exit 1'


class ValidationNotConfiguredError(RuntimeError):
    def __init__(self, repo_root: Path) -> None:
        super().__init__(
            f"No validation commands are configured and no supported project type was detected in {repo_root}. "
            "Add WORKFLOW_VALIDATION_COMMANDS (semicolon-separated) to .agentic-workflow.env in the target repository."
        )


def _parse(configured: str) -> Commands:
    commands = tuple(tuple(shlex.split(command)) for command in configured.split(";") if command.strip())
    if not commands or any(not command for command in commands):
        raise RuntimeError("WORKFLOW_VALIDATION_COMMANDS must contain one or more shell-style commands")
    return commands


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _dart(repo: Path) -> Optional[Tuple[str, Commands]]:
    pubspec = _read_text(repo / "pubspec.yaml")
    if not pubspec:
        return None
    if re.search(r"^\s*sdk:\s*flutter\s*$", pubspec, re.MULTILINE):
        return "Flutter", (("flutter", "analyze"), ("flutter", "test"))
    return "Dart", (("dart", "analyze"), ("dart", "test"))


def _node(repo: Path) -> Optional[Tuple[str, Commands]]:
    if not (repo / "package.json").is_file():
        return None
    try:
        scripts = json.loads(_read_text(repo / "package.json")).get("scripts") or {}
    except (json.JSONDecodeError, AttributeError):
        scripts = {}
    if (repo / "pnpm-lock.yaml").is_file():
        manager = "pnpm"
    elif (repo / "yarn.lock").is_file():
        manager = "yarn"
    elif (repo / "bun.lock").is_file() or (repo / "bun.lockb").is_file():
        manager = "bun"
    else:
        manager = "npm"
    commands = tuple(
        (manager, "run", script)
        for script in ("lint", "typecheck", "test")
        if isinstance(scripts.get(script), str) and scripts[script].strip() != _NPM_PLACEHOLDER_TEST
    )
    # A package.json without runnable scripts gives no signal; require config.
    return (f"Node ({manager})", commands) if commands else None


def _python(repo: Path) -> Optional[Tuple[str, Commands]]:
    if not any((repo / marker).is_file() for marker in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        return None
    interpreter = next(
        (str(repo / venv / "bin" / "python") for venv in (".venv", "venv") if (repo / venv / "bin" / "python").is_file()),
        "python3",
    )
    return "Python", ((interpreter, "-m", "pytest"),)


def _simple(marker: str, label: str, commands: Commands):
    def detect(repo: Path) -> Optional[Tuple[str, Commands]]:
        return (label, commands) if (repo / marker).is_file() else None

    return detect


# Order matters: Flutter/Node repositories often also contain Gradle or Python
# files for tooling, so the application's primary toolchain is checked first.
_PROFILES = (
    _dart,
    _node,
    _simple("go.mod", "Go", (("go", "vet", "./..."), ("go", "test", "./..."))),
    _simple("Cargo.toml", "Rust", (("cargo", "test"),)),
    _simple("Package.swift", "Swift package", (("swift", "test"),)),
    _simple("gradlew", "Gradle", (("./gradlew", "test"),)),
    _simple("pom.xml", "Maven", (("mvn", "-q", "test"),)),
    _python,
)


def resolve_validation(repo_root: Path) -> Dict[str, object]:
    """Return ``{"source", "label", "commands"}`` for the target repository.

    Raises ``ValidationNotConfiguredError`` when nothing is configured and no
    profile matches.
    """
    configured = os.getenv("WORKFLOW_VALIDATION_COMMANDS", "").strip()
    if configured:
        return {"source": "configured", "label": "WORKFLOW_VALIDATION_COMMANDS", "commands": _parse(configured)}
    for detect in _PROFILES:
        match = detect(repo_root)
        if match:
            label, commands = match
            return {"source": "detected", "label": label, "commands": commands}
    raise ValidationNotConfiguredError(repo_root)

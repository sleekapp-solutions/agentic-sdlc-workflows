"""Find and remember target repositories for the local UI.

The UI can switch its target repository at runtime. This module resolves a
user-supplied path to its Git top level, keeps a short most-recent-first list
in the user's configuration directory, and scans the usual code folders for
Git checkouts to suggest. It only reads folder names and Git remotes.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from .settings import user_config_path

SEARCH_FOLDERS = ("Desktop", "Documents", "Developer", "Projects", "src", "code", "repos", "GitHub", "workspace")
SKIPPED_FOLDERS = {"node_modules", ".venv", "venv", "build", ".build", "Pods", "DerivedData", "Library", ".git"}
SCAN_DEPTH = 3
RECENT_LIMIT = 8

_scan_cache: Dict[str, object] = {"at": 0.0, "paths": []}


def recent_file() -> Path:
    return user_config_path().parent / "recent-repositories.json"


def git_top_level(path: Path) -> Optional[Path]:
    """The repository root containing ``path``, or None if it is not in a Git checkout."""
    if not path.is_dir():
        return None
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, text=True, capture_output=True, check=False)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def resolve_repository(raw: str) -> Path:
    path = Path(raw.strip()).expanduser()
    if not raw.strip() or not path.is_absolute():
        raise ValueError("Enter the absolute path of a Git repository, for example ~/src/my-app")
    root = git_top_level(path)
    if root is None:
        raise ValueError(f"{path} is not inside a Git repository")
    return root


def recent_repositories() -> List[Path]:
    try:
        stored = json.loads(recent_file().read_text())
    except (OSError, ValueError):
        return []
    return [Path(item) for item in stored if isinstance(item, str) and (Path(item) / ".git").exists()]


def remember_repository(repo: Path) -> None:
    paths = [str(repo)] + [str(path) for path in recent_repositories() if path != repo]
    target = recent_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(paths[:RECENT_LIMIT], indent=2))
    except OSError:
        pass  # Remembering is a convenience; never block a run on it.


def _scan(home: Path) -> List[Path]:
    found = []
    for folder in SEARCH_FOLDERS:
        base = home / folder
        if not base.is_dir():
            continue
        for current, directories, _ in os.walk(base):
            depth = len(Path(current).relative_to(base).parts)
            if ".git" in directories:
                found.append(Path(current))
                directories[:] = []  # Do not descend into a checkout.
                continue
            directories[:] = [] if depth >= SCAN_DEPTH else [d for d in directories if d not in SKIPPED_FOLDERS and not d.startswith(".")]
    return sorted(set(found), key=lambda path: path.name.lower())


def discovered_repositories(home: Optional[Path] = None, max_age: float = 60.0) -> List[Path]:
    if home is not None:
        return _scan(home)
    if time.monotonic() - float(_scan_cache["at"]) > max_age:
        _scan_cache.update(at=time.monotonic(), paths=_scan(Path.home()))
    return list(_scan_cache["paths"])  # type: ignore[arg-type]


def startup_repository(requested: Optional[Path], cwd: Path) -> Path:
    """``--repo`` if given, else the checkout containing cwd, else the last used repository."""
    if requested is not None:
        return requested.resolve()
    root = git_top_level(cwd)
    if root is not None:
        return root
    recent = recent_repositories()
    return recent[0] if recent else cwd.resolve()

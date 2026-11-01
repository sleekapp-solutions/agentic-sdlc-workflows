"""Structural blast radius for changed Swift production files.

Before review, the SDLC beta flow lists the types declared in each changed
Swift production file and finds the other Swift files that reference them.
This is a fast, dependency-free text scan (``git grep`` on whole words), not a
compiler-accurate graph: it tells the reviewer where to look, not what breaks.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

NON_PRODUCTION_DIRECTORIES = ("Tests/", "Test/", "UITests/", "Pods/", ".build/", "DerivedData/", "Carthage/")
NON_PRODUCTION_SUFFIXES = ("Tests", "Test", "Spec", "Mock", "Mocks", "Fake", "Stub")
DECLARATION = re.compile(
    r"^\s*(?:(?:public|open|internal|fileprivate|private|final|indirect|@\w+(?:\([^)]*\))?)\s+)*"
    r"(?:class|struct|enum|protocol|actor)\s+([A-Z]\w*)",
    re.M,
)


def is_swift_production_file(path: str) -> bool:
    return (
        path.endswith(".swift")
        and not any(f"/{directory}" in f"/{path}" for directory in NON_PRODUCTION_DIRECTORIES)
        and not Path(path).stem.endswith(NON_PRODUCTION_SUFFIXES)
    )


def _referencing_files(worktree: Path, symbol: str) -> List[str]:
    result = subprocess.run(
        ["git", "grep", "--untracked", "-l", "-w", symbol, "--", "*.swift"],
        cwd=worktree, text=True, capture_output=True, check=False,
    )
    # Exit code 1 means no matches; anything else is a real failure.
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "git grep failed")
    return result.stdout.split()


def assess_blast_radius(worktree: Path, changed_paths: List[str]) -> Dict[str, Any]:
    swift_files = sorted(path for path in changed_paths if is_swift_production_file(path))
    if not swift_files:
        return {"blast_radius": "not_applicable", "swift_files": [], "symbols": [], "summary": "No Swift production files changed."}

    symbols, dependents = [], set()
    for path in swift_files:
        source = worktree / path
        if not source.is_file():
            symbols.append({"file": path, "symbol": None, "dependents": [], "note": "File was deleted"})
            continue
        for name in sorted(set(DECLARATION.findall(source.read_text(errors="replace")))):
            users = sorted(user for user in _referencing_files(worktree, name) if user != path)
            dependents.update(users)
            symbols.append({"file": path, "symbol": name, "dependents": users})

    count = len(dependents)
    radius = "none" if count == 0 else "low" if count <= 5 else "medium" if count <= 20 else "high"
    return {
        "blast_radius": radius,
        "swift_files": swift_files,
        "symbols": symbols,
        "summary": f"{len(swift_files)} Swift production file(s) changed; {count} other Swift file(s) reference their types ({radius}).",
    }

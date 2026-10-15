"""Loads the target project's own agent instructions.

Any repo can describe its conventions in AGENTS.md (or CLAUDE.md, or
.github/copilot-instructions.md). The root file is inlined into the stable
system prompt; per-directory AGENTS.md files are only listed, so the agent
reads the one for a subsystem when it starts working there. This keeps the
always-sent prefix small and byte-stable across turns.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

ROOT_CANDIDATES = ("AGENTS.md", "CLAUDE.md", os.path.join(".github", "copilot-instructions.md"))
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".harness", "vendor"}


def _read_with_imports(path: str, root: str, seen: set) -> str:
    """Reads a file, expanding lines of the form '@relative/path.md' once each."""
    real = os.path.realpath(path)
    if real in seen or not real.startswith(os.path.realpath(root)):
        return ""
    seen.add(real)
    with open(real, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("@") and stripped.endswith(".md") and " " not in stripped:
            target = os.path.join(os.path.dirname(real), stripped[1:])
            if os.path.isfile(target):
                out.append(_read_with_imports(target, root, seen))
                continue
        out.append(line)
    return "\n".join(out).strip()


def find_root_instructions(workspace: str) -> Optional[Tuple[str, str]]:
    """Returns (relative path, content) of the first root instruction file found."""
    for name in ROOT_CANDIDATES:
        path = os.path.join(workspace, name)
        if os.path.isfile(path):
            return name, _read_with_imports(path, workspace, set())
    return None


def find_subdirectory_contracts(workspace: str, max_entries: int = 50) -> List[str]:
    """Lists relative paths of AGENTS.md files below the root."""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if os.path.samefile(dirpath, workspace):
            continue
        if "AGENTS.md" in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, "AGENTS.md"), workspace))
            if len(found) >= max_entries:
                break
    return found


def build_project_instructions(workspace: str, max_chars: int = 8000) -> str:
    """Builds the project-instructions block for the system prompt, or '' if none."""
    parts: List[str] = []
    root = find_root_instructions(workspace)
    if root:
        name, content = root
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [truncated; read {name} for the rest]"
        parts.append(f"[PROJECT INSTRUCTIONS from {name}. Follow them.]\n{content}")

    contracts = find_subdirectory_contracts(workspace)
    if contracts:
        listing = "\n".join(f"- {p}" for p in contracts)
        parts.append(
            "[SUBDIRECTORY CONTRACTS. Read the one covering a directory before editing files in it.]\n"
            + listing
        )
    return "\n\n".join(parts)


def hide_runtime_dir_from_git(workspace: str, entry: str = ".harness/") -> bool:
    """Adds the harness runtime dir to the repo's local .git/info/exclude.

    That file is per-clone and never committed, so the target project's
    .gitignore is left alone. Returns True if an entry was added.
    """
    git_dir = os.path.join(workspace, ".git")
    if not os.path.isdir(git_dir):
        return False  # not a git repo, or a worktree/submodule with a .git file
    exclude = os.path.join(git_dir, "info", "exclude")
    existing = ""
    if os.path.isfile(exclude):
        with open(exclude, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
        if entry in existing.splitlines():
            return False
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")
    return True

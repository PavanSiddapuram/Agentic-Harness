"""What a change touches: files, added lines, and before/after text.

Everything in planes/verify works on the change, not the whole repo: an agent
can act on findings in lines it just wrote, while a whole-repo scan of an
existing project buries it in old findings it cannot fix.

The change is the working tree (staged, unstaged and untracked files) compared
with a base ref, HEAD by default.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAX_TEXT_BYTES = 1024 * 1024
# Tool output that is never part of a reviewable change, even when untracked.
GENERATED_PREFIXES = ("graphify-out/", ".harness/", ".codebase-memory/", ".serena/cache/", ".code-review-graph/")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class NotAGitRepo(Exception):
    pass


@dataclass
class FileChange:
    path: str                      # repo-relative, forward slashes
    status: str                    # added | modified | deleted
    added: Dict[int, str] = field(default_factory=dict)   # new line number -> text
    removed_count: int = 0
    base_text: Optional[str] = None
    new_text: Optional[str] = None

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()


@dataclass
class ChangeSet:
    root: str
    base: str
    files: List[FileChange]

    @property
    def added_lines(self) -> int:
        return sum(len(f.added) for f in self.files)

    @property
    def removed_lines(self) -> int:
        return sum(f.removed_count for f in self.files)


def _git(root: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def _unquote(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return path


def _harnessignore(root: str) -> List[str]:
    path = os.path.join(root, ".harnessignore")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [l.strip().lstrip("/") for l in f if l.strip() and not l.lstrip().startswith(("#", "!"))]


def _skipped(path: str, patterns: List[str]) -> bool:
    if path.startswith(GENERATED_PREFIXES):
        return True
    for pat in patterns:
        bare = pat.rstrip("/")
        if fnmatch.fnmatch(path, bare) or fnmatch.fnmatch(path, bare + "/*") or fnmatch.fnmatch(os.path.basename(path), bare):
            return True
    return False


def collect_changes(root: str, base: str = "HEAD") -> ChangeSet:
    root = os.path.realpath(root)
    try:
        top = _git(root, "rev-parse", "--show-toplevel").strip()
    except (RuntimeError, OSError):
        raise NotAGitRepo(f"{root} is not inside a git repository")
    root = os.path.realpath(top)

    has_base = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=root, capture_output=True,
    ).returncode == 0
    if not has_base and base != "HEAD":
        raise RuntimeError(f"base ref '{base}' does not exist")

    files: Dict[str, FileChange] = {}
    if has_base:
        diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0", base, "--")
        current: Optional[FileChange] = None
        new_line = 0
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                current = None
            elif line.startswith("--- "):
                old_path = line[4:]
            elif line.startswith("+++ "):
                target = line[4:]
                if target == "/dev/null":
                    path = _unquote(old_path)[2:]
                    current = files.setdefault(path, FileChange(path, "deleted"))
                else:
                    path = _unquote(target)[2:]
                    status = "added" if old_path == "/dev/null" else "modified"
                    current = files.setdefault(path, FileChange(path, status))
            elif line.startswith("@@"):
                m = HUNK_RE.match(line)
                new_line = int(m.group(1)) if m else 0
            elif current is not None and line.startswith("+"):
                current.added[new_line] = line[1:]
                new_line += 1
            elif current is not None and line.startswith("-"):
                current.removed_count += 1

    # Untracked files are part of the change too; `git diff` does not list them.
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    if not has_base:  # a repo with no commits yet: everything is new
        untracked += _git(root, "ls-files", "--cached", "-z").split("\0")
    for path in filter(None, untracked):
        if path in files:
            continue
        text = _read_text(os.path.join(root, path))
        change = FileChange(path, "added", new_text=text)
        if text is not None:
            change.added = {i: t for i, t in enumerate(text.splitlines(), start=1)}
        files[path] = change

    for change in files.values():
        if change.status != "deleted" and change.new_text is None:
            change.new_text = _read_text(os.path.join(root, change.path))
        if change.status != "added" and has_base:
            shown = subprocess.run(
                ["git", "show", f"{base}:{change.path}"], cwd=root, capture_output=True,
            )
            if shown.returncode == 0 and len(shown.stdout) <= MAX_TEXT_BYTES and b"\x00" not in shown.stdout[:4096]:
                change.base_text = shown.stdout.decode("utf-8", errors="replace")

    patterns = _harnessignore(root)
    kept = [f for f in files.values() if not _skipped(f.path, patterns)]
    return ChangeSet(root=root, base=base, files=sorted(kept, key=lambda f: f.path))

"""Stdlib-only symbol index for any codebase.

Python files are parsed with `ast`, so their symbols are exact. Other
languages use declaration regexes: good for "where is X defined" and a map of
the repo, but heuristic, and references are text matches rather than
compiler-resolved. A precise per-language index (SCIP) can replace this later
behind the same interface.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".harness", "node_modules", "bower_components", "vendor",
    ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
    "dist", "build", "out", "target", ".next", ".nuxt", ".gradle", ".idea", ".vscode",
    "coverage", "htmlcov",
}
DEFAULT_IGNORE_FILES = [
    "*.min.js", "*.min.mjs", "*.min.cjs", "*.min.css", "*.bundle.js", "*.map",
    "*.generated.*", "*_pb2.py", "*.pb.go", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum",
]
IGNORE_FILE_NAMES = (".harnessignore", ".gitignore")
MAX_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class Symbol:
    name: str      # qualified for methods: Class.method
    kind: str      # class | function | method | type | const | module
    line: int


_N = r"(?P<name>[A-Za-z_$][\w$]*)"
LANG_PATTERNS: Dict[Tuple[str, ...], List[Tuple[re.Pattern, str]]] = {
    (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"): [
        (re.compile(rf"^\s*(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*{_N}"), "function"),
        (re.compile(rf"^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+{_N}"), "class"),
        (re.compile(rf"^\s*(export\s+)?(declare\s+)?(interface|type|enum)\s+{_N}"), "type"),
        (re.compile(rf"^(export\s+)?(const|let)\s+{_N}\s*(:[^=]+)?=\s*(async\s*)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "function"),
        (re.compile(rf"^export\s+(const|let|var)\s+{_N}"), "const"),
    ],
    (".go",): [
        (re.compile(rf"^func\s+(\([^)]*\)\s*)?{_N}"), "function"),
        (re.compile(rf"^type\s+{_N}"), "type"),
    ],
    (".rs",): [
        (re.compile(rf"^\s*(pub(\([^)]*\))?\s+)?(async\s+)?(unsafe\s+)?fn\s+{_N}"), "function"),
        (re.compile(rf"^\s*(pub(\([^)]*\))?\s+)?(struct|enum|trait|type|union)\s+{_N}"), "type"),
        (re.compile(rf"^\s*(pub(\([^)]*\))?\s+)?mod\s+{_N}"), "module"),
    ],
    (".java", ".kt", ".kts", ".scala", ".cs"): [
        (re.compile(rf"^\s*([\w@]+\s+)*(class|interface|enum|record|object|trait)\s+{_N}"), "class"),
        (re.compile(rf"^\s*(public|private|protected|internal|static|final|override|suspend|async|virtual|\s)+[\w<>\[\],.? ]+\s+{_N}\s*\([^;]*$"), "function"),
        (re.compile(rf"^\s*(private\s+|public\s+|internal\s+)?fun\s+(<[^>]+>\s*)?([\w.]+\.)?{_N}"), "function"),
    ],
    (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"): [
        (re.compile(rf"^\s*(class|struct|enum|union)\s+{_N}\s*[:{{]"), "type"),
        (re.compile(rf"^(?!\s*(if|for|while|switch|return|else)\b)[A-Za-z_][\w\s\*&:<>,]*[\s\*&]{_N}\s*\([^;]*\)\s*(const)?\s*\{{?\s*$"), "function"),
    ],
    (".rb",): [
        (re.compile(r"^\s*def\s+(self\.)?(?P<name>[\w?!=]+)"), "function"),
        (re.compile(r"^\s*(class|module)\s+(?P<name>[\w:]+)"), "class"),
    ],
    (".php",): [
        (re.compile(rf"^\s*(abstract\s+|final\s+)?(class|interface|trait|enum)\s+{_N}"), "class"),
        (re.compile(rf"^\s*(public\s+|private\s+|protected\s+|static\s+)*function\s+&?{_N}"), "function"),
    ],
    (".swift",): [
        (re.compile(rf"^\s*(public\s+|private\s+|open\s+|internal\s+|final\s+)*(class|struct|enum|protocol|extension|actor)\s+{_N}"), "class"),
        (re.compile(rf"^\s*(public\s+|private\s+|open\s+|internal\s+|static\s+|override\s+)*func\s+{_N}"), "function"),
    ],
}
EXT_TO_PATTERNS = {ext: pats for exts, pats in LANG_PATTERNS.items() for ext in exts}
INDEXED_EXTS = set(EXT_TO_PATTERNS) | {".py", ".pyi"}


def _python_symbols(source: str) -> List[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(Symbol(node.name, "function", node.lineno))
        elif isinstance(node, ast.ClassDef):
            out.append(Symbol(node.name, "class", node.lineno))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(Symbol(f"{node.name}.{item.name}", "method", item.lineno))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id.isupper():
                out.append(Symbol(node.targets[0].id, "const", node.lineno))
    return out


def _regex_symbols(source: str, ext: str) -> List[Symbol]:
    patterns = EXT_TO_PATTERNS.get(ext, [])
    out: List[Symbol] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, kind in patterns:
            m = pattern.match(line)
            if m:
                out.append(Symbol(m.group("name"), kind, lineno))
                break
    return out


def extract_symbols(path: str, source: str) -> List[Symbol]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py", ".pyi"):
        return _python_symbols(source)
    return _regex_symbols(source, ext)


def _load_ignore_patterns(root: str) -> List[str]:
    """Reads a simple subset of ignore syntax: globs, '/' suffix for dirs, '#' comments."""
    patterns: List[str] = []
    for name in IGNORE_FILE_NAMES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if line and not line.startswith(("#", "!")):
                        patterns.append(line.lstrip("/"))
    return patterns


def _is_ignored(rel: str, name: str, is_dir: bool, patterns: List[str]) -> bool:
    for pat in patterns:
        dir_only = pat.endswith("/")
        p = pat.rstrip("/")
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
    return False


def _read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\x00" in data[:1024]:
        return None
    return data.decode("utf-8", errors="replace")


class RepoIndex:
    """Walks a workspace and caches symbols per file, re-parsing only changed files."""

    def __init__(self, root: str, max_files: int = 20000):
        self.root = os.path.realpath(root)
        self.max_files = max_files
        self._cache: Dict[str, Tuple[float, List[Symbol]]] = {}
        self.last_walk_truncated = False

    def iter_files(self, scope: str = ".", indexed_only: bool = True) -> Iterator[str]:
        """Yields workspace-relative paths of candidate source files under scope."""
        patterns = _load_ignore_patterns(self.root) + DEFAULT_IGNORE_FILES
        start = os.path.realpath(os.path.join(self.root, scope))
        if os.path.commonpath([self.root, start]) != self.root or not os.path.exists(start):
            return
        if os.path.isfile(start):
            yield os.path.relpath(start, self.root)
            return
        count = 0
        self.last_walk_truncated = False
        for dirpath, dirnames, filenames in os.walk(start):
            rel_dir = os.path.relpath(dirpath, self.root)
            kept = []
            for d in sorted(dirnames):
                rel = os.path.normpath(os.path.join(rel_dir, d))
                if d in DEFAULT_IGNORE_DIRS or _is_ignored(rel, d, True, patterns):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for name in sorted(filenames):
                rel = os.path.normpath(os.path.join(rel_dir, name))
                if indexed_only and os.path.splitext(name)[1].lower() not in INDEXED_EXTS:
                    continue
                if _is_ignored(rel, name, False, patterns):
                    continue
                count += 1
                if count > self.max_files:
                    self.last_walk_truncated = True
                    return
                yield rel

    def symbols_for(self, rel: str) -> List[Symbol]:
        path = os.path.join(self.root, rel)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return []
        cached = self._cache.get(rel)
        if cached and cached[0] == mtime:
            return cached[1]
        source = _read_text(path)
        symbols = extract_symbols(rel, source) if source is not None else []
        self._cache[rel] = (mtime, symbols)
        return symbols

    def read_text(self, rel: str) -> Optional[str]:
        return _read_text(os.path.join(self.root, rel))

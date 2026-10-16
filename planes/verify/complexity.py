"""Cyclomatic complexity of the functions a change touches, before and after.

Only increases are reported (a ratchet): an existing project's legacy hotspots
are not the agent's problem, but making a function harder to test is.

Python is measured exactly with `ast` (McCabe: 1 + decision points). Other
languages use lizard when installed (20+ languages, MIT); otherwise a
brace-matching heuristic that counts branch keywords, marked as approximate.
"""

from __future__ import annotations

import ast
import csv
import io
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.external import find_tool, run_tool
from planes.context.index import extract_symbols
from planes.verify.diff import ChangeSet, FileChange

THRESHOLD = 10        # McCabe's classic limit
BIG_JUMP = 5          # an increase this large is reported even under the threshold


@dataclass(frozen=True)
class FunctionComplexity:
    name: str
    start: int
    end: int
    ccn: int


@dataclass(frozen=True)
class ComplexityChange:
    path: str
    function: str
    line: int
    before: Optional[int]   # None = new function
    after: int
    method: str             # ast | lizard | heuristic

    def render(self) -> str:
        approx = " (approx.)" if self.method == "heuristic" else ""
        if self.before is None:
            return f"{self.path}:{self.line} {self.function}: new function, complexity {self.after}{approx}"
        return f"{self.path}:{self.line} {self.function}: complexity {self.before} -> {self.after}{approx}"


# ------------------------------------------------------------------ python
class _McCabe(ast.NodeVisitor):
    def __init__(self):
        self.score = 1

    def generic_visit(self, node):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler)):
            self.score += 1
        elif isinstance(node, ast.BoolOp):
            self.score += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            self.score += 1 + len(node.ifs)
        elif hasattr(ast, "match_case") and isinstance(node, ast.match_case):
            self.score += 1
        super().generic_visit(node)

    def visit_FunctionDef(self, node):  # nested functions are measured on their own
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


def python_functions(source: str) -> List[FunctionComplexity]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[FunctionComplexity] = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                counter = _McCabe()
                for child in node.body:
                    counter.visit(child)
                name = f"{prefix}{node.name}"
                out.append(FunctionComplexity(name, node.lineno, getattr(node, "end_lineno", node.lineno), counter.score))
                walk(node.body, name + ".")
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.")

    walk(tree.body, "")
    return out


# --------------------------------------------------------------- heuristic
BRANCH_RE = re.compile(r"\b(?:if|for|while|case|catch|elif|elsif|rescue|when)\b|&&|\|\||\?(?![.?:])")
STRING_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")
LINE_COMMENT_RE = re.compile(r"//.*$|#.*$")


def heuristic_functions(path: str, source: str) -> List[FunctionComplexity]:
    lines = source.splitlines()
    out: List[FunctionComplexity] = []
    for sym in extract_symbols(path, source):
        if sym.kind not in ("function", "method"):
            continue
        depth, started, end = 0, False, None
        body: List[str] = []
        for i in range(sym.line - 1, min(len(lines), sym.line + 2000)):
            code = LINE_COMMENT_RE.sub("", STRING_RE.sub('""', lines[i]))
            body.append(code)
            for ch in code:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
            if started and depth <= 0:
                end = i + 1
                break
        if end is None:
            continue
        ccn = 1 + sum(len(BRANCH_RE.findall(c)) for c in body)
        out.append(FunctionComplexity(sym.name, sym.line, end, ccn))
    return out


# ------------------------------------------------------------------ lizard
def lizard_functions(path: str, source: str, binary: str) -> Optional[List[FunctionComplexity]]:
    suffix = os.path.splitext(path)[1] or ".txt"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(source)
        name = tmp.name
    try:
        code, out, _ = run_tool([binary, "--csv", name], timeout=60)
    finally:
        os.unlink(name)
    if code not in (0, 1):
        return None
    result = []
    # columns: NLOC, CCN, token, PARAM, length, location, file, function, long_name, start, end
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 11 and row[1].isdigit():
            result.append(FunctionComplexity(row[7], int(row[9]), int(row[10]), int(row[1])))
    return result


def measure(path: str, source: Optional[str], lizard: Optional[str]) -> Tuple[List[FunctionComplexity], str]:
    if not source:
        return [], "none"
    if path.endswith((".py", ".pyi")):
        return python_functions(source), "ast"
    if lizard:
        measured = lizard_functions(path, source, lizard)
        if measured is not None:
            return measured, "lizard"
    return heuristic_functions(path, source), "heuristic"


CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts",
    ".scala", ".cs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".rb", ".php", ".swift",
}


def analyze(changes: ChangeSet, use_external: bool = True, threshold: int = THRESHOLD) -> Tuple[List[ComplexityChange], str]:
    """Returns (functions whose complexity rose past the bar, method summary)."""
    lizard = find_tool("lizard") if use_external else None
    results: List[ComplexityChange] = []
    methods = set()
    for change in changes.files:
        if change.status == "deleted" or change.ext not in CODE_EXTS or not change.added:
            continue
        after, method = measure(change.path, change.new_text, lizard)
        before, _ = measure(change.path, change.base_text, lizard)
        methods.add(method)
        before_by_name: Dict[str, int] = {}
        for f in before:
            before_by_name.setdefault(f.name, f.ccn)
        for f in after:
            touched = any(f.start <= line <= f.end for line in change.added)
            if not touched:
                continue
            old = before_by_name.get(f.name)
            if old is None:
                if f.ccn > threshold:
                    results.append(ComplexityChange(change.path, f.name, f.start, None, f.ccn, method))
            elif f.ccn > old and (f.ccn > threshold or f.ccn - old >= BIG_JUMP):
                results.append(ComplexityChange(change.path, f.name, f.start, old, f.ccn, method))
    results.sort(key=lambda c: -(c.after - (c.before or 0)))
    return results, ", ".join(sorted(methods - {"none"})) or "no code files"

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
    method: str = ""


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


def measure(path: str, source: Optional[str], lizard: Optional[str]) -> List[FunctionComplexity]:
    """Functions with complexity, each tagged with how it was measured."""
    if not source:
        return []
    if path.endswith((".py", ".pyi")):
        return [FunctionComplexity(f.name, f.start, f.end, f.ccn, "ast") for f in python_functions(source)]
    heuristic = [FunctionComplexity(f.name, f.start, f.end, f.ccn, "heuristic") for f in heuristic_functions(path, source)]
    if not lizard:
        return heuristic
    from_lizard = lizard_functions(path, source, lizard) or []
    # lizard misparses some real code (TypeScript overloads made it skip or garble
    # every later function in a file), so trust it only for functions it found
    # where our own symbol scan found them too; otherwise keep the heuristic.
    merged = []
    for f in heuristic:
        match = next((l for l in from_lizard if l.name == f.name and abs(l.start - f.start) <= 2), None)
        merged.append(FunctionComplexity(f.name, f.start, match.end if match else f.end,
                                         match.ccn if match else f.ccn, "lizard" if match else "heuristic"))
    known = {(f.name, f.start) for f in merged}
    for l in from_lizard:  # functions our patterns do not recognise (other languages)
        if l.name != "(anonymous)" and not any(l.name == n and abs(l.start - st) <= 2 for n, st in known):
            merged.append(FunctionComplexity(l.name, l.start, l.end, l.ccn, "lizard"))
    return merged


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
        after = measure(change.path, change.new_text, lizard)
        before = measure(change.path, change.base_text, lizard)
        before_by_name: Dict[str, int] = {}
        for f in before:
            before_by_name.setdefault(f.name, f.ccn)
        for f in after:
            touched = any(f.start <= line <= f.end for line in change.added)
            if not touched:
                continue
            methods.add(f.method)
            old = before_by_name.get(f.name)
            if old is None:
                if f.ccn > threshold:
                    results.append(ComplexityChange(change.path, f.name, f.start, None, f.ccn, f.method))
            elif f.ccn > old and (f.ccn > threshold or f.ccn - old >= BIG_JUMP):
                results.append(ComplexityChange(change.path, f.name, f.start, old, f.ccn, f.method))
    results.sort(key=lambda c: -(c.after - (c.before or 0)))
    return results, ", ".join(sorted(methods)) or "no changed functions"

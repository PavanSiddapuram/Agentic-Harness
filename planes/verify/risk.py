"""How closely a human should look at a change, and why.

A deterministic score from named factors, so every point is explainable and
the same change always scores the same. It is a triage signal for reviewers,
not a verdict on correctness: a low score means nothing alarming was detected,
not that the change is right.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from planes.context.index import extract_symbols
from planes.verify.complexity import ComplexityChange
from planes.verify.diff import ChangeSet
from planes.verify.security import Finding

SENSITIVE_AREAS = [
    ("auth and sessions", re.compile(r"(?i)(^|/)(auth|login|logout|session|oauth|sso|jwt|token|password|permission|rbac|acl)[^/]*($|/)")),
    ("payments and billing", re.compile(r"(?i)(^|/)(payment|billing|checkout|invoice|stripe|refund|subscription)[^/]*($|/)")),
    ("crypto and secrets", re.compile(r"(?i)(^|/)(crypto|encrypt|secret|keys?|cert)[^/]*($|/)")),
    ("database schema and migrations", re.compile(r"(?i)(^|/)(migrations?|schema|alembic|flyway|liquibase)[^/]*($|/)|\.sql$")),
    ("deploy, infra and CI", re.compile(r"(?i)(^|/)(deploy|infra|terraform|helm|k8s|kubernetes|\.github/workflows|\.gitlab-ci|ci)[^/]*($|/)|dockerfile|docker-compose|\.tf$")),
]
TEST_PATH_RE = re.compile(r"(?i)(^|/)(tests?|__tests__|spec|specs)/|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+$")
DOC_EXTS = (".md", ".rst", ".txt", ".adoc")
MANIFESTS = {
    "package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml", "build.gradle", "build.gradle.kts",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "uv.lock",
    "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock",
}


@dataclass
class RiskFactor:
    points: float
    reason: str


@dataclass
class RiskAssessment:
    level: str
    score: float
    factors: List[RiskFactor] = field(default_factory=list)

    def render(self) -> List[str]:
        lines = [f"Risk: {self.level.upper()} (score {self.score:g})"]
        for f in sorted(self.factors, key=lambda f: -f.points):
            lines.append(f"  +{f.points:g}  {f.reason}")
        return lines


def _dependency_changes(changes: ChangeSet) -> Tuple[List[str], List[str]]:
    """(new packages, packages whose version changed) in manifests, best effort."""
    found: List[str] = []
    bumped: List[str] = []
    for change in changes.files:
        name = change.path.rsplit("/", 1)[-1]
        if name not in MANIFESTS:
            continue
        for text in change.added.values():
            t = text.strip()
            m = None
            if name == "package.json":
                m = re.match(r"\"(@?[\w./-]+)\"\s*:\s*\"[\^~><=*\d]", t)
            elif name.startswith("requirements") or name == "Pipfile":
                m = re.match(r"([A-Za-z0-9][\w.-]*)\s*(?:[=<>!~\[;]|$)", t) if not t.startswith(("#", "-")) else None
            elif name in ("pyproject.toml", "setup.py", "setup.cfg"):
                m = re.match(r"[\"']([A-Za-z0-9][\w.-]*)\s*(?:[=<>!~\[;]|[\"'])", t)
            elif name == "go.mod":
                m = re.match(r"(?:require\s+)?([\w.-]+\.[a-z]+/[\w./-]+)\s+v", t)
            elif name == "Cargo.toml":
                m = re.match(r"([A-Za-z0-9_-]+)\s*=\s*[\"{]", t)
            elif name == "Gemfile":
                m = re.match(r"gem\s+[\"']([\w-]+)", t)
            if m and m.group(1) not in ("name", "version", "description", "main", "scripts"):
                pkg = m.group(1)
                # Present in the old manifest too: a version change, not a new package.
                if change.base_text and re.search(r"(?<![\w@./-])" + re.escape(pkg) + r"(?![\w./-])", change.base_text):
                    bumped.append(pkg)
                else:
                    found.append(pkg)
    return sorted(set(found)), sorted(set(bumped) - set(found))


def _removed_public_symbols(changes: ChangeSet) -> List[str]:
    removed: List[str] = []
    for change in changes.files:
        if change.status == "added" or not change.base_text:
            continue
        before = {s.name for s in extract_symbols(change.path, change.base_text)
                  if s.kind in ("function", "class", "method", "type") and not s.name.split(".")[-1].startswith("_")}
        after = {s.name for s in extract_symbols(change.path, change.new_text or "")} if change.new_text else set()
        removed += [f"{change.path}:{name}" for name in sorted(before - after)]
    return removed


def assess(changes: ChangeSet, findings: List[Finding], complexity: List[ComplexityChange],
           impacted_callers: Optional[int] = None) -> RiskAssessment:
    factors: List[RiskFactor] = []
    code_files = [f for f in changes.files if not f.path.endswith(DOC_EXTS)]
    if not code_files:
        return RiskAssessment("low", 0, [RiskFactor(0, "documentation-only change")])

    size = changes.added_lines + changes.removed_lines
    if size > 400:
        factors.append(RiskFactor(3, f"large change: {size} lines"))
    elif size > 150:
        factors.append(RiskFactor(2, f"sizeable change: {size} lines"))
    elif size > 50:
        factors.append(RiskFactor(1, f"{size} lines changed"))
    if len(code_files) > 15:
        factors.append(RiskFactor(2, f"{len(code_files)} files touched"))
    elif len(code_files) > 5:
        factors.append(RiskFactor(1, f"{len(code_files)} files touched"))

    area_points = 0
    for area, regex in SENSITIVE_AREAS:
        hits = [f.path for f in code_files if regex.search(f.path)]
        if hits and area_points < 4:
            factors.append(RiskFactor(2, f"touches {area}: {', '.join(hits[:3])}"))
            area_points += 2

    high = [f for f in findings if f.severity == "high"]
    medium = [f for f in findings if f.severity == "medium"]
    if high:
        factors.append(RiskFactor(min(8, 4 * len(high)), f"{len(high)} high-severity security finding(s)"))
    if medium:
        factors.append(RiskFactor(min(4, 2 * len(medium)), f"{len(medium)} medium-severity security finding(s)"))

    if complexity:
        names = ", ".join(c.function for c in complexity[:3])
        factors.append(RiskFactor(min(3, len(complexity)), f"complexity rose in {len(complexity)} function(s): {names}"))

    deps, bumps = _dependency_changes(changes)
    lock_only = not deps and not bumps and any(f.path.rsplit("/", 1)[-1] in LOCKFILES for f in changes.files)
    if deps:
        factors.append(RiskFactor(2, f"new dependencies: {', '.join(deps[:6])} (confirm each exists and is the intended package)"))
    if bumps:
        factors.append(RiskFactor(1, f"dependency versions changed: {', '.join(bumps[:6])}"))
    if lock_only:
        factors.append(RiskFactor(1, "lockfile changed without a manifest change"))

    source = [f for f in code_files if not TEST_PATH_RE.search(f.path) and f.path.rsplit("/", 1)[-1] not in MANIFESTS | LOCKFILES]
    tests = [f for f in code_files if TEST_PATH_RE.search(f.path)]
    if source and not tests:
        factors.append(RiskFactor(2, "source changed but no tests changed"))

    removed = _removed_public_symbols(changes)
    if removed:
        factors.append(RiskFactor(2, f"removed or renamed public symbols: {', '.join(removed[:4])} (callers may break)"))

    if impacted_callers is not None:
        if impacted_callers > 20:
            factors.append(RiskFactor(2, f"{impacted_callers} callers depend on the changed code"))
        elif impacted_callers > 5:
            factors.append(RiskFactor(1, f"{impacted_callers} callers depend on the changed code"))

    score = sum(f.points for f in factors)
    level = "high" if score >= 8 else "medium" if score >= 4 else "low"
    if high:
        level = "high"  # one committed secret or injection is reason enough to look closely
    return RiskAssessment(level, score, factors)

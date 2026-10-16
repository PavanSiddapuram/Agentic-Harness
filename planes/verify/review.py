"""Review a change: security findings, complexity increases, and a risk level.

    python -m planes.verify.review                     # working tree vs HEAD
    python -m planes.verify.review --base origin/main  # a branch vs main
    python -m planes.verify.review --json --fail-on high

Exit code is 0 unless --fail-on is set and the risk level reaches it, so the
same command works in CI, a pre-commit hook, or a Claude Code hook.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.models import ToolExecutionResult
from core.tools.base import BaseTool
from planes.verify import complexity as complexity_mod
from planes.verify import security
from planes.verify.diff import ChangeSet, NotAGitRepo, collect_changes
from planes.verify.risk import RiskAssessment, assess

LEVELS = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Report:
    base: str
    files: int
    added: int
    removed: int
    risk: RiskAssessment
    findings: List[security.Finding] = field(default_factory=list)
    scanners: List[str] = field(default_factory=list)
    complexity: List[complexity_mod.ComplexityChange] = field(default_factory=list)
    complexity_method: str = ""
    impacted_callers: Optional[int] = None

    def render(self, max_findings: int = 25) -> str:
        if not self.files:
            return f"No changes against {self.base}."
        out = [f"Change review vs {self.base}: {self.files} files, +{self.added} -{self.removed} lines"]
        out += self.risk.render()
        out.append(f"\nSecurity: {len(self.findings)} finding(s) in added lines (scanners: {', '.join(self.scanners)})")
        out += [f"  {f.render()}" for f in self.findings[:max_findings]]
        if len(self.findings) > max_findings:
            out.append(f"  [{len(self.findings) - max_findings} more not shown]")
        out.append(f"\nComplexity ({self.complexity_method}): "
                   + (f"{len(self.complexity)} function(s) got harder to test" if self.complexity else "no increases past the bar"))
        out += [f"  {c.render()}" for c in self.complexity]
        out.append("\nNext: " + self.next_step())
        return "\n".join(out)

    def next_step(self) -> str:
        if any(f.severity == "high" for f in self.findings):
            return "fix the high-severity findings, then run review_change again."
        if self.risk.level == "high":
            return "address the top risk factors, or say clearly in your summary why each is acceptable."
        if self.complexity:
            return "consider splitting the functions listed under Complexity, and make sure tests cover their branches."
        return "nothing blocking found; still run the tests that cover the changed code."

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base": self.base, "files": self.files, "added": self.added, "removed": self.removed,
            "risk": {"level": self.risk.level, "score": self.risk.score,
                     "factors": [asdict(f) for f in self.risk.factors]},
            "findings": [asdict(f) for f in self.findings],
            "scanners": self.scanners,
            "complexity": [asdict(c) for c in self.complexity],
            "complexity_method": self.complexity_method,
            "impacted_callers": self.impacted_callers,
        }


def _impacted_callers(graph) -> Optional[int]:
    if graph is None or not getattr(graph, "available", False):
        return None
    try:
        graph.ensure_indexed(force=True)
        out = graph.query("detect_changes", {"base_branch": "HEAD", "depth": 2, "scope": "impact", "max_output_tokens": 600})
    except RuntimeError:
        return None
    m = re.search(r"impacted_total:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def run_review(root: str, base: str = "HEAD", use_external: bool = True, graph=None) -> Report:
    changes: ChangeSet = collect_changes(root, base)
    findings, scanners = security.scan(changes, use_external=use_external)
    increases, method = complexity_mod.analyze(changes, use_external=use_external)
    impacted = _impacted_callers(graph) if base == "HEAD" else None
    risk = assess(changes, findings, increases, impacted)
    return Report(base, len(changes.files), changes.added_lines, changes.removed_lines, risk,
                  findings, scanners, increases, method, impacted)


class ReviewChangeTool(BaseTool):
    name = "review_change"
    description = (
        "Review your change before finishing: security findings in the lines you added (secrets, "
        "injection, unsafe eval, disabled TLS, ...), functions whose complexity rose, and a risk "
        "level with reasons. Run it after editing and before your final answer; fix high-severity "
        "findings."
    )

    def __init__(self, root: str, graph=None):
        self.root = root
        self.graph = graph

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "Git ref to compare against (default HEAD: your uncommitted changes)"},
                    },
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        base = (parameters.get("base") or "HEAD").strip()
        try:
            report = run_review(self.root, base, graph=self.graph)
        except NotAGitRepo as e:
            return ToolExecutionResult(call_id="", success=False, output=f"REVIEW_UNAVAILABLE: {e}")
        except RuntimeError as e:
            return ToolExecutionResult(call_id="", success=False, output=f"REVIEW_FAILED: {e}")
        return ToolExecutionResult(call_id="", success=True, output=report.render())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-on", choices=["low", "medium", "high", "never"], default="never",
                        help="exit 1 when the risk level reaches this")
    parser.add_argument("--no-external", action="store_true", help="builtin analysis only")
    args = parser.parse_args(argv)
    try:
        report = run_review(args.workspace, args.base, use_external=not args.no_external)
    except (NotAGitRepo, RuntimeError) as e:
        print(f"review failed: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render(max_findings=100))
    if args.fail_on != "never" and report.files and LEVELS[report.risk.level] >= LEVELS[args.fail_on]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Call-graph tools backed by codebase-memory-mcp, when it is installed.

codebase-memory-mcp (DeusData, MIT) is a single static binary that indexes a
repo with tree-sitter for 158 languages, refines call edges with language-server
type information for about 10 of them, and answers graph queries in
milliseconds. The harness calls its CLI mode, so no MCP client is needed.

These tools are registered only when the binary is on PATH. Without it the
harness still has map_repository and find_symbol from the stdlib index.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from core.external import find_tool, run_tool
from core.models import ToolExecutionResult
from core.tools.base import BaseTool

BINARY = "codebase-memory-mcp"


class CodeGraph:
    """Thin client for `codebase-memory-mcp cli`. Indexes lazily, once per session."""

    def __init__(self, root: str, binary: Optional[str] = None, index_timeout: int = 900):
        self.root = os.path.realpath(root)
        self.binary = binary or find_tool(BINARY)
        self.index_timeout = index_timeout
        self.project: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.binary)

    def ensure_indexed(self, force: bool = False) -> str:
        """Indexes the workspace (incremental on later calls) and returns the project name."""
        if self.project and not force:
            return self.project
        code, out, err = run_tool(
            [self.binary, "cli", "--quiet", "index_repository", json.dumps({"repo_path": self.root})],
            cwd=self.root,
            timeout=self.index_timeout,
        )
        if code != 0:
            raise RuntimeError(f"indexing failed: {(err or out).strip()[:300]}")
        try:
            self.project = json.loads(out)["project"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"unexpected index output: {out.strip()[:300]}")
        return self.project

    def query(self, tool: str, flags: Dict[str, Any], timeout: int = 120) -> str:
        argv: List[str] = [self.binary, "cli", "--quiet", tool, "--project", self.ensure_indexed()]
        for key, value in flags.items():
            if value is None:
                continue
            argv += [f"--{key.replace('_', '-')}", str(value).lower() if isinstance(value, bool) else str(value)]
        code, out, err = run_tool(argv, cwd=self.root, timeout=timeout)
        if code != 0:
            raise RuntimeError((err or out).strip()[:500] or f"{tool} exited {code}")
        # Project names embed the absolute path; strip it so output stays short.
        return out.replace(f"{self.project}.", "").replace(self.project, "<repo>").strip()


def _result(fn) -> ToolExecutionResult:
    try:
        return ToolExecutionResult(call_id="", success=True, output=fn())
    except RuntimeError as e:
        return ToolExecutionResult(
            call_id="", success=False,
            output=f"CODE_GRAPH_ERROR: {e}\nFall back to find_symbol or map_repository.",
        )


class TraceCallsTool(BaseTool):
    name = "trace_calls"
    description = (
        "Trace the call graph of a function: who calls it (inbound), what it calls (outbound), "
        "or both, several hops deep. Edges are parsed and type-resolved, not text matches, so "
        "use this over find_symbol to judge the blast radius of changing a function."
    )

    def __init__(self, graph: CodeGraph):
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
                        "function_name": {"type": "string", "description": "Function or method name"},
                        "direction": {"type": "string", "enum": ["inbound", "outbound", "both"], "description": "Default inbound (callers)"},
                        "depth": {"type": "integer", "description": "Hops to follow (default 2, max 5)"},
                    },
                    "required": ["function_name"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        name = (parameters.get("function_name") or "").strip()
        if not name:
            return ToolExecutionResult(call_id="", success=False, output="trace_calls needs 'function_name'.")
        direction = parameters.get("direction") or "inbound"
        if direction not in ("inbound", "outbound", "both"):
            direction = "inbound"
        depth = max(1, min(int(parameters.get("depth") or 2), 5))
        return _result(lambda: self.graph.query(
            "trace_path",
            {"function_name": name, "direction": direction, "depth": depth, "max_output_tokens": 1500},
        ))


class ChangeImpactTool(BaseTool):
    name = "change_impact"
    description = (
        "Show what your uncommitted changes affect: the changed files, the symbols in them, and "
        "every caller that depends on those symbols. Run it after editing and before finishing, "
        "then run the tests that cover the impacted code."
    )

    def __init__(self, graph: CodeGraph):
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
                        "base": {"type": "string", "description": "Compare against this git ref (default HEAD: uncommitted changes)"},
                        "depth": {"type": "integer", "description": "Caller hops to follow (default 2, max 4)"},
                    },
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        base = (parameters.get("base") or "HEAD").strip()
        depth = max(1, min(int(parameters.get("depth") or 2), 4))

        def run() -> str:
            self.graph.ensure_indexed(force=True)  # pick up the edits just made
            return self.graph.query(
                "detect_changes",
                {"base_branch": base, "depth": depth, "scope": "impact", "max_output_tokens": 1500},
            )

        return _result(run)


def graph_tools(root: str) -> List[BaseTool]:
    """Returns the graph tools if codebase-memory-mcp is installed, else an empty list."""
    graph = CodeGraph(root)
    if not graph.available:
        return []
    return [TraceCallsTool(graph), ChangeImpactTool(graph)]

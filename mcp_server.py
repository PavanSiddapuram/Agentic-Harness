#!/usr/bin/env python3
"""The harness as an MCP server, for whichever agent you already use.

    python mcp_server.py [--workspace PATH]

Claude Code, Codex, Gemini CLI, Cursor, Kimi Code and VS Code Copilot all
load local stdio MCP servers. This exposes only what those agents lack on a
large codebase (precise context, change review, performance checks) and none of
what they already do well (reading, editing, running commands), so the harness
stays a thin layer instead of one more agent. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core.permissions import GateConfigError, PermissionGate, SecurityException  # noqa: E402
from core.tools.base import BaseTool  # noqa: E402
from planes.context.graph import CodeGraph, graph_tools  # noqa: E402
from planes.context.index import RepoIndex  # noqa: E402
from planes.context.tools import FindSymbolTool, MapRepositoryTool  # noqa: E402
from planes.verify.perf import PerfCheckTool  # noqa: E402
from planes.verify.review import ReviewChangeTool  # noqa: E402

SERVER_NAME = "agentic-harness"
SERVER_VERSION = "0.2.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Codebase tools for large repositories. Use map_repository and find_symbol (and trace_calls / "
    "change_impact when listed) to locate code before reading files. Before finishing a change, run "
    "review_change and fix high-severity findings. For performance work, run perf_check with "
    "mode='baseline' before editing and mode='compare' after."
)

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
RUNS_COMMANDS = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


class GatedPerfCheck(PerfCheckTool):
    """perf_check runs a shell command repeatedly, so it passes the same owner
    gates and denylist as any harness command, whatever the host's own policy."""

    def __init__(self, root: str):
        super().__init__(root)
        try:
            self.gate: Optional[PermissionGate] = PermissionGate(root, input_func=lambda _: "n")
            self.gate_error: Optional[str] = None
        except GateConfigError as e:
            self.gate, self.gate_error = None, str(e)

    def execute(self, parameters: Dict[str, Any]):
        from core.models import ToolExecutionResult
        if self.gate is None:  # fail closed on an unreadable gate file
            return ToolExecutionResult(call_id="", success=False, output=f"REFUSED: {self.gate_error}")
        try:
            self.gate.check_command(str(parameters.get("command", "")))
        except SecurityException as e:
            return ToolExecutionResult(call_id="", success=False, output=f"BLOCKED: {e}")
        return super().execute(parameters)


def build_tools(root: str) -> List[BaseTool]:
    index = RepoIndex(root)
    graph = CodeGraph(root)
    graph.warm_up()
    tools: List[BaseTool] = [MapRepositoryTool(index), FindSymbolTool(index)]
    tools += graph_tools(root, graph)
    tools += [ReviewChangeTool(root, graph if graph.available else None), GatedPerfCheck(root)]
    return tools


def tool_listing(tool: BaseTool) -> Dict[str, Any]:
    fn = tool.get_schema()["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        "annotations": RUNS_COMMANDS if tool.name == "perf_check" else READ_ONLY,
    }


class Server:
    def __init__(self, root: str):
        self.root = root
        self.tools = {t.name: t for t in build_tools(root)}

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # a notification: never answered
            return None
        try:
            result = self.dispatch(method, msg.get("params") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {e}"}}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(e)}}

    def dispatch(self, method: Optional[str], params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool_listing(t) for t in self.tools.values()]}
        if method == "tools/call":
            tool = self.tools.get(params.get("name", ""))
            if tool is None:
                raise ValueError(f"unknown tool {params.get('name')!r}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            try:
                result = tool.execute(arguments)
                text, failed = result.output, not result.success
            except Exception as e:  # a tool bug must not kill the server
                text, failed = f"INTERNAL_ERROR in {tool.name}: {type(e).__name__}: {e}", True
                traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": text}], "isError": failed}
        raise KeyError(method)


def resolve_root(explicit: Optional[str] = None) -> str:
    """The repository to serve: an explicit path, else what the host says, else the cwd.

    Hosts start stdio servers from the project directory (Claude Code also sets
    CLAUDE_PROJECT_DIR); a session started in a subdirectory still gets the whole
    repo because the path is widened to the git top level.
    """
    start = (explicit or os.environ.get("HARNESS_WORKSPACE") or os.environ.get("CLAUDE_PROJECT_DIR")
             or os.environ.get("GEMINI_PROJECT_DIR") or os.getcwd())
    start = os.path.realpath(start)
    try:
        import subprocess
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                             capture_output=True, text=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            return os.path.realpath(top.stdout.strip())
    except (OSError, ValueError):
        pass
    return start


def serve(root: str, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    # stdout is the protocol channel: one stray print() from tool code would
    # corrupt it, so everything else written to stdout goes to stderr instead.
    if stdout is sys.stdout:
        sys.stdout = sys.stderr
    server = Server(root)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=None,
                        help="repository root (default: the host's project directory, widened to the git root)")
    args = parser.parse_args(argv)
    serve(resolve_root(args.workspace))
    return 0


if __name__ == "__main__":
    sys.exit(main())

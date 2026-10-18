"""Tests for mcp_server.py, the harness as an MCP server for any host agent."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mcp_server
from core.tools.base import BaseTool
from core.models import ToolExecutionResult

MAX_TOOLS = 6  # each extra tool costs every host prompt space and selection accuracy


def rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.ws], check=True)
        with open(os.path.join(self.ws, "app.py"), "w") as f:
            f.write("def charge(amount):\n    return amount\n")
        self.env = mock.patch.dict(os.environ, {"HARNESS_NO_EXTERNAL": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def exchange(self, *lines):
        out = io.StringIO()
        mcp_server.serve(self.ws, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        return [json.loads(line) for line in out.getvalue().splitlines()]


class TestProtocol(ServerCase):
    def test_initialize_negotiates_version_and_gives_instructions(self):
        [reply] = self.exchange(rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}))
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "agentic-harness")
        self.assertIn("review_change", result["instructions"])
        [reply] = self.exchange(rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(reply["result"]["protocolVersion"], mcp_server.SUPPORTED_PROTOCOLS[0])

    def test_notifications_are_not_answered_and_ping_is(self):
        replies = self.exchange(rpc("notifications/initialized", msg_id=None), rpc("ping", msg_id=7))
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 7, "result": {}}])

    def test_errors(self):
        replies = self.exchange("{not json", rpc("no/such/method"), rpc("tools/call", {"name": "nope"}),
                                rpc("tools/call", {"name": "find_symbol", "arguments": "x"}))
        self.assertEqual([r["error"]["code"] for r in replies], [-32700, -32601, -32602, -32602])

    def test_tool_list_is_small_and_annotated(self):
        [reply] = self.exchange(rpc("tools/list"))
        tools = reply["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertLessEqual(len(tools), MAX_TOOLS)
        self.assertEqual(names, ["map_repository", "find_symbol", "review_change", "perf_check"])
        by = {t["name"]: t for t in tools}
        self.assertTrue(by["find_symbol"]["annotations"]["readOnlyHint"])
        self.assertFalse(by["perf_check"]["annotations"]["readOnlyHint"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_tool_call_success_and_tool_error(self):
        replies = self.exchange(
            rpc("tools/call", {"name": "find_symbol", "arguments": {"name": "charge"}}, msg_id=1),
            rpc("tools/call", {"name": "find_symbol", "arguments": {}}, msg_id=2),
        )
        self.assertFalse(replies[0]["result"]["isError"])
        self.assertIn("app.py:1  function charge", replies[0]["result"]["content"][0]["text"])
        self.assertTrue(replies[1]["result"]["isError"])

    def test_tool_crash_is_reported_not_fatal(self):
        class Boom(BaseTool):
            name = "boom"
            def get_schema(self):
                return {"type": "function", "function": {"name": "boom", "description": "x", "parameters": {"type": "object"}}}
            def execute(self, parameters):
                raise RuntimeError("kaboom")

        server = mcp_server.Server(self.ws)
        server.tools["boom"] = Boom()
        with mock.patch("sys.stderr", io.StringIO()):
            result = server.dispatch("tools/call", {"name": "boom", "arguments": {}})
        self.assertTrue(result["isError"])
        self.assertIn("INTERNAL_ERROR in boom", result["content"][0]["text"])
        self.assertEqual(server.dispatch("ping", {}), {})

    def test_stray_print_from_a_tool_cannot_corrupt_the_protocol(self):
        script = (
            "import sys, io, mcp_server\n"
            "from core.models import ToolExecutionResult\n"
            "orig = mcp_server.build_tools\n"
            "def noisy(root):\n"
            "    tools = orig(root)\n"
            "    t = tools[0]; real = t.execute\n"
            "    def ex(p):\n"
            "        print('debug noise from a tool')\n"
            "        return real(p)\n"
            "    t.execute = ex\n"
            "    return tools\n"
            "mcp_server.build_tools = noisy\n"
            f"mcp_server.serve({self.ws!r})\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], input=rpc("tools/call", {"name": "map_repository", "arguments": {}}) + "\n",
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=dict(os.environ, HARNESS_NO_EXTERNAL="1"),
        )
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        self.assertIn("result", json.loads(lines[0]))
        self.assertIn("debug noise", proc.stderr)


class TestGatedPerfCheck(ServerCase):
    def test_denylisted_and_owner_gated_commands_are_blocked(self):
        with open(os.path.join(self.ws, "harness.gates.json"), "w") as f:
            json.dump({"commandGates": [{"id": "deploy", "pattern": "make deploy", "why": "prod"}]}, f)
        replies = self.exchange(
            rpc("tools/call", {"name": "perf_check", "arguments": {"command": "make deploy", "mode": "baseline"}}, msg_id=1),
            rpc("tools/call", {"name": "perf_check", "arguments": {"command": "rm -rf /", "mode": "baseline"}}, msg_id=2),
        )
        for r in replies:
            self.assertTrue(r["result"]["isError"])
            self.assertIn("BLOCKED", r["result"]["content"][0]["text"])

    def test_unreadable_gate_file_fails_closed(self):
        with open(os.path.join(self.ws, "harness.gates.json"), "w") as f:
            f.write("{broken")
        [reply] = self.exchange(rpc("tools/call", {"name": "perf_check", "arguments": {"command": "true", "mode": "baseline"}}))
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("REFUSED", reply["result"]["content"][0]["text"])


class TestResolveRoot(ServerCase):
    def test_subdirectory_widens_to_git_root(self):
        sub = os.path.join(self.ws, "pkg", "deep")
        os.makedirs(sub)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "", "HARNESS_WORKSPACE": "", "GEMINI_PROJECT_DIR": ""}):
            self.assertEqual(mcp_server.resolve_root(sub), os.path.realpath(self.ws))

    def test_host_env_is_used_when_no_argument(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws, "HARNESS_WORKSPACE": ""}):
            self.assertEqual(mcp_server.resolve_root(None), os.path.realpath(self.ws))

    def test_non_repo_directory_is_used_as_is(self):
        plain = tempfile.mkdtemp()
        try:
            self.assertEqual(mcp_server.resolve_root(plain), os.path.realpath(plain))
        finally:
            shutil.rmtree(plain)


if __name__ == "__main__":
    unittest.main()

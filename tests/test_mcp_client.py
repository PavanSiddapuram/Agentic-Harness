"""Tests for the stdlib MCP stdio client and the code graph's server mode.

A small fake MCP server (a Python script) stands in for real servers.
"""

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from core.mcp_client import MCPError, StdioMCPClient
from planes.context.graph import CodeGraph, graph_tools

FAKE_SERVER = """\
import json, os, sys, time
base = sys.argv[0]
with open(base + ".starts", "a") as f:
    f.write("start\\n")
def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error: out["error"] = error
    else: out["result"] = result
    sys.stdout.write(json.dumps(out) + "\\n"); sys.stdout.flush()
print("server noise that is not json", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, params = msg["method"], msg.get("params", {})
    if method == "initialize":
        reply(msg["id"], {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}})
        continue
    if method == "tools/call":
        name, args = params["name"], params["arguments"]
        with open(base + ".calls", "a") as f:
            f.write(json.dumps([name, args]) + "\\n")
        if name == "crash":
            os._exit(3)
        if name == "slow":
            time.sleep(5)
        if name == "index_repository":
            text = json.dumps({"project": "very-long-project-name"})
        elif args.get("function_name", "").endswith("missing"):
            reply(msg["id"], {"content": [{"type": "text", "text": "function not found"}], "isError": True}); continue
        else:
            text = "very-long-project-name.lib.response: send 1\\nargs=" + json.dumps(args, sort_keys=True)
        reply(msg["id"], {"content": [{"type": "text", "text": text}]})
        continue
    reply(msg["id"], error={"code": -32601, "message": "unknown method"})
"""

POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "fake executables are POSIX scripts")


def write_exe(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n" + textwrap.dedent(body))
    os.chmod(path, 0o755)
    return path


@POSIX_ONLY
class ServerBase(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.ws = tempfile.mkdtemp()
        self.server = write_exe(self.bin, "codebase-memory-mcp", FAKE_SERVER)
        self.env = mock.patch.dict(os.environ, {"PATH": self.bin})
        self.env.start()
        os.environ.pop("HARNESS_NO_EXTERNAL", None)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.env.stop()
        shutil.rmtree(self.bin, ignore_errors=True)
        shutil.rmtree(self.ws, ignore_errors=True)

    def starts(self):
        with open(self.server + ".starts") as f:
            return len(f.read().split())

    def calls(self):
        with open(self.server + ".calls") as f:
            return [json.loads(line) for line in f]


class TestStdioClient(ServerBase):
    def client(self):
        c = StdioMCPClient([self.server])
        self.clients.append(c)
        return c

    def test_calls_reuse_one_process_and_skip_noise(self):
        c = self.client()
        for _ in range(3):
            self.assertIn("send 1", c.call_tool("trace_path", {"function_name": "send"}))
        self.assertEqual(self.starts(), 1)

    def test_tool_error_raises(self):
        with self.assertRaises(MCPError) as ctx:
            self.client().call_tool("trace_path", {"function_name": "missing"})
        self.assertIn("function not found", str(ctx.exception))

    def test_timeout_raises_and_server_survives(self):
        c = self.client()
        with self.assertRaises(MCPError) as ctx:
            c.call_tool("slow", {}, timeout=0.5)
        self.assertIn("timed out", str(ctx.exception))

    def test_dead_server_is_restarted_once(self):
        c = self.client()
        c.call_tool("trace_path", {"function_name": "send"})
        with self.assertRaises(MCPError):
            c.call_tool("crash", {})  # crashes again on the retry
        self.assertEqual(self.starts(), 2)  # restarted once, crashed again, gave up
        self.assertIn("send 1", c.call_tool("trace_path", {"function_name": "send"}))
        self.assertEqual(self.starts(), 3)  # a later call starts a fresh server

    def test_missing_binary(self):
        c = StdioMCPClient([os.path.join(self.bin, "nope")])
        with self.assertRaises(MCPError):
            c.call_tool("x", {})


class TestCodeGraphServerMode(ServerBase):
    def graph(self):
        g = CodeGraph(self.ws)
        self.clients.append(g)
        return g

    def test_indexes_once_and_queries_through_one_server(self):
        g = self.graph()
        out = g.query("trace_path", {"function_name": "send", "depth": 2})
        g.query("trace_path", {"function_name": "json"})
        self.assertEqual(g.mode, "server")
        self.assertEqual(self.starts(), 1)
        self.assertEqual([c[0] for c in self.calls()], ["index_repository", "trace_path", "trace_path"])
        self.assertNotIn("very-long-project-name", out)
        self.assertIn('"project": "very-long-project-name"', json.dumps(self.calls()[1][1]))

    def test_warm_up_indexes_once_in_the_background(self):
        g = self.graph()
        g.warm_up()
        g.query("trace_path", {"function_name": "send"})  # waits on the lock if warm-up is mid-index
        self.assertEqual([c[0] for c in self.calls()].count("index_repository"), 1)
        self.assertEqual(self.starts(), 1)

    def test_qualified_name_gets_project_prefix_back(self):
        self.graph().query("trace_path", {"function_name": "lib.response.render"})
        self.assertEqual(self.calls()[-1][1]["function_name"], "very-long-project-name.lib.response.render")

    def test_tool_error_surfaces_without_falling_back(self):
        tools = {t.name: t for t in graph_tools(self.ws)}
        self.clients.append(tools["trace_calls"].graph)
        result = tools["trace_calls"].execute({"function_name": "missing"})
        self.assertFalse(result.success)
        self.assertIn("function not found", result.output)
        self.assertEqual(tools["trace_calls"].graph.mode, "server")

    def test_change_impact_reindexes_on_the_same_server(self):
        tools = {t.name: t for t in graph_tools(self.ws)}
        self.clients.append(tools["trace_calls"].graph)
        tools["trace_calls"].execute({"function_name": "send"})
        tools["change_impact"].execute({})
        self.assertEqual(self.starts(), 1)
        self.assertEqual([c[0] for c in self.calls()].count("index_repository"), 2)


if __name__ == "__main__":
    unittest.main()

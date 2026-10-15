"""Tests for optional external tools (RTK, codebase-memory-mcp).

Fake executables on a temporary PATH stand in for the real binaries, so these
tests need nothing installed and check both the 'tool present' and 'tool
absent' paths.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from core.external import find_tool
from core.models import PermissionLevel, ToolCallRequest
from core.permissions import PermissionGate
from core.tools.fs_tools import SafePathResolver
from core.tools.shell import ExecuteCommandTool
from planes.context.graph import graph_tools

POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "fake executables are POSIX scripts")

FAKE_RTK = """\
import subprocess, sys
args = sys.argv[1:]
if args[:1] == ["rewrite"]:
    cmd = args[1]
    mode = open(sys.argv[0] + ".mode").read().strip()
    if mode == "unsafe":
        print("rm -rf /tmp/nothing", end=""); sys.exit(0)
    if cmd.startswith("git "):
        print("rtk " + cmd, end=""); sys.exit(0)
    sys.exit(1)
proc = subprocess.run(args, capture_output=True, text=True)
print("[filtered by fake rtk]")
print(proc.stdout, end="")
sys.exit(proc.returncode)
"""

FAKE_CBM = """\
import json, os, sys
log = sys.argv[0] + ".log"
args = sys.argv[1:]
tool = [a for a in args if not a.startswith("--") and a != "cli"][0]
with open(log, "a") as f:
    f.write(tool + "\\n")
if os.path.exists(sys.argv[0] + ".fail"):
    print("boom", file=sys.stderr); sys.exit(2)
if tool == "index_repository":
    print(json.dumps({"project": "very-long-project-name"})); sys.exit(0)
print("tool=" + tool)
print("very-long-project-name.lib.response: send 1")
print("args=" + " ".join(args[args.index(tool) + 1:]))
"""


def write_exe(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n" + textwrap.dedent(body))
    os.chmod(path, 0o755)
    return path


class ExternalBase(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.ws = tempfile.mkdtemp()
        os.system(f"git -C '{self.ws}' init -q")
        # Only the fakes plus git: real installs of these tools must not leak in.
        git_dir = os.path.dirname(shutil.which("git") or "/usr/bin/git")
        self.env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + git_dir})
        self.env.start()
        os.environ.pop("HARNESS_NO_EXTERNAL", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.bin, ignore_errors=True)
        shutil.rmtree(self.ws, ignore_errors=True)


@POSIX_ONLY
class TestRtk(ExternalBase):
    def make_rtk(self, mode="safe"):
        path = write_exe(self.bin, "rtk", FAKE_RTK)
        with open(path + ".mode", "w") as f:
            f.write(mode)

    def tool(self, **kw):
        return ExecuteCommandTool(SafePathResolver(self.ws), **kw)

    def test_supported_command_runs_through_rtk(self):
        self.make_rtk()
        result = self.tool().execute({"command": "git status --short"})
        self.assertIn("[filtered by fake rtk]", result.output)

    def test_exit_code_is_preserved(self):
        self.make_rtk()
        result = self.tool().execute({"command": "git log no-such-ref"})
        self.assertFalse(result.success)
        self.assertIn("[filtered by fake rtk]", result.output)

    def test_unsupported_command_runs_unchanged(self):
        self.make_rtk()
        result = self.tool().execute({"command": "echo plain"})
        self.assertNotIn("fake rtk", result.output)
        self.assertIn("plain", result.output)

    def test_rewrite_that_changes_the_command_is_rejected(self):
        self.make_rtk(mode="unsafe")
        tool = self.tool()
        self.assertIsNone(tool._rtk_rewrite("git status"))
        self.assertIn("On branch", tool.execute({"command": "git status"}).output)

    def test_absent_rtk_uses_stdlib_path(self):
        tool = self.tool()
        self.assertIsNone(tool.rtk_path)
        self.assertIn("plain", tool.execute({"command": "echo plain"}).output)

    def test_can_be_disabled(self):
        self.make_rtk()
        self.assertIsNone(self.tool(use_rtk=False).rtk_path)
        with mock.patch.dict(os.environ, {"HARNESS_NO_EXTERNAL": "1"}):
            self.assertIsNone(find_tool("rtk"))
            self.assertIsNone(self.tool().rtk_path)


@POSIX_ONLY
class TestCodeGraph(ExternalBase):
    def make_cbm(self):
        self.cbm = write_exe(self.bin, "codebase-memory-mcp", FAKE_CBM)

    def calls(self):
        with open(self.cbm + ".log") as f:
            return f.read().split()

    def test_no_binary_means_no_tools(self):
        self.assertEqual(graph_tools(self.ws), [])

    def test_trace_calls_indexes_once_and_strips_project_prefix(self):
        self.make_cbm()
        trace = {t.name: t for t in graph_tools(self.ws)}["trace_calls"]
        out1 = trace.execute({"function_name": "send", "direction": "both", "depth": 9}).output
        trace.execute({"function_name": "json"})
        self.assertIn("tool=trace_path", out1)
        self.assertIn("lib.response: send 1", out1)
        self.assertNotIn("very-long-project-name", out1)
        self.assertIn("--direction both", out1)
        self.assertIn("--depth 5", out1)  # clamped
        self.assertEqual(self.calls(), ["index_repository", "trace_path", "trace_path"])

    def test_change_impact_reindexes_and_defaults_to_head(self):
        self.make_cbm()
        tools = {t.name: t for t in graph_tools(self.ws)}
        tools["trace_calls"].execute({"function_name": "x"})
        out = tools["change_impact"].execute({}).output
        self.assertIn("tool=detect_changes", out)
        self.assertIn("--base-branch HEAD", out)
        self.assertEqual(self.calls().count("index_repository"), 2)

    def test_failure_reports_fallback_instead_of_crashing(self):
        self.make_cbm()
        open(self.cbm + ".fail", "w").close()
        result = graph_tools(self.ws)[0].execute({"function_name": "send"})
        self.assertFalse(result.success)
        self.assertIn("Fall back to find_symbol", result.output)

    def test_missing_name(self):
        self.make_cbm()
        self.assertFalse(graph_tools(self.ws)[0].execute({}).success)


class TestGraphToolsAreReadOnly(unittest.TestCase):
    def test_auto_allowed(self):
        gate = PermissionGate(".")
        for name in ["trace_calls", "change_impact"]:
            call = ToolCallRequest(call_id="1", tool_name=name, parameters={})
            self.assertEqual(gate.assess(call), PermissionLevel.AUTO_ALLOW)


if __name__ == "__main__":
    unittest.main()

"""Tests for the before/after performance check."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from core.models import PermissionLevel, ToolCallRequest
from core.permissions import PermissionGate, SecurityException
from planes.verify import perf

PY = f'"{sys.executable}"'


def sleeper(seconds, mb=0):
    alloc = f"x=bytearray({mb}*1024*1024); x[::4096]=b'1'*len(x[::4096]); " if mb else ""
    return f'{PY} -c "import time; {alloc}time.sleep({seconds})"'


class TestPerfCheck(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.ws], check=True)
        self.tool = perf.PerfCheckTool(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def run_tool(self, command, mode, runs=3):
        return self.tool.execute({"command": command, "mode": mode, "runs": runs})

    def test_same_code_is_not_a_regression(self):
        cmd = sleeper(0.05)
        self.assertTrue(self.run_tool(cmd, "baseline").success)
        result = self.run_tool(cmd, "compare")
        self.assertTrue(result.success, result.output)
        self.assertIn("no regression detected", result.output)

    def test_slowdown_is_a_regression(self):
        perf.save_baseline(self.ws, "bench", perf.measure(sleeper(0.05), self.ws, runs=3))
        baseline = perf.load_baseline(self.ws, "bench")
        lines, regressed = perf.compare(baseline, perf.measure(sleeper(0.25), self.ws, runs=3))
        self.assertTrue(regressed)
        self.assertIn("REGRESSION", lines[0])

    @unittest.skipUnless(hasattr(os, "wait4"), "peak memory needs wait4")
    def test_peak_memory_is_measured_through_the_shell(self):
        sample = perf.run_once("echo start && " + sleeper(0, mb=150), self.ws, timeout=60)
        self.assertGreater(sample.rss_mb, 140)

    def test_compare_without_baseline_and_bad_args(self):
        result = self.run_tool(sleeper(0), "compare")
        self.assertFalse(result.success)
        self.assertIn("mode='baseline' first", result.output)
        self.assertFalse(self.tool.execute({"command": "x"}).success)

    def test_failing_command_reports_output(self):
        result = self.run_tool(f'{PY} -c "import sys; print(\'boom here\'); sys.exit(3)"', "baseline")
        self.assertFalse(result.success)
        self.assertIn("PERF_COMMAND_FAILED (exit code 3)", result.output)
        self.assertIn("boom here", result.output)

    def test_timeout(self):
        result = self.tool.execute({"command": sleeper(5), "mode": "baseline", "runs": 1, "timeout": 1})
        self.assertFalse(result.success)
        self.assertIn("PERF_TIMEOUT", result.output)

    def test_baseline_warns_when_tree_is_dirty(self):
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", self.ws, "add", "a.txt"], check=True)
        self.assertIn("uncommitted changes", self.run_tool(sleeper(0), "baseline", runs=1).output)


class TestVerdict(unittest.TestCase):
    def stats(self, median, spread=0.0):
        return {"median": median, "min": median - spread, "max": median + spread, "mad": spread / 2}

    def test_threshold_and_noise(self):
        self.assertTrue(perf._verdict("t", "s", self.stats(1.0), self.stats(1.2))[1])        # +20% > 10%
        self.assertFalse(perf._verdict("t", "s", self.stats(1.0), self.stats(1.08))[1])      # +8% < 10%
        self.assertFalse(perf._verdict("t", "s", self.stats(1.0, 0.3), self.stats(1.2))[1])  # +20% but noise is 30%
        self.assertIn("improvement", perf._verdict("t", "s", self.stats(1.0), self.stats(0.7))[0])

    def test_memory_floor(self):
        self.assertFalse(perf._verdict("m", " MB", self.stats(10), self.stats(14), floor=5)[1])


class TestPerfPermissions(unittest.TestCase):
    def test_perf_check_is_gated_like_a_shell_command(self):
        ws = tempfile.mkdtemp()
        try:
            with open(os.path.join(ws, "harness.gates.json"), "w") as f:
                f.write('{"commandGates": [{"id": "deploy", "pattern": "make deploy", "why": "prod"}]}')
            gate = PermissionGate(ws, auto_approve_all=True)
            ok = ToolCallRequest(call_id="1", tool_name="perf_check", parameters={"command": "npm run bench", "mode": "baseline"})
            self.assertEqual(gate.assess(ok), PermissionLevel.RESTRICTED)
            for command in ["make deploy", "rm -rf /"]:
                bad = ToolCallRequest(call_id="2", tool_name="perf_check", parameters={"command": command, "mode": "baseline"})
                with self.assertRaises(SecurityException):
                    gate.assess(bad)
        finally:
            shutil.rmtree(ws)


if __name__ == "__main__":
    unittest.main()

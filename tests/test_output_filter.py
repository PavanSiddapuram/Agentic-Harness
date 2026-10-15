"""Tests for shell output compression."""

import os
import shutil
import sys
import tempfile
import unittest

from core.output_filter import compress
from core.tools.fs_tools import SafePathResolver
from core.tools.shell import ExecuteCommandTool


class TestCompress(unittest.TestCase):
    def test_strips_ansi_and_progress_frames(self):
        out, _ = compress("\x1b[32mok\x1b[0m\nDownloading 10%\r20%\r100% done")
        self.assertEqual(out, "ok\n100% done")

    def test_pytest_pass_lines_collapse_but_failures_stay(self):
        raw = "\n".join(
            [
                "tests/test_a.py ......  [ 40%]",
                "tests/test_b.py ....  [ 80%]",
                "tests/test_c.py ..F.  [100%]",
                "FAILED tests/test_c.py::test_x - AssertionError: 1 != 2",
                "1 failed, 12 passed in 0.31s",
            ]
        )
        out, removed = compress(raw)
        self.assertIn("tests/test_c.py ..F.", out)
        self.assertIn("FAILED tests/test_c.py::test_x", out)
        self.assertIn("1 failed, 12 passed", out)
        self.assertIn("[2 passing-test lines omitted]", out)
        self.assertNotIn("test_a.py", out)
        self.assertGreater(removed, 0)

    def test_go_and_cargo_and_jest_pass_lines(self):
        raw = "\n".join(
            [
                "=== RUN   TestAdd",
                "--- PASS: TestAdd (0.00s)",
                "--- FAIL: TestSub (0.00s)",
                "test parser::ok_case ... ok",
                "test parser::bad_case ... FAILED",
                "  ✓ renders header (3 ms)",
                "  ✕ renders footer (2 ms)",
            ]
        )
        out, _ = compress(raw)
        for keep in ["--- FAIL: TestSub", "bad_case ... FAILED", "✕ renders footer"]:
            self.assertIn(keep, out)
        for drop in ["--- PASS", "ok_case", "renders header"]:
            self.assertNotIn(drop, out)

    def test_repeated_lines_collapse(self):
        out, _ = compress("\n".join(["retrying connection"] * 6 + ["connected"]))
        self.assertEqual(
            out, "retrying connection\n[previous line repeated 5 more times]\nconnected"
        )

    def test_plain_output_is_untouched(self):
        raw = "line one\nline two\nline two\nline three"
        self.assertEqual(compress(raw)[0], raw)


class TestShellToolCompression(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_raw_output_is_saved_when_compressed(self):
        raw_dir = os.path.join(self.ws, ".harness", "raw")
        tool = ExecuteCommandTool(SafePathResolver(self.ws), raw_log_dir=raw_dir)
        script = "for i in range(5): print('tests/test_a.py ....  [ 50%]')"
        result = tool.execute({"command": f'"{sys.executable}" -c "{script}"'})
        self.assertIn("passing-test lines omitted", result.output)
        self.assertIn("unfiltered output saved to", result.output)
        saved = os.listdir(raw_dir)
        self.assertEqual(len(saved), 1)
        with open(os.path.join(raw_dir, saved[0])) as f:
            self.assertEqual(f.read().count("test_a.py"), 5)

    def test_compression_can_be_disabled(self):
        tool = ExecuteCommandTool(SafePathResolver(self.ws), compress_output=False)
        script = "print('tests/test_a.py ....  [ 50%]')"
        result = tool.execute({"command": f'"{sys.executable}" -c "{script}"'})
        self.assertIn("test_a.py", result.output)


if __name__ == "__main__":
    unittest.main()

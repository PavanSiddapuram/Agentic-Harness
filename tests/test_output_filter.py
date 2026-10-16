"""Tests for shell output compression."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_mocha_suite_headings_fold_when_all_pass(self):
        raw = "\n".join([
            "", "  Route", "    ✔ should work", "    .all", "      ✔ should add handler", "      ✔ should handle VERBS",
            "", "  Router", "    ✔ should return a function", "", "  12 passing (40ms)",
        ])
        out, _ = compress(raw)
        self.assertNotIn("Route", out)
        self.assertNotIn("Router", out)
        self.assertIn("[4 passing-test lines omitted, in 3 groups]", out)
        self.assertIn("12 passing (40ms)", out)

    def test_headings_leading_to_failures_and_failure_report_stay(self):
        raw = "\n".join([
            "  app", "    ✔ boots", "  res", "    .links(obj)", "      1) should set Link header field",
            "", "  1 passing (5ms)", "  1 failing", "", "  1) res", "       .links(obj)",
            "         should set Link header field:", "     Error: expected a, got b",
        ])
        out, _ = compress(raw)
        self.assertNotIn("  app", out.split("\n"))
        for keep in ["  res", "    .links(obj)", "1) should set Link header field", "1 failing",
                     "should set Link header field:", "Error: expected a, got b"]:
            self.assertIn(keep, out)

    def test_headings_with_failure_words_are_never_folded(self):
        raw = "\n".join(["  error handling", "    ✔ returns 500", "  plain group", "    ✔ works"])
        out, _ = compress(raw)
        self.assertIn("error handling", out)
        self.assertNotIn("plain group", out)

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

    def test_rtk_elided_failure_is_recovered(self):
        bindir = tempfile.mkdtemp()
        try:
            fake = os.path.join(bindir, "rtk")
            with open(fake, "w") as f:
                f.write(f"#!{sys.executable}\n" + (
                    "import sys\n"
                    "a = sys.argv[1:]\n"
                    "if a[:1] == ['rewrite']: sys.exit(1)\n"
                    "if a[:1] == ['recall']: print('ok 1\\nFAILED test_important_name\\n  at frame 1'); sys.exit(0)\n"
                    "print('OUTPUT (last 1 lines):\\n  at frame 30\\n[full output: rtk recall abc123]'); sys.exit(1)\n"
                ))
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
                os.environ.pop("HARNESS_NO_EXTERNAL", None)
                tool = ExecuteCommandTool(SafePathResolver(self.ws))
                result = tool.execute({"command": "rtk test make check"})
            self.assertFalse(result.success)
            self.assertIn("FAILED test_important_name", result.output)
            self.assertIn("recovered from rtk recall", result.output)
        finally:
            shutil.rmtree(bindir, ignore_errors=True)

    def test_compression_can_be_disabled(self):
        tool = ExecuteCommandTool(SafePathResolver(self.ws), compress_output=False)
        script = "print('tests/test_a.py ....  [ 50%]')"
        result = tool.execute({"command": f'"{sys.executable}" -c "{script}"'})
        self.assertIn("test_a.py", result.output)


if __name__ == "__main__":
    unittest.main()

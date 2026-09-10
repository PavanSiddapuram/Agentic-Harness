"""Unit tests for the Evaluator layer."""

import os
import shutil
import tempfile
import unittest

from core.evaluator import Evaluator
from core.models import ToolCallRequest, ToolExecutionResult
from core.tools.fs_tools import SafePathResolver


class TestEvaluator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.resolver = SafePathResolver(self.test_dir)
        self.evaluator = Evaluator(self.resolver)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_valid_python_syntax_passes(self):
        py_file = os.path.join(self.test_dir, "good.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def add(a: int, b: int) -> int:\n    return a + b\n")

        call = ToolCallRequest(
            call_id="c1", tool_name="patch_file", parameters={"filepath": "good.py"}
        )
        raw = ToolExecutionResult(
            call_id="c1",
            success=True,
            output="Applied patch",
            modified_files=["good.py"],
        )

        evaluated = self.evaluator.evaluate(call, raw)
        self.assertTrue(evaluated.success)
        self.assertIsNone(evaluated.evaluator_feedback)

    def test_broken_python_syntax_fails_and_generates_feedback(self):
        py_file = os.path.join(self.test_dir, "broken.py")
        # Deliberately broken Python code (missing colon after if)
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def check(x):\n    if x > 0\n        return True\n")

        call = ToolCallRequest(
            call_id="c2", tool_name="patch_file", parameters={"filepath": "broken.py"}
        )
        raw = ToolExecutionResult(
            call_id="c2",
            success=True,
            output="Applied patch",
            modified_files=["broken.py"],
        )

        evaluated = self.evaluator.evaluate(call, raw)
        self.assertFalse(evaluated.success)
        self.assertIsNotNone(evaluated.evaluator_feedback)
        self.assertIn("AST SyntaxError", evaluated.evaluator_feedback)
        self.assertIn("broken.py", evaluated.evaluator_feedback)

    def test_invalid_json_fails_and_generates_feedback(self):
        json_file = os.path.join(self.test_dir, "config.json")
        with open(json_file, "w", encoding="utf-8") as f:
            f.write('{"key": "value",}')  # Invalid trailing comma

        call = ToolCallRequest(
            call_id="c3", tool_name="create_file", parameters={"filepath": "config.json"}
        )
        raw = ToolExecutionResult(
            call_id="c3",
            success=True,
            output="Created file",
            modified_files=["config.json"],
        )

        evaluated = self.evaluator.evaluate(call, raw)
        self.assertFalse(evaluated.success)
        self.assertIn("JSONDecodeError", evaluated.evaluator_feedback)

    def test_shell_command_nonzero_exit_code_interception(self):
        call = ToolCallRequest(
            call_id="c4",
            tool_name="execute_command",
            parameters={"command": "pytest"},
        )
        raw = ToolExecutionResult(
            call_id="c4",
            success=False,
            output="Exit code: 1\nOutput:\nFAILED tests/test_calc.py - AssertionError",
        )

        evaluated = self.evaluator.evaluate(call, raw)
        self.assertFalse(evaluated.success)
        self.assertIn("non-zero status code", evaluated.evaluator_feedback)


if __name__ == "__main__":
    unittest.main()

"""Evaluator layer for post-execution static validation and self-healing.

Intercepts tool execution results before they enter memory.
Validates AST syntax for code files and exit codes for shell commands,
formatting rich diagnostic hints so the LLM can self-correct immediately.
"""

from __future__ import annotations

import ast
import json
import os
from typing import Optional

from core.models import ToolCallRequest, ToolExecutionResult
from core.tools.fs_tools import SafePathResolver


class Evaluator:
    """Performs pre-commit / post-execution static analysis and lint checks."""

    def __init__(self, path_resolver: SafePathResolver):
        self.resolver = path_resolver

    def evaluate(
        self, tool_call: ToolCallRequest, raw_result: ToolExecutionResult
    ) -> ToolExecutionResult:
        """Inspects raw tool execution output and enriches it with compiler/syntax feedback."""
        # Check shell command exit codes
        if tool_call.tool_name == "execute_command":
            # Trust the tool's own exit-code verdict; matching "Exit code: 0" in the
            # text misfires when the command's output happens to contain it.
            if not raw_result.success:
                raw_result.success = False
                raw_result.evaluator_feedback = (
                    "Command failed with a non-zero status code or timed out. "
                    "Inspect the stderr/output above, diagnose the root cause, and correct the error."
                )
                return raw_result
            return raw_result

        # For file operations: if it already failed at tool level without modifying files, return as is
        if not raw_result.success and not raw_result.modified_files:
            return raw_result

        # Check file mutation operations
        if tool_call.tool_name in ["patch_file", "create_file"]:
            filepath_arg = tool_call.parameters.get("filepath", "")
            feedback = self._validate_file_syntax(filepath_arg)
            if feedback:
                raw_result.success = False
                raw_result.evaluator_feedback = feedback
                return raw_result

        return raw_result

    def _validate_file_syntax(self, relative_path: str) -> Optional[str]:
        """Validates that modified files are syntactically valid."""
        try:
            safe_path = self.resolver.resolve(relative_path)
        except Exception:
            return None

        if not os.path.exists(safe_path) or os.path.isdir(safe_path):
            return None

        # 1. Python AST Validation
        if relative_path.endswith(".py"):
            try:
                with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
                ast.parse(source, filename=relative_path)
            except SyntaxError as e:
                snippet = e.text.strip() if e.text else ""
                caret = " " * (max(1, e.offset or 1) - 1) + "^" if e.offset else ""
                return (
                    f"AST SyntaxError in '{relative_path}' on line {e.lineno}, column {e.offset}:\n"
                    f"  {snippet}\n"
                    f"  {caret}\n"
                    f"Error: {e.msg}\n"
                    f"Action Required: Apply a patch to fix this syntax error immediately before continuing."
                )

        # 2. JSON Validation
        if relative_path.endswith(".json"):
            try:
                with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                return (
                    f"JSONDecodeError in '{relative_path}' on line {e.lineno}, column {e.colno}: {e.msg}.\n"
                    f"Action Required: Fix the JSON formatting syntax."
                )

        return None

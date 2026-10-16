"""Subprocess shell execution tool with deadlock prevention and timeouts.

Ensures child processes do not freeze on filled OS pipe buffers.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from typing import Any, Dict, Optional

from core.external import find_tool, run_tool
from core.output_filter import compress

from core.models import ToolExecutionResult
from core.tools.base import BaseTool
from core.tools.fs_tools import SafePathResolver


class ExecuteCommandTool(BaseTool):
    name = "execute_command"
    description = (
        "Execute a shell command (e.g. pytest, git, python, npm) within the workspace."
    )

    def __init__(
        self,
        path_resolver: SafePathResolver,
        default_timeout: int = 30,
        compress_output: bool = True,
        raw_log_dir: Optional[str] = None,
        use_rtk: Optional[bool] = None,
    ):
        self.resolver = path_resolver
        self.default_timeout = default_timeout
        self.compress_output = compress_output
        # When output is compressed, the untouched original is saved here so
        # nothing is truly lost; the agent gets the path and can read it.
        self.raw_log_dir = raw_log_dir
        # RTK (rtk-ai/rtk), when installed, filters 100+ command types far more
        # thoroughly than the stdlib filter. None = use it if found on PATH.
        self.rtk_path = find_tool("rtk") if use_rtk is not False and compress_output else None

    def _rtk_rewrite(self, command: str) -> Optional[str]:
        """Asks RTK for its equivalent of a command; None if it has no filter for it."""
        if not self.rtk_path:
            return None
        code, out, _ = run_tool([self.rtk_path, "rewrite", command], timeout=10)
        rewritten = out.strip()
        if code not in (0, 3) or not rewritten or rewritten == command:
            return None
        # The approved command must be the one that runs: accept the rewrite
        # only if it is the original with 'rtk ' prefixes inserted.
        if re.sub(r"(?<![\w-])rtk\s+", "", rewritten) != command:
            return None
        return rewritten

    RECALL_RE = re.compile(r"rtk recall ([0-9a-f]{6,})")

    def _recover_rtk_elision(self, stdout: str) -> str:
        """RTK's summary modes can drop the line naming a failing test and leave a
        recall ID instead. On failure, fetch the full output so the agent sees it."""
        match = self.RECALL_RE.search(stdout or "")
        rtk = self.rtk_path or find_tool("rtk")
        if not match or not rtk:
            return stdout
        code, full, _ = run_tool([rtk, "recall", match.group(1)], timeout=15)
        if code != 0 or not full.strip():
            return stdout
        return full.rstrip("\n") + "\n[full output recovered from rtk recall; RTK had elided it]"

    def _compress(self, stdout: str) -> str:
        if not self.compress_output or not stdout:
            return stdout
        compressed, _ = compress(stdout)
        if compressed == stdout:
            return stdout
        if self.raw_log_dir:
            os.makedirs(self.raw_log_dir, exist_ok=True)
            raw_path = os.path.join(self.raw_log_dir, f"cmd_{uuid.uuid4().hex[:8]}.log")
            with open(raw_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(stdout)
            shown = os.path.relpath(raw_path, self.resolver.workspace_root)
            compressed += f"\n[noise filtered; unfiltered output saved to {shown}]"
        return compressed

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command string to execute",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional relative subdirectory to run command in",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 30, max 120)",
                        },
                    },
                    "required": ["command"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        command = parameters.get("command", "")
        cwd_arg = parameters.get("cwd", ".") or "."
        timeout = min(int(parameters.get("timeout", self.default_timeout)), 120)

        try:
            safe_cwd = self.resolver.resolve(cwd_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        # Permission checks already ran on the original command; RTK only
        # changes how its output is filtered, not what it does.
        run_command = self._rtk_rewrite(command) or command

        try:
            # Popen with communicate prevents OS 64KB pipe buffer deadlock
            proc = subprocess.Popen(
                run_command,
                shell=True,
                cwd=safe_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Combine stderr into stdout stream
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            stdout, _ = proc.communicate(timeout=timeout)
            exit_code = proc.returncode

            if exit_code != 0:
                stdout = self._recover_rtk_elision(stdout)
            stdout = self._compress(stdout)
            output = f"Exit code: {exit_code}\nOutput:\n{stdout if stdout else '(no output)'}"
            success = exit_code == 0

            return ToolExecutionResult(
                call_id="",
                success=success,
                output=output,
            )

        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"COMMAND_TIMEOUT: Command exceeded {timeout}s ceiling and was terminated.\nPartial Output:\n{stdout}",
            )
        except Exception as e:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"EXECUTION_ERROR: {type(e).__name__}: {str(e)}",
            )

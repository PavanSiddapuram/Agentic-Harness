"""Permission gatekeeper and security boundary.

Classifies risk tiers and prompts developer confirmation on non-trivial mutations.
Blocks known destructive command patterns outright.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional

from core.models import PermissionLevel, ToolCallRequest


class SecurityException(Exception):
    """Raised when a forbidden or catastrophic operation is requested."""
    pass


class PermissionGate:
    """Evaluates security risk and enforces developer approval policies."""

    def __init__(
        self,
        workspace_root: str,
        auto_approve_all: bool = False,
        input_func: Optional[Callable[[str], str]] = None,
    ):
        self.workspace_root = workspace_root
        self.auto_approve_all = auto_approve_all
        self.input_func = input_func or input
        self.destructive_patterns: List[str] = [
            r"rm\s+-rf",
            r":\(\)\{\s*:\|\:&\s*\};:",  # Fork bomb
            r"mkfs",
            r"dd\s+if=",
            r">\s*/dev/sd",
            r"chmod\s+-R\s+777",
            r"format\s+[A-Za-z]:",        # Windows format command
            r"Remove-Item.*-Recurse.*-Force",
        ]

    def assess(self, call: ToolCallRequest) -> PermissionLevel:
        """Determines the risk classification tier for a proposed tool call."""
        if call.tool_name in ["read_file", "search_directory", "list_dir"]:
            return PermissionLevel.AUTO_ALLOW

        if call.tool_name in ["patch_file", "create_file"]:
            return PermissionLevel.CONFIRMATION

        if call.tool_name == "execute_command":
            cmd = call.parameters.get("command", "")
            for pattern in self.destructive_patterns:
                if re.search(pattern, cmd, re.IGNORECASE):
                    raise SecurityException(
                        f"FORBIDDEN_COMMAND: Malicious/destructive command pattern matched: '{pattern}' in '{cmd}'."
                    )
            return PermissionLevel.RESTRICTED

        # Default unknown tools to RESTRICTED
        return PermissionLevel.RESTRICTED

    def authorize(self, call: ToolCallRequest, level: PermissionLevel) -> bool:
        """Determines whether execution is permitted to proceed."""
        if level == PermissionLevel.AUTO_ALLOW:
            return True

        if self.auto_approve_all:
            return True

        print(f"\n[SECURITY GATE] Review Requested for: {call.tool_name}")
        print(f"Risk Tier: {level.value}")
        print(f"Parameters: {call.parameters}")

        try:
            choice = self.input_func("Approve execution? [y/N]: ").strip().lower()
            return choice in ["y", "yes"]
        except (EOFError, KeyboardInterrupt):
            return False

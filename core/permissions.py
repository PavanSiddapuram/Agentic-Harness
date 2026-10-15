"""Permission gatekeeper and security boundary.

Classifies risk tiers and prompts developer confirmation on non-trivial mutations.
Blocks known destructive command patterns outright.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional

from core.models import PermissionLevel, ToolCallRequest


# Tools that only read the workspace; they run without a prompt.
READ_ONLY_TOOLS = {"read_file", "search_directory", "list_dir", "map_repository", "find_symbol"}


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
        # Defense in depth only: a regex denylist over a shell string can always be
        # evaded (aliases, scripts, eval). The real boundary is human approval for
        # RESTRICTED calls and, eventually, a sandbox. Keep this list tested.
        self.destructive_patterns: List[str] = [
            # rm with both recursive and force flags, any order or form
            r"\brm\s+(?=[^|;&]*(-[a-zA-Z]*r|--recursive))(?=[^|;&]*(-[a-zA-Z]*f|--force))",
            r"\bfind\b[^|;&]*\s-delete\b",
            r"\bgit\s+push\b[^|;&]*(--force\b|--force-with-lease\b|\s-f\b|--mirror\b)",
            r"\bgit\s+reset\s+--hard\b",
            r"\bgit\s+clean\s+-[a-zA-Z]*f",
            r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b",  # pipe-to-shell
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
        if call.tool_name in READ_ONLY_TOOLS:
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

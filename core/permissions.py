"""Permission gatekeeper and security boundary.

Classifies risk tiers and prompts developer confirmation on non-trivial mutations.
Blocks known destructive command patterns outright.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from core.models import PermissionLevel, ToolCallRequest


# Tools that only read the workspace; they run without a prompt.
READ_ONLY_TOOLS = {
    "read_file", "search_directory", "list_dir", "map_repository", "find_symbol",
    "trace_calls", "change_impact",
}


GATES_FILE = "harness.gates.json"


class SecurityException(Exception):
    """Raised when a forbidden or catastrophic operation is requested."""
    pass


class GateConfigError(Exception):
    """Raised when the project's gate file exists but cannot be trusted."""
    pass


def load_project_gates(workspace_root: str) -> Dict[str, Any]:
    """Loads owner gates a project declares in harness.gates.json.

    Fails closed: a gate file that exists but is malformed stops the harness
    instead of being silently ignored, because a gate that quietly stops
    guarding is worse than none (it is still believed).
    """
    path = os.path.join(workspace_root, GATES_FILE)
    gates: Dict[str, Any] = {"commandGates": [], "protectedPaths": []}
    if not os.path.isfile(path):
        return gates
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise GateConfigError(f"{GATES_FILE} is unreadable: {e}")
    if not isinstance(data, dict):
        raise GateConfigError(f"{GATES_FILE} must be a JSON object.")
    for i, gate in enumerate(data.get("commandGates", [])):
        if not isinstance(gate, dict) or not isinstance(gate.get("pattern"), str):
            raise GateConfigError(f"{GATES_FILE} commandGates[{i}] needs a string 'pattern'.")
        try:
            compiled = re.compile(gate["pattern"], re.IGNORECASE)
        except re.error as e:
            raise GateConfigError(f"{GATES_FILE} commandGates[{i}] has a bad regex: {e}")
        gates["commandGates"].append(
            {"id": gate.get("id", f"gate-{i}"), "why": gate.get("why", ""), "re": compiled}
        )
    paths = data.get("protectedPaths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise GateConfigError(f"{GATES_FILE} protectedPaths must be a list of glob strings.")
    gates["protectedPaths"] = paths
    return gates


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
        # Project-declared owner gates. These are checked before approval, so
        # neither the agent nor --auto-approve can get past them.
        self.project_gates = load_project_gates(workspace_root)
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
            self._check_protected_path(call.parameters.get("filepath", ""))
            return PermissionLevel.CONFIRMATION

        if call.tool_name == "execute_command":
            cmd = call.parameters.get("command", "")
            for gate in self.project_gates["commandGates"]:
                if gate["re"].search(cmd):
                    raise SecurityException(
                        f"OWNER_GATE '{gate['id']}': {gate['why']} This action is reserved for a "
                        f"human by {GATES_FILE}. Do not look for another route to the same effect; "
                        "stop and tell the user exactly what they need to run."
                    )
            for pattern in self.destructive_patterns:
                if re.search(pattern, cmd, re.IGNORECASE):
                    raise SecurityException(
                        f"FORBIDDEN_COMMAND: Malicious/destructive command pattern matched: '{pattern}' in '{cmd}'."
                    )
            return PermissionLevel.RESTRICTED

        # Default unknown tools to RESTRICTED
        return PermissionLevel.RESTRICTED

    def _check_protected_path(self, filepath: str) -> None:
        if os.path.isabs(filepath):
            filepath_rel = os.path.relpath(filepath, self.workspace_root)
        else:
            filepath_rel = filepath
        # normpath drops "./" and resolves "a/../b", so "./x" and "a/../x" match "x".
        rel = os.path.normpath(filepath_rel).replace(os.sep, "/")
        for pattern in self.project_gates["protectedPaths"]:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.rstrip("/") + "/*"):
                raise SecurityException(
                    f"PROTECTED_PATH: '{filepath}' matches '{pattern}' in {GATES_FILE}. "
                    "Edits here are reserved for a human; describe the change instead."
                )

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

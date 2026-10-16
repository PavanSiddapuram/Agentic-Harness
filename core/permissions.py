"""Permission gatekeeper and security boundary.

Classifies risk tiers and prompts developer confirmation on non-trivial mutations.
Blocks known destructive command patterns outright.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional

from core.models import PermissionLevel, ToolCallRequest


# Tools that only read the workspace; they run without a prompt.
READ_ONLY_TOOLS = {
    "read_file", "search_directory", "list_dir", "map_repository", "find_symbol",
    "trace_calls", "change_impact",
}


GATES_FILE = "harness.gates.json"
# vjvarada/coding-agent-setup's plan-guard config, enforced by its Claude Code
# hook. The harness reads it too, so one file can guard both.
PLAN_GUARD_FILE = os.path.join(".claude", "plan-guard.config.json")
GATE_FILES = (GATES_FILE, PLAN_GUARD_FILE)


class SecurityException(Exception):
    """Raised when a forbidden or catastrophic operation is requested."""
    pass


class GateConfigError(Exception):
    """Raised when a gate file exists but cannot be trusted."""
    pass


def _compile_gates(entries: Any, source: str, field: str) -> List[Dict[str, Any]]:
    if not isinstance(entries, list):
        raise GateConfigError(f"{source} {field} must be a list.")
    compiled = []
    for i, gate in enumerate(entries):
        if not isinstance(gate, dict) or not isinstance(gate.get("pattern"), str):
            raise GateConfigError(f"{source} {field}[{i}] needs a string 'pattern'.")
        try:
            regex = re.compile(gate["pattern"], re.IGNORECASE)
        except re.error as e:
            raise GateConfigError(f"{source} {field}[{i}] has a bad regex: {e}")
        compiled.append({
            "id": gate.get("id", f"{field}-{i}"),
            "why": gate.get("why", ""),
            "re": regex,
            "scanContent": bool(gate.get("scanContent")),
            "source": source,
        })
    return compiled


def load_project_gates(workspace_root: str) -> Dict[str, Any]:
    """Loads owner gates from harness.gates.json and .claude/plan-guard.config.json.

    Both are optional and merged. Fails closed: a gate file that exists but is
    malformed stops the harness instead of being silently ignored, because a
    gate that quietly stops guarding is worse than none (it is still believed).
    """
    gates: Dict[str, Any] = {"commandGates": [], "pathGates": [], "protectedPaths": [], "protectedBranches": []}
    for source in GATE_FILES:
        path = os.path.join(workspace_root, source)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise GateConfigError(f"{source} is unreadable: {e}")
        if not isinstance(data, dict):
            raise GateConfigError(f"{source} must be a JSON object.")
        gates["commandGates"] += _compile_gates(data.get("commandGates", []), source, "commandGates")
        gates["pathGates"] += _compile_gates(data.get("pathGates", []), source, "pathGates")
        for field in ("protectedPaths", "protectedBranches"):
            values = data.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise GateConfigError(f"{source} {field} must be a list of strings.")
            gates[field] += [v for v in values if v not in gates[field]]
    return gates


def _current_branch(workspace_root: str) -> Optional[str]:
    # symbolic-ref works on a branch with no commits yet, where
    # `rev-parse --abbrev-ref HEAD` fails and would let the first commit through.
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=workspace_root,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None  # None when detached


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
            body = call.parameters.get("content") or call.parameters.get("replace_block") or ""
            for gate in self.project_gates["commandGates"]:
                if gate["scanContent"] and body and gate["re"].search(body):
                    raise SecurityException(self._gate_message(gate, "in the content being written"))
            return PermissionLevel.CONFIRMATION

        if call.tool_name == "execute_command":
            self.check_command(call.parameters.get("command", ""))
            return PermissionLevel.RESTRICTED

        # Default unknown tools to RESTRICTED
        return PermissionLevel.RESTRICTED

    @staticmethod
    def _gate_message(gate: Dict[str, Any], where: str = "") -> str:
        return (
            f"OWNER_GATE '{gate['id']}'{' ' + where if where else ''}: {gate['why']} This action is "
            f"reserved for a human by {gate['source']}. Do not look for another route to the same "
            "effect; stop and tell the user exactly what they need to run."
        )

    def check_command(self, cmd: str) -> None:
        """Raises SecurityException if a shell command hits an owner gate or the denylist.

        Shared by every tool that runs a command (execute_command, perf_check).
        """
        for gate in self.project_gates["commandGates"]:
            if gate["re"].search(cmd):
                raise SecurityException(self._gate_message(gate))
        if self.project_gates["protectedBranches"] and re.search(r"\bgit\s+(commit|push|merge|rebase)\b", cmd):
            branch = _current_branch(self.workspace_root)
            if branch in self.project_gates["protectedBranches"]:
                raise SecurityException(
                    f"PROTECTED_BRANCH: committing or pushing directly on '{branch}'. Create a "
                    f"branch first (git switch -c <ticket>-<slug>); the PR is the review step."
                )
        for pattern in self.destructive_patterns:
            if re.search(pattern, cmd, re.IGNORECASE):
                raise SecurityException(
                    f"FORBIDDEN_COMMAND: Malicious/destructive command pattern matched: '{pattern}' in '{cmd}'."
                )

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
                    f"PROTECTED_PATH: '{filepath}' matches '{pattern}' in a gate file. "
                    "Edits here are reserved for a human; describe the change instead."
                )
        for gate in self.project_gates["pathGates"]:
            if gate["re"].search(rel):
                raise SecurityException(
                    f"PROTECTED_PATH: '{filepath}' matches {gate['id']} in {gate['source']}: {gate['why']} "
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

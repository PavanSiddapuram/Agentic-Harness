#!/usr/bin/env python3
"""One hook for every host agent: owner gates before a tool runs, review before stopping.

    agentic-harness-gate --host claude pre     # PreToolUse
    agentic-harness-gate --host codex  stop    # Stop
    (hosts: claude, codex, gemini, cursor, kimi)

Each host sends a JSON payload on stdin with its own field names; this file
maps them to one shape, runs the same checks, and answers in the form that host
understands. Every host listed blocks on exit code 2 with the reason on stderr,
except Cursor, which reads JSON on stdout.

pre:  shell commands and file writes are checked against the repo's gate files
      (.claude/plan-guard.config.json, harness.gates.json) and the destructive-
      command denylist. Fails closed: a broken gate file or a crash here blocks.
stop: reviews the uncommitted change (builtin rules and gitleaks; Semgrep is
      left to review_change) and sends the agent back once when there are
      high-severity findings. Fails open: a problem here never traps a session.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

HOSTS = ("claude", "codex", "gemini", "cursor", "kimi")
SHELL_TOOLS = {"bash", "shell", "run_shell_command", "exec_command", "local_shell", "terminal"}
WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "write_file", "replace", "create_file",
               "edit_file", "str_replace", "search_replace", "delete", "delete_file"}
PATH_KEYS = ("file_path", "path", "notebook_path", "target_file", "filePath")
CONTENT_KEYS = ("content", "contents", "new_string", "new_source", "code_edit", "replace_block")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
MAX_LISTED_FINDINGS = 8


# ------------------------------------------------------------------ input
def _text(value: Any) -> str:
    if isinstance(value, list):
        import shlex  # only Codex sends argv lists
        return shlex.join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def _parse_patch(patch: str) -> List[Tuple[str, str]]:
    """Files and added text from a Codex apply_patch body."""
    files = [a or b for a, b in PATCH_FILE_RE.findall(patch)]
    added = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    return [(path.strip(), added) for path in files]


def normalize(payload: Dict[str, Any]) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """(shell command or None, [(path, written text)]) from any host's pre-tool payload."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}

    # Cursor's beforeShellExecution puts the command at the top level.
    if not tool and isinstance(payload.get("command"), (str, list)):
        return _text(payload["command"]), []

    command = _text(tool_input.get("command"))
    if "*** Begin Patch" in command or tool.lower() == "apply_patch":
        patch = command or _text(tool_input.get("input")) or _text(tool_input.get("patch"))
        return None, _parse_patch(patch)
    if tool.lower() in SHELL_TOOLS:
        return command, []

    path = next((tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str)), None)
    pieces = [tool_input[k] for k in CONTENT_KEYS if isinstance(tool_input.get(k), str)]
    # Only writes are gated: a known write tool, or any tool carrying new content.
    # Reads (Read, read_file, ...) carry a path but no content and pass through.
    if path is None or (tool.lower() not in WRITE_TOOLS and not pieces and not tool_input.get("edits")):
        return None, []
    for edit in tool_input.get("edits") or []:  # Claude's MultiEdit
        if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
            pieces.append(edit["new_string"])
    return None, [(path, "\n".join(pieces))]


def find_root(payload: Dict[str, Any]) -> str:
    """The repo the agent is working in: the nearest directory with a .git, from the
    payload's cwd. Walks the filesystem instead of running git: this hook runs
    before every tool call, so each saved process start counts."""
    start = (payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("GEMINI_PROJECT_DIR")
             or os.getcwd())
    roots = payload.get("workspace_roots")  # Cursor
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        start = roots[0]
    current = os.path.realpath(start)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.realpath(start)
        current = parent


# ----------------------------------------------------------------- output
def deny(host: str, reason: str) -> int:
    if host == "cursor":
        print(json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}))
        return 0
    print(reason, file=sys.stderr)
    return 2


def keep_going(host: str, reason: str) -> int:
    if host == "cursor":
        print(json.dumps({"followup_message": reason}))
        return 0
    print(reason, file=sys.stderr)
    return 2


# ----------------------------------------------------------------- checks
def check_pre(host: str, payload: Dict[str, Any]) -> int:
    from core.permissions import GateConfigError, PermissionGate, SecurityException

    # Cursor fires preToolUse for shell commands too; beforeShellExecution already
    # checked them, so skip the second check instead of paying for it twice.
    if host == "cursor" and str(payload.get("tool_name") or "").lower() in SHELL_TOOLS:
        return 0
    command, writes = normalize(payload)
    if command is None and not writes:
        return 0
    root = find_root(payload)
    try:
        gate = PermissionGate(root, input_func=lambda _: "n")
    except GateConfigError as e:
        return deny(host, f"BLOCKED by agentic-harness: {e} Fix the gate file; nothing runs until it is valid.")
    try:
        if command is not None:
            gate.check_command(command)
        for path, text in writes:
            gate.check_write(path, text)
    except SecurityException as e:
        return deny(host, f"BLOCKED by agentic-harness: {e}")
    return 0


def _state_path(root: str) -> str:
    return os.path.join(root, ".harness", "hook-state.json")


def _already_sent_back(root: str, session: str, fingerprint: str) -> bool:
    """True if this session was already sent back for exactly these findings."""
    path = _state_path(root)
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if state.get(session) == fingerprint:
        return True
    state[session] = fingerprint
    state = dict(list(state.items())[-50:])  # keep the file small
    try:
        from core.project_context import hide_runtime_dir_from_git
        os.makedirs(os.path.dirname(path), exist_ok=True)
        hide_runtime_dir_from_git(root)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass
    return False


def check_stop(host: str, payload: Dict[str, Any]) -> int:
    if payload.get("stop_hook_active") or (host == "cursor" and int(payload.get("loop_count") or 0) > 0):
        return 0  # the host already continued once for a hook: never loop
    if host == "cursor" and payload.get("status") not in (None, "completed"):
        return 0
    from planes.verify.diff import NotAGitRepo
    from planes.verify.review import run_review

    root = find_root(payload)
    try:
        report = run_review(root, "HEAD", semgrep=False)
    except (NotAGitRepo, RuntimeError):
        return 0
    high = [f for f in report.findings if f.severity == "high"]
    if not high:
        return 0
    import hashlib
    fingerprint = hashlib.sha256("\n".join(f.render() for f in high).encode()).hexdigest()[:16]
    session = str(payload.get("session_id") or payload.get("conversation_id") or "default")
    if _already_sent_back(root, session, fingerprint):
        return 0
    lines = [f"agentic-harness review: {len(high)} high-severity finding(s) in lines you added. "
             "Fix them, or state in your answer why each is safe, then finish."]
    lines += [f"- {f.render()}" for f in high[:MAX_LISTED_FINDINGS]]
    if len(high) > MAX_LISTED_FINDINGS:
        lines.append(f"- ... {len(high) - MAX_LISTED_FINDINGS} more; run review_change for the full list")
    return keep_going(host, "\n".join(lines))


USAGE = "usage: agentic-harness-gate --host {claude,codex,gemini,cursor,kimi} {pre,stop}"


def parse_args(argv: List[str]) -> Tuple[str, str]:
    """Two fixed arguments, parsed by hand: argparse costs ~12 ms on every tool call."""
    host = event = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host, i = argv[i + 1], i + 2
            continue
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            event = arg
        i += 1
    if host not in HOSTS or event not in ("pre", "stop"):
        print(USAGE, file=sys.stderr)
        raise SystemExit(64)
    return host, event


def main(argv: Optional[List[str]] = None) -> int:
    host, event = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except ValueError as e:
        if event == "pre":
            return deny(host, f"BLOCKED by agentic-harness: unreadable hook input ({e}); failing closed.")
        return 0
    try:
        return check_pre(host, payload) if event == "pre" else check_stop(host, payload)
    except Exception as e:  # noqa: BLE001
        if event == "pre":  # an unevaluated call is not a safe call
            return deny(host, f"BLOCKED by agentic-harness: gate hook crashed ({type(e).__name__}: {e}); "
                              "failing closed. Run the harness tests or remove the hook deliberately.")
        print(f"agentic-harness stop review skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())

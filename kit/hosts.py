"""Write the harness into each host agent's project config.

The harness adds one MCP server and one hook command to whichever agents a
team uses. Each host keeps its config in its own file and format; this module
knows those files and merges into them without changing anything already
there. Formats were checked against each host's docs and installed CLIs
(Claude Code 2.1.302, Codex 0.163.0, Gemini CLI 0.64.0, Kimi Code 2.2.0) in
October 2026.

| host    | MCP server                | hooks (pre-tool gate, review on stop)        |
| claude  | .mcp.json                 | .claude/settings.json (PreToolUse, Stop)     |
| copilot | .mcp.json (VS Code)       | none                                         |
| codex   | .codex/config.toml        | .codex/hooks.json (PreToolUse, Stop)         |
| gemini  | .gemini/settings.json     | .gemini/settings.json (BeforeTool, AfterAgent)|
| cursor  | .cursor/mcp.json          | .cursor/hooks.json (beforeShellExecution, preToolUse, stop) |
| kimi    | .kimi-code/mcp.json       | user-level only: printed for ~/.kimi-code/config.toml |
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOSTS = ("claude", "copilot", "codex", "gemini", "cursor", "kimi")
SERVER = "agentic-harness"
MARKERS = ("agentic-harness-gate", "gate_hook.py")  # how we recognise our own hook entries

# How each host shows up on a machine or in a repo, for auto-detection.
DETECT = {
    "claude": (["claude"], [".claude", "CLAUDE.md", ".mcp.json"]),
    "copilot": ([], [".vscode", ".github/copilot-instructions.md"]),
    "codex": (["codex"], [".codex"]),
    "gemini": (["gemini"], [".gemini", "GEMINI.md"]),
    "cursor": (["cursor-agent", "cursor"], [".cursor", ".cursorrules"]),
    "kimi": (["kimi"], [".kimi-code"]),
}


# ------------------------------------------------------------- commands
def harness_commands() -> Tuple[List[str], List[str], bool]:
    """(MCP server argv, gate hook argv, portable).

    Installed with pip/pipx/uv, the commands are on PATH and the configs a team
    commits work on every machine. Otherwise absolute paths are used, which only
    work on this machine.
    """
    mcp, gate = shutil.which("agentic-harness-mcp"), shutil.which("agentic-harness-gate")
    if mcp and gate:
        return ["agentic-harness-mcp"], ["agentic-harness-gate"], True
    python = sys.executable or "python3"
    return ([python, os.path.join(REPO_ROOT, "mcp_server.py")],
            [python, os.path.join(REPO_ROOT, "kit", "gate_hook.py")], False)


def _cmdline(argv: List[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def detect_hosts(target: str) -> List[str]:
    found = []
    for host in HOSTS:
        binaries, paths = DETECT[host]
        if any(shutil.which(b) for b in binaries) or any(os.path.exists(os.path.join(target, p)) for p in paths):
            found.append(host)
    return found


# ------------------------------------------------------------- file I/O
class ConfigError(SystemExit):
    pass


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} is not valid JSON ({e}); fix it before running setup.")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return data


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Plan:
    """Collects file changes so --dry-run can report them without writing."""

    def __init__(self, target: str, dry_run: bool):
        self.target, self.dry_run = target, dry_run
        self.messages: List[str] = []

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.target)

    def json_file(self, rel: str, change: Callable[[Dict[str, Any]], List[str]]) -> None:
        path = os.path.join(self.target, rel)
        data = _read_json(path)
        before = json.dumps(data, sort_keys=True)
        notes = change(data)
        if json.dumps(data, sort_keys=True) == before:
            self.messages.append(f"   kept {rel} (already set up)")
            return
        if not self.dry_run:
            _write(path, json.dumps(data, indent=2) + "\n")
        verb = "would update" if self.dry_run else "updated"
        self.messages.append(f"   {verb} {rel}: {', '.join(notes)}")

    def text_file(self, rel: str, change: Callable[[str], Tuple[str, List[str]]]) -> None:
        path = os.path.join(self.target, rel)
        current = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                current = f.read()
        updated, notes = change(current)
        if updated == current:
            self.messages.append(f"   kept {rel} (already set up)")
            return
        if not self.dry_run:
            _write(path, updated)
        verb = "would update" if self.dry_run else "updated"
        self.messages.append(f"   {verb} {rel}: {', '.join(notes)}")


# ------------------------------------------------------- merge helpers
def add_servers(data: Dict[str, Any], key: str, servers: Dict[str, Dict[str, Any]]) -> List[str]:
    table = data.setdefault(key, {})
    added = []
    for name, entry in servers.items():
        if name not in table:
            table[name] = copy.deepcopy(entry)
            added.append(f"MCP server {name}")
    return added


def _is_ours(entry: Any) -> bool:
    return any(m in json.dumps(entry) for m in MARKERS)


def add_hook(data: Dict[str, Any], event: str, group: Dict[str, Any]) -> List[str]:
    """Adds a matcher group to hooks[event] unless one of ours is already there."""
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    if any(_is_ours(g) for g in groups):
        return []
    groups.append(group)
    return [f"{event} hook"]


def has_plan_guard(data: Dict[str, Any]) -> bool:
    return "plan-guard" in json.dumps(data.get("hooks", {}).get("PreToolUse", []))


# ------------------------------------------------------- server entries
def server_entries(mcp_argv: List[str], extra: Dict[str, Dict[str, Any]], workspace_arg: Optional[str] = None
                   ) -> Dict[str, Dict[str, Any]]:
    args = mcp_argv[1:] + (["--workspace", workspace_arg] if workspace_arg else [])
    entries = {SERVER: {"command": mcp_argv[0], **({"args": args} if args else {})}}
    entries.update(extra)
    return entries


def gemini_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    if entry.get("type") == "http":  # Gemini names the streamable-HTTP field httpUrl
        return {k: v for k, v in entry.items() if k not in ("type", "url")} | {"httpUrl": entry["url"]}
    return entry


def toml_server(name: str, entry: Dict[str, Any]) -> str:
    """One [mcp_servers.<name>] table for Codex. JSON strings and arrays are valid TOML."""
    lines = [f"[mcp_servers.{json.dumps(name) if not re.fullmatch(r'[A-Za-z0-9_-]+', name) else name}]"]
    if entry.get("type") == "http" or "url" in entry:
        lines.append(f"url = {json.dumps(entry['url'])}")
    else:
        lines.append(f"command = {json.dumps(entry['command'])}")
        if entry.get("args"):
            lines.append(f"args = {json.dumps(entry['args'])}")
    if entry.get("env"):
        pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in entry["env"].items())
        lines.append(f"env = {{ {pairs} }}")
    return "\n".join(lines) + "\n"


def add_toml_servers(current: str, servers: Dict[str, Dict[str, Any]]) -> Tuple[str, List[str]]:
    added, out = [], current
    for name, entry in servers.items():
        if re.search(rf'^\[mcp_servers\.(?:"{re.escape(name)}"|{re.escape(name)})\]\s*$', current, re.MULTILINE):
            continue
        if out and not out.endswith("\n"):
            out += "\n"
        out += ("\n" if out else "") + toml_server(name, entry)
        added.append(f"MCP server {name}")
    if added:
        try:
            import tomllib  # Python 3.11+: refuse to write a config Codex could not parse
            tomllib.loads(out)
        except ImportError:
            pass
        except Exception as e:  # tomllib.TOMLDecodeError
            raise ConfigError(f".codex/config.toml would not parse after the merge ({e}); fix it first.")
    return out, added


# ------------------------------------------------------------- hosts
def configure(target: str, hosts: List[str], extra_servers: Dict[str, Dict[str, Any]], dry_run: bool
              ) -> List[str]:
    mcp_argv, gate_argv, portable = harness_commands()
    gate = lambda host, event: _cmdline(gate_argv + ["--host", host, event])  # noqa: E731
    plan = Plan(target, dry_run)
    servers = server_entries(mcp_argv, extra_servers)

    if "claude" in hosts or "copilot" in hosts:
        plan.json_file(".mcp.json", lambda d: add_servers(d, "mcpServers", servers))
    if "claude" in hosts:
        def claude_settings(d: Dict[str, Any]) -> List[str]:
            notes = []
            if not has_plan_guard(d):  # the kit's plan-guard already enforces the same gate file
                notes += add_hook(d, "PreToolUse", {"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                                    "hooks": [{"type": "command", "command": gate("claude", "pre"), "timeout": 15}]})
            notes += add_hook(d, "Stop", {"hooks": [{"type": "command", "command": gate("claude", "stop"), "timeout": 120}]})
            return notes
        plan.json_file(os.path.join(".claude", "settings.json"), claude_settings)

    if "codex" in hosts:
        plan.text_file(os.path.join(".codex", "config.toml"), lambda t: add_toml_servers(t, servers))

        def codex_hooks(d: Dict[str, Any]) -> List[str]:
            return (add_hook(d, "PreToolUse", {"matcher": "Bash|apply_patch",
                                                "hooks": [{"type": "command", "command": gate("codex", "pre"), "timeout": 15}]})
                    + add_hook(d, "Stop", {"hooks": [{"type": "command", "command": gate("codex", "stop"), "timeout": 120}]}))
        plan.json_file(os.path.join(".codex", "hooks.json"), codex_hooks)

    if "gemini" in hosts:
        def gemini_settings(d: Dict[str, Any]) -> List[str]:
            notes = add_servers(d, "mcpServers", {n: gemini_entry(e) for n, e in servers.items()})
            notes += add_hook(d, "BeforeTool", {"matcher": "run_shell_command|write_file|replace", "hooks": [
                {"type": "command", "name": "agentic-harness-gate", "command": gate("gemini", "pre"), "timeout": 15000}]})
            notes += add_hook(d, "AfterAgent", {"hooks": [
                {"type": "command", "name": "agentic-harness-review", "command": gate("gemini", "stop"), "timeout": 120000}]})
            context = d.setdefault("context", {})
            if "fileName" not in context:  # Gemini reads GEMINI.md only, unless told otherwise
                context["fileName"] = ["AGENTS.md", "GEMINI.md"]
                notes.append("read AGENTS.md")
            elif "AGENTS.md" not in json.dumps(context["fileName"]):
                plan.messages.append("   note: .gemini/settings.json sets context.fileName without AGENTS.md; "
                                     "add it if Gemini should follow AGENTS.md")
            return notes
        plan.json_file(os.path.join(".gemini", "settings.json"), gemini_settings)

    if "cursor" in hosts:
        cursor_servers = server_entries(mcp_argv, extra_servers, workspace_arg="${workspaceFolder}")
        plan.json_file(os.path.join(".cursor", "mcp.json"), lambda d: add_servers(d, "mcpServers", cursor_servers))

        def cursor_hooks(d: Dict[str, Any]) -> List[str]:
            d.setdefault("version", 1)
            return (add_hook(d, "beforeShellExecution", {"command": gate("cursor", "pre")})
                    + add_hook(d, "preToolUse", {"command": gate("cursor", "pre")})
                    + add_hook(d, "stop", {"command": gate("cursor", "stop")}))
        plan.json_file(os.path.join(".cursor", "hooks.json"), cursor_hooks)

    if "kimi" in hosts:
        plan.json_file(os.path.join(".kimi-code", "mcp.json"), lambda d: add_servers(d, "mcpServers", servers))
        snippet = "\n".join(
            f'   [[hooks]]\n   event = "{event}"\n' + (f'   matcher = "{matcher}"\n' if matcher else "")
            + f"   command = {json.dumps(gate('kimi', kind))}\n   timeout = {timeout}"
            for event, matcher, kind, timeout in (("PreToolUse", "Bash|Write|Edit", "pre", 15), ("Stop", "", "stop", 120)))
        plan.messages.append("   Kimi Code reads hooks only from ~/.kimi-code/config.toml; to add the gate and "
                             "review hooks there, append:\n" + snippet)

    if not portable:
        plan.messages.append("   warning: agentic-harness-mcp / agentic-harness-gate are not on PATH, so these configs "
                             "use absolute paths on this machine. Install the harness (pipx install "
                             "git+https://github.com/PavanSiddapuram/Agentic-Harness) and re-run before committing them.")
    trust = [h for h in hosts if h in ("codex", "gemini", "cursor", "kimi", "claude")]
    if trust and not dry_run:
        plan.messages.append("   each agent asks you to trust the project (or approve its servers) before it "
                             "loads project-level servers and hooks")
    return plan.messages

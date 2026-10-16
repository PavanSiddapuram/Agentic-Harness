#!/usr/bin/env python3
"""Set up a target repo for agent work, and report which tools are installed.

    python kit/setup.py doctor
    python kit/setup.py install /path/to/repo                  # kit + one code-graph server
    python kit/setup.py install /path/to/repo --servers sentry,grafana
    python kit/setup.py install /path/to/repo --dry-run

`install` does two things, both idempotent and non-destructive:
  1. Runs the vendored vjvarada/coding-agent-setup installer in the target
     (Graphify, RTK hook, AGENTS.md contract, plan/build agents, plan-guard).
  2. Merges MCP servers into the target's .mcp.json for Claude Code and other
     MCP hosts: one code-graph server by default, others with --servers.
     Existing entries are never changed.
  3. Adds graphify-out/ to .cbmignore when Graphify is present.

Standard library only. Nothing here is required by the harness itself.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_DIR = os.path.join(HERE, "coding-agent-setup")

# Tools the harness or the kit can use. All optional.
TOOLS: List[Dict[str, str]] = [
    {"id": "rtk", "binary": "rtk", "adds": "Filters noisy command output for 100+ tools (harness uses it automatically)",
     "install": "brew install rtk | winget install rtk-ai.rtk | cargo install --git https://github.com/rtk-ai/rtk"},
    {"id": "code-graph", "binary": "codebase-memory-mcp", "adds": "Call graph for 158 languages: trace_calls, change_impact (harness uses it automatically)",
     "install": "pip install codebase-memory-mcp  (or npm, brew; see github.com/DeusData/codebase-memory-mcp)"},
    {"id": "graphify", "binary": "graphify", "adds": "Knowledge graph of code plus docs, PDFs and notes; /graphify skill",
     "install": "uv tool install graphifyy  (or pipx install graphifyy)"},
    {"id": "serena", "binary": "serena", "adds": "Language-server symbol search and symbol-level edits",
     "install": "uv tool install -p 3.13 serena-agent && serena init"},
    {"id": "review-graph", "binary": "code-review-graph", "adds": "Blast radius and affected tests for PR review",
     "install": "pipx install code-review-graph"},
    {"id": "gitleaks", "binary": "gitleaks", "adds": "Secret detection for 150+ providers in review_change (harness uses it automatically)",
     "install": "brew install gitleaks | github.com/gitleaks/gitleaks/releases"},
    {"id": "semgrep", "binary": "semgrep", "adds": "Semantic security rules in review_change (repo .semgrep.yml, or registry p/default with network)",
     "install": "pipx install semgrep  (or brew install semgrep)"},
    {"id": "lizard", "binary": "lizard", "adds": "Exact complexity for 20+ languages in review_change (Python is exact without it)",
     "install": "pipx install lizard"},
    {"id": "node", "binary": "node", "adds": "Runs the kit's plan-guard owner-gate hook in Claude Code",
     "install": "https://nodejs.org (LTS)"},
    {"id": "uv", "binary": "uv", "adds": "Installer used by Graphify, Serena and the Grafana server",
     "install": "https://docs.astral.sh/uv/getting-started/installation/"},
]

# MCP servers setup can add to a target's .mcp.json.
# By default only ONE code-intelligence server is added: the first of PRIMARY
# that is installed. Every extra server adds tools (and "use me first"
# instructions) the agent must choose between, and a larger tool menu makes
# selection worse. The rest are opt-in with --servers.
SERVERS: Dict[str, Dict] = {
    "code-graph": {
        "requires": "codebase-memory-mcp",
        "entry": {"command": "codebase-memory-mcp"},
    },
    "serena": {
        "requires": "serena",
        "entry": {"command": "serena", "args": ["start-mcp-server", "--context", "claude-code", "--project-from-cwd"]},
    },
    "review-graph": {
        "requires": "code-review-graph",
        "entry": {"command": "code-review-graph", "args": ["serve"]},
    },
    "graphify": {
        "requires": "graphify.serve",  # python module from graphifyy[mcp]
        "entry": {"command": "python", "args": ["-m", "graphify.serve", "graphify-out/graph.json"]},
    },
    "sentry": {
        "requires": None,
        "note": "Production errors, issues and traces. Sign in through OAuth on first use.",
        "entry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"},
    },
    "grafana": {
        "requires": "uvx",
        "note": "Metrics, logs and traces from Grafana, read-only. Set GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN.",
        "entry": {
            "command": "uvx", "args": ["mcp-grafana", "--disable-write"],
            "env": {"GRAFANA_URL": "${GRAFANA_URL}", "GRAFANA_SERVICE_ACCOUNT_TOKEN": "${GRAFANA_SERVICE_ACCOUNT_TOKEN}"},
        },
    },
}


def is_available(requirement: Optional[str]) -> bool:
    if requirement is None:
        return True
    if "." in requirement:  # a python module
        try:
            return importlib.util.find_spec(requirement) is not None
        except (ImportError, ValueError):
            return False
    return shutil.which(requirement) is not None


PRIMARY = ["code-graph", "graphify"]


def choose_servers(requested: Optional[List[str]]) -> List[str]:
    """One primary code-graph server if installed, plus any explicitly requested."""
    unknown = [s for s in (requested or []) if s not in SERVERS]
    if unknown:
        raise SystemExit(f"unknown server(s): {', '.join(unknown)}. Choose from: {', '.join(SERVERS)}")
    chosen = [name for name in PRIMARY if is_available(SERVERS[name]["requires"])][:1]
    for name in requested or []:
        if name not in chosen:
            chosen.append(name)
    return chosen


def merge_mcp(existing: Dict, names: List[str]) -> (Dict, List[str], List[str]):
    """Returns (merged config, added names, kept names). Never changes an existing entry."""
    merged = copy.deepcopy(existing) if existing else {}
    servers = merged.setdefault("mcpServers", {})
    added, kept = [], []
    for name in names:
        if name in servers:
            kept.append(name)
        else:
            servers[name] = copy.deepcopy(SERVERS[name]["entry"])
            added.append(name)
    return merged, added, kept


def read_json(path: str) -> Dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path} is not valid JSON ({e}); fix it before running setup.")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object.")
    return data


def ensure_lines(path: str, lines: List[str], dry_run: bool) -> List[str]:
    """Appends lines missing from a text file. Returns the lines added."""
    existing = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read().splitlines()
    missing = [line for line in lines if line not in existing]
    if missing and not dry_run:
        with open(path, "a", encoding="utf-8") as f:
            if existing and existing[-1] != "":
                f.write("\n")
            f.write("\n".join(missing) + "\n")
    return missing


def exclude_generated_from_graph(target: str, dry_run: bool) -> None:
    """Keeps Graphify's output out of codebase-memory-mcp's index via .cbmignore.

    Not .gitignore: Graphify expects teams may commit graphify-out/graph.json.
    """
    if not (os.path.isdir(os.path.join(target, "graphify-out")) or shutil.which("graphify")):
        return
    added = ensure_lines(os.path.join(target, ".cbmignore"), ["graphify-out/"], dry_run)
    if added:
        print(f"   {'would add' if dry_run else 'added'} graphify-out/ to .cbmignore (keeps it out of the code graph)")


CLAUDE_FILES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"))
IMPORT_RE = re.compile(r"^@(\.\./)?AGENTS\.md\s*$", re.MULTILINE)


def keep_agents_md_loaded(target: str, claude_existed: Dict[str, bool], dry_run: bool) -> None:
    """Keeps Claude Code reading the repo's AGENTS.md after setup.

    Claude Code reads only CLAUDE.md when both CLAUDE.md and AGENTS.md exist.
    The kit's Graphify step creates CLAUDE.md, which would silently stop
    Claude Code from following the team's AGENTS.md. When setup created the
    CLAUDE.md files, add the documented `@AGENTS.md` import; never edit a
    CLAUDE.md the team already had, only warn.
    """
    if not os.path.isfile(os.path.join(target, "AGENTS.md")):
        return
    present = [f for f in CLAUDE_FILES if os.path.isfile(os.path.join(target, f))]
    if not present and not dry_run:
        return
    for rel in present:
        with open(os.path.join(target, rel), "r", encoding="utf-8", errors="replace") as fh:
            if IMPORT_RE.search(fh.read()):
                return
    if any(claude_existed.values()):
        print("   note: Claude Code reads only CLAUDE.md when both CLAUDE.md and AGENTS.md exist. "
              "Add '@AGENTS.md' as the first line of CLAUDE.md if AGENTS.md should apply.")
        return
    root = os.path.join(target, "CLAUDE.md")
    if dry_run:
        print("   would add '@AGENTS.md' to CLAUDE.md if setup creates one (keeps AGENTS.md loaded in Claude Code)")
        return
    existing = ""
    if os.path.isfile(root):
        with open(root, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    with open(root, "w", encoding="utf-8") as fh:
        fh.write("@AGENTS.md\n" + ("\n" + existing if existing else ""))
    print("   added '@AGENTS.md' to CLAUDE.md so Claude Code keeps reading AGENTS.md")


def run_kit_installer(target: str, dry_run: bool) -> int:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        argv = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(KIT_DIR, "install.ps1")]
    else:
        argv = [shutil.which("bash") or "bash", os.path.join(KIT_DIR, "install.sh")]
    print(f"\n== Kit installer (vjvarada/coding-agent-setup) in {target}")
    if dry_run:
        print("   would run: " + " ".join(argv))
        return 0
    if not argv[0]:
        print("   skipped: no bash or PowerShell found")
        return 1
    return subprocess.call(argv, cwd=target)


def doctor() -> None:
    print("Tool status (all optional; the harness runs on the standard library alone)\n")
    width = max(len(t["id"]) for t in TOOLS)
    for tool in TOOLS:
        path = shutil.which(tool["binary"])
        mark = "ok     " if path else "missing"
        print(f"  {tool['id']:<{width}}  {mark}  {tool['adds']}")
        if not path:
            print(f"  {'':<{width}}           install: {tool['install']}")
    off = os.environ.get("HARNESS_NO_EXTERNAL")
    print("\nHarness mode: " + ("stdlib only (HARNESS_NO_EXTERNAL is set)" if off else "uses rtk and codebase-memory-mcp when present"))


def install(target: str, requested: Optional[List[str]], skip_kit: bool, dry_run: bool) -> int:
    target = os.path.realpath(target)
    if not os.path.isdir(target):
        raise SystemExit(f"target is not a directory: {target}")

    claude_existed = {f: os.path.isfile(os.path.join(target, f)) for f in CLAUDE_FILES}
    status = 0 if skip_kit else run_kit_installer(target, dry_run)

    print("\n== Agent instructions")
    keep_agents_md_loaded(target, claude_existed, dry_run)
    exclude_generated_from_graph(target, dry_run)

    mcp_path = os.path.join(target, ".mcp.json")
    names = choose_servers(requested)
    merged, added, kept = merge_mcp(read_json(mcp_path), names)
    print(f"\n== MCP servers in {mcp_path}")
    for name in added:
        note = SERVERS[name].get("note")
        print(f"   {'would add' if dry_run else 'added'}: {name}" + (f"  ({note})" if note else ""))
    for name in kept:
        print(f"   kept existing: {name}")
    skipped = [n for n, s in SERVERS.items() if n not in names]
    if skipped:
        print("   not added: " + ", ".join(skipped)
              + ". One code graph is added by default; add others with --servers name,...")
    if added and not dry_run:
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        print("   Claude Code asks you to approve project servers the first time it sees them.")
    return status


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="show which optional tools are installed")
    inst = sub.add_parser("install", help="set up a target repo")
    inst.add_argument("target")
    inst.add_argument("--servers", default="", help=f"extra MCP servers: {', '.join(SERVERS)}")
    inst.add_argument("--no-kit", action="store_true", help="skip the coding-agent-setup installer")
    inst.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        doctor()
        return 0
    requested = [s.strip() for s in args.servers.split(",") if s.strip()]
    return install(args.target, requested, args.no_kit, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

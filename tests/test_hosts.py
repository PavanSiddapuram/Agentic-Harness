"""Tests for kit/hosts.py: each host's project config gets the harness, safely."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kit import hosts

PORTABLE = (["agentic-harness-mcp"], ["agentic-harness-gate"], True)
LOCAL = ([sys.executable, "/opt/h/mcp_server.py"], [sys.executable, "/opt/h/kit/gate_hook.py"], False)


class HostCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cmds = mock.patch.object(hosts, "harness_commands", return_value=PORTABLE)
        self.cmds.start()

    def tearDown(self):
        self.cmds.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        with open(self.path(rel)) as f:
            return f.read()

    def json(self, rel):
        return json.loads(self.read(rel))

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)) or self.root, exist_ok=True)
        with open(self.path(rel), "w") as f:
            f.write(text)

    def configure(self, *names, extra=None, dry_run=False):
        return hosts.configure(self.root, list(names), extra or {}, dry_run)


class TestEachHost(HostCase):
    def test_claude(self):
        self.configure("claude")
        self.assertEqual(self.json(".mcp.json")["mcpServers"]["agentic-harness"], {"command": "agentic-harness-mcp"})
        settings = self.json(".claude/settings.json")["hooks"]
        self.assertEqual(settings["PreToolUse"][0]["hooks"][0]["command"], "agentic-harness-gate --host claude pre")
        self.assertEqual(settings["Stop"][0]["hooks"][0]["command"], "agentic-harness-gate --host claude stop")

    def test_claude_with_plan_guard_gets_no_second_gate(self):
        self.write(".claude/settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": 'node "${CLAUDE_PROJECT_DIR}/.claude/hooks/plan-guard.mjs"'}]}]}}))
        self.configure("claude")
        settings = self.json(".claude/settings.json")["hooks"]
        self.assertEqual(len(settings["PreToolUse"]), 1)
        self.assertIn("plan-guard", json.dumps(settings["PreToolUse"]))
        self.assertIn("Stop", settings)

    def test_copilot_shares_the_root_mcp_json(self):
        self.configure("copilot")
        self.assertIn("agentic-harness", self.json(".mcp.json")["mcpServers"])
        self.assertFalse(os.path.exists(self.path(".claude/settings.json")))

    def test_codex(self):
        self.write(".codex/config.toml", 'model = "gpt-5"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\n')
        self.configure("codex", extra={"sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"},
                                       "grafana": {"command": "uvx", "args": ["mcp-grafana"], "env": {"A": "${A}"}}})
        text = self.read(".codex/config.toml")
        self.assertTrue(text.startswith('model = "gpt-5"\n\n[mcp_servers.docs]'))
        if sys.version_info >= (3, 11):
            import tomllib
            parsed = tomllib.loads(text)["mcp_servers"]
            self.assertEqual(parsed["agentic-harness"], {"command": "agentic-harness-mcp"})
            self.assertEqual(parsed["sentry"], {"url": "https://mcp.sentry.dev/mcp"})
            self.assertEqual(parsed["grafana"]["env"], {"A": "${A}"})
            self.assertEqual(parsed["docs"], {"command": "docs-mcp"})
        hooks = self.json(".codex/hooks.json")["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|apply_patch")
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["command"], "agentic-harness-gate --host codex stop")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib needed to detect a broken file")
    def test_codex_refuses_to_append_to_broken_toml(self):
        self.write(".codex/config.toml", "model = \n")
        with self.assertRaises(SystemExit):
            self.configure("codex")
        self.assertEqual(self.read(".codex/config.toml"), "model = \n")

    def test_gemini(self):
        self.configure("gemini", extra={"sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"}})
        settings = self.json(".gemini/settings.json")
        self.assertEqual(settings["mcpServers"]["sentry"], {"httpUrl": "https://mcp.sentry.dev/mcp"})
        self.assertEqual(settings["hooks"]["BeforeTool"][0]["matcher"], "run_shell_command|write_file|replace")
        self.assertEqual(settings["hooks"]["AfterAgent"][0]["hooks"][0]["timeout"], 120000)
        self.assertEqual(settings["context"]["fileName"], ["AGENTS.md", "GEMINI.md"])

    def test_gemini_keeps_a_custom_context_file_and_warns(self):
        self.write(".gemini/settings.json", json.dumps({"context": {"fileName": "TEAM.md"}}))
        messages = self.configure("gemini")
        self.assertEqual(self.json(".gemini/settings.json")["context"]["fileName"], "TEAM.md")
        self.assertTrue(any("without AGENTS.md" in m for m in messages))

    def test_cursor(self):
        self.configure("cursor")
        server = self.json(".cursor/mcp.json")["mcpServers"]["agentic-harness"]
        self.assertEqual(server["args"], ["--workspace", "${workspaceFolder}"])
        hooks = self.json(".cursor/hooks.json")
        self.assertEqual(hooks["version"], 1)
        self.assertEqual(hooks["hooks"]["beforeShellExecution"], [{"command": "agentic-harness-gate --host cursor pre"}])
        self.assertEqual(hooks["hooks"]["stop"], [{"command": "agentic-harness-gate --host cursor stop"}])

    def test_kimi_project_server_and_printed_user_hooks(self):
        messages = self.configure("kimi")
        self.assertIn("agentic-harness", self.json(".kimi-code/mcp.json")["mcpServers"])
        snippet = next(m for m in messages if "~/.kimi-code/config.toml" in m)
        self.assertIn('event = "PreToolUse"', snippet)
        self.assertIn("agentic-harness-gate --host kimi stop", snippet)
        self.assertFalse(os.path.exists(os.path.expanduser("~/.kimi-code/config.toml.agentic-harness")))


class TestSafety(HostCase):
    def test_idempotent_and_keeps_existing_entries(self):
        self.write(".cursor/mcp.json", json.dumps({"mcpServers": {"agentic-harness": {"command": "custom"}, "db": {"command": "db"}}}))
        self.configure(*hosts.HOSTS)
        snapshot = {rel: self.read(rel) for rel in (".mcp.json", ".claude/settings.json", ".codex/config.toml",
                                                    ".codex/hooks.json", ".gemini/settings.json", ".cursor/mcp.json",
                                                    ".cursor/hooks.json", ".kimi-code/mcp.json")}
        messages = self.configure(*hosts.HOSTS)
        self.assertEqual(snapshot, {rel: self.read(rel) for rel in snapshot})
        self.assertTrue(all("kept" in m for m in messages if m.strip().startswith(("updated", "kept"))))
        self.assertEqual(self.json(".cursor/mcp.json")["mcpServers"]["agentic-harness"], {"command": "custom"})
        self.assertIn("db", self.json(".cursor/mcp.json")["mcpServers"])

    def test_dry_run_writes_nothing(self):
        messages = self.configure(*hosts.HOSTS, dry_run=True)
        self.assertEqual(os.listdir(self.root), [])
        self.assertTrue(any(m.strip().startswith("would update .codex/config.toml") for m in messages))

    def test_malformed_json_is_refused_and_left_alone(self):
        self.write(".gemini/settings.json", "{oops")
        with self.assertRaises(SystemExit):
            self.configure("gemini")
        self.assertEqual(self.read(".gemini/settings.json"), "{oops")

    def test_unportable_commands_are_flagged(self):
        with mock.patch.object(hosts, "harness_commands", return_value=LOCAL):
            messages = self.configure("codex")
        self.assertIn("/opt/h/kit/gate_hook.py --host codex pre", self.read(".codex/hooks.json"))
        self.assertTrue(any("not on PATH" in m for m in messages))


class TestDetect(HostCase):
    def test_detects_from_repo_files(self):
        os.makedirs(self.path(".codex"))
        self.write("GEMINI.md", "x")
        with mock.patch.object(hosts.shutil, "which", return_value=None):
            self.assertEqual(hosts.detect_hosts(self.root), ["codex", "gemini"])

    def test_detects_from_installed_cli(self):
        with mock.patch.object(hosts.shutil, "which", side_effect=lambda b: "/bin/kimi" if b == "kimi" else None):
            self.assertEqual(hosts.detect_hosts(self.root), ["kimi"])


if __name__ == "__main__":
    unittest.main()

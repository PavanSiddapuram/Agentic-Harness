"""Tests for kit/setup.py: server selection, safe .mcp.json merging, doctor."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kit import setup as kit_setup


class TestMergeMcp(unittest.TestCase):
    def test_adds_missing_and_never_overwrites(self):
        existing = {"mcpServers": {"code-graph": {"command": "my-custom-build"}}, "other": 1}
        merged, added, kept = kit_setup.merge_mcp(existing, ["code-graph", "sentry"])
        self.assertEqual(merged["mcpServers"]["code-graph"], {"command": "my-custom-build"})
        self.assertEqual(merged["mcpServers"]["sentry"]["url"], "https://mcp.sentry.dev/mcp")
        self.assertEqual(merged["other"], 1)
        self.assertEqual((added, kept), (["sentry"], ["code-graph"]))
        self.assertNotIn("sentry", existing["mcpServers"])  # input not mutated

    def test_grafana_is_read_only_and_uses_env_placeholders(self):
        merged, _, _ = kit_setup.merge_mcp({}, ["grafana"])
        entry = merged["mcpServers"]["grafana"]
        self.assertIn("--disable-write", entry["args"])
        self.assertEqual(entry["env"]["GRAFANA_URL"], "${GRAFANA_URL}")


class TestChooseServers(unittest.TestCase):
    def test_only_installed_local_servers_by_default(self):
        with mock.patch.object(kit_setup, "is_available", side_effect=lambda r: r == "codebase-memory-mcp"):
            self.assertEqual(kit_setup.choose_servers([]), ["code-graph"])

    def test_credentialed_servers_only_when_requested(self):
        with mock.patch.object(kit_setup, "is_available", return_value=True):
            default = kit_setup.choose_servers([])
            self.assertNotIn("sentry", default)
            self.assertNotIn("grafana", default)
            self.assertIn("sentry", kit_setup.choose_servers(["sentry"]))

    def test_unknown_server_is_an_error(self):
        with self.assertRaises(SystemExit):
            kit_setup.choose_servers(["not-a-server"])


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp()
        self.mcp = os.path.join(self.target, ".mcp.json")
        self.avail = mock.patch.object(kit_setup, "is_available", return_value=False)
        self.avail.start()

    def tearDown(self):
        self.avail.stop()
        shutil.rmtree(self.target, ignore_errors=True)

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = kit_setup.main(list(args))
        return code, out.getvalue()

    def test_dry_run_changes_nothing(self):
        code, out = self.run_main("install", self.target, "--servers", "sentry", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would run:", out)
        self.assertIn("would add: sentry", out)
        self.assertEqual(os.listdir(self.target), [])

    def test_writes_requested_servers_without_kit(self):
        self.run_main("install", self.target, "--servers", "sentry", "--no-kit")
        with open(self.mcp) as f:
            self.assertIn("sentry", json.load(f)["mcpServers"])
        _, out = self.run_main("install", self.target, "--servers", "sentry", "--no-kit")
        self.assertIn("kept existing: sentry", out)

    def test_invalid_existing_config_is_not_touched(self):
        with open(self.mcp, "w") as f:
            f.write("{broken")
        with self.assertRaises(SystemExit):
            self.run_main("install", self.target, "--servers", "sentry", "--no-kit")
        with open(self.mcp) as f:
            self.assertEqual(f.read(), "{broken")

    def test_missing_target(self):
        with self.assertRaises(SystemExit):
            self.run_main("install", os.path.join(self.target, "nope"), "--no-kit")


class TestDoctor(unittest.TestCase):
    def test_lists_every_tool(self):
        out = io.StringIO()
        with redirect_stdout(out):
            kit_setup.main(["doctor"])
        for tool in kit_setup.TOOLS:
            self.assertIn(tool["id"], out.getvalue())

    def test_vendored_kit_is_present(self):
        for rel in ["install.sh", "install.ps1", "LICENSE", "templates/claude/hooks/plan-guard.mjs", "UPSTREAM.md"]:
            self.assertTrue(os.path.isfile(os.path.join(kit_setup.KIT_DIR, rel)), rel)


if __name__ == "__main__":
    unittest.main()

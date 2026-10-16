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

    def test_one_code_graph_by_default_even_when_all_installed(self):
        with mock.patch.object(kit_setup, "is_available", return_value=True):
            self.assertEqual(kit_setup.choose_servers([]), ["code-graph"])

    def test_graphify_is_the_fallback_primary(self):
        with mock.patch.object(kit_setup, "is_available", side_effect=lambda r: r == "graphify.serve"):
            self.assertEqual(kit_setup.choose_servers([]), ["graphify"])

    def test_extra_servers_are_opt_in(self):
        with mock.patch.object(kit_setup, "is_available", return_value=True):
            self.assertEqual(kit_setup.choose_servers(["serena", "code-graph"]), ["code-graph", "serena"])

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

    def test_cbmignore_added_once_when_graphify_output_exists(self):
        os.makedirs(os.path.join(self.target, "graphify-out"))
        with open(os.path.join(self.target, ".cbmignore"), "w") as f:
            f.write("fixtures/")  # existing entry, no trailing newline
        self.run_main("install", self.target, "--no-kit", "--dry-run")
        with open(os.path.join(self.target, ".cbmignore")) as f:
            self.assertEqual(f.read(), "fixtures/")  # dry run changed nothing
        self.run_main("install", self.target, "--no-kit")
        self.run_main("install", self.target, "--no-kit")
        with open(os.path.join(self.target, ".cbmignore")) as f:
            self.assertEqual(f.read(), "fixtures/\ngraphify-out/\n")
        self.assertFalse(os.path.exists(os.path.join(self.target, ".gitignore")))

    def test_no_cbmignore_without_graphify(self):
        with mock.patch.object(kit_setup.shutil, "which", return_value=None):
            self.run_main("install", self.target, "--no-kit")
        self.assertFalse(os.path.exists(os.path.join(self.target, ".cbmignore")))

    def simulate_installer(self, creates):
        """Stand-in for the kit installer: creates the given files."""
        def fake(target, dry_run):
            for rel, text in creates.items():
                os.makedirs(os.path.dirname(os.path.join(target, rel)) or target, exist_ok=True)
                with open(os.path.join(target, rel), "w") as f:
                    f.write(text)
            return 0
        return mock.patch.object(kit_setup, "run_kit_installer", side_effect=fake)

    def read(self, rel):
        with open(os.path.join(self.target, rel)) as f:
            return f.read()

    def test_import_added_when_setup_creates_claude_md_next_to_agents_md(self):
        with open(os.path.join(self.target, "AGENTS.md"), "w") as f:
            f.write("Run make check before committing.\n")
        with self.simulate_installer({"CLAUDE.md": "## graphify\nrules\n", ".claude/CLAUDE.md": "# graphify skill\n"}):
            _, out = self.run_main("install", self.target)
        self.assertTrue(self.read("CLAUDE.md").startswith("@AGENTS.md\n\n## graphify"))
        self.assertIn("keeps reading AGENTS.md", out)
        with self.simulate_installer({}):
            self.run_main("install", self.target)  # idempotent
        self.assertEqual(self.read("CLAUDE.md").count("@AGENTS.md"), 1)

    def test_import_added_when_only_dot_claude_file_is_created(self):
        with open(os.path.join(self.target, "AGENTS.md"), "w") as f:
            f.write("rules\n")
        with self.simulate_installer({".claude/CLAUDE.md": "# graphify skill\n"}):
            self.run_main("install", self.target)
        self.assertEqual(self.read("CLAUDE.md"), "@AGENTS.md\n")

    def test_teams_own_claude_md_is_never_edited(self):
        for rel, text in {"AGENTS.md": "rules\n", "CLAUDE.md": "our own notes\n"}.items():
            with open(os.path.join(self.target, rel), "w") as f:
                f.write(text)
        with self.simulate_installer({}):
            _, out = self.run_main("install", self.target)
        self.assertEqual(self.read("CLAUDE.md"), "our own notes\n")
        self.assertIn("Add '@AGENTS.md' as the first line", out)

    def test_existing_import_or_no_agents_md_means_no_change(self):
        with self.simulate_installer({"CLAUDE.md": "## graphify\n"}):
            _, out = self.run_main("install", self.target)
        self.assertEqual(self.read("CLAUDE.md"), "## graphify\n")  # no AGENTS.md: nothing to keep loaded
        self.assertNotIn("AGENTS.md", out)

    def test_dry_run_does_not_touch_instruction_files(self):
        with open(os.path.join(self.target, "AGENTS.md"), "w") as f:
            f.write("rules\n")
        _, out = self.run_main("install", self.target, "--dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.target, "CLAUDE.md")))
        self.assertIn("would add '@AGENTS.md'", out)

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

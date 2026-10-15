"""Tests for loading a target project's agent instructions."""

import os
import shutil
import tempfile
import unittest

from core.memory import MemoryEngine, SpillCache
from core.project_context import build_project_instructions


class TestProjectContext(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _write(self, rel, text):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_empty_project_gives_no_block(self):
        self.assertEqual(build_project_instructions(self.ws), "")

    def test_agents_md_is_inlined_and_preferred(self):
        self._write("AGENTS.md", "Use pnpm, never npm.")
        self._write("CLAUDE.md", "Ignored when AGENTS.md exists.")
        block = build_project_instructions(self.ws)
        self.assertIn("Use pnpm, never npm.", block)
        self.assertNotIn("Ignored", block)

    def test_claude_md_import_line_is_expanded(self):
        self._write("CLAUDE.md", "@docs/rules.md")
        self._write("docs/rules.md", "Run make test before committing.")
        block = build_project_instructions(self.ws)
        self.assertIn("Run make test before committing.", block)

    def test_import_cannot_escape_workspace(self):
        outside = tempfile.mkdtemp()
        try:
            with open(os.path.join(outside, "secret.md"), "w") as f:
                f.write("SECRET")
            rel = os.path.relpath(os.path.join(outside, "secret.md"), self.ws)
            self._write("CLAUDE.md", f"@{rel}")
            self.assertNotIn("SECRET", build_project_instructions(self.ws))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_subdirectory_contracts_listed_not_inlined(self):
        self._write("AGENTS.md", "root rules")
        self._write("services/billing/AGENTS.md", "billing internals")
        self._write("node_modules/pkg/AGENTS.md", "should be skipped")
        block = build_project_instructions(self.ws)
        self.assertIn(os.path.join("services", "billing", "AGENTS.md"), block)
        self.assertNotIn("billing internals", block)
        self.assertNotIn("node_modules", block)

    def test_long_instructions_are_truncated(self):
        self._write("AGENTS.md", "x" * 20000)
        block = build_project_instructions(self.ws, max_chars=1000)
        self.assertLess(len(block), 1500)
        self.assertIn("truncated", block)

    def test_instructions_join_the_stable_system_prompt(self):
        memory = MemoryEngine(
            spill_cache=SpillCache(os.path.join(self.ws, "spill")),
            project_instructions="[PROJECT INSTRUCTIONS] use tabs",
        )
        memory.add_user_message("task")
        first = memory.compile_context()[0]
        self.assertEqual(first["role"], "system")
        self.assertIn("use tabs", first["content"])


if __name__ == "__main__":
    unittest.main()

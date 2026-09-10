"""Unit tests for the targeted patch engine."""

import os
import shutil
import tempfile
import unittest

from core.tools.fs_tools import SafePathResolver
from core.tools.patcher import PatchFileTool, apply_targeted_patch


class TestPatcher(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.file_path = os.path.join(self.test_dir, "calc.py")
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write(
                "def add(a, b):\n"
                "    return a - b\n"
                "\n"
                "def multiply(a, b):\n"
                "    return a * b\n"
            )
        self.resolver = SafePathResolver(self.test_dir)
        self.patch_tool = PatchFileTool(self.resolver)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_successful_targeted_patch(self):
        result = self.patch_tool.execute({
            "filepath": "calc.py",
            "search_block": "def add(a, b):\n    return a - b",
            "replace_block": "def add(a, b):\n    return a + b",
        })
        self.assertTrue(result.success)
        self.assertIn("calc.py", result.modified_files)

        with open(self.file_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("return a + b", content)
        self.assertIn("def multiply(a, b):", content)

    def test_missing_search_block_fails(self):
        result = self.patch_tool.execute({
            "filepath": "calc.py",
            "search_block": "def divide(a, b):",
            "replace_block": "def divide(a, b): return a / b",
        })
        self.assertFalse(result.success)
        self.assertIn("Patch target block not found", result.output)

    def test_ambiguous_search_block_fails(self):
        # Add duplicate lines to calc.py
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write("\n# Duplicate\n    return a * b\n")

        result = self.patch_tool.execute({
            "filepath": "calc.py",
            "search_block": "    return a * b",
            "replace_block": "    return a ** b",
        })
        self.assertFalse(result.success)
        self.assertIn("Ambiguous patch target", result.output)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for filesystem tools and shell runner."""

import os
import shutil
import tempfile
import unittest

from core.models import ToolCallRequest
from core.tools.base import ToolDispatcher
from core.tools.fs_tools import (
    CreateFileTool,
    ListDirTool,
    ReadFileTool,
    SafePathResolver,
    SearchDirectoryTool,
)
from core.tools.shell import ExecuteCommandTool


class TestTools(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.resolver = SafePathResolver(self.test_dir)
        self.dispatcher = ToolDispatcher()

        self.dispatcher.register(ReadFileTool(self.resolver))
        self.dispatcher.register(CreateFileTool(self.resolver))
        self.dispatcher.register(ListDirTool(self.resolver))
        self.dispatcher.register(SearchDirectoryTool(self.resolver))
        self.dispatcher.register(ExecuteCommandTool(self.resolver, default_timeout=5))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_path_jail_prevents_directory_traversal(self):
        with self.assertRaises(PermissionError):
            self.resolver.resolve("../../sensitive.txt")

        with self.assertRaises(PermissionError):
            self.resolver.resolve("../outside_jail")

    def test_create_and_read_file(self):
        # Create
        create_res = self.dispatcher.execute(
            ToolCallRequest(
                call_id="1",
                tool_name="create_file",
                parameters={"filepath": "sub/hello.txt", "content": "Line 1\nLine 2\nLine 3"},
            )
        )
        self.assertTrue(create_res.success)

        # Read
        read_res = self.dispatcher.execute(
            ToolCallRequest(
                call_id="2",
                tool_name="read_file",
                parameters={"filepath": "sub/hello.txt", "start_line": 2, "end_line": 2},
            )
        )
        self.assertTrue(read_res.success)
        self.assertIn("Line 2", read_res.output)
        self.assertNotIn("Line 1", read_res.output)

    def test_search_directory(self):
        # Create test files
        self.dispatcher.execute(
            ToolCallRequest(
                call_id="c1",
                tool_name="create_file",
                parameters={"filepath": "a.py", "content": "def find_me(): pass"},
            )
        )
        self.dispatcher.execute(
            ToolCallRequest(
                call_id="c2",
                tool_name="create_file",
                parameters={"filepath": "b.py", "content": "def ignore(): pass"},
            )
        )

        search_res = self.dispatcher.execute(
            ToolCallRequest(
                call_id="s1",
                tool_name="search_directory",
                parameters={"query": "find_me"},
            )
        )
        self.assertTrue(search_res.success)
        self.assertIn("a.py:1: def find_me(): pass", search_res.output)
        self.assertNotIn("b.py", search_res.output)

    def test_execute_command(self):
        cmd_res = self.dispatcher.execute(
            ToolCallRequest(
                call_id="sh1",
                tool_name="execute_command",
                parameters={"command": "python -c \"print('Hello from shell')\""},
            )
        )
        self.assertTrue(cmd_res.success)
        self.assertIn("Exit code: 0", cmd_res.output)
        self.assertIn("Hello from shell", cmd_res.output)


if __name__ == "__main__":
    unittest.main()

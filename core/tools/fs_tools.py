"""Filesystem tools with strict path jailing and canonicalization.

Prevents path traversal attacks (e.g. ../../) and confines operations
strictly to the designated workspace root.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, List

from core.models import ToolExecutionResult
from core.tools.base import BaseTool


class SafePathResolver:
    """Canonical path validator that confines operations to the repository root."""

    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.realpath(workspace_root)

    def resolve(self, requested_path: str) -> str:
        """Converts a relative or absolute path to a safe, canonical absolute path.
        
        Raises PermissionError if the path breaks out of the workspace jail.
        """
        # Join relative to workspace if not already absolute
        if not os.path.isabs(requested_path):
            full_path = os.path.join(self.workspace_root, requested_path)
        else:
            full_path = requested_path

        canonical = os.path.realpath(full_path)

        try:
            common = os.path.commonpath([self.workspace_root, canonical])
        except ValueError:
            # Different drive letters on Windows
            raise PermissionError(
                f"PATH_TRAVERSAL_DETECTED: Target path '{requested_path}' resides on a different drive."
            )

        if common != self.workspace_root:
            raise PermissionError(
                f"PATH_TRAVERSAL_DETECTED: Target path '{requested_path}' escapes workspace jail '{self.workspace_root}'."
            )

        return canonical


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read lines from a file within the workspace with line numbering."

    def __init__(self, path_resolver: SafePathResolver):
        self.resolver = path_resolver

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "Relative path to file from workspace root",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Optional 1-indexed starting line number",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Optional 1-indexed ending line number (inclusive)",
                        },
                    },
                    "required": ["filepath"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        filepath_arg = parameters.get("filepath", "")
        start_line = parameters.get("start_line", 1)
        end_line = parameters.get("end_line")

        try:
            safe_path = self.resolver.resolve(filepath_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        if not os.path.exists(safe_path):
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"FILE_NOT_FOUND: '{filepath_arg}' does not exist.",
            )

        if os.path.isdir(safe_path):
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"IS_A_DIRECTORY: '{filepath_arg}' is a directory, not a file. Use 'list_dir' instead.",
            )

        try:
            with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, end_line) if end_line else total_lines

            numbered_lines = [
                f"{i + 1:4d} | {lines[i]}" for i in range(start_idx, end_idx)
            ]
            output = "".join(numbered_lines)
            if not output:
                output = "(empty file or line range)"

            return ToolExecutionResult(call_id="", success=True, output=output)
        except Exception as e:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"READ_ERROR: {type(e).__name__}: {str(e)}",
            )


class CreateFileTool(BaseTool):
    name = "create_file"
    description = "Create a new file with specified content or overwrite an existing file."

    def __init__(self, path_resolver: SafePathResolver):
        self.resolver = path_resolver

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "Relative path to file from workspace root",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text content to write to the file",
                        },
                    },
                    "required": ["filepath", "content"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        filepath_arg = parameters.get("filepath", "")
        content = parameters.get("content", "")

        try:
            safe_path = self.resolver.resolve(filepath_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        try:
            os.makedirs(os.path.dirname(safe_path), exist_ok=True)
            with open(safe_path, "w", encoding="utf-8") as f:
                f.write(content)
            return ToolExecutionResult(
                call_id="",
                success=True,
                output=f"Successfully wrote {len(content)} characters to '{filepath_arg}'.",
                modified_files=[filepath_arg],
            )
        except Exception as e:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"WRITE_ERROR: {type(e).__name__}: {str(e)}",
            )


class ListDirTool(BaseTool):
    name = "list_dir"
    description = "List files and subdirectories within a directory in the workspace."

    def __init__(self, path_resolver: SafePathResolver):
        self.resolver = path_resolver

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory_path": {
                            "type": "string",
                            "description": "Relative directory path (use '.' or empty string for root)",
                        }
                    },
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        dir_arg = parameters.get("directory_path", ".") or "."
        try:
            safe_path = self.resolver.resolve(dir_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        if not os.path.exists(safe_path):
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"DIRECTORY_NOT_FOUND: '{dir_arg}' does not exist.",
            )

        if not os.path.isdir(safe_path):
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"NOT_A_DIRECTORY: '{dir_arg}' is a file.",
            )

        try:
            entries = os.listdir(safe_path)
            lines = []
            for entry in sorted(entries):
                full_entry = os.path.join(safe_path, entry)
                kind = "DIR " if os.path.isdir(full_entry) else "FILE"
                lines.append(f"[{kind}] {entry}")
            output = "\n".join(lines) if lines else "(empty directory)"
            return ToolExecutionResult(call_id="", success=True, output=output)
        except Exception as e:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"LIST_ERROR: {type(e).__name__}: {str(e)}",
            )


class SearchDirectoryTool(BaseTool):
    name = "search_directory"
    description = "Search for a text pattern or regex across files in the workspace."

    def __init__(self, path_resolver: SafePathResolver):
        self.resolver = path_resolver

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text substring or regex pattern to search for",
                        },
                        "file_glob": {
                            "type": "string",
                            "description": "Optional file pattern to filter by (e.g. '*.py')",
                        },
                        "directory_path": {
                            "type": "string",
                            "description": "Optional sub-directory to search within",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        query = parameters.get("query", "")
        file_glob = parameters.get("file_glob", "*")
        dir_arg = parameters.get("directory_path", ".") or "."

        try:
            search_root = self.resolver.resolve(dir_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        try:
            regex = re.compile(query)
        except re.error as err:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"INVALID_REGEX: '{query}' is not a valid regular expression: {err}",
            )

        matches: List[str] = []
        max_matches = 50

        for root, dirs, files in os.walk(search_root):
            # Exclude common noisy directories
            dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", ".venv", "node_modules", ".harness"]]
            for file in files:
                if fnmatch.fnmatch(file, file_glob):
                    filepath = os.path.join(root, file)
                    rel_path = os.path.relpath(filepath, self.resolver.workspace_root)
                    try:
                        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                            for lineno, line in enumerate(f, 1):
                                if regex.search(line):
                                    matches.append(f"{rel_path}:{lineno}: {line.strip()}")
                                    if len(matches) >= max_matches:
                                        break
                    except Exception:
                        continue
                if len(matches) >= max_matches:
                    break
            if len(matches) >= max_matches:
                break

        if not matches:
            return ToolExecutionResult(
                call_id="",
                success=True,
                output=f"No matches found for pattern '{query}'.",
            )

        output = "\n".join(matches)
        if len(matches) >= max_matches:
            output += f"\n[Search capped at {max_matches} matches]"
        return ToolExecutionResult(call_id="", success=True, output=output)

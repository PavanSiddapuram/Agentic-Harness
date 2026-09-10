"""Targeted Search & Replace patch engine.

Enforces zero blind rewrites and exact string match verification.
Prevents line-shift drift and ambiguous replacements.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from core.models import ToolExecutionResult
from core.tools.base import BaseTool
from core.tools.fs_tools import SafePathResolver


def apply_targeted_patch(filepath: str, search_block: str, replace_block: str) -> None:
    """Applies an exact search-and-replace block to a file.
    
    Raises ValueError if block is missing or ambiguous (matches multiple times).
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Target file '{filepath}' does not exist.")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    occurrences = content.count(search_block)

    if occurrences == 0:
        raise ValueError(
            "Patch target block not found in file. Ensure that whitespace, indentation, "
            "and line breaks match the source file exactly."
        )

    if occurrences > 1:
        raise ValueError(
            f"Ambiguous patch target: Block appears {occurrences} times in file. "
            "Provide more surrounding context lines to identify a unique target."
        )

    updated_content = content.replace(search_block, replace_block, 1)

    # Temporary file write + atomic replace to avoid corrupted files on interrupt
    temp_path = filepath + ".patch_tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(updated_content)

    os.replace(temp_path, filepath)


class PatchFileTool(BaseTool):
    name = "patch_file"
    description = (
        "Apply a targeted search-and-replace patch block to an existing file. "
        "The search block must match uniquely and exactly."
    )

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
                        "search_block": {
                            "type": "string",
                            "description": "Exact text block in the file to find and replace",
                        },
                        "replace_block": {
                            "type": "string",
                            "description": "New text block to insert in place of search_block",
                        },
                    },
                    "required": ["filepath", "search_block", "replace_block"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        filepath_arg = parameters.get("filepath", "")
        search_block = parameters.get("search_block", "")
        replace_block = parameters.get("replace_block", "")

        try:
            safe_path = self.resolver.resolve(filepath_arg)
        except PermissionError as pe:
            return ToolExecutionResult(call_id="", success=False, output=str(pe))

        try:
            apply_targeted_patch(safe_path, search_block, replace_block)
            return ToolExecutionResult(
                call_id="",
                success=True,
                output=f"Successfully applied targeted patch to '{filepath_arg}'.",
                modified_files=[filepath_arg],
            )
        except Exception as e:
            return ToolExecutionResult(
                call_id="",
                success=False,
                output=f"PATCH_FAILED: {type(e).__name__}: {str(e)}",
            )

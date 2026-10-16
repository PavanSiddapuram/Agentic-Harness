"""Budgeted Memory Engine with 3-tier sliding context and spill cache.

Prevents context window overflows and breaks quadratic token growth
using deterministic compaction and disk-offloading.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.models import PlanStep


def estimate_tokens(text: str) -> int:
    """Estimates tokens defensively for code and JSON (~3.2 characters per token)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.2))


class SpillCache:
    """Offloads massive tool payloads to ephemeral disk logs to protect context window."""

    def __init__(self, spill_dir: Optional[str] = None):
        self.spill_dir = spill_dir or os.path.join(".harness", "spill")

    def process_output(
        self, call_id: str, raw_output: str, max_chars: int = 5000, head_lines: int = 25, tail_lines: int = 25
    ) -> str:
        """If raw_output exceeds max_chars, caches full output to disk and returns head/tail snippet."""
        if len(raw_output) <= max_chars:
            return raw_output

        os.makedirs(self.spill_dir, exist_ok=True)
        spill_file = os.path.join(self.spill_dir, f"{call_id}.log")

        with open(spill_file, "w", encoding="utf-8", errors="replace") as f:
            f.write(raw_output)

        lines = raw_output.splitlines()
        total_lines = len(lines)

        if total_lines <= (head_lines + tail_lines):
            # Short lines but very long single line
            truncated = raw_output[:max_chars]
            return f"{truncated}\n... [OUTPUT TRUNCATED. Full log cached at: {spill_file}]"

        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - (head_lines + tail_lines)

        return (
            f"[OUTPUT TRUNCATED: {total_lines} lines total. Showing first {head_lines} and last {tail_lines} lines.]\n"
            f"{head}\n"
            f"\n... [{omitted} lines omitted] ...\n\n"
            f"{tail}\n"
            f"\n[FULL OUTPUT CACHED AT: {spill_file}]"
        )


class MemoryEngine:
    """Manages system directives, sliding conversation turns, and context compilation."""

    DEFAULT_SYSTEM_PROMPT = (
        "You are an expert autonomous software engineer operating inside a deterministic harness.\n"
        "You have access to tools for reading files, applying targeted search-and-replace patches, "
        "and executing shell commands.\n"
        "Rules:\n"
        "0. Locate code with 'map_repository' and 'find_symbol' before reading files; read only what they point to.\n"
        "1. Never rewrite entire files blindly. Use 'patch_file' with exact search and replace blocks.\n"
        "2. Ensure syntax is valid before proceeding.\n"
        "3. Run tests using 'execute_command' to verify your changes.\n"
        "4. Before finishing a change, run 'review_change' and fix high-severity findings.\n"
        "5. When a task is fully accomplished, reply with a concise final summary and no tool calls."
    )

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        max_sliding_turns: int = 8,
        spill_cache: Optional[SpillCache] = None,
        project_instructions: str = "",
    ):
        self.system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        # Project instructions join the stable system block (not the per-turn
        # anchor) so the prefix stays identical across turns for prompt caching.
        if project_instructions:
            self.system_prompt = f"{self.system_prompt}\n\n{project_instructions}"
        self.max_sliding_turns = max_sliding_turns
        self.spill_cache = spill_cache or SpillCache()
        self.messages: List[Dict[str, Any]] = []

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: Optional[str], tool_calls: Optional[List[Dict[str, Any]]] = None) -> None:
        msg: Dict[str, Any] = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, call_id: str, success: bool, output: str) -> None:
        """Formats and registers tool output, applying spill truncation if necessary."""
        processed_output = self.spill_cache.process_output(call_id, output)
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": processed_output,
            }
        )

    def compile_context(
        self,
        plan: Optional[List[PlanStep]] = None,
        dirty_files: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Compiles the 3-tier context:
        - Tier 1: Fixed System Prompt
        - Tier 2: Dynamic Planner State & Workspace Tracker (injected at head of working context)
        - Tier 3: Sliding buffer of recent messages
        """
        # Build Tier 2 Anchor
        anchor_lines = []
        if plan:
            anchor_lines.append("[ACTIVE EXECUTION PLAN]")
            for step in plan:
                status_icon = (
                    "✓" if step.status.value == "COMPLETED" else "⏳" if step.status.value == "IN_PROGRESS" else " "
                )
                summary_part = f" - {step.result_summary}" if step.result_summary else ""
                anchor_lines.append(f"[{status_icon}] Step {step.id}: {step.instruction} ({step.status.value}){summary_part}")

        if dirty_files:
            anchor_lines.append("\n[MODIFIED WORKSPACE FILES]")
            for f in dirty_files:
                anchor_lines.append(f"- {f}")

        tier2_block = "\n".join(anchor_lines) if anchor_lines else ""

        # Tier 3 Sliding window (keep initial user prompt + last K messages)
        if len(self.messages) <= self.max_sliding_turns:
            sliding_buffer = list(self.messages)
        else:
            # Always preserve the initial prompt (turn 0)
            initial_prompt = self.messages[0] if self.messages else None
            recent_turns = self.messages[-(self.max_sliding_turns - 1):]
            # A 'tool' message must follow the assistant message that issued its
            # tool_calls; providers reject an orphaned one. Drop leading tool
            # results whose parent assistant turn fell outside the window.
            while recent_turns and recent_turns[0].get("role") == "tool":
                recent_turns = recent_turns[1:]
            if initial_prompt and initial_prompt not in recent_turns:
                sliding_buffer = [initial_prompt] + recent_turns
            else:
                sliding_buffer = recent_turns

        compiled_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]

        if tier2_block:
            compiled_messages.append(
                {"role": "system", "content": f"[HARNESS CONTEXT ANCHOR]\n{tier2_block}"}
            )

        compiled_messages.extend(sliding_buffer)
        return compiled_messages

"""Unit tests for the Budgeted Memory Engine and Spill Cache."""

import os
import shutil
import tempfile
import unittest

from core.memory import MemoryEngine, SpillCache, estimate_tokens
from core.models import PlanStep, TaskStatus


class TestMemoryEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.spill_dir = os.path.join(self.test_dir, "spill")
        self.spill_cache = SpillCache(self.spill_dir)
        self.memory = MemoryEngine(max_sliding_turns=4, spill_cache=self.spill_cache)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_token_estimation(self):
        self.assertEqual(estimate_tokens(""), 0)
        sample = "def hello_world(): return 42"  # 28 chars
        est = estimate_tokens(sample)
        self.assertGreaterEqual(est, 5)
        self.assertLessEqual(est, 15)

    def test_spill_cache_offloading(self):
        # Small output -> no spill
        small_output = "Tests passed: 2 OK"
        res = self.spill_cache.process_output("call_1", small_output, max_chars=100)
        self.assertEqual(res, small_output)
        self.assertFalse(os.path.exists(os.path.join(self.spill_dir, "call_1.log")))

        # Huge output -> spill to file and truncate
        huge_lines = [f"Log line {i}: test output data" for i in range(200)]
        huge_output = "\n".join(huge_lines)

        res2 = self.spill_cache.process_output("call_huge", huge_output, max_chars=200)
        self.assertIn("[OUTPUT TRUNCATED: 200 lines total.", res2)
        self.assertIn("FULL OUTPUT CACHED AT:", res2)

        spill_file = os.path.join(self.spill_dir, "call_huge.log")
        self.assertTrue(os.path.exists(spill_file))
        with open(spill_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), huge_output)

    def test_context_compilation_with_planner_state(self):
        self.memory.add_user_message("Refactor the parser")
        plan = [
            PlanStep(id=1, instruction="Read parser.py", status=TaskStatus.COMPLETED),
            PlanStep(id=2, instruction="Patch AST", status=TaskStatus.IN_PROGRESS),
        ]
        dirty = ["src/parser.py"]

        context = self.memory.compile_context(plan=plan, dirty_files=dirty)

        # First message is System
        self.assertEqual(context[0]["role"], "system")

        # Second message is Harness Context Anchor (Tier 2)
        self.assertEqual(context[1]["role"], "system")
        self.assertIn("[ACTIVE EXECUTION PLAN]", context[1]["content"])
        self.assertIn("Read parser.py", context[1]["content"])
        self.assertIn("[MODIFIED WORKSPACE FILES]", context[1]["content"])
        self.assertIn("src/parser.py", context[1]["content"])

        # Subsequent is User message
        self.assertEqual(context[2]["role"], "user")
        self.assertEqual(context[2]["content"], "Refactor the parser")

    def test_sliding_window_preserves_initial_turn(self):
        # Add 1 initial turn and 10 intermediate turns
        self.memory.add_user_message("INITIAL GOAL: Build web scraper")
        for i in range(10):
            self.memory.add_assistant_message(f"Assistant turn {i}")
            self.memory.add_tool_result(f"call_{i}", True, f"Output {i}")

        context = self.memory.compile_context()
        # Find messages in Tier 3
        # System prompt is index 0
        messages = [m for m in context if m["role"] != "system"]

        # Initial prompt must still be present!
        self.assertEqual(messages[0]["content"], "INITIAL GOAL: Build web scraper")
        # Total messages should be constrained by sliding turns limit
        self.assertLessEqual(len(messages), self.memory.max_sliding_turns)


if __name__ == "__main__":
    unittest.main()

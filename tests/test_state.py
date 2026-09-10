"""Unit tests for state management, serialization, and budgeting trip-wires."""

import os
import shutil
import tempfile
import unittest

from core.models import (
    ExecutionState,
    PlanStep,
    TaskStatus,
    ToolCallRequest,
    ToolExecutionResult,
)
from core.state import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.session_file = os.path.join(self.test_dir, "session.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_model_serialization_roundtrip(self):
        step = PlanStep(id=1, instruction="Read code", status=TaskStatus.IN_PROGRESS)
        state = ExecutionState(
            session_id="test_sess",
            current_turn=3,
            max_turns=10,
            tokens_consumed=1500,
            max_token_budget=50000,
            plan=[step],
            dirty_files=["foo.py"],
        )

        data = state.to_dict()
        restored = ExecutionState.from_dict(data)

        self.assertEqual(restored.session_id, "test_sess")
        self.assertEqual(restored.current_turn, 3)
        self.assertEqual(len(restored.plan), 1)
        self.assertEqual(restored.plan[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(restored.dirty_files, ["foo.py"])

    def test_state_persistence_and_hydration(self):
        store = StateStore.create_new(
            session_id="persist_test",
            max_turns=20,
            storage_path=self.session_file,
        )
        store.advance_turn()
        store.mark_dirty_files(["src/main.py"])

        # Verify file exists on disk
        self.assertTrue(os.path.exists(self.session_file))

        # Re-hydrate
        reloaded = StateStore.load_or_create(
            session_id="persist_test", storage_path=self.session_file
        )
        self.assertEqual(reloaded.state.current_turn, 1)
        self.assertIn("src/main.py", reloaded.state.dirty_files)

    def test_turn_limit_termination(self):
        store = StateStore.create_new(
            session_id="turn_limit",
            max_turns=3,
            storage_path=self.session_file,
        )
        self.assertFalse(store.state.is_terminal)

        store.advance_turn()  # turn 1
        self.assertFalse(store.state.is_terminal)

        store.advance_turn()  # turn 2
        self.assertFalse(store.state.is_terminal)

        store.advance_turn()  # turn 3 >= max_turns 3 -> terminate
        self.assertTrue(store.state.is_terminal)
        self.assertIn("Max turn limit", store.state.termination_reason or "")

    def test_token_and_spend_budget_enforcement(self):
        store = StateStore.create_new(
            session_id="budget_limit",
            max_token_budget=10_000,
            max_spend_usd=0.05,
            storage_path=self.session_file,
        )

        # Incur 12,000 tokens
        store.record_token_usage(prompt_tokens=8_000, completion_tokens=4_000)
        self.assertTrue(store.state.is_terminal)
        self.assertIn("Token budget exhausted", store.state.termination_reason or "")

    def test_infinite_hallucination_loop_tripwire(self):
        store = StateStore.create_new(
            session_id="loop_tripwire",
            storage_path=self.session_file,
        )
        call = ToolCallRequest(
            call_id="c1",
            tool_name="patch_file",
            parameters={"filepath": "app.py", "search": "old", "replace": "new"},
        )

        # First failure -> allowed to proceed
        can_proceed_1 = store.record_tool_call_outcome(call, success=False)
        self.assertTrue(can_proceed_1)
        self.assertFalse(store.state.is_terminal)

        # Identical second failure -> blocked!
        can_proceed_2 = store.record_tool_call_outcome(call, success=False)
        self.assertFalse(can_proceed_2)
        self.assertTrue(store.state.is_terminal)
        self.assertIn("Infinite loop trip-wire triggered", store.state.termination_reason or "")


if __name__ == "__main__":
    unittest.main()

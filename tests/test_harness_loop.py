"""End-to-end integration tests for the Master Harness Loop."""

import os
import shutil
import tempfile
import unittest

from core.evaluator import Evaluator
from core.loop import HarnessLoop
from core.memory import MemoryEngine
from core.model_client import MockModelClient
from core.models import ModelTurnResponse, ToolCallRequest
from core.permissions import PermissionGate
from core.state import StateStore
from core.telemetry import TelemetryLogger
from core.tools.base import ToolDispatcher
from core.tools.fs_tools import CreateFileTool, ReadFileTool, SafePathResolver
from core.tools.patcher import PatchFileTool
from core.tools.shell import ExecuteCommandTool


class TestHarnessLoop(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.session_path = os.path.join(self.workspace, ".harness", "session.json")
        self.events_path = os.path.join(self.workspace, ".harness", "events.jsonl")

        self.resolver = SafePathResolver(self.workspace)
        self.dispatcher = ToolDispatcher()
        self.dispatcher.register(ReadFileTool(self.resolver))
        self.dispatcher.register(CreateFileTool(self.resolver))
        self.dispatcher.register(PatchFileTool(self.resolver))
        self.dispatcher.register(ExecuteCommandTool(self.resolver))

        self.evaluator = Evaluator(self.resolver)
        self.telemetry = TelemetryLogger(self.events_path)
        self.gate = PermissionGate(self.workspace, auto_approve_all=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_e2e_autonomous_self_healing_flow(self):
        # 1. Create a buggy code file
        src_file = os.path.join(self.workspace, "calc.py")
        with open(src_file, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a - b\n")

        # 2. Script the mock model responses
        mock_model = MockModelClient()

        # Turn 1: Read file
        mock_model.add_response(
            ModelTurnResponse(
                text="Inspecting calc.py",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c1",
                        tool_name="read_file",
                        parameters={"filepath": "calc.py"},
                    )
                ],
                prompt_tokens=100,
                completion_tokens=20,
            )
        )

        # Turn 2: Attempt patch with intentional syntax error
        mock_model.add_response(
            ModelTurnResponse(
                text="Applying patch",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c2",
                        tool_name="patch_file",
                        parameters={
                            "filepath": "calc.py",
                            "search_block": "return a - b",
                            "replace_block": "if True\n    return a + b",  # Missing colon!
                        },
                    )
                ],
                prompt_tokens=150,
                completion_tokens=30,
            )
        )

        # Turn 3: Self-heal after receiving AST SyntaxError feedback
        mock_model.add_response(
            ModelTurnResponse(
                text="Fixing syntax error",
                tool_calls=[
                    ToolCallRequest(
                        call_id="c3",
                        tool_name="patch_file",
                        parameters={
                            "filepath": "calc.py",
                            "search_block": "if True\n    return a + b",
                            "replace_block": "return a + b",
                        },
                    )
                ],
                prompt_tokens=200,
                completion_tokens=25,
            )
        )

        # Turn 4: Final verification and task completion
        mock_model.add_response(
            ModelTurnResponse(
                text="Refactoring complete! Verified add() returns a + b.",
                tool_calls=[],
                prompt_tokens=100,
                completion_tokens=20,
            )
        )

        state_store = StateStore.create_new(
            session_id="test_e2e",
            max_turns=10,
            storage_path=self.session_path,
        )
        memory = MemoryEngine()

        loop = HarnessLoop(
            state_store=state_store,
            memory=memory,
            model_client=mock_model,
            permissions=self.gate,
            tools=self.dispatcher,
            evaluator=self.evaluator,
            telemetry=self.telemetry,
        )

        final_state = loop.run("Fix the subtraction bug in calc.py")

        # Invariants to verify
        self.assertTrue(final_state.is_terminal)
        self.assertEqual(final_state.current_turn, 4)
        self.assertIn("calc.py", final_state.dirty_files)

        # Check file content on disk
        with open(src_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("return a + b", content)

        # Verify telemetry log
        self.assertTrue(os.path.exists(self.events_path))
        with open(self.events_path, "r", encoding="utf-8") as f:
            events_text = f.read()

        self.assertIn("SESSION_STARTED", events_text)
        self.assertIn("EVAL_FAILED", events_text)  # AST check caught broken syntax!
        self.assertIn("GOAL_COMPLETED", events_text)
        self.assertIn("SESSION_TERMINATED", events_text)

    def test_permission_denial_injects_synthetic_refusal(self):
        # Gate that denies all mutations
        denying_gate = PermissionGate(self.workspace, input_func=lambda prompt: "n")

        mock_model = MockModelClient()
        mock_model.add_response(
            ModelTurnResponse(
                tool_calls=[
                    ToolCallRequest(
                        call_id="deny_me",
                        tool_name="create_file",
                        parameters={"filepath": "blocked.txt", "content": "secret"},
                    )
                ]
            )
        )
        mock_model.add_response(
            ModelTurnResponse(text="Understood, action was denied. Aborting.")
        )

        state_store = StateStore.create_new(
            session_id="test_deny", storage_path=self.session_path
        )
        memory = MemoryEngine()

        loop = HarnessLoop(
            state_store=state_store,
            memory=memory,
            model_client=mock_model,
            permissions=denying_gate,
            tools=self.dispatcher,
            evaluator=self.evaluator,
            telemetry=self.telemetry,
        )

        final_state = loop.run("Create blocked.txt")
        self.assertTrue(final_state.is_terminal)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "blocked.txt")))


if __name__ == "__main__":
    unittest.main()

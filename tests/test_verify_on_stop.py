"""The loop reminds the agent to run review_change before finishing with unreviewed edits."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from core.evaluator import Evaluator
from core.loop import HarnessLoop
from core.memory import MemoryEngine, SpillCache
from core.model_client import MockModelClient
from core.models import ModelTurnResponse, ToolCallRequest
from core.permissions import PermissionGate
from core.state import StateStore
from core.telemetry import TelemetryLogger
from core.tools.base import ToolDispatcher
from core.tools.fs_tools import CreateFileTool, SafePathResolver
from planes.verify.review import ReviewChangeTool


def call(name, **params):
    return ModelTurnResponse(text=None, tool_calls=[ToolCallRequest(call_id=f"c-{name}-{id(params)}", tool_name=name, parameters=params)],
                             prompt_tokens=1, completion_tokens=1)


def final(text="done"):
    return ModelTurnResponse(text=text, prompt_tokens=1, completion_tokens=1)


class TestVerifyOnStop(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.ws], check=True)
        self.env = mock.patch.dict(os.environ, {"HARNESS_NO_EXTERNAL": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def run_loop(self, responses, with_review=True):
        resolver = SafePathResolver(self.ws)
        tools = ToolDispatcher()
        tools.register(CreateFileTool(resolver))
        if with_review:
            tools.register(ReviewChangeTool(self.ws))
        model = MockModelClient(list(responses))
        loop = HarnessLoop(
            state_store=StateStore.create_new("s", storage_path=os.path.join(self.ws, ".harness", "s.json")),
            memory=MemoryEngine(spill_cache=SpillCache(os.path.join(self.ws, ".harness", "spill"))),
            model_client=model,
            permissions=PermissionGate(self.ws, auto_approve_all=True),
            tools=tools,
            evaluator=Evaluator(resolver),
            telemetry=TelemetryLogger(os.path.join(self.ws, ".harness", "events.jsonl")),
        )
        state = loop.run("task")
        nudges = sum(1 for m in loop.memory.messages if m["role"] == "user" and "[HARNESS]" in str(m.get("content")))
        return state, nudges, len(model.call_history)

    def test_edit_then_finish_is_nudged_then_review_lets_it_finish(self):
        state, nudges, calls = self.run_loop([
            call("create_file", filepath="a.py", content="x = 1\n"),
            final("all done"),
            call("review_change"),
            final("done after review"),
        ])
        self.assertEqual(nudges, 1)
        self.assertEqual(calls, 4)
        self.assertIn("Goal completed", state.termination_reason)

    def test_reviewed_edit_finishes_without_nudge(self):
        _, nudges, calls = self.run_loop([
            call("create_file", filepath="a.py", content="x = 1\n"),
            call("review_change"),
            final(),
        ])
        self.assertEqual((nudges, calls), (0, 3))

    def test_ignoring_the_nudge_does_not_loop(self):
        state, nudges, calls = self.run_loop([
            call("create_file", filepath="a.py", content="x = 1\n"),
            final(),
            final("finishing anyway"),
        ])
        self.assertEqual((nudges, calls), (1, 3))
        self.assertIn("Goal completed", state.termination_reason)

    def test_edit_after_review_needs_another_review(self):
        _, nudges, _ = self.run_loop([
            call("create_file", filepath="a.py", content="x = 1\n"),
            call("review_change"),
            call("create_file", filepath="b.py", content="y = 2\n"),
            final(),
            call("review_change"),
            final(),
        ])
        self.assertEqual(nudges, 1)

    def test_no_edits_or_no_review_tool_means_no_nudge(self):
        self.assertEqual(self.run_loop([final()])[1], 0)
        self.assertEqual(self.run_loop([call("create_file", filepath="a.py", content="x\n"), final()], with_review=False)[1], 0)

    def test_nudges_are_capped(self):
        responses = []
        for i in range(4):
            responses += [call("create_file", filepath=f"f{i}.py", content="x\n"), final()]
        _, nudges, _ = self.run_loop(responses)
        self.assertEqual(nudges, HarnessLoop.MAX_REVIEW_NUDGES)


if __name__ == "__main__":
    unittest.main()

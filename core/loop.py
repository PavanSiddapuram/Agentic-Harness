"""The Master Finite State Machine Orchestration Loop.

Directs the execution flow, coordinates all subsystems, and enforces
deterministic boundaries between stochastic LLM inferences and local OS interactions.
"""

from __future__ import annotations

import json
from typing import Optional

from core.evaluator import Evaluator
from core.memory import MemoryEngine
from core.model_client import BaseModelClient
from core.models import ExecutionState, ToolCallRequest
from core.permissions import PermissionGate
from core.state import StateStore
from core.telemetry import TelemetryLogger
from core.tools.base import ToolDispatcher


class HarnessLoop:
    """The central orchestrator for the deterministic code agent runtime."""

    def __init__(
        self,
        state_store: StateStore,
        memory: MemoryEngine,
        model_client: BaseModelClient,
        permissions: PermissionGate,
        tools: ToolDispatcher,
        evaluator: Evaluator,
        telemetry: Optional[TelemetryLogger] = None,
    ):
        self.state_store = state_store
        self.memory = memory
        self.model = model_client
        self.permissions = permissions
        self.tools = tools
        self.evaluator = evaluator
        self.telemetry = telemetry or TelemetryLogger()

    @property
    def state(self) -> ExecutionState:
        return self.state_store.state

    def run(self, initial_prompt: str) -> ExecutionState:
        """Executes the master ReAct agent loop until a terminal condition is met."""
        self.telemetry.emit(
            "SESSION_STARTED",
            {
                "session_id": self.state.session_id,
                "initial_prompt": initial_prompt,
                "max_turns": self.state.max_turns,
                "max_token_budget": self.state.max_token_budget,
            },
        )

        self.memory.add_user_message(initial_prompt)

        while not self.state.is_terminal:
            # 1. Enforce hard budget limits before spending inference tokens
            if self.state.current_turn >= self.state.max_turns:
                self.state_store.terminate("Max turn limit ceiling reached.")
                break

            if self.state.tokens_consumed >= self.state.max_token_budget:
                self.state_store.terminate("Token budget ceiling exhausted.")
                break

            # 2. Advance turn counter
            turn = self.state_store.advance_turn()
            self.telemetry.emit("TURN_STARTED", {"turn": turn})

            # 3. Compile context window (3-tier: System + Anchor + Sliding Buffer)
            context = self.memory.compile_context(
                plan=self.state.plan, dirty_files=self.state.dirty_files
            )

            # 4. Model Inference
            tool_schemas = self.tools.list_schemas()
            with self.telemetry.timer() as t:
                try:
                    response = self.model.generate_turn(context, tools=tool_schemas)
                except Exception as err:
                    self.state_store.terminate(f"Model inference failed: {str(err)}")
                    self.telemetry.emit("INFERENCE_ERROR", {"error": str(err)})
                    break

            # 5. Account for consumed tokens and spend
            self.state_store.record_token_usage(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            self.telemetry.emit(
                "TOKEN_UPDATE",
                {
                    "turn": turn,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "cumulative_tokens": self.state.tokens_consumed,
                    "spend_usd": self.state.spend_usd,
                    "inference_duration_ms": t.elapsed_ms(),
                },
            )

            # 6. Check for pure conversational response (No tool calls -> Terminal)
            if not response.tool_calls:
                self.memory.add_assistant_message(response.text)
                self.state_store.terminate("Goal completed: agent emitted final response.")
                self.telemetry.emit("GOAL_COMPLETED", {"response": response.text})
                break

            # Record assistant turn with tool calls into memory
            tool_calls_payload = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    # OpenAI-compatible APIs require arguments as a JSON string.
                    "function": {"name": tc.tool_name, "arguments": json.dumps(tc.parameters)},
                }
                for tc in response.tool_calls
            ]
            self.memory.add_assistant_message(response.text, tool_calls=tool_calls_payload)

            # 7. Process proposed tool calls
            for tool_call in response.tool_calls:
                self._dispatch_and_reconcile_tool(tool_call)
                if self.state.is_terminal:
                    break

        self.telemetry.emit(
            "SESSION_TERMINATED",
            {
                "session_id": self.state.session_id,
                "total_turns": self.state.current_turn,
                "total_tokens": self.state.tokens_consumed,
                "total_spend_usd": self.state.spend_usd,
                "reason": self.state.termination_reason,
                "dirty_files": self.state.dirty_files,
            },
        )
        return self.state

    def _dispatch_and_reconcile_tool(self, tool_call: ToolCallRequest) -> None:
        """Processes a single tool call through Permission -> Execution -> Evaluation."""
        self.telemetry.emit(
            "TOOL_INVOKED",
            {
                "call_id": tool_call.call_id,
                "tool_name": tool_call.tool_name,
                "parameters": tool_call.parameters,
            },
        )

        # A. Permission Assessment & Authorization Gate
        try:
            risk_level = self.permissions.assess(tool_call)
            authorized = self.permissions.authorize(tool_call, risk_level)
        except Exception as e:
            # Dangerous pattern caught
            self.telemetry.emit(
                "SECURITY_TRIPWIRE", {"call_id": tool_call.call_id, "error": str(e)}
            )
            self.memory.add_tool_result(
                tool_call.call_id,
                success=False,
                output=f"SECURITY_DENIAL: {str(e)}",
            )
            return

        if not authorized:
            self.telemetry.emit("PERMISSION_DENIED", {"call_id": tool_call.call_id})
            self.memory.add_tool_result(
                tool_call.call_id,
                success=False,
                output="PERMISSION_DENIED: Action rejected by user approval gate.",
            )
            return

        # B. Deterministic Execution
        with self.telemetry.timer() as t:
            raw_result = self.tools.execute(tool_call)

        # C. Evaluator Inspection (AST checks, exit code analysis)
        eval_result = self.evaluator.evaluate(tool_call, raw_result)

        # D. State Reconcile
        if eval_result.modified_files:
            self.state_store.mark_dirty_files(eval_result.modified_files)

        # Check infinite loop tripwire for identical consecutive failing calls
        can_proceed = self.state_store.record_tool_call_outcome(
            tool_call, success=eval_result.success
        )
        if not can_proceed:
            self.telemetry.emit(
                "INFINITE_LOOP_ABORT",
                {"call_id": tool_call.call_id, "tool_name": tool_call.tool_name},
            )

        # E. Record Result or Formulate Synthetic Error Hint for next turn
        if not eval_result.success and eval_result.evaluator_feedback:
            final_payload = (
                f"EXECUTION_FAILED_VALIDATION:\n"
                f"Diagnostic Hint: {eval_result.evaluator_feedback}\n"
                f"Raw Tool Output: {eval_result.output}"
            )
            self.telemetry.emit(
                "EVAL_FAILED",
                {
                    "call_id": tool_call.call_id,
                    "feedback": eval_result.evaluator_feedback,
                },
            )
        else:
            final_payload = eval_result.output
            self.telemetry.emit(
                "TOOL_COMPLETED",
                {
                    "call_id": tool_call.call_id,
                    "success": eval_result.success,
                    "duration_ms": t.elapsed_ms(),
                },
            )

        self.memory.add_tool_result(
            tool_call.call_id,
            success=eval_result.success,
            output=final_payload,
        )

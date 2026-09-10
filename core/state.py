"""State Store implementation for session tracking and disk persistence.

Ensures the agent harness can hydrate, advance, and recover its execution state
at any point in time. Uses atomic file writes to prevent corrupted state files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from core.models import ExecutionState, PlanStep, TaskStatus, ToolCallRequest


class StateStore:
    """Manages runtime variables, budget enforcement checks, and atomic disk persistence."""

    def __init__(self, state: ExecutionState, storage_path: Optional[str] = None):
        self.state = state
        self.storage_path = storage_path or os.path.join(".harness", "session.json")

    @classmethod
    def create_new(
        cls,
        session_id: str,
        max_turns: int = 25,
        max_token_budget: int = 200_000,
        max_spend_usd: float = 0.50,
        storage_path: Optional[str] = None,
    ) -> StateStore:
        """Initializes a brand new execution session."""
        state = ExecutionState(
            session_id=session_id,
            max_turns=max_turns,
            max_token_budget=max_token_budget,
            max_spend_usd=max_spend_usd,
        )
        store = cls(state, storage_path=storage_path)
        store.save()
        return store

    @classmethod
    def load_or_create(
        cls,
        session_id: str,
        max_turns: int = 25,
        max_token_budget: int = 200_000,
        max_spend_usd: float = 0.50,
        storage_path: Optional[str] = None,
    ) -> StateStore:
        """Hydrates an existing session from disk, or initializes a new one if not found."""
        path = storage_path or os.path.join(".harness", "session.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                state = ExecutionState.from_dict(data)
                return cls(state, storage_path=path)
            except Exception:
                # If file is corrupted, fall back to new session
                pass
        return cls.create_new(
            session_id=session_id,
            max_turns=max_turns,
            max_token_budget=max_token_budget,
            max_spend_usd=max_spend_usd,
            storage_path=path,
        )

    def save(self) -> None:
        """Atomically saves the state to disk using a temporary file rename."""
        target = Path(self.storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_suffix(".tmp")

        payload = self.state.to_dict()
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Atomic replace (standard in POSIX and Windows NT)
        os.replace(temp_path, target)

    def advance_turn(self) -> int:
        """Increments turn counter and validates turn ceiling."""
        self.state.current_turn += 1
        if self.state.current_turn >= self.state.max_turns:
            self.terminate("Max turn limit reached.")
        self.save()
        return self.state.current_turn

    def record_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        price_per_1m_in: float = 3.00,
        price_per_1m_out: float = 15.00,
    ) -> None:
        """Updates consumed tokens and calculates dollar spend against budget ceilings."""
        turn_tokens = prompt_tokens + completion_tokens
        self.state.tokens_consumed += turn_tokens

        turn_spend = (prompt_tokens / 1_000_000.0) * price_per_1m_in + (
            completion_tokens / 1_000_000.0
        ) * price_per_1m_out
        self.state.spend_usd += turn_spend

        if self.state.tokens_consumed >= self.state.max_token_budget:
            self.terminate(
                f"Token budget exhausted ({self.state.tokens_consumed} >= {self.state.max_token_budget})."
            )
        elif self.state.spend_usd >= self.state.max_spend_usd:
            self.terminate(
                f"Dollar spend ceiling reached (${self.state.spend_usd:.4f} >= ${self.state.max_spend_usd:.2f})."
            )

        self.save()

    def mark_dirty_files(self, filepaths: List[str]) -> None:
        """Registers files that were mutated during execution."""
        for fp in filepaths:
            if fp not in self.state.dirty_files:
                self.state.dirty_files.append(fp)
        self.save()

    def set_plan(self, steps: List[PlanStep]) -> None:
        """Updates the high-level task queue."""
        self.state.plan = steps
        self.save()

    def update_plan_step(
        self, step_id: int, status: TaskStatus, summary: Optional[str] = None
    ) -> bool:
        """Updates the status of an existing plan step."""
        for step in self.state.plan:
            if step.id == step_id:
                step.status = status
                if summary is not None:
                    step.result_summary = summary
                self.save()
                return True
        return False

    def record_tool_call_outcome(self, tool_call: ToolCallRequest, success: bool) -> bool:
        """Monitors repeated failing tool signatures to break infinite hallucination loops.
        
        Returns True if execution can proceed, or False if an infinite loop was detected.
        """
        sig = tool_call.signature_hash()

        if not success:
            if self.state.last_failing_signature == sig:
                self.state.consecutive_signature_failures += 1
            else:
                self.state.last_failing_signature = sig
                self.state.consecutive_signature_failures = 1

            if self.state.consecutive_signature_failures >= 2:
                self.terminate(
                    f"Infinite loop trip-wire triggered: tool '{tool_call.tool_name}' failed with "
                    f"identical parameters twice consecutively."
                )
                self.save()
                return False
        else:
            # Reset consecutive failure counter on any successful execution
            self.state.last_failing_signature = None
            self.state.consecutive_signature_failures = 0

        self.save()
        return True

    def terminate(self, reason: str) -> None:
        """Marks the session as terminal with a formal reason."""
        self.state.is_terminal = True
        self.state.termination_reason = reason
        self.save()

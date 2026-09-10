"""Domain models and schema definitions for the deterministic agent harness.

Zero external dependencies - uses standard library dataclasses and typing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """Execution status of a single step in the agent's plan."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PermissionLevel(str, Enum):
    """Risk tier determined by the permission gate."""
    AUTO_ALLOW = "AUTO_ALLOW"         # Read-only operations (view_file, list_dir, search)
    CONFIRMATION = "CONFIRMATION"     # Non-destructive edits (patch_file, create_file)
    RESTRICTED = "RESTRICTED"         # Shell command execution (subprocess)


@dataclass
class PlanStep:
    """A discrete, tracked objective inside the harness planning engine."""
    id: int
    instruction: str
    status: TaskStatus = TaskStatus.PENDING
    result_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "instruction": self.instruction,
            "status": self.status.value,
            "result_summary": self.result_summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanStep:
        return cls(
            id=data["id"],
            instruction=data["instruction"],
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            result_summary=data.get("result_summary"),
        )


@dataclass
class ToolCallRequest:
    """Structured tool invocation proposed by the LLM."""
    call_id: str
    tool_name: str
    parameters: Dict[str, Any]

    def signature_hash(self) -> str:
        """Computes a deterministic hash of tool name and parameters.
        
        Used to detect duplicate failing tool calls and prevent infinite hallucination loops.
        """
        payload = {
            "tool_name": self.tool_name,
            "parameters": self.parameters,
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "parameters": self.parameters,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ToolCallRequest:
        return cls(
            call_id=data["call_id"],
            tool_name=data["tool_name"],
            parameters=data.get("parameters", {}),
        )


@dataclass
class ToolExecutionResult:
    """Result of executing a tool on the host environment or virtual jail."""
    call_id: str
    success: bool
    output: str
    evaluator_feedback: Optional[str] = None
    modified_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "success": self.success,
            "output": self.output,
            "evaluator_feedback": self.evaluator_feedback,
            "modified_files": self.modified_files,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ToolExecutionResult:
        return cls(
            call_id=data["call_id"],
            success=data["success"],
            output=data.get("output", ""),
            evaluator_feedback=data.get("evaluator_feedback"),
            modified_files=data.get("modified_files", []),
        )


@dataclass
class ModelTurnResponse:
    """Normalized response from the LLM model gateway."""
    text: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ModelTurnResponse:
        return cls(
            text=data.get("text"),
            tool_calls=[ToolCallRequest.from_dict(tc) for tc in data.get("tool_calls", [])],
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
        )


@dataclass
class ExecutionState:
    """The central state of the harness session.
    
    Fully serializable to disk as session.json for crash recovery and checkpointing.
    """
    session_id: str
    current_turn: int = 0
    max_turns: int = 25
    tokens_consumed: int = 0
    max_token_budget: int = 200_000
    spend_usd: float = 0.0
    max_spend_usd: float = 0.50
    plan: List[PlanStep] = field(default_factory=list)
    dirty_files: List[str] = field(default_factory=list)
    is_terminal: bool = False
    termination_reason: Optional[str] = None
    # Guard against infinite hallucination loops: tracks consecutive identical failures
    last_failing_signature: Optional[str] = None
    consecutive_signature_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_turn": self.current_turn,
            "max_turns": self.max_turns,
            "tokens_consumed": self.tokens_consumed,
            "max_token_budget": self.max_token_budget,
            "spend_usd": self.spend_usd,
            "max_spend_usd": self.max_spend_usd,
            "plan": [step.to_dict() for step in self.plan],
            "dirty_files": self.dirty_files,
            "is_terminal": self.is_terminal,
            "termination_reason": self.termination_reason,
            "last_failing_signature": self.last_failing_signature,
            "consecutive_signature_failures": self.consecutive_signature_failures,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExecutionState:
        return cls(
            session_id=data["session_id"],
            current_turn=data.get("current_turn", 0),
            max_turns=data.get("max_turns", 25),
            tokens_consumed=data.get("tokens_consumed", 0),
            max_token_budget=data.get("max_token_budget", 200_000),
            spend_usd=data.get("spend_usd", 0.0),
            max_spend_usd=data.get("max_spend_usd", 0.50),
            plan=[PlanStep.from_dict(step) for step in data.get("plan", [])],
            dirty_files=data.get("dirty_files", []),
            is_terminal=data.get("is_terminal", False),
            termination_reason=data.get("termination_reason"),
            last_failing_signature=data.get("last_failing_signature"),
            consecutive_signature_failures=data.get("consecutive_signature_failures", 0),
        )

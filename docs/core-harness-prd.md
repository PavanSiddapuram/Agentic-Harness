# Engineering PRD & Technical Architecture Document

## System: Deterministic Agent Harness from Scratch (`core-harness`)
**Target:** Local Code Agent Runtime (Claude Code / Cursor Composer Class)  
**Dependencies:** Zero framework abstractions (No LangChain, CrewAI, AutoGen, or LlamaIndex). Standard library runtime + raw Model SDK / HTTP Client.

---

## 1. Product Requirements Document (PRD)

### 1.1 Problem Statement
Modern production coding assistants (e.g., Cursor Composer, Claude Code, GitHub Copilot Workspace) require rigorous isolation between non-deterministic Large Language Models and deterministic operating system interactions.

Frameworks like LangChain and CrewAI introduce excessive indirection, unmanaged execution graphs, implicit state retention, and unpredictable context-window packing.

This project defines and implements **`core-harness`**: a minimal, high-throughput, predictable, and fully auditable agent runtime written from scratch.

### 1.2 System Goals & Anti-Goals

#### Goals
- **Zero Magic**: Explicit control loops with deterministic state transitions.
- **Strict Isolation**: Shell and file mutations pass through a gatekeeper checking paths, policies, and interactive user approvals.
- **Context Budgeting**: Deterministic token compaction strategies ensuring the system never throws context overflow errors.
- **Self-Correction**: Structural recovery loops that format stack traces and linter failures back into model-digestible corrections.
- **Observable Execution**: Every transition, tool invocation, token delta, and cost increment is emitted to a machine-readable event log.

#### Anti-Goals
- No generalized multi-agent conversational role-play swarms.
- No proprietary vector database wrappers (retrieval relies on local ripgrep/AST symbol lookups or simple SQLite vector tables).
- No implicit prompts hidden behind magic "chains."

### 1.3 Target Personas & Primary Use Cases
- **Primary Persona**: Senior Systems & Full-Stack Engineers wanting absolute control over automated code generation and refactoring.
- **Core Use Cases**:
  1. **Autonomous File Editing**: Search, inspect, modify, and verify syntax across multiple local files.
  2. **Command Execution & TDD**: Execute test runners (`pytest`, `npm test`), ingest test failures, and iteratively modify code until green.
  3. **Controlled Refactoring**: Large multi-step migrations operating under an explicit plan that pauses for user confirmation on critical disk writes.

---

## 2. Foundational Engineering Practices & Reliability Guardrails

To prevent the agent from entering infinite loops, corrupting workspaces, or blowing through API budgets, `core-harness` enforces four operational boundaries:

### 2.1 The Hard Budgeting Envelope
Every invocation runs inside a strict computational envelope:
- **Iteration Cap**: Max turns per request default to 25 (configurable).
- **Token Budget**: Configurable maximum cumulative tokens (e.g., 200,000 tokens) and spend budget ($0.50/run).
- **Timeout Ceiling**: Individual tool calls cap at 30 seconds; interactive shell commands cap at 120 seconds.

### 2.2 Re-entrancy & State Machine Idempotency
The agent loop is modeled as an Idempotent Finite State Machine (FSM). Any session can be serialized to disk as `session.json`. If execution crashes mid-turn, the harness can hydrate the state and resume or roll back uncommitted workspace diffs using an integrated Git working-tree shadow stash.

### 2.3 File Mutation Invariants
- **No Blind Rewrites**: The agent is discouraged from rewriting 1,000-line files wholesale. File modifications are executed via targeted patch blocks (Search & Replace) or strict line-offset ranges.
- **Pre-Execution Syntax Checking**: Any edit targeting code files must pass a local language parser pass (e.g., Python `ast.parse`, JavaScript parser) before being committed to disk.

---

## 3. High-Level Design (HLD)

### 3.1 System Topology

```
                   ┌──────────────────────────────────────────┐
                   │               CLI / Client               │
                   └────────────────────┬─────────────────────┘
                                        │ User Prompt & Config
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             AGENT HARNESS RUNTIME                                │
│                                                                                  │
│   ┌──────────────────────────────────────────────────────────────────────────┐   │
│   │                          Execution Orchestrator                          │   │
│   │                                                                          │   │
│   │    ┌──────────────┐     ┌──────────────┐     ┌──────────────────────┐    │   │
│   │    │ State Store  │ <-> │ Memory Engine│ <-> │    Planner / Todo    │    │   │
│   │    └──────────────┘     └──────────────┘     └──────────────────────┘    │   │
│   └───────────┬───────────────────────────────────────────────▲──────────────┘   │
│               │ Context Window (System + History + Budget)    │                  │
│               ▼                                               │                  │
│   ┌───────────────────────┐                                   │ Tool Result /    │
│   │  Model Gateway / API  │                                   │ Evaluator State  │
│   └───────────┬───────────┘                                   │                  │
│               │ Emits Structured Tool Call                    │                  │
│               ▼                                               │                  │
│   ┌───────────────────────┐                                   │                  │
│   │   Permission Policy   │ ──(Rejected)──► Injected Refusal ─┤                  │
│   └───────────┬───────────┘                                   │                  │
│               │ (Approved)                                    │                  │
│               ▼                                               │                  │
│   ┌───────────────────────┐                                   │                  │
│   │     Tool Dispatch     │                                   │                  │
│   └───────────┬───────────┘                                   │                  │
│               │ Raw Result                                    │                  │
│               ▼                                               │                  │
│   ┌───────────────────────┐                                   │                  │
│   │    Evaluator Layer    │ ──(Passed)────────────────────────┘                  │
│   │   (Syntax/Lint/Diff)  │ ──(Failed)──► Injected Self-Correction Prompt        │
│   └───────────────────────┘                                                      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Responsibilities

| Component | Responsibility | Boundary Constraints |
| :--- | :--- | :--- |
| **Orchestrator** | Coordinates the main event loop, checks termination conditions, enforces budget ceilings. | Contains zero LLM-specific logic; strictly an operational controller. |
| **State Store** | Tracks run variables: turn count, active files, dirty diffs, exit codes, and raw spend. | Pure, serializable in-memory schema with on-disk JSON snapshotting. |
| **Planner** | Maintains an explicit ordered sequence of subtasks (`PENDING`, `IN_PROGRESS`, `DONE`, `FAILED`). | Updates dynamically via dedicated planning tools; prevents goal drift. |
| **Memory Engine** | Builds the LLM prompt. Manages sliding windows, pruning, and contextual compression. | Enforces strict token ceilings; never allows context window overflows. |
| **Permission Gate** | Intercepts all tool calls. Classifies risk and demands terminal confirmation when required. | Sandboxed; can bypass the LLM and return synthetic error messages directly. |
| **Tool Dispatch** | Standard library execution engine (File I/O, Subprocess Management, Search). | Never allows directory traversal outside project root without explicit flag. |
| **Evaluator** | Post-execution validation: AST checks, linter runs, non-zero exit code analysis. | Intercepts tool stdout/stderr before it enters memory; formats rich error hints. |
| **Retry Subsystem** | Formulates structured recovery prompts when an action fails validation or times out. | Tracks retry counters per subtask to halt cascading hallucination loops. |

---

## 4. Low-Level Design (LLD)

### 4.1 Domain Models & Schema Definitions

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class PermissionLevel(str, Enum):
    AUTO_ALLOW = "AUTO_ALLOW"     # Read operations (view_file, search)
    CONFIRMATION = "CONFIRMATION" # Non-destructive modifications (patch_file)
    RESTRICTED = "RESTRICTED"     # Arbitrary bash execution (subprocess)

@dataclass
class PlanStep:
    id: int
    instruction: str
    status: TaskStatus = TaskStatus.PENDING
    result_summary: Optional[str] = None

@dataclass
class ToolCallRequest:
    call_id: str
    tool_name: str
    parameters: Dict[str, Any]

@dataclass
class ToolExecutionResult:
    call_id: str
    success: bool
    output: str
    evaluator_feedback: Optional[str] = None
    modified_files: List[str] = field(default_factory=list)

@dataclass
class ExecutionState:
    session_id: str
    current_turn: int = 0
    max_turns: int = 25
    tokens_consumed: int = 0
    max_token_budget: int = 200_000
    plan: List[PlanStep] = field(default_factory=list)
    dirty_files: List[str] = field(default_factory=list)
    is_terminal: bool = False
```

### 4.2 The Core State Machine & Event Loop

```python
import sys

class HarnessLoop:
    def __init__(self, state, memory, model_client, permissions, tools, evaluator):
        self.state = state
        self.memory = memory
        self.model = model_client
        self.permissions = permissions
        self.tools = tools
        self.evaluator = evaluator

    def run(self, initial_prompt: str) -> None:
        self.memory.add_user_message(initial_prompt)

        while not self.state.is_terminal:
            # 1. Enforce hard limits
            if self.state.current_turn >= self.state.max_turns:
                self.state.is_terminal = True
                print("[Harness] Max turn limit reached. Halting.")
                break

            if self.state.tokens_consumed >= self.state.max_token_budget:
                self.state.is_terminal = True
                print("[Harness] Token budget exhausted. Halting.")
                break

            self.state.current_turn += 1

            # 2. Prepare Context (Working Context + Compressed Memory)
            context = self.memory.compile_context(
                plan=self.state.plan,
                dirty_files=self.state.dirty_files
            )

            # 3. Model Inference
            response = self.model.generate_turn(context)
            self.state.tokens_consumed += response.total_tokens

            # 4. Handle text response vs. tool execution
            if not response.tool_calls:
                self.memory.add_assistant_message(response.text)
                print(f"[Agent]: {response.text}")
                self.state.is_terminal = True
                break

            # 5. Process tool call pipeline
            for tool_call in response.tool_calls:
                self._handle_tool_call(tool_call)

    def _handle_tool_call(self, tool_call: ToolCallRequest) -> None:
        # A. Permission Inspection
        risk_level = self.permissions.assess(tool_call)
        if not self.permissions.authorize(tool_call, risk_level):
            self.memory.add_tool_result(
                tool_call.call_id,
                success=False,
                output="PERMISSION_DENIED: Action rejected by user or policy."
            )
            return

        # B. Deterministic Execution
        raw_result = self.tools.execute(tool_call)

        # C. Evaluation (Syntax validation, test checks, etc.)
        eval_result = self.evaluator.evaluate(tool_call, raw_result)

        # D. Update State
        if eval_result.modified_files:
            self.state.dirty_files = list(set(self.state.dirty_files + eval_result.modified_files))

        # E. Record Result or Feedback for Next Turn
        final_payload = eval_result.output
        if not eval_result.success and eval_result.evaluator_feedback:
            final_payload = f"ERROR: Execution failed validation.\nFeedback: {eval_result.evaluator_feedback}\nRaw Output: {eval_result.output}"

        self.memory.add_tool_result(
            tool_call.call_id,
            success=eval_result.success,
            output=final_payload
        )
```

### 4.3 Subsystems Implementation

#### 1. Memory Compactor (Sliding Context & Truncation)
- Any tool output exceeding 1,500 words is truncated, saved to `.harness/spill/`, and summarized:  
  `"[Output truncated: showing first 50 lines. Full output cached at .harness/spill/tool_xyz.log]"`
- 3-tier window: System Persona (Permanent) + Task Planner State (Permanent) + Rolling Message Buffer (Last $K$ turns).

#### 2. Permission Gatekeeper
```python
import re

class PermissionGate:
    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        self.destructive_patterns = [
            r"rm\s+-rf", r":(){ :|:& };:", r"mkfs", r"dd\s+if=", r">\s*/dev/sd"
        ]

    def assess(self, call: ToolCallRequest) -> PermissionLevel:
        if call.tool_name in ["read_file", "search_directory", "list_dir"]:
            return PermissionLevel.AUTO_ALLOW
        
        if call.tool_name in ["patch_file", "create_file"]:
            return PermissionLevel.CONFIRMATION

        if call.tool_name == "execute_command":
            cmd = call.parameters.get("command", "")
            for pattern in self.destructive_patterns:
                if re.search(pattern, cmd):
                    raise SecurityException(f"Forbidden command pattern detected: {pattern}")
            return PermissionLevel.RESTRICTED
            
        return PermissionLevel.RESTRICTED

    def authorize(self, call: ToolCallRequest, level: PermissionLevel) -> bool:
        if level == PermissionLevel.AUTO_ALLOW:
            return True
        print(f"\n[SECURITY GATE] Operation: {call.tool_name}")
        print(f"Parameters: {call.parameters}")
        choice = input("Approve execution? [y/N]: ").strip().lower()
        return choice == 'y'
```

#### 3. Targeted File Patching (Search & Replace Engine)
```python
def apply_targeted_patch(filepath: str, search_block: str, replace_block: str) -> bool:
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Exact match validation
    occurrences = content.count(search_block)
    if occurrences == 0:
        raise ValueError("Patch target block not found in file. Ensure whitespace and lines match exactly.")
    if occurrences > 1:
        raise ValueError("Ambiguous patch target: Block appears multiple times. Provide more context lines.")

    updated_content = content.replace(search_block, replace_block, 1)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(updated_content)
    
    return True
```

#### 4. Evaluator & AST Lint Checker
```python
import ast

class Evaluator:
    def evaluate(self, tool_call: ToolCallRequest, raw_result: ToolExecutionResult) -> ToolExecutionResult:
        if tool_call.tool_name in ["patch_file", "create_file"]:
            filepath = tool_call.parameters.get("filepath", "")
            if filepath.endswith(".py"):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        ast.parse(f.read())
                except SyntaxError as e:
                    raw_result.success = False
                    raw_result.evaluator_feedback = f"AST SyntaxError on line {e.lineno}: {e.msg}"
                    return raw_result

        # Check bash return codes
        if tool_call.tool_name == "execute_command":
            if "Exit code: 0" not in raw_result.output:
                raw_result.success = False
                raw_result.evaluator_feedback = "Subprocess exited with a non-zero exit code. Inspect stderr."

        return raw_result
```

---

## 5. Failure Modes & Edge Case Matrix

| Failure Mode | Root Cause | Harness Defense Mechanism |
| :--- | :--- | :--- |
| **Hallucination Loop** | Agent repeatedly fails a patch, re-trying the exact same edit. | Track tool call signature hashing. If identical call fails twice, force-abort or prompt human. |
| **Context Explosion** | Tool prints huge binary or 50,000-line bundle. | Stream interceptor: Truncate at 64KB, spill full log to `.harness/spill/`, give head/tail to context. |
| **Path Traversal** | Agent tries to access `../../etc/passwd`. | Canonical path check: `os.path.commonpath([workspace, target]) == workspace`. |
| **Cascading Syntax Corruption** | Partial patch breaks syntax, agent wanders off. | Evaluator blocks progression on AST failure, injecting rich compiler error. |
| **Goal Drift** | Agent loses track after 15 tool calls. | Inject Planner state at top of context every turn to anchor objective. |

---

## 6. Scaling, Observability & Extensibility

### 6.1 Event Telemetry (JSON-Lines)
Every transition streams to `.harness/events.jsonl`:
```json
{"timestamp": "2026-09-07T12:11:00Z", "event": "TOOL_INVOKED", "call_id": "call_123", "tool": "patch_file", "duration_ms": 12}
{"timestamp": "2026-09-07T12:11:01Z", "event": "EVAL_FAILED", "call_id": "call_123", "reason": "AST SyntaxError on line 42"}
{"timestamp": "2026-09-07T12:11:03Z", "event": "TOKEN_UPDATE", "cumulative_tokens": 14200, "cost_usd": 0.042}
```

### 6.2 Scaling Up: Sandboxing via Containers
- **Local Run**: Direct process invocation with restricted paths and interactive confirmation.
- **Production Sandbox Run**: Swap `ToolDispatch` to target Docker/Podman or ephemeral microVM (Firecracker/gVisor).

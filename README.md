# 🧠 AI Harness (`core-harness`)

> **A minimal, deterministic, high-throughput autonomous code agent runtime built from scratch.**  
> Target: Local Code Agent Runtime (Claude Code / Cursor Composer Class)  
> Zero framework abstractions: **No LangChain, No CrewAI, No AutoGen, No LlamaIndex.** Standard Library + raw Model API.

---

## 📌 Project Overview

**`core-harness`** is a production-grade execution harness that bridges the gap between **non-deterministic Large Language Models (LLMs)** and **deterministic local operating systems**. 

While general frameworks wrap prompts in opaque chains and hide execution state, `core-harness` models autonomous coding as an **Idempotent Finite State Machine (FSM)** with explicit boundaries:
- **Strict Isolation**: Shell executions and file mutations are checked by a security gatekeeper and confirmed by the human developer.
- **AST Pre-Commit Validation**: Python/JS code edits are syntactically validated before being finalized.
- **Context Envelope Budgeting**: Compaction algorithms, spill-file offloading, and turn ceilings guarantee the system never runs out of context or budget.
- **Full Observability**: Every transition, tool invocation, token count, and spend increment is streamed to `.harness/events.jsonl`.

---

## 🏛️ System Topology

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

---

## 📚 Study & Fundamentals Curriculum

Before writing code, study the comprehensive learning modules in [`docs/`](./docs/) and the complete specification in [`PRD_AND_ARCHITECTURE.md`](./PRD_AND_ARCHITECTURE.md):

| Document | Core Focus & Muscle Memory |
| :--- | :--- |
| 📄 **[PRD & Technical Architecture](./PRD_AND_ARCHITECTURE.md)** | Full product requirements, subsystem specifications, domain schemas, and failure modes matrix. |
| 📘 **[01. Core Fundamentals & Mental Model](./docs/01_CORE_FUNDAMENTALS_AND_MENTAL_MODEL.md)** | Stochastic vs. Deterministic, OS Kernel analogy, ReAct loops, and why frameworks fail for code agents. |
| 📗 **[02. Vocabulary & Terminology Bible](./docs/02_VOCABULARY_AND_TERMINOLOGY_BIBLE.md)** | 50+ definitions spanning Tokens, KV Caching, FSM, AST, Idempotency, Spill Files, and Pipe Deadlocks. |
| 📙 **[03. Under-The-Hood Execution Flows](./docs/03_UNDER_THE_HOOD_EXECUTION_FLOWS.md)** | Sequence diagrams tracing prompt $\to$ patch $\to$ AST failure $\to$ self-correction $\to$ test suite green. |
| 📕 **[04. Math, Economics & Context Budgeting](./docs/04_MATH_ECONOMICS_AND_CONTEXT_BUDGETING.md)** | Quadratic token explosion ($\mathcal{O}(N^2)$), 3-tier sliding context, spill cache, and spend formulas. |
| 📓 **[05. Defensive Coder Logic & Patterns](./docs/05_DEFENSIVE_CODER_LOGIC_AND_PATTERNS.md)** | Targeted block search-and-replace algorithm, AST pre-commit checks, path jail, and non-blocking subprocesses. |

---

## 📂 Planned Codebase Directory Structure

```
ai-harness/
├── README.md                           # Project vision and study curriculum (this file)
├── PRD_AND_ARCHITECTURE.md             # Complete PRD & Low-Level Design specifications
├── docs/                               # 5-part conceptual mastery guides
│   ├── 01_CORE_FUNDAMENTALS_AND_MENTAL_MODEL.md
│   ├── 02_VOCABULARY_AND_TERMINOLOGY_BIBLE.md
│   ├── 03_UNDER_THE_HOOD_EXECUTION_FLOWS.md
│   ├── 04_MATH_ECONOMICS_AND_CONTEXT_BUDGETING.md
│   └── 05_DEFENSIVE_CODER_LOGIC_AND_PATTERNS.md
├── core/                               # Core Engine (Zero 3rd-party framework abstractions)
│   ├── __init__.py
│   ├── models.py                       # Dataclasses: ExecutionState, PlanStep, ToolCallRequest
│   ├── state.py                        # Snapshotting state store (session.json)
│   ├── memory.py                       # Token compactor, sliding context, spill file offloader
│   ├── permissions.py                  # Permission gatekeeper, destructive pattern interceptor
│   ├── tools/                          # Deterministic tool implementations
│   │   ├── __init__.py
│   │   ├── base.py                     # Base tool interface
│   │   ├── fs_tools.py                 # read_file, search_directory, list_dir (jailed paths)
│   │   ├── patcher.py                  # Exact search-and-replace block patcher
│   │   └── shell.py                    # Subprocess execution with pipe safety & timeouts
│   ├── evaluator.py                    # AST syntax parsing, exit code analysis, synthetic error hints
│   ├── telemetry.py                    # Structured JSONL event stream (.harness/events.jsonl)
│   ├── model_client.py                 # Raw HTTP/SDK model gateway (Mock & Live provider)
│   └── loop.py                         # Master Harness FSM Loop & termination checks
├── tests/                              # Unit & integration verification suite
│   ├── test_patcher.py                 # Patch engine verification
│   ├── test_evaluator.py               # AST syntax failure interception & recovery
│   ├── test_permissions.py             # Security boundary & path traversal defenses
│   ├── test_memory.py                  # Token compaction & spill cache
│   └── test_harness_loop.py            # End-to-end autonomous coding loop with mock LLM
└── run.py                              # Interactive CLI entrypoint
```

---

## 🛠️ Implementation Phases (Upcoming)

When you are ready to begin coding, we will proceed in systematic phases:

1. **Phase 1: Domain Models & Serialization (`core/models.py`, `core/state.py`)**  
   Strict typed schemas for `ExecutionState`, `PlanStep`, `ToolCallRequest`, and `ToolExecutionResult`.
2. **Phase 2: Deterministic Tools & Security Gate (`core/tools/`, `core/permissions.py`)**  
   Safe filesystem tools, exact search-and-replace patch engine, and interactive permission gate.
3. **Phase 3: Evaluator Layer & Self-Correction Engine (`core/evaluator.py`)**  
   Python AST parsing to catch syntax errors before disk write and feed formatted compiler hints back to the LLM.
4. **Phase 4: Budgeted Memory Engine & Spill Cache (`core/memory.py`)**  
   Sliding window context management, token estimation, and `.harness/spill/` log caching.
5. **Phase 5: Telemetry, Master Loop & CLI Runner (`core/loop.py`, `core/telemetry.py`, `run.py`)**  
   The core FSM loop, duplicate call trip-wires, `.harness/events.jsonl` streaming, and interactive terminal UI.

---

*Take your time reviewing the documents in `docs/` and `PRD_AND_ARCHITECTURE.md`. When you are ready, let's start coding Phase 1!*

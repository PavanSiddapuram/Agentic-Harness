# 01. Core Fundamentals & Mental Model

> **Goal**: Build an unshakeable intuition of what an AI Agent Harness is, how it differs from generic LLM chat wrappers, and why deterministic boundaries are mandatory for code generation.

---

## 1. What is an AI Agent? (Deconstructing the Hype)

At its simplest, an **AI Agent** is an autonomous system where an LLM is placed inside a **feedback control loop**:

$$\text{Observation} \longrightarrow \text{Reasoning (Inference)} \longrightarrow \text{Action (Tool Call)} \longrightarrow \text{Environment Mutation} \longrightarrow \text{New Observation}$$

### The Three Tiers of LLM Applications

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Zero-Shot Completion (Single Turn)                                       │
│    User: "Write a quicksort in Python" ──► LLM ──► Output code               │
│    Limitation: No awareness of project files, no ability to run or verify.  │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. Retrieval-Augmented Generation (RAG)                                     │
│    User Query ──► Vector Search / Chunks ──► LLM ──► Output answer          │
│    Limitation: Read-only. Cannot mutate code, cannot run tests, cannot fix. │
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. Autonomous Agent Harness (Active ReAct Loop)                             │
│    User Goal ──► Harness Plan ──► Tool Call (Read) ──► Tool Call (Patch)    │
│    ──► Tool Call (pytest) ──► Syntax Failure ──► Self-Correction ──► Green  │
│    Power: Interactive, self-healing, multi-step problem solving.             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. The Fundamental Conflict: Stochastic Brain vs. Deterministic Kernel

To build an agent harness, you must internalize this truth:

> **Large Language Models are probabilistic next-token predictors. They have no concept of physical constraints, filesystem hierarchies, syntax rules, or computational cost.**

### What Happens Without a Harness (The Failure Modes)
1. **The Hallucinated Library**: The LLM calls `import super_fast_crypto_v4` which does not exist on PyPI.
2. **The Truncated Patch**: The LLM outputs:
   ```python
   def massive_function():
       # ... [existing 400 lines remain the same] ...
       new_logic()
   ```
   If a dumb script writes this blindly to disk, 400 lines of production code are wiped out instantly.
3. **The Infinite Hallucination Loop**: A patch fails. The model repeats the exact same failing patch 20 times until your OpenAI bill reaches \$50.
4. **Context Overflow Crash**: A command runs `git diff` on a generated minified JS bundle (50MB). The raw text is passed to the LLM. The API throws `400 Bad Request: Context length exceeded`.
5. **Accidental Disaster**: The model generates `rm -rf /` or modifies files outside the repository.

### The Solution: The Harness as an OS Kernel
In an operating system, user programs (untrusted) cannot directly touch memory or disk; they must request system calls (`syscalls`) which are mediated, permission-checked, and virtualized by the kernel.

In `core-harness`:
- **The LLM is the Untrusted User Process.**
- **The Harness is the OS Kernel.**
- **The Tools are the System Calls.**

```
 ┌─────────────────────────────────────────────────────────┐
 │               UNTRUSTED: LLM INFERENCE                  │
 │   "Please run: rm -rf /tmp/test && patch ../secret.env" │
 └────────────────────────────┬────────────────────────────┘
                              │ Syscall Request (Tool Call)
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │             KERNEL: HARNESS POLICY ENFORCEMENT          │
 │   - Regex Check: Destructive pattern rejected?          │
 │   - Canonical Path: Is path inside workspace?           │
 │   - Interactive Approval: Prompt human operator?        │
 │   - AST Verification: Does patch parse cleanly?         │
 └────────────────────────────┬────────────────────────────┘
                              │ Sanitized & Approved
                              ▼
 ┌─────────────────────────────────────────────────────────┐
 │              HARDWARE / LOCAL ENVIRONMENT               │
 │           Subprocess, File System, Git Stash            │
 └─────────────────────────────────────────────────────────┘
```

---

## 3. Why Zero Frameworks?

Many developers reach for **LangChain**, **CrewAI**, or **AutoGen** when building agents. In production coding runtimes (like Claude Code, Cursor Composer, Aider), this is considered an anti-pattern. Here is why:

| Feature | LangChain / CrewAI | `core-harness` (From Scratch) |
| :--- | :--- | :--- |
| **Prompt Inspection** | Buried in 8 layers of nested prompt template classes. | Direct string formatting / raw list of dicts. You see every token. |
| **Context Window Control** | Implicit trimming or sudden API overflow errors. | Explicit token budgeting with deterministic spill-to-disk logic. |
| **Execution Flow** | Dynamic async graph runners; difficult to step-debug. | Explicit `while not state.is_terminal:` loop. 100% debuggable. |
| **State Persistence** | Complex database session stores with pickle/JSON bloat. | Pure Python `dataclasses` serialized directly to `session.json`. |
| **Dependencies** | 40+ transitive dependencies (pydantic, langchain-core, etc.). | **Standard Library only** (`ast`, `subprocess`, `dataclasses`, `re`, `json`). |

---

## 4. The Finite State Machine (FSM) Mental Model

An agent is **not** a chat session. It is a **State Machine**:

```
 [IDLE]
   │
   │ (User Prompt Ingested)
   ▼
 [PLANNING] ◄────────────────────────────────────────┐
   │                                                 │
   │ (Plan Formulated)                               │
   ▼                                                 │
 [CONTEXT_COMPILATION]                               │
   │                                                 │
   │ (Prompt Assembled within Budget)                │
   ▼                                                 │
 [INFERENCE]                                         │
   │                                                 │
   ├── (No tool calls: Pure text response) ──► [TERMINAL_SUCCESS]
   │                                                 │
   └── (Emits ToolCallRequest)                       │
         │                                           │
         ▼                                           │
       [PERMISSION_GATE]                             │
         │                                           │
         ├── (Rejected) ──► [INJECT_REFUSAL] ────────┤
         │                                           │
         └── (Approved)                              │
               │                                     │
               ▼                                     │
             [TOOL_EXECUTION]                        │
               │                                     │
               ▼                                     │
             [EVALUATOR_VERIFICATION]                │
               │                                     │
               ├── (Passed) ──► [RECORD_SUCCESS] ────┤
               │                                     │
               └── (Failed) ──► [INJECT_RECOVERY] ───┘
```

Every state transition is:
1. **Observable**: Emits an event to `.harness/events.jsonl`.
2. **Deterministic**: Follows strict invariant rules.
3. **Idempotent**: If interrupted, the session can be safely restored from `session.json`.

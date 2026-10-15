# 03. Under-The-Hood Execution Flows

> **Goal**: Trace every micro-step of execution from the moment the user types a command to the moment the harness shuts down. Understand the data transformations, state transitions, and error interception loops.

---

## 1. The High-Level Lifecycle

The complete execution of `core-harness` is composed of five distinct stages:

```
┌─────────────────┐
│ 1. INIT         │ Load config, inspect workspace, hydrate session.json (if resuming)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 2. COMPILE      │ Assemble context: System Prompt + Permanent Plan + Sliding Window
└────────┬────────┘
         ▼
┌─────────────────┐
│ 3. INFERENCE    │ Model API generates Next Token Stream -> Emits Tool Call or Message
└────────┬────────┘
         ▼
┌─────────────────┐
│ 4. DISPATCH     │ Permission Gate -> Safe Execution -> AST / Exit Code Evaluation
└────────┬────────┘
         ▼
┌─────────────────┐
│ 5. RECONCILE    │ Update State (turns, tokens, dirty files) -> Emit JSONL Telemetry
└────────┬────────┘
         │
         ├─── (Not Terminal: Loop back to 2. COMPILE)
         └─── (Terminal: Max turns reached, Budget hit, or Goal accomplished) -> SHUTDOWN
```

---

## 2. End-to-End Sequence Trace: A Bug Fix Scenario

Suppose the user asks:
> *"Fix the bug in `utils.py` where `divide(a, b)` crashes on zero."*

Here is the exact step-by-step trace of what happens under the hood:

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer (Terminal)
    participant Loop as HarnessLoop
    participant Mem as MemoryEngine
    participant LLM as Model Gateway
    participant Gate as PermissionGate
    participant Tool as ToolDispatch
    participant Eval as Evaluator
    participant Disk as Local Filesystem

    Dev->>Loop: run("Fix the bug in utils.py...")
    Loop->>Mem: add_user_message("Fix the bug in utils.py...")
    
    Note over Loop: === TURN 1: DISCOVERY ===
    Loop->>Mem: compile_context(plan, dirty_files)
    Mem-->>Loop: [SystemPrompt, UserMessage]
    Loop->>LLM: POST /chat/completions (tools=[read_file, patch_file, ...])
    LLM-->>Loop: ToolCallRequest(id="c1", tool="read_file", params={"filepath": "utils.py"})
    
    Loop->>Gate: assess("read_file")
    Gate-->>Loop: AUTO_ALLOW
    Loop->>Tool: execute(read_file, "utils.py")
    Tool->>Disk: Read utils.py
    Disk-->>Tool: "def divide(a, b):\n    return a / b"
    Tool-->>Loop: ToolExecutionResult(output="def divide(a, b):\n    return a / b")
    Loop->>Eval: evaluate(read_file, result)
    Eval-->>Loop: Success (no code modified)
    Loop->>Mem: add_tool_result("c1", content)
    
    Note over Loop: === TURN 2: ATTEMPTED PATCH WITH SYNTAX ERROR ===
    Loop->>Mem: compile_context(...)
    Loop->>LLM: POST /chat/completions
    LLM-->>Loop: ToolCallRequest(id="c2", tool="patch_file", params={"filepath": "utils.py", "search": "return a / b", "replace": "if b == 0\n        return 0\n    return a / b"})
    
    Loop->>Gate: assess("patch_file")
    Gate-->>Dev: [SECURITY GATE] Approve patch on utils.py? [y/N]
    Dev-->>Gate: "y" (Approved)
    
    Loop->>Tool: execute(patch_file)
    Tool->>Disk: Apply replacement to utils.py
    Tool-->>Loop: ToolExecutionResult(success=True, modified_files=["utils.py"])
    
    Loop->>Eval: evaluate(patch_file, result)
    Note over Eval: Run ast.parse(utils.py)<br/>Detects missing ':' on `if b == 0`
    Eval-->>Loop: ToolExecutionResult(success=False, feedback="AST SyntaxError on line 2: expected ':'")
    
    Loop->>Mem: add_tool_result("c2", success=False, "ERROR: AST SyntaxError on line 2: expected ':'")
    
    Note over Loop: === TURN 3: SELF-HEALING RECOVERY ===
    Loop->>Mem: compile_context(...)
    Note over Mem: LLM receives its own error feedback!
    Loop->>LLM: POST /chat/completions
    LLM-->>Loop: ToolCallRequest(id="c3", tool="patch_file", params={"filepath": "utils.py", "search": "if b == 0\n        return 0", "replace": "if b == 0:\n        return 0"})
    
    Loop->>Gate: assess("patch_file")
    Gate-->>Dev: Approve patch? [y/N]
    Dev-->>Gate: "y"
    Loop->>Tool: execute(patch_file)
    Tool->>Disk: Update utils.py
    Loop->>Eval: evaluate(patch_file, result)
    Note over Eval: ast.parse(utils.py) -> Valid!
    Eval-->>Loop: Success!
    Loop->>Mem: add_tool_result("c3", success=True)
    
    Note over Loop: === TURN 4: RUN TEST RUNNER ===
    Loop->>LLM: POST /chat/completions
    LLM-->>Loop: ToolCallRequest(id="c4", tool="execute_command", params={"command": "pytest tests/test_utils.py"})
    Loop->>Gate: assess("execute_command")
    Gate-->>Dev: Approve `pytest tests/test_utils.py`? [y/N]
    Dev-->>Gate: "y"
    Loop->>Tool: execute("pytest ...")
    Tool-->>Loop: "Exit code: 0\n1 passed in 0.02s"
    Loop->>Eval: evaluate("execute_command", result)
    Eval-->>Loop: Success!
    Loop->>Mem: add_tool_result("c4", "1 passed in 0.02s")
    
    Note over Loop: === TURN 5: TERMINAL REPORT ===
    Loop->>LLM: POST /chat/completions
    LLM-->>Loop: TextResponse("I have updated divide() to handle b == 0 safely and confirmed tests pass.")
    Loop->>Dev: Print final response & Terminate
```

---

## 3. Data Transformations at Each Boundary

Understanding the exact shape of Python objects transitioning through the system prevents runtime typing bugs:

### Step A: The Tool Call Request (From LLM)
```python
ToolCallRequest(
    call_id="call_001",
    tool_name="patch_file",
    parameters={
        "filepath": "calculator.py",
        "search": "def add(a, b):\n    return a - b",
        "replace": "def add(a, b):\n    return a + b"
    }
)
```

### Step B: The Raw Tool Execution (From Operating System)
```python
ToolExecutionResult(
    call_id="call_001",
    success=True,
    output="Successfully replaced 1 occurrence in calculator.py",
    evaluator_feedback=None,
    modified_files=["calculator.py"]
)
```

### Step C: The Evaluator Pass (Pre-Inference Guard)
If `calculator.py` had a syntax error:
```python
ToolExecutionResult(
    call_id="call_001",
    success=False,
    output="Successfully replaced 1 occurrence in calculator.py",
    evaluator_feedback="AST SyntaxError on line 5: invalid syntax. Ensure indentation matches.",
    modified_files=["calculator.py"]
)
```

### Step D: The Injected Synthetic Message (Into Context)
```json
{
  "role": "tool",
  "tool_call_id": "call_001",
  "content": "ERROR: Execution failed validation.\nFeedback: AST SyntaxError on line 5: invalid syntax.\nRaw Output: Successfully replaced 1 occurrence in calculator.py"
}
```

---

## 4. Loop Termination Invariants

The loop will strictly stop if **any** of the following conditions evaluate to `True`:

1. **Terminal Completion**: The LLM returns a text response with **no tool calls**.
2. **Turn Limit Ceiling**: `state.current_turn >= state.max_turns` (default 25).
3. **Token Budget Cap**: `state.tokens_consumed >= state.max_token_budget` (default 200,000).
4. **Dollar Spend Cap**: Estimated run cost reaches threshold (e.g. \$0.50).
5. **Infinite Loop Trip-wire**: The exact same tool call (identical hash of `tool_name` + `parameters`) failed twice consecutively.
6. **User Abort**: The user inputs `n` at an interactive permission prompt.

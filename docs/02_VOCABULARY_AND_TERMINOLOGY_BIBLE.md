# 02. Vocabulary & Terminology Bible

> **Purpose**: Master the exact vocabulary of AI agent architectures, language model runtimes, compiler mechanics, and systems engineering. Build mental models that translate directly into clean code.

---

## 1. Large Language Model & Transformer Fundamentals

### Token
- **Definition**: The fundamental atomic unit of information processed by an LLM. Unlike human words or characters, tokens are sub-word chunks produced by Byte-Pair Encoding (BPE).
- **Rule of Thumb**: 1 token $\approx$ 4 characters in English, or ~0.75 words. 1,000 tokens $\approx$ 750 words.
- **Harness Significance**: Code has higher token density due to punctuation, camelCase, and leading indentation whitespace. You must calculate token limits carefully.

### Context Window
- **Definition**: The fixed-length sequence buffer (in tokens) that an attention mechanism can evaluate simultaneously.
- **Components**: $\text{Context Window} = \text{System Prompt} + \text{Conversation History} + \text{Tool Schemas} + \text{Tool Results} + \text{Model Output Buffer}$.
- **Harness Significance**: Exceeding the window causes an unrecoverable HTTP 400 error. The harness must enforce a **token envelope** well below the provider limit.

### KV Cache (Key-Value Cache)
- **Definition**: In Transformer self-attention, for tokens $1 \dots t-1$, key ($K$) and value ($V$) projections are cached in GPU memory so the model does not recalculate attention over previous tokens when generating token $t$.
- **Prompt Caching**: If the prompt prefix (system prompt, initial instructions) matches previous calls bit-for-bit, modern providers (Anthropic, Gemini, OpenAI) reuse the KV cache, saving up to 90% cost and drastically reducing latency.
- **Harness Invariant**: Never randomize or dynamically alter the top of the prompt; keep the system message and tool definitions byte-identical to maximize KV cache hits.

### Autoregressive Generation
- **Definition**: A generation process where output tokens are produced sequentially one at a time:
  $$P(w_{1:T}) = \prod_{t=1}^{T} P(w_t \mid w_{<t})$$
- **Harness Significance**: The model cannot "jump backward" to fix an earlier token in the same turn. If it begins generating a bad patch, it will commit to it until the tool execution phase catches it.

### Temperature & Top-p (Nucleus Sampling)
- **Temperature ($T$)**: Modulates the sharpness of the probability distribution over the vocabulary. $T \to 0$ approaches pure argmax (greedy deterministic decoding).
- **Top-p ($p$)**: Selects only from the cumulative top probability mass $p$.
- **Harness Standard**: For coding agents, use $T = 0.0$ or $0.1$. High temperatures introduce syntax errors and hallucinated filenames.

---

## 2. Agentic Architecture & Harness Mechanics

### ReAct (Reasoning + Acting)
- **Definition**: A prompting and execution paradigm where the model alternates between emitting explicit reasoning ("Thought"), generating an action ("Action"), and absorbing the environment feedback ("Observation").
- **Flow**: `Thought -> Tool Call -> Harness Execution -> Tool Result -> Next Thought`.

### Tool Calling / Function Calling
- **Definition**: An API-level contract where the LLM does not generate conversational prose, but instead outputs a JSON structure conforming to a specified JSON Schema:
  ```json
  {
    "call_id": "call_9823",
    "tool_name": "patch_file",
    "parameters": {
      "filepath": "core/models.py",
      "search": "status: str",
      "replace": "status: TaskStatus"
    }
  }
  ```
- **Harness Role**: The LLM *never* executes the tool. The LLM only proposes the invocation. The harness evaluates, authorizes, and executes it.

### Idempotency
- **Definition**: The property of an operation whereby applying it multiple times produces the exact same state as applying it once ($f(f(x)) = f(x)$).
- **Harness Role**: If a network glitch or crash occurs, resuming a turn must never double-apply a patch or execute duplicate mutating commands.

### Sliding Context Window
- **Definition**: An active memory management strategy where the oldest turns are evicted or summarized, keeping only the most recent $K$ turns in context while preserving permanent directives.

### Spill File
- **Definition**: Writing oversized command outputs or file dumps to an ephemeral disk directory (`.harness/spill/`) and passing a truncated summary to the LLM.
- **Example**: `"[Output 12,000 lines truncated. Head & Tail shown. Full log: .harness/spill/run_42.log]"`.

### Synthetic Error Injection
- **Definition**: When a tool fails validation (e.g. invalid AST or denied permission), the harness synthesizes a structured tool result message and injects it into the conversation history, prompting the model to self-correct.

### Goal Drift
- **Definition**: The tendency of an agent, after 10+ turns of fixing small errors, to forget its primary original goal.
- **Harness Defense**: Injecting the high-level Planner state (`[PENDING]`, `[IN_PROGRESS]`, `[DONE]`) at the head of every context compilation.

---

## 3. Systems Programming, OS & Security Concepts

### Path Traversal (`../../`)
- **Definition**: An attack or error where relative paths break out of the intended root directory to inspect or overwrite host files.
- **Defense**: Canonicalization via `os.path.realpath` and verification via `os.path.commonpath([workspace, target]) == workspace`.

### Subprocess Pipe Deadlock
- **Definition**: In Unix/Windows, child processes have fixed-size OS pipe buffers (typically 64KB). If a child writes more than 64KB to `stdout` while the parent is waiting synchronously without reading, the child blocks forever, freezing the harness.
- **Defense**: Use `subprocess.Popen.communicate(timeout=30)` which handles OS buffers asynchronously in separate threads.

### Git Working Tree Shadow Stash
- **Definition**: Creating a low-level Git tree commit (`git write-tree`) without touching the user's active branch or commit history.
- **Harness Role**: Provides a zero-friction rollback checkpoint before the agent modifies files. If an agent ruins a codebase, the harness can perform an instant rollback.

### AST (Abstract Syntax Tree)
- **Definition**: A tree representation of the syntactic structure of source code, generated by a compiler or language parser.
- **Harness Role**: Python's `ast.parse()` validates whether modified code can compile before committing it to disk, stopping syntax corruption in turn 1 instead of discovering it in turn 10.

---

## 4. Telemetry & Observability Terms

### JSONL (JSON Lines)
- **Definition**: A text format where each line is a valid JSON object.
- **Advantage**: Can be appended to sequentially with $O(1)$ writes without loading the entire file into memory; ideal for streaming event logs (`.harness/events.jsonl`).

### Cumulative Token Delta ($\Delta T$)
- **Definition**: The token change between successive turns. Tracking $\Delta T$ allows real-time calculation of burn rate and immediate trip-wire activation if a turn consumes excessive context.

# 04. Math, Economics & Context Budgeting

> **Goal**: Master the quantitative side of agent engineering: token arithmetic, context window budgeting, cost calculation formulas, KV-cache optimization, and compaction algorithms.

---

## 1. Token Arithmetic & Rules of Thumb

Large Language Models do not read characters or bytes; they read **tokens**.

### General Heuristics
- **English Prose**: 1 token $\approx$ 0.75 words $\approx$ 4 characters.
- **Source Code**: 1 token $\approx$ 2.5 to 3.5 characters.
  - Variable names with underscores or camelCase split into multiple tokens (`get_user_by_id` $\to$ `['get', '_user', '_by', '_id']`).
  - Indentation whitespace (e.g. 4 spaces) can consume 1 token per indentation level.
- **Tool Definitions**: Defining 5 tool schemas in JSON Schema typically consumes 800 to 1,500 tokens before any conversation begins!

### Rough Token Estimation in Python (Without external tokenizer)
In standard library Python (zero external dependencies like `tiktoken`), we use the defensive character heuristic:
```python
def estimate_tokens(text: str) -> int:
    """Defensive token estimation for code & JSON: ~3.2 chars per token."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.2))
```

---

## 2. The Quadratic Token Explosion Problem

In a naive conversational agent, every turn appends the new message and sends the **entire accumulated history** back to the API.

Let:
- $P$ = Size of System Prompt and Tool Schemas (~1,500 tokens).
- $U$ = Average size of each turn's input/output exchange (~1,000 tokens).
- $N$ = Number of turns executed.

The context size at turn $k$ is:
$$\text{Context}_k = P + k \cdot U$$

The **cumulative tokens processed** by the API over $N$ turns is:
$$\text{Total Tokens} = \sum_{k=1}^{N} (P + k \cdot U) = N \cdot P + U \cdot \frac{N(N+1)}{2} = \mathcal{O}(N^2)$$

### Numerical Impact Table ($P = 1,500, U = 1,000$)

| Turn ($N$) | Context Size at Turn $N$ | Cumulative Tokens Sent to API | Cumulative Cost (at \$3/1M in, \$15/1M out) |
| :---: | :---: | :---: | :---: |
| 1 | 2,500 tokens | 2,500 tokens | \$0.010 |
| 5 | 6,500 tokens | 22,500 tokens | \$0.090 |
| 10 | 11,500 tokens | 70,000 tokens | \$0.280 |
| 20 | 21,500 tokens | 240,000 tokens | \$0.960 |
| 30 | 31,500 tokens | 510,000 tokens | \$2.040 |

Notice how at turn 25, you are paying **10x more per turn** than at turn 1, even if you are only asking a 1-line question!

---

## 3. The 3-Tier Budgeted Memory Architecture

To break the $\mathcal{O}(N^2)$ quadratic explosion, `core-harness` enforces a **3-tier budgeted context manager**:

```
┌───────────────────────────────────────────────────────────┐
│ Tier 1: Fixed System Prompt & Tool Schemas               │  ~1,500 tokens (Static)
├───────────────────────────────────────────────────────────┤
│ Tier 2: Planner State & Workspace Dirty Tracker           │  ~500 tokens (Overwritten)
├───────────────────────────────────────────────────────────┤
│ Tier 3: Sliding Message History Window                    │
│   - Turn N-3 (Tool results compacted/truncated)           │  ~400 tokens
│   - Turn N-2 (Tool results compacted/truncated)           │  ~400 tokens
│   - Turn N-1 (Full Tool Call & Evaluator Result)          │  ~1,000 tokens
│   - Turn N   (Current In-Progress Turn)                   │  ~1,000 tokens
└───────────────────────────────────────────────────────────┘
Total Steady-State Context: <= 5,000 tokens (Linear Growth!)
```

### The Compaction Strategy
1. **Tier 1 (Immutable)**: The system prompt never mutates mid-session, maximizing **KV-cache reuse**.
2. **Tier 2 (Current Snapshot)**: Instead of keeping 10 turns of "I updated step 1, then step 2...", we keep **only the latest Planner state**:
   ```
   [TODO LIST]
   1. Read calculator.py [DONE]
   2. Patch add() to support float [DONE]
   3. Run pytest [IN_PROGRESS]
   ```
3. **Tier 3 (Sliding Window)**: Older messages beyond $K$ turns (default $K = 6$) are pruned, while older tool outputs are truncated.

---

## 4. The Spill File Mechanism

Suppose an agent runs:
```bash
git diff
```
or
```bash
cat large_generated_bundle.js
```
The command produces 500KB (150,000 tokens) of output. If sent to the LLM, the API will fail immediately.

### The Spill Algorithm:
```python
def handle_tool_output(call_id: str, raw_output: str, max_chars: int = 4000) -> str:
    if len(raw_output) <= max_chars:
        return raw_output
    
    # Spill to local cache directory
    spill_dir = os.path.join(".harness", "spill")
    os.makedirs(spill_dir, exist_ok=True)
    spill_path = os.path.join(spill_dir, f"{call_id}.log")
    
    with open(spill_path, "w", encoding="utf-8") as f:
        f.write(raw_output)
        
    lines = raw_output.splitlines()
    head = "\n".join(lines[:25])
    tail = "\n".join(lines[-25:])
    
    return (
        f"[OUTPUT TRUNCATED: {len(lines)} lines total. Showing first 25 and last 25 lines.]\n"
        f"{head}\n"
        f"\n... [{len(lines) - 50} lines omitted] ...\n\n"
        f"{tail}\n"
        f"\n[FULL OUTPUT CACHED TO: {spill_path}]"
    )
```

---

## 5. Cost Formula and Budget Trip-Wires

Every turn tracks token deltas and calculates exact spend:

$$\text{Spend} = \frac{\text{Input Tokens}}{1,000,000} \times \text{Price}_{\text{in}} + \frac{\text{Output Tokens}}{1,000,000} \times \text{Price}_{\text{out}}$$

### Trip-Wire Rules:
1. **Per-Turn Spending Alarm**: If a single turn consumes $> 30,000$ tokens, flag a context-leak warning.
2. **Hard Ceiling**: If cumulative spend reaches `max_spend_usd` (default \$0.50), halt execution immediately and notify the developer.

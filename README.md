# Agentic Harness

A full-lifecycle harness for coding agents working on large codebases: from a PRD or bug report, through code and tests, to CI, rollout and production signals, and back into the next spec.

**Zero dependencies.** Everything runs on the Python 3.10+ standard library. There is nothing to `pip install`, and it works against a codebase in any language: point `--workspace` at it.

Today the repo has the agent loop (`core/`) and the first context tools (`planes/context/`). The rest of the lifecycle is planned in [docs/ROADMAP.md](docs/ROADMAP.md).

## What works today

| Piece | What it does |
| --- | --- |
| `core/loop.py` | Turn loop: budget checks, inference, permission gate, tool dispatch, evaluation |
| `core/state.py` | Session state with atomic writes to `.harness/session.json`; turn, token and dollar ceilings; repeated-failure trip-wire |
| `core/memory.py` | System prompt, plan/dirty-file anchor, sliding message window, spill-to-disk for large tool output |
| `core/project_context.py` | Loads the target project's `AGENTS.md` (or `CLAUDE.md`, or Copilot instructions) into the prompt and lists subdirectory `AGENTS.md` files |
| `core/output_filter.py` | Strips colour codes, progress bars, passing-test lines and repeats from command output; saves the raw output to `.harness/raw/` |
| `core/permissions.py` | Risk tiers, destructive-command denylist, interactive approval, and project owner gates from `harness.gates.json` |
| `core/evaluator.py` | Python AST and JSON validation after edits; correction hints on failed commands |
| `core/tools/` | Jailed file tools, exact search-and-replace patcher, shell runner with timeouts |
| `planes/context/` | `map_repository` and `find_symbol`: a symbol map and blast radius in one call, for Python, TS/JS, Go, Rust, Java/Kotlin/C#, C/C++, Ruby, PHP, Swift |
| `core/telemetry.py` | JSON Lines event stream to `.harness/events.jsonl` |
| `core/model_client.py` | Raw HTTP client for any OpenAI-compatible API, plus a scripted mock |

Known limits, on purpose for now: no sandbox (shell commands run on the host after approval), a character-based token estimate, regex-based symbols for non-Python languages, and a denylist that is defense in depth, not a security boundary.

## Quickstart

```bash
# Run the tests (standard library unittest; pytest also works if you have it)
python -m unittest

# Try the loop with the scripted mock model (no API key needed)
python run.py --mock --prompt "inspect the workspace"

# Run against a real model on any repo
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1   # or any compatible endpoint
python run.py --workspace path/to/any/repo --prompt "fix the failing test in the checkout module"
```

`--auto-approve` skips confirmation prompts, including shell commands. The denylist and the project's owner gates still apply. Use it only in a throwaway workspace.

## Using it on your project

Nothing is required. Three optional files in the target repo's root make it better:

| File | Effect |
| --- | --- |
| `AGENTS.md` | Your conventions (test command, layout, rules). Inlined into the system prompt. Put more `AGENTS.md` files in subdirectories for subsystem rules; the agent is told where they are. |
| `harness.gates.json` | Actions only a human may take: command regexes and protected path globs. Enforced before approval, even with `--auto-approve`. A malformed file stops the harness. See [docs/harness.gates.example.json](docs/harness.gates.example.json). |
| `.harnessignore` | Extra globs for the context tools to skip (generated code, fixtures). `.gitignore` is read too. |

## Repository layout

```
core/                 Execution plane: the agent loop and its tools
planes/context/       Context plane: repo map and symbol finder
tests/                Tests for everything, plus regression tests for fixed defects
docs/ROADMAP.md       The six planes and the build order
docs/core-harness-prd.md   Original PRD and low-level design for core/
docs/learning/        Study notes written while designing core/
run.py                CLI entrypoint
AGENTS.md             Conventions for agents (and people) changing this repo
```

## Credits

The project-instructions loader, output filter, context tools and owner gates reimplement ideas from [vjvarada/coding-agent-setup](https://github.com/vjvarada/coding-agent-setup) (DOX contracts, RTK, Graphify, plan-guard) in the standard library, so they need no installs.

## Contributing

Read [AGENTS.md](AGENTS.md) first. Every change ships with a test, and every bug fix ships with a test that fails without it.

# Agentic Harness

A full-lifecycle harness for coding agents working on large codebases: from a PRD or bug report, through code and tests, to CI, rollout and production signals, and back into the next spec.

Today the repo contains the first piece, `core/`: a small, dependency-free agent loop. The rest of the lifecycle is planned in [docs/ROADMAP.md](docs/ROADMAP.md) and will land here.

## What works today

`core/` is a deterministic execution loop around any OpenAI-compatible model, written with the Python standard library only.

| Module | Responsibility |
| --- | --- |
| `core/loop.py` | Turn loop: budget checks, inference, permission gate, tool dispatch, evaluation |
| `core/state.py` | Session state with atomic writes to `.harness/session.json`; turn, token and dollar ceilings; repeated-failure trip-wire |
| `core/memory.py` | System prompt, plan/dirty-file anchor, sliding message window, spill-to-disk for large tool output |
| `core/permissions.py` | Risk tiers (auto-allow, confirm, restricted), destructive-command denylist, interactive approval |
| `core/evaluator.py` | Python AST and JSON validation after edits; correction hints on failed commands |
| `core/tools/` | Jailed file tools, exact search-and-replace patcher, shell runner with timeouts |
| `core/telemetry.py` | JSON Lines event stream to `.harness/events.jsonl` |
| `core/model_client.py` | Raw HTTP client for OpenAI-compatible APIs, plus a scripted mock |

Known limits, on purpose for now: no sandbox (shell commands run on the host after approval), a character-based token estimate, and a regex denylist that is defense in depth, not a security boundary.

## Quickstart

Requires Python 3.10 or newer. No runtime dependencies.

```bash
# Run the test suite
python -m pip install pytest
python -m pytest

# Try the loop with the scripted mock model (no API key needed)
python run.py --mock --prompt "inspect the workspace"

# Run against a real model
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1   # or any compatible endpoint
python run.py --workspace path/to/repo --prompt "fix the failing test in tests/test_calc.py"
```

`--auto-approve` skips every confirmation prompt, including shell commands. Only the denylist still applies, so use it only in a throwaway workspace.

## Repository layout

```
core/                 Execution plane: the agent loop and its tools
tests/                Unit and loop tests, plus regression tests for fixed defects
docs/ROADMAP.md       The six planes and the build order
docs/core-harness-prd.md   Original PRD and low-level design for core/
docs/learning/        Study notes written while designing core/
run.py                CLI entrypoint
AGENTS.md             Conventions for agents (and people) changing this repo
```

## Contributing

Read [AGENTS.md](AGENTS.md) first. Every change ships with a test, and every bug fix ships with a test that fails without it.

# AGENTS.md

Conventions for any agent or person changing this repo. Read this before editing.

## Commands

- Test: `python -m unittest` (standard library; pytest also works if installed)
- Smoke run: `python run.py --mock --prompt "inspect the workspace"`

## Rules

- Standard library only, everywhere (`core/`, `planes/`, `kit/setup.py`, tests). The harness must run on any machine with Python 3.10+ and nothing else, against any codebase in any language.
- External tools (RTK, codebase-memory-mcp, ...) are optional: find them with `core.external.find_tool`, keep a stdlib fallback, and test both paths with fake executables (see `tests/test_external_tools.py`).
- Never edit `kit/coding-agent-setup/`; it is an unchanged upstream copy (see its `UPSTREAM.md`).
- Every change ships with a test. Every bug fix ships with a test that fails without the fix (see `tests/test_regressions.py`).
- Changes to `core/permissions.py` must add cases to its tests for both what is blocked and what stays allowed.
- Do not commit `.harness/` runtime data, secrets, or `.env` files.

## Where new work goes

Decide the plane before writing code (see `docs/ROADMAP.md`).

| Kind of work | Home |
| --- | --- |
| Agent loop, tools, state, memory, model client | `core/` |
| Issue or PRD to reproduction and acceptance commands | `planes/intake/` |
| Code context retrieval and indexing | `planes/context/` |
| Preview environments and behavioral checks | `planes/verify/` |
| CI, rollout and canary watching | `planes/delivery/` |
| Telemetry queries and production signal clustering | `planes/signals/` |
| Benchmarks and scoring of the harness itself | `bench/` |
| Setting up other repos; MCP server registry | `kit/setup.py` |

A new plane gets its own directory with a short `AGENTS.md` stating what it owns and its test command.

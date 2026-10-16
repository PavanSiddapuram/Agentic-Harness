# Agentic Harness

A full-lifecycle harness for coding agents working on large codebases: from a PRD or bug report, through code and tests, to CI, rollout and production signals, and back into the next spec.

**Zero required dependencies.** The harness runs on the Python 3.10+ standard library and works against a codebase in any language: point `--workspace` at it.

**Stronger when the best tools are installed.** It picks up [RTK](https://github.com/rtk-ai/rtk) and [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) automatically, and `kit/setup.py` wires a target repo for Claude Code with vjvarada's multi-agent plan and build loops, Graphify, Serena, code-review-graph, and read-only Sentry and Grafana servers. See [docs/TOOLING.md](docs/TOOLING.md) for what each adds and why it was chosen.

Today the repo has the agent loop (`core/`), context tools (`planes/context/`) and the setup kit (`kit/`). The rest of the lifecycle is planned in [docs/ROADMAP.md](docs/ROADMAP.md).

## What works today

| Piece | What it does |
| --- | --- |
| `core/loop.py` | Turn loop: budget checks, inference, permission gate, tool dispatch, evaluation |
| `core/state.py` | Session state with atomic writes to `.harness/session.json`; turn, token and dollar ceilings; repeated-failure trip-wire |
| `core/memory.py` | System prompt, plan/dirty-file anchor, sliding message window, spill-to-disk for large tool output |
| `core/project_context.py` | Loads the target project's `AGENTS.md` (or `CLAUDE.md`, or Copilot instructions) into the prompt and lists subdirectory `AGENTS.md` files |
| `core/output_filter.py` | Strips colour codes, progress bars, passing-test lines and repeats from command output; saves the raw output to `.harness/raw/` |
| `core/permissions.py` | Risk tiers, destructive-command denylist, interactive approval, and project owner gates from `harness.gates.json` and/or `.claude/plan-guard.config.json` |
| `core/evaluator.py` | Python AST and JSON validation after edits; correction hints on failed commands |
| `core/tools/` | Jailed file tools, exact search-and-replace patcher, shell runner with timeouts |
| `planes/context/` | `map_repository` and `find_symbol` (stdlib, any language); `trace_calls` and `change_impact` (resolved call graph, when codebase-memory-mcp is installed) |
| `planes/verify/` | `review_change`: security findings in added lines (secrets redacted), complexity increases, and an explainable risk level; `perf_check`: before/after speed and memory with a noise-aware verdict. Both scoped to the change, with gitleaks, Semgrep and lizard used when installed. The loop reminds the agent to review before finishing. |
| `kit/` | `setup.py install <repo>` and `setup.py doctor`; vendored [vjvarada/coding-agent-setup](https://github.com/vjvarada/coding-agent-setup) |
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

Review a change or check performance from the command line (CI, pre-commit, or a Claude Code hook):

```bash
python -m planes.verify.review --workspace path/to/repo                  # working tree vs HEAD
python -m planes.verify.review --workspace path/to/repo --base origin/main --fail-on high
python -m planes.verify.perf baseline --workspace path/to/repo --cmd "npm run bench"   # before the change
python -m planes.verify.perf compare  --workspace path/to/repo --cmd "npm run bench"   # after
```

Set up another repo for agent work (Claude Code, Copilot, or this harness):

```bash
python kit/setup.py doctor                         # which optional tools you have
python kit/setup.py install path/to/repo --dry-run # see what it would change
python kit/setup.py install path/to/repo           # add --servers sentry,grafana for production signals
```

`--auto-approve` skips confirmation prompts, including shell commands. The denylist and the project's owner gates still apply. Use it only in a throwaway workspace.

## Using it on your project

Nothing is required. Three optional files in the target repo's root make it better:

| File | Effect |
| --- | --- |
| `AGENTS.md` | Your conventions (test command, layout, rules). Inlined into the system prompt. Put more `AGENTS.md` files in subdirectories for subsystem rules; the agent is told where they are. |
| `.claude/plan-guard.config.json` or `harness.gates.json` | Actions only a human may take: command regexes, protected paths, content patterns (such as production flag flips) and protected branches. The harness enforces either file, or both, before approval, even with `--auto-approve`; Claude Code enforces the plan-guard file through the kit's hook, so one file guards both. A malformed file stops the harness. See [docs/harness.gates.example.json](docs/harness.gates.example.json). |
| `.harnessignore` | Extra globs for the context tools and change review to skip (generated code, fixtures). `.gitignore` is read too. |

### Rollout on an existing codebase

1. Clone this repo outside your project, install the optional tools you want, and run `python kit/setup.py doctor`.
2. In your project, on a new branch: `python <harness>/kit/setup.py install . --dry-run`, then without `--dry-run`. It only adds files; it does not modify tracked files. If you already have an `AGENTS.md`, it is kept, and Claude Code is pointed at it with an `@AGENTS.md` import in the `CLAUDE.md` setup creates.
3. Edit `AGENTS.md` (real test and build commands, layout, rules) and `.claude/plan-guard.config.json` (your deploy, migration and production-flag commands). These two files decide most of the quality.
4. Commit `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.mcp.json`, `.cbmignore` and `.graphifyignore`. Commit `graphify-out/` only if the team wants the shared graph.
5. Add the review to CI as a report first (`python -m planes.verify.review --base origin/main`), and make it blocking (`--fail-on high`) once its findings look right on your code.
6. Run agents on real tickets with approval on (no `--auto-approve`) until you trust the gates. There is no sandbox yet: approved shell commands run on your machine.

## Repository layout

```
core/                 Execution plane: the agent loop and its tools
planes/context/       Context plane: repo map, symbol finder, call-graph tools
planes/verify/        Verification plane: change review (security, complexity, risk), perf_check
kit/                  Setup for target repos; vendored coding-agent-setup
tests/                Tests for everything, plus regression tests for fixed defects
docs/ROADMAP.md       The six planes and the build order
docs/TOOLING.md       Optional tools, why each was chosen, how the harness uses them
docs/core-harness-prd.md   Original PRD and low-level design for core/
run.py                CLI entrypoint
AGENTS.md             Conventions for agents (and people) changing this repo
```

## Credits

`kit/coding-agent-setup/` is [vjvarada/coding-agent-setup](https://github.com/vjvarada/coding-agent-setup), vendored unchanged under its MIT license. The harness's own project-instructions loader, output filter, context tools and owner gates reimplement its ideas (DOX contracts, RTK, Graphify, plan-guard) in the standard library, so they work with nothing installed.

## Contributing

Read [AGENTS.md](AGENTS.md) first. Every change ships with a test, and every bug fix ships with a test that fails without it.

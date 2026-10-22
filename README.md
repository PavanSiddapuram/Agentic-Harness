# Agentic Harness

A thin layer that makes the coding agent you already use better on a large codebase. It works with Claude Code, Codex, Gemini CLI, Cursor, Kimi Code and VS Code Copilot, alone or mixed in one team.

Those agents already read and edit files, run commands, plan, run subagents and sandbox. The harness does not compete with them. It adds what they lack on a big repo, through the two extension points they all share:

- **One MCP server** (6 tools at most): find code without reading files (`map_repository`, `find_symbol`), real call graphs and change impact (`trace_calls`, `change_impact`), a security, complexity and risk review of your change (`review_change`), and a before/after speed and memory check (`perf_check`).
- **One hook**: before a shell command or file write, the team's gate file decides what only a human may do (deploy, publish, migrations, production flags, `.env`, protected branches). When the agent tries to finish, the change is reviewed and the agent is sent back once if it added a secret, an injection or similar high-severity issue.

**Zero required dependencies.** Python 3.10+ standard library only, and any language in the target repo. Optional tools ([codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp), [gitleaks](https://github.com/gitleaks/gitleaks), [Semgrep](https://github.com/semgrep/semgrep), [lizard](https://github.com/terryyin/lizard), [RTK](https://github.com/rtk-ai/rtk)) are used when installed. See [docs/TOOLING.md](docs/TOOLING.md).

## Quickstart

```bash
# 1. Install the harness once per machine (puts agentic-harness-* commands on PATH)
pipx install git+https://github.com/PavanSiddapuram/Agentic-Harness
#    or: uv tool install git+https://github.com/PavanSiddapuram/Agentic-Harness

# 2. In your project, on a branch: see what would change, then do it
agentic-harness-setup install . --dry-run
agentic-harness-setup install .                       # agents found on this machine or in the repo
agentic-harness-setup install . --hosts codex,cursor  # or name them; --hosts all

# 3. Open your agent in the project and accept its trust / approval prompt
```

Setup only adds entries; it never changes ones already there. Commit what it writes, so teammates get the same server, hooks and gates. Each teammate needs step 1 once.

| Agent | What setup writes | What you get |
| --- | --- | --- |
| Claude Code | `.mcp.json`, `.claude/settings.json`, plus the vendored [vjvarada kit](https://github.com/vjvarada/coding-agent-setup): plan/build agents, plan-guard, Graphify | tools, gates, review on stop, `/plan-feature` and `/next-ticket` |
| Codex | `.codex/config.toml`, `.codex/hooks.json` | tools, gates (shell and `apply_patch`), review on stop |
| Gemini CLI | `.gemini/settings.json` (also tells it to read `AGENTS.md`) | tools, gates, review after each turn |
| Cursor | `.cursor/mcp.json`, `.cursor/hooks.json` | tools, gates, review on stop |
| Kimi Code | `.kimi-code/mcp.json`; prints a hook block for `~/.kimi-code/config.toml` (Kimi reads hooks only there) | tools; gates and review once you paste the block |
| VS Code Copilot | `.mcp.json` | tools |

Per-agent details, what was verified with each real CLI, and the tradeoffs are in [docs/HOSTS.md](docs/HOSTS.md).

## Files your repo can add

| File | Effect |
| --- | --- |
| `AGENTS.md` | Your conventions (test command, layout, rules). Every supported agent reads it. Setup writes a template if you have none. |
| `.claude/plan-guard.config.json` or `harness.gates.json` | What only a human may do: command patterns, protected paths, content patterns (production flag flips), protected branches. Enforced by the hook for every agent, before the agent's own approval. A malformed file blocks everything until fixed. Example: [docs/harness.gates.example.json](docs/harness.gates.example.json). |
| `.harnessignore` | Globs the context tools and the review skip (generated code, fixtures). `.gitignore` is read too. |

## Also usable without an agent

```bash
agentic-harness-review --workspace . --base origin/main --fail-on high   # in CI
agentic-harness-perf baseline --workspace . --cmd "npm run bench"        # before a change
agentic-harness-perf compare  --workspace . --cmd "npm run bench"        # after
agentic-harness-setup doctor                                             # which optional tools you have
```

Roll the review into CI as a report first, and make it blocking once its findings look right on your code.

## The reference loop (frozen)

`core/` also contains a small agent loop (`agentic-harness-run`, any OpenAI-compatible API) used for tests, scripted CI runs and as a reference. It is deliberately frozen: host agents own execution, so new capabilities go into the MCP server, the hook or a CLI, where every agent gets them. `tests/test_budget.py` enforces this with size budgets.

## Repository layout

```
mcp_server.py         The MCP server every host agent loads
kit/gate_hook.py      The hook every host agent runs (gates before tools, review on stop)
kit/hosts.py          Where each agent keeps its config, and safe merging into it
kit/setup.py          install / doctor
kit/coding-agent-setup/   vjvarada/coding-agent-setup, vendored unchanged (MIT)
planes/context/       Repo map, symbol finder, call-graph tools
planes/verify/        Change review (security, complexity, risk), perf_check
core/                 Shared pieces (gates, project instructions, output filter, MCP client) and the frozen loop
tests/                Tests for everything, including size and tool budgets
docs/                 HOSTS.md, TOOLING.md, ROADMAP.md
```

## Contributing

Read [AGENTS.md](AGENTS.md) first. Every change ships with a test, and every bug fix ships with a test that fails without it.

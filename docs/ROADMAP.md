# Roadmap

The agent loop is the commoditized part. The value is the environment around it: precise context, a checkable definition of done, a real place to test, and eyes on production, with production signals feeding the next spec. This repo grows into that environment one plane at a time, and each plane ships only after it beats a measured baseline.

## The six planes

```
 1 Intent  ->  2 Execution  ->  3 Verification
    ^                               |
    |        6 Context              v
 5 Signals  <-----------------  4 Delivery
        (served to every plane)

 Governance across all planes: risk-classed tools, fail-closed hooks,
 human gates at merge, deploy and remediation
```

| Plane | Job | Lives in | Status |
| --- | --- | --- | --- |
| 1 Intent | Turn an issue or PRD into a failing reproduction plus acceptance commands before any fix | `planes/intake/` | Planned (P1) |
| 2 Execution | Run the agent in an isolated worktree with scoped tools | The host agent (Claude Code, Codex, Gemini CLI, Cursor, Kimi Code) | Owned by the host. The harness plugs in through one MCP server and one hook (`mcp_server.py`, `kit/gate_hook.py`); its own loop in `core/` is frozen as a reference (see `tests/test_budget.py`). Multi-agent plan and build loops come from the host (and the vendored kit for Claude Code), not from the harness |
| 3 Verification | Preview environment with real dependencies; scenario, trace and latency-budget checks against main | `planes/verify/` | Started: change review (security, complexity, risk), `perf_check`, verify-on-stop; preview environments planned (P2) |
| 4 Delivery | Affected-target CI, flaky-test quarantine, canary watch that proposes (never executes) rollback | `planes/delivery/` | Planned (P3) |
| 5 Signals | Read-only telemetry queries over traces, logs, metrics and deploy markers; cluster errors and complaints into issues | `planes/signals/` | Sentry and Grafana servers configurable via `kit/setup.py`; harness-side tools planned (P3, P4) |
| 6 Context | Token-budgeted context pack per task: precise symbols, ownership, recent history, hot runtime paths | `planes/context/` | Started: stdlib map and symbols, `AGENTS.md` loading, call graph and change impact via codebase-memory-mcp |
| Governance | Tool risk classes, audit log, approval queue | `core/permissions.py`, `kit/gate_hook.py` | Partial: owner gates and denylist enforced in every host through one hook; approval and sandboxing stay with the host |

Every plane ships as tools on the one harness MCP server, hook behaviour, or a CLI, so it works in every host agent at once. The MCP server stays at 6 tools or fewer; a plane that needs more tools must replace one, not add one.

## Build order

| Phase | Build | Gate to move on |
| --- | --- | --- |
| P0 Baseline | Benchmark of 20 to 30 already-closed issues from one large open-source repo, with the merged fix hidden; a runner that scores `core/` on it | Baseline recorded for every metric below |
| P1 Intake + context | `/intake` repro-first flow; context-pack server on a precise (SCIP) index | Task success and tokens per task beat baseline |
| P2 Verification | Preview environment with seeded data; scenario, trace and latency checks | Verifier false-pass rate drops |
| P3 Production eyes | Read-only telemetry server with deploy markers; canary watcher drafting rollbacks | Correct root cause on injected faults |
| P4 Signal to spec | Cluster errors and complaints into evidence-backed issues that feed `/intake` | Repeat the benchmark on a second, larger repo |

## Metrics

Task success rate on the benchmark, repro rate, verifier false-pass rate, tokens and wall time per task, human interventions per PR, PRs merged without edits, CI time to first signal, escaped defects and time to fix.

## Hardening `core/` along the way

- Run shell commands in a container or worktree sandbox instead of on the host.
- Count tokens with the provider's tokenizer or reported usage instead of a character estimate.
- Summarize trimmed history instead of dropping it, at a fixed fraction of the real context window.
- Keep the system prompt and tool list byte-stable so provider prompt caching hits.
- Verify-on-stop: refuse a final answer after edits that lack a fresh test run.

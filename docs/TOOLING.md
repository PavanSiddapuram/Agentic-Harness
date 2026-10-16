# Tooling

The harness runs on the Python standard library alone. Everything below is optional: install what you want, and the harness or your agent host uses it. `python kit/setup.py doctor` shows what is installed; `python kit/setup.py install <repo>` wires a target repo.

Chosen October 2026. Each tool is local-first, MIT or Apache licensed, works on any repo, and was run against [expressjs/express](https://github.com/expressjs/express) before being added.

## The stack, by plane

| Plane | Tool | What it adds | Used by |
| --- | --- | --- | --- |
| Context | stdlib `map_repository`, `find_symbol` | Symbol map and whole-word mentions, any language, no installs | Harness, always |
| Context | [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) | Parsed, type-resolved call graph for 158 languages in one static binary; `trace_calls`, `change_impact` | Harness when installed; MCP server `code-graph` |
| Context | [Graphify](https://github.com/Graphify-Labs/graphify) | Knowledge graph over code plus docs, PDFs and notes; query-first hook | Kit installer; MCP server `graphify` |
| Context | [Serena](https://github.com/oraios/serena) | Language-server symbol search, references and symbol-level edits (2.0 exposes them through one `serena_repl` tool) | MCP server `serena` |
| Verification | [code-review-graph](https://github.com/tirth8205/code-review-graph) | Blast radius and affected tests for a diff | MCP server `review-graph` |
| Execution | [RTK](https://github.com/rtk-ai/rtk) | Filters output of 100+ commands (git, test runners, linters, docker, kubectl) | Harness when installed; kit's Claude Code hook |
| Orchestration | [coding-agent-setup](https://github.com/vjvarada/coding-agent-setup) (vendored in `kit/`) | Plan loop and build loop with seven roles, `AGENTS.md` contracts, plan-guard owner gates | Claude Code and Copilot via the kit installer |
| Signals | [Sentry MCP](https://github.com/getsentry/sentry-mcp) | Production errors, issues, traces and root-cause analysis | MCP server `sentry` (OAuth) |
| Signals | [Grafana MCP](https://github.com/grafana/mcp-grafana) | Metrics, logs and traces; configured with `--disable-write` | MCP server `grafana` |

## Why these and not others

- **Graph over embeddings for code.** Structural tools answer "who calls this" and "what does this change break" exactly; embedding search answers "find code like this" approximately and needs a vector database. A [source study of eleven coding agents](https://arxiv.org/pdf/2609.00006) found none relies on embedding retrieval for code.
- **codebase-memory-mcp as the primary graph.** It has no runtime dependencies, and per [its comparison](https://dev.to/coder11/code-review-graph-vs-graphify-vs-codebase-memory-mcp-the-best-code-intelligence-mcp-tools-for-ai-3ea) it indexed the Linux kernel (28M lines) in about 3 minutes with sub-millisecond queries. The harness keeps its server running for the session through a small stdlib MCP client: about 5 seconds to start, then milliseconds per query (the one-shot CLI cost about 7 seconds per call on Express).
- **One graph by default.** `setup.py` adds a single code-intelligence server (code-graph, else Graphify). Each extra server adds tools and its own "use me first" instructions, and a larger tool menu makes agents choose worse; on Express, four servers meant 59 tools. Graphify (code plus PRDs, RFCs and notes), Serena (compiler-accurate edits) and code-review-graph (review) are opt-in with `--servers`.
- **Graphify's output stays out of the code graph.** When Graphify is present, setup adds `graphify-out/` to `.cbmignore` (not `.gitignore`, since teams may commit Graphify's graph).
- **Read-only signals.** SRE practice in 2026 is propose-and-approve; Grafana is configured without write tools, and Sentry's write scopes stay behind its own OAuth consent.

Considered and left out: Sourcegraph (needs a deployed service; worth it for many-repo orgs), Claude Context and CocoIndex (embedding-based; need a vector store or daemon), GitHub Spec Kit (overlaps the kit's plan loop). Parallel-session apps such as Agent Orchestrator, Bernstein and Vibe Kanban run several agents in git worktrees and can drive this harness or Claude Code; they are apps you run, not libraries to vendor.

## How the harness uses them

- **RTK:** for each shell command, the harness asks `rtk rewrite` for a filtered equivalent. It accepts the rewrite only if it is the original command with `rtk ` prefixes inserted, so the approved command is the one that runs. Exit codes are preserved. RTK has no filter for some commands (`npm test` among them); those get the stdlib filter. If a command fails and RTK left only a recall ID (its `rtk test` mode can drop the failing test's name), the harness fetches the full output with `rtk recall`.
- **Test output (stdlib):** passing-test lines and suite headings that only introduce passing tests fold into one marker; failures, their headings and the failure report stay. On Express's 1,261-test suite: green run 16,575 to 215 tokens; one failing function 17,508 to 1,200 tokens.
- **codebase-memory-mcp:** indexed lazily on first use; `change_impact` re-indexes so it sees edits just made, and lists impacted callers and test files.
- **Turning them off:** `HARNESS_NO_EXTERNAL=1` forces the standard-library path everywhere.

## Updating the vendored kit

`kit/coding-agent-setup/` is an unchanged upstream copy; `UPSTREAM.md` records the commit. Replace the folder with a newer copy and update that commit. Harness-specific code lives in `kit/setup.py`.

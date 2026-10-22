# Host agents

The harness plugs into coding agents that already exist instead of being one more. Every supported agent loads local MCP servers and runs command hooks, so the harness ships as one MCP server (`agentic-harness-mcp`) and one hook (`agentic-harness-gate`), and `agentic-harness-setup` writes each agent's project config.

Checked against each agent's documentation and installed CLI in October 2026.

## Per agent

| Agent (version checked) | MCP server config | Hook config | Pre-tool gate covers | Review on stop | AGENTS.md |
| --- | --- | --- | --- | --- | --- |
| Claude Code (2.1.302) | `.mcp.json` | `.claude/settings.json` | Bash, Write, Edit, MultiEdit, NotebookEdit (the kit's plan-guard covers this when installed, so setup adds only the Stop hook) | `Stop` | via `@AGENTS.md` in `CLAUDE.md`, added by setup |
| Codex (0.163.0) | `.codex/config.toml` | `.codex/hooks.json` | `Bash` (argv lists too), `apply_patch` (paths and added lines parsed from the patch, including `Move to`) | `Stop` | native |
| Gemini CLI (0.64.0) | `.gemini/settings.json` | same file | `run_shell_command`, `write_file`, `replace` | `AfterAgent` (after every turn) | `context.fileName` set by setup |
| Cursor (not installable here) | `.cursor/mcp.json` | `.cursor/hooks.json` | `beforeShellExecution`; writes via `preToolUse` | `stop` (`followup_message`) | native |
| Kimi Code (2.2.0) | `.kimi-code/mcp.json` | user-level only: `~/.kimi-code/config.toml`; setup prints the block | `Bash`, `Write`, `Edit` | `Stop` | native |
| VS Code Copilot | `.mcp.json` | none | none | none | not documented |

How each answers a block: exit code 2 with the reason on stderr (Claude Code, Codex, Gemini CLI, Kimi Code); JSON on stdout for Cursor (`permission: deny`, `followup_message`).

## What was verified with the real CLIs

On a fresh clone of expressjs/express after `agentic-harness-setup install . --hosts all`:

| Agent | Evidence |
| --- | --- |
| Claude Code | `claude mcp list` reads the project server and holds it as "Pending approval" (a cloned repo cannot approve itself); the same command added at local scope health-checks "Connected". |
| Codex | `codex doctor` loads `.codex/config.toml` (parse ok, 1 server) once the project is trusted. Through `codex app-server`, the server went starting to ready in about 100 ms with all 6 tools, and `trace_calls` and `review_change` called through Codex returned real results. |
| Gemini CLI | `gemini mcp list`: "Disabled" in an untrusted folder, "Connected" once trusted. The settings file with our hooks loads cleanly; a deliberately broken hook entry is rejected, so the check is real. |
| Kimi Code | The printed `[[hooks]]` block loads cleanly; a block with an extra field is rejected ("Ignored invalid config section 'hooks'"). The project `mcp.json` follows the documented format; Kimi Code has no command to list servers. |
| Cursor | Not installable in the test environment. Files follow Cursor's documented formats; hook payloads are tested with the documented shapes. |

Hooks were not fired by a live agent turn (that needs model credentials); `tests/test_gate_hook.py` drives the hook with each agent's documented payload, and `tests/test_hosts.py` checks every config file the setup writes.

## Tradeoffs

| Decision | Chosen | Given up | Why |
| --- | --- | --- | --- |
| Who runs the agent loop | The host agent | Control over the loop's internals | Hosts already have better models, sandboxes, approvals, subagents and IDE integration, and improve every month. The harness loop is frozen at about 1,650 lines and budgeted in `tests/test_budget.py`. |
| How many MCP tools | 6 at most | The raw graph's 17 tools, Serena's, Graphify's | Every tool costs each request prompt space and tool-selection accuracy. Extras stay one flag away (`--servers`). |
| What the pre-tool hook blocks | Only what the team's gate file and the denylist name | Policy beyond that | The host's own approval flow stays in charge; the hook is the floor it cannot argue past. It fails closed (broken gate file or crash blocks). |
| What the stop hook blocks | High-severity findings, once per identical set, never medium | Forcing every finding to be fixed | An agent that chose to explain a finding is not sent back again, and a hook bug never traps a session (fails open). Medium findings are in `review_change` and CI. |
| Stop-hook depth | Builtin rules and gitleaks | Semgrep at stop | Speed: the hook runs at the end of every turn in Gemini. Semgrep runs in `review_change` and CI. |
| Hook speed | Python, 43 ms per checked call (21 ms is interpreter start) | A native binary's ~5 ms | One language and the same gate code as everything else. No argparse, no git subprocess, lazy imports on the hot path. |
| Where config lives | Project files the team commits | Per-user setup | Everyone on the team gets the same server, hooks and gates. Each agent asks once to trust the project. Kimi Code is the exception: its hooks are user-level. |
| How configs name the harness | Commands on PATH (`agentic-harness-*`) | Zero-install use | Paths would only work on one machine. Setup falls back to paths, and says so, when the commands are missing. |
| Claude Code gates | The kit's plan-guard when present | One gate implementation everywhere | Avoids two hooks blocking the same call; both read the same `.claude/plan-guard.config.json`. |
| Dependencies | Python standard library only | Faster parsers in the core | Works on any machine and any repo; optional tools (codebase-memory-mcp, gitleaks, Semgrep, lizard, RTK) add depth when installed. |

## Limits

- No live agent turn was run, so hook firing inside each agent is checked through documented payloads, not observed.
- Cursor support follows its documentation and is untested against a real Cursor build.
- Kimi Code needs the hook block pasted into `~/.kimi-code/config.toml` by each user.
- VS Code Copilot gets the tools but no gates or review hook.
- The hooks are only as strong as the gate file: the regex denylist is defense in depth, not a sandbox. Sandboxing is the host's job.

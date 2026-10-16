# planes/verify

Owns checks on a change before it ships: security findings, complexity increases, a risk level, and before/after performance. Everything here looks at the change (working tree vs a base ref, untracked files included), not the whole repo.

- `diff.py`: collects changed files, added line numbers, and before/after text from git.
- `security.py`: builtin pattern rules (secrets, injection, eval, TLS, deserialization, HTML injection) on added lines; gitleaks and Semgrep when installed. Secret values are always redacted.
- `complexity.py`: cyclomatic complexity of touched functions, before vs after. Python via `ast` (exact); lizard when installed; a brace heuristic otherwise. Only increases are reported.
- `risk.py`: a deterministic, explainable score from named factors (size, sensitive areas, findings, complexity, dependencies, missing tests, removed public symbols, caller count).
- `review.py`: the `review_change` tool and `python -m planes.verify.review` CLI.
- `perf.py`: the `perf_check` tool and CLI: repeated timed runs, a stored baseline, and a noise-aware comparison.

Rules: standard library only; external scanners are optional and tested with fake executables. A new security rule needs a positive and a negative test in `tests/test_verify.py`. Rules flag risky constructs; they cannot prove a bug, so messages say what to check, not that the code is broken.

Test: `python -m unittest tests.test_verify tests.test_verify_on_stop tests.test_perf`

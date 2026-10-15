# planes/context

Owns code context retrieval: finding where code lives and what touches it, for any language, with no dependencies.

- `index.py`: walks the workspace (skipping `.git`, `node_modules`, build output, minified and generated files, plus `.harnessignore` and `.gitignore` globs) and caches symbols per file by mtime. Python symbols come from `ast` and are exact; other languages use declaration regexes and are heuristic.
- `tools.py`: the read-only agent tools `map_repository` and `find_symbol`.

Rules: standard library only. A new language means a pattern list in `LANG_PATTERNS` plus a test in `tests/test_context_tools.py`. Precise indexes (SCIP) plug in later behind `RepoIndex`.

Test: `python -m unittest tests.test_context_tools`

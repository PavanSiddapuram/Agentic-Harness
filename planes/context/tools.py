"""Read-only context tools: one call instead of a list/read/grep loop.

map_repository  -> where things live: files and their declared symbols
find_symbol     -> where one symbol is defined and every place it is mentioned
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from core.models import ToolExecutionResult
from core.tools.base import BaseTool
from planes.context.index import RepoIndex


class MapRepositoryTool(BaseTool):
    name = "map_repository"
    description = (
        "Map the repository: lists source files with their classes, functions and types and "
        "line numbers. Call this first to find where code lives instead of listing directories "
        "and reading files one by one. Narrow with 'path' (a subdirectory) or 'query' "
        "(substring of a symbol or file name)."
    )

    def __init__(self, index: RepoIndex):
        self.index = index

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Subdirectory to map (default: whole repo)"},
                        "query": {"type": "string", "description": "Only symbols or files containing this text"},
                        "max_chars": {"type": "integer", "description": "Output budget in characters (default 6000)"},
                    },
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        scope = parameters.get("path") or "."
        query = (parameters.get("query") or "").lower()
        budget = int(parameters.get("max_chars") or 6000)

        entries: List[tuple] = []  # (rel, symbols)
        scanned = 0
        for rel in self.index.iter_files(scope):
            scanned += 1
            symbols = self.index.symbols_for(rel)
            if query:
                if query not in rel.lower():
                    symbols = [s for s in symbols if query in s.name.lower()]
                    if not symbols:
                        continue
            elif not symbols:
                continue
            entries.append((rel, symbols))

        full = [f"{rel}: " + ", ".join(f"{s.name}:{s.line}" for s in syms) for rel, syms in entries]
        footer: List[str] = []
        if sum(len(e) + 1 for e in full) <= budget:
            lines = full
            header = f"[repository map: {len(entries)} files, {scanned} source files scanned under '{scope}']"
        else:
            # Too big to list: roll up by the next directory level so the agent
            # can see the shape of the repo and drill in with 'path'.
            lines = self._rollup(entries, scope, budget)
            header = (
                f"[repository overview: {len(entries)} files with symbols under '{scope}' exceed the "
                f"{budget}-char budget; showing directories. Call again with 'path' set to one of them, "
                "or with 'query'.]"
            )
        if self.index.last_walk_truncated:
            footer.append(f"[stopped after {self.index.max_files} files; narrow with 'path']")
        if not lines:
            footer.append("[no matching symbols]")
        return ToolExecutionResult(
            call_id="", success=True, output="\n".join([header] + lines + footer)
        )

    @staticmethod
    def _rollup(entries: List[tuple], scope: str, budget: int) -> List[str]:
        base = os.path.normpath(scope)
        groups: Dict[str, List[tuple]] = {}
        for rel, syms in entries:
            inner = os.path.relpath(rel, base) if base != "." else rel
            head = inner.split(os.sep, 1)
            key = os.path.normpath(os.path.join(base, head[0])) + (os.sep if len(head) > 1 else "")
            groups.setdefault(key, []).append((rel, syms))

        ranked = sorted(groups.items(), key=lambda kv: -sum(len(s) for _, s in kv[1]))
        lines: List[str] = []
        used = 0
        for key, items in ranked:
            n_syms = sum(len(s) for _, s in items)
            # Name a few of the biggest top-level symbols so the directory means something.
            top = [
                s.name for _, syms in items for s in syms
                if s.kind in ("class", "function", "type") and not s.name.startswith("_")
            ][:6]
            label = f"{key}  ({len(items)} files, {n_syms} symbols)"
            if top:
                label += ": " + ", ".join(top)
            if used + len(label) + 1 > budget:
                lines.append(f"[{len(ranked) - len(lines)} more directories not shown]")
                break
            lines.append(label)
            used += len(label) + 1
        return lines


class FindSymbolTool(BaseTool):
    name = "find_symbol"
    description = (
        "Find a symbol: returns where it is defined and every line that mentions it (likely "
        "callers and importers), so you can see the blast radius of a change in one call. "
        "Mentions are whole-word text matches, not compiler-resolved."
    )

    def __init__(self, index: RepoIndex):
        self.index = index

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Symbol name, e.g. 'HarnessLoop' or 'run'"},
                        "path": {"type": "string", "description": "Subdirectory to search (default: whole repo)"},
                        "max_results": {"type": "integer", "description": "Maximum mention lines (default 40)"},
                    },
                    "required": ["name"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        name = (parameters.get("name") or "").strip()
        if not name:
            return ToolExecutionResult(call_id="", success=False, output="find_symbol needs a 'name'.")
        scope = parameters.get("path") or "."
        limit = int(parameters.get("max_results") or 40)
        short = name.rsplit(".", 1)[-1]
        word = re.compile(rf"(?<![\w$]){re.escape(short)}(?![\w$])")

        definitions: List[str] = []
        def_sites = set()
        mentions: List[str] = []
        extra = 0
        for rel in self.index.iter_files(scope):
            for s in self.index.symbols_for(rel):
                # "run" matches run and Class.run; "Class.run" matches only itself.
                if s.name == name or ("." not in name and s.name.endswith("." + name)):
                    definitions.append(f"{rel}:{s.line}  {s.kind} {s.name}")
                    def_sites.add((rel, s.line))
            text = self.index.read_text(rel)
            if text is None or short not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (rel, lineno) in def_sites or not word.search(line):
                    continue
                if len(mentions) < limit:
                    mentions.append(f"{rel}:{lineno}  {line.strip()[:160]}")
                else:
                    extra += 1

        out = [f"[definitions of '{name}': {len(definitions)}]"] + (definitions or ["(none found)"])
        out.append(f"[mentions: {len(mentions) + extra}]")
        out.extend(mentions or ["(none found)"])
        if extra:
            out.append(f"[{extra} more mentions not shown; narrow with 'path']")
        if self.index.last_walk_truncated:
            out.append(f"[stopped after {self.index.max_files} files; narrow with 'path']")
        return ToolExecutionResult(call_id="", success=True, output="\n".join(out))

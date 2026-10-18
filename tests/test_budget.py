"""Size budgets: the harness stays a thin layer, not one more agent.

Claude Code, Codex, Gemini CLI, Cursor and Kimi Code already read, edit, run
commands, plan and sandbox. The harness only adds what they lack on a large
codebase (context, review, performance, gates), delivered as one MCP server,
one hook and a few CLIs. These budgets make growth a deliberate decision: if
a test here fails, either delete something or raise the number in the same PR
and say why in its description.

Counted: non-blank, non-comment lines of first-party Python (tests and the
vendored kit excluded).
"""

import glob
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The in-house agent loop is frozen: bug fixes only. Hosts own execution.
AGENT_LOOP = [
    "core/loop.py", "core/memory.py", "core/state.py", "core/model_client.py", "core/evaluator.py",
    "core/models.py", "core/telemetry.py", "core/tools/fs_tools.py", "core/tools/patcher.py",
    "core/tools/shell.py", "core/tools/base.py", "run.py",
]
AGENT_LOOP_BUDGET = 1750   # 1,636 when frozen in October 2026
TOTAL_BUDGET = 5500        # 4,651 in October 2026
MCP_TOOL_BUDGET = 6        # also checked in tests/test_mcp_server.py


def code_lines(rel: str) -> int:
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return sum(1 for line in f if line.strip() and not line.strip().startswith("#"))


def first_party() -> list:
    paths = glob.glob("**/*.py", root_dir=REPO, recursive=True)
    skip = ("tests" + os.sep, "kit" + os.sep + "coding-agent-setup", "build" + os.sep)
    return [p for p in paths if not p.startswith(skip) and ".venv" not in p and "venv" + os.sep not in p]


class TestBudgets(unittest.TestCase):
    def test_agent_loop_is_frozen(self):
        used = sum(code_lines(p) for p in AGENT_LOOP)
        self.assertLessEqual(used, AGENT_LOOP_BUDGET,
                             f"the in-house agent loop grew to {used} lines; host agents own execution, so add the "
                             "capability as an MCP tool, hook or CLI instead (see AGENTS.md)")

    def test_total_size(self):
        used = sum(code_lines(p) for p in first_party())
        self.assertLessEqual(used, TOTAL_BUDGET,
                             f"first-party code is {used} lines, over the {TOTAL_BUDGET} budget: delete something, "
                             "or raise the budget in this PR and say why")

    def test_mcp_tool_budget(self):
        # Every tool costs each host prompt space and tool-selection accuracy.
        import mcp_server
        self.assertLessEqual(len(mcp_server.build_tools(REPO)), MCP_TOOL_BUDGET)


if __name__ == "__main__":
    unittest.main()

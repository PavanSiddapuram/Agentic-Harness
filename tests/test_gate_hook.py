"""Tests for kit/gate_hook.py: one gate and stop-review hook for every host agent.

Payload shapes follow each host's hook documentation (fields and tool names
checked against the installed CLIs where possible).
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kit import gate_hook

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_GUARD = os.path.join(REPO, "kit", "coding-agent-setup", "templates", "claude", "plan-guard.config.json")
TOKEN = "ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"


def shell(host, command, ws):
    return {
        "claude": {"tool_name": "Bash", "tool_input": {"command": command}},
        "codex": {"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", command]}},
        "gemini": {"tool_name": "run_shell_command", "tool_input": {"command": command}},
        "kimi": {"tool_name": "Bash", "tool_input": {"command": command}},
        "cursor": {"command": command, "workspace_roots": [ws]},
    }[host] | {"cwd": ws}


def write(host, path, content, ws):
    return {
        "claude": {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}},
        "codex": {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n*** Add File: {path}\n+{content}\n*** End Patch\n"}},
        "gemini": {"tool_name": "write_file", "tool_input": {"file_path": path, "content": content}},
        "kimi": {"tool_name": "Write", "tool_input": {"path": path, "content": content}},
        "cursor": {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}, "workspace_roots": [ws]},
    }[host] | {"cwd": ws}


class HookCase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.git("init", "-q", "-b", "work")
        os.makedirs(os.path.join(self.ws, ".claude"))
        shutil.copy(PLAN_GUARD, os.path.join(self.ws, ".claude", "plan-guard.config.json"))
        self.env = mock.patch.dict(os.environ, {"HARNESS_NO_EXTERNAL": "1", "CLAUDE_PROJECT_DIR": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", "-C", self.ws, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)

    def run_hook(self, host, event, payload):
        out, err = io.StringIO(), io.StringIO()
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with mock.patch("sys.stdin", io.StringIO(raw)), redirect_stdout(out), redirect_stderr(err):
            code = gate_hook.main(["--host", host, event])
        return code, out.getvalue(), err.getvalue()

    def assertBlocked(self, host, result, needle=""):
        code, out, err = result
        if host == "cursor":
            self.assertEqual(code, 0)
            reply = json.loads(out)
            self.assertEqual(reply["permission"], "deny")
            self.assertIn(needle, reply["agent_message"])
        else:
            self.assertEqual(code, 2, err)
            self.assertIn(needle, err)

    def assertAllowed(self, result):
        code, out, err = result
        self.assertEqual((code, out), (0, ""), err)


class TestPreToolGates(HookCase):
    def test_every_host_blocks_gated_commands_and_writes(self):
        for host in gate_hook.HOSTS:
            with self.subTest(host=host):
                self.assertBlocked(host, self.run_hook(host, "pre", shell(host, "terraform apply", self.ws)), "infra-apply")
                self.assertBlocked(host, self.run_hook(host, "pre", shell(host, "rm -rf /", self.ws)), "FORBIDDEN_COMMAND")
                self.assertBlocked(host, self.run_hook(host, "pre", write(host, "infra/main.tf", "x", self.ws)), "deploy-dir")
                self.assertBlocked(host, self.run_hook(host, "pre", write(host, os.path.join(self.ws, ".env"), "x", self.ws)), "dotenv")
                self.assertBlocked(host, self.run_hook(host, "pre", write(host, "src/flags.py", "ENABLE_PROD_PAY = true", self.ws)), "prod-flag-flip")

    def test_every_host_allows_ordinary_work(self):
        for host in gate_hook.HOSTS:
            with self.subTest(host=host):
                self.assertAllowed(self.run_hook(host, "pre", shell(host, "npm test", self.ws)))
                self.assertAllowed(self.run_hook(host, "pre", write(host, "src/app.py", "x = 1", self.ws)))

    def test_reads_are_not_gated(self):
        for payload in [
            {"tool_name": "Read", "tool_input": {"file_path": os.path.join(self.ws, ".env")}},
            {"tool_name": "read_file", "tool_input": {"file_path": ".env"}},
            {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "infra"}},
        ]:
            self.assertAllowed(self.run_hook("claude", "pre", payload | {"cwd": self.ws}))

    def test_edit_variants_are_gated(self):
        cases = [
            ("claude", {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "ENABLE_PROD_X = 1"}}),
            ("claude", {"tool_name": "MultiEdit", "tool_input": {"file_path": "a.py", "edits": [{"old_string": "a", "new_string": "ENABLE_PROD_Y = on"}]}}),
            ("gemini", {"tool_name": "replace", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "ENABLE_PROD_Z = true"}}),
            ("kimi", {"tool_name": "Edit", "tool_input": {"path": "a.py", "old_string": "x", "new_string": "ENABLE_PROD_Q = true"}}),
        ]
        for host, payload in cases:
            with self.subTest(host=host, tool=payload["tool_name"]):
                self.assertBlocked(host, self.run_hook(host, "pre", payload | {"cwd": self.ws}), "prod-flag-flip")

    def test_codex_patch_move_and_multiple_files(self):
        patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n+ok\n"
                 "*** Update File: src/b.py\n*** Move to: deploy/b.py\n*** End Patch\n")
        result = self.run_hook("codex", "pre", {"cwd": self.ws, "tool_name": "apply_patch", "tool_input": {"command": patch}})
        self.assertBlocked("codex", result, "deploy/b.py")

    def test_protected_branch(self):
        self.git("switch", "-q", "-c", "main")
        self.assertBlocked("codex", self.run_hook("codex", "pre", shell("codex", "git commit -m x", self.ws)), "PROTECTED_BRANCH")

    def test_fails_closed(self):
        with open(os.path.join(self.ws, "harness.gates.json"), "w") as f:
            f.write("{broken")
        self.assertBlocked("claude", self.run_hook("claude", "pre", shell("claude", "ls", self.ws)), "harness.gates.json")
        self.assertBlocked("claude", self.run_hook("claude", "pre", "not json"), "unreadable hook input")
        with mock.patch.object(gate_hook, "check_pre", side_effect=RuntimeError("bug")):
            self.assertBlocked("gemini", self.run_hook("gemini", "pre", shell("gemini", "ls", self.ws)), "crashed")

    def test_usage_errors(self):
        for argv in (["pre"], ["--host", "vim", "pre"], ["--host", "claude", "after"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                gate_hook.main(argv)
            self.assertEqual(ctx.exception.code, 64)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(shell("kimi", "npm publish", self.ws)))), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(gate_hook.main(["--host=kimi", "pre"]), 2)

    def test_root_found_from_a_subdirectory(self):
        sub = os.path.join(self.ws, "pkg", "deep")
        os.makedirs(sub)
        self.assertEqual(gate_hook.find_root({"cwd": sub}), os.path.realpath(self.ws))
        self.assertBlocked("claude", self.run_hook("claude", "pre", shell("claude", "terraform apply", sub)), "infra-apply")

    def test_real_process_round_trip(self):
        proc = subprocess.run([sys.executable, os.path.join(REPO, "kit", "gate_hook.py"), "--host", "codex", "pre"],
                              input=json.dumps(shell("codex", "npm publish", self.ws)), capture_output=True, text=True,
                              env=dict(os.environ, HARNESS_NO_EXTERNAL="1"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("publish", proc.stderr)


class TestStopReview(HookCase):
    def setUp(self):
        super().setUp()
        self.git("commit", "-q", "--allow-empty", "-m", "base")

    def add_secret(self):
        with open(os.path.join(self.ws, "app.py"), "w") as f:
            f.write(f'TOKEN = "{TOKEN}"\nx = eval(y)\n')

    def test_clean_change_lets_the_agent_stop(self):
        with open(os.path.join(self.ws, "app.py"), "w") as f:
            f.write("x = 1\n")
        for host in gate_hook.HOSTS:
            self.assertAllowed(self.run_hook(host, "stop", {"cwd": self.ws, "workspace_roots": [self.ws]}))

    def test_high_findings_send_the_agent_back_once(self):
        self.add_secret()
        code, _, err = self.run_hook("claude", "stop", {"cwd": self.ws, "session_id": "s1", "stop_hook_active": False})
        self.assertEqual(code, 2)
        self.assertIn("2 high-severity finding(s)", err)
        self.assertNotIn(TOKEN, err)
        # Same session, same findings: the agent chose to finish; do not loop.
        self.assertAllowed(self.run_hook("claude", "stop", {"cwd": self.ws, "session_id": "s1"}))
        # The host says it already continued once.
        self.assertAllowed(self.run_hook("codex", "stop", {"cwd": self.ws, "session_id": "s2", "stop_hook_active": True}))
        self.assertTrue(os.path.isfile(os.path.join(self.ws, ".harness", "hook-state.json")))
        git_status = subprocess.run(["git", "-C", self.ws, "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertNotIn(".harness", git_status)

    def test_cursor_gets_a_followup_message(self):
        self.add_secret()
        code, out, _ = self.run_hook("cursor", "stop", {"workspace_roots": [self.ws], "conversation_id": "c1",
                                                        "status": "completed", "loop_count": 0})
        self.assertEqual(code, 0)
        self.assertIn("high-severity", json.loads(out)["followup_message"])
        self.assertAllowed(self.run_hook("cursor", "stop", {"workspace_roots": [self.ws], "conversation_id": "c2",
                                                            "status": "completed", "loop_count": 1}))
        self.assertAllowed(self.run_hook("cursor", "stop", {"workspace_roots": [self.ws], "conversation_id": "c3",
                                                            "status": "aborted", "loop_count": 0}))

    def test_medium_findings_do_not_block(self):
        with open(os.path.join(self.ws, "view.js"), "w") as f:
            f.write("el.innerHTML = name;\n")
        self.assertAllowed(self.run_hook("gemini", "stop", {"cwd": self.ws, "session_id": "g1"}))

    def test_fails_open(self):
        self.add_secret()
        outside = tempfile.mkdtemp()
        try:
            self.assertAllowed(self.run_hook("kimi", "stop", {"cwd": outside}))
        finally:
            shutil.rmtree(outside)
        with mock.patch("planes.verify.review.run_review", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_hook("kimi", "stop", {"cwd": self.ws})[0], 0)
        self.assertEqual(self.run_hook("kimi", "stop", "not json")[0], 0)


if __name__ == "__main__":
    unittest.main()

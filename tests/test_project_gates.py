"""Tests for project-declared owner gates in harness.gates.json."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from core.models import ToolCallRequest
from core.permissions import GateConfigError, PermissionGate, SecurityException

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATES = {
    "commandGates": [
        {"id": "deploy", "pattern": r"\b(make|npm run)\s+deploy\b", "why": "Deploys reach customers."},
        {"id": "prod-db", "pattern": r"psql[^|;&]*prod", "why": "Production data."},
    ],
    "protectedPaths": ["migrations/", ".github/workflows/*", "*.lock"],
}


class GatesBase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_gates(self, content):
        with open(os.path.join(self.ws, "harness.gates.json"), "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def cmd(self, command):
        return ToolCallRequest(call_id="1", tool_name="execute_command", parameters={"command": command})

    def edit(self, path, tool="patch_file"):
        return ToolCallRequest(call_id="1", tool_name=tool, parameters={"filepath": path})


class TestCommandGates(GatesBase):
    def test_blocked_even_with_auto_approve(self):
        self.write_gates(GATES)
        gate = PermissionGate(self.ws, auto_approve_all=True)
        for command in ["make deploy", "npm run deploy -- --prod", "psql -h prod-db -c 'select 1'"]:
            with self.subTest(command=command):
                with self.assertRaises(SecurityException) as ctx:
                    gate.assess(self.cmd(command))
                self.assertIn("OWNER_GATE", str(ctx.exception))

    def test_ordinary_commands_still_allowed(self):
        self.write_gates(GATES)
        gate = PermissionGate(self.ws)
        for command in ["make test", "npm run build", "psql -h localhost -c 'select 1'"]:
            with self.subTest(command=command):
                gate.assess(self.cmd(command))


class TestProtectedPaths(GatesBase):
    def test_protected_paths_blocked(self):
        self.write_gates(GATES)
        gate = PermissionGate(self.ws, auto_approve_all=True)
        for path in [
            "migrations/0042_add_index.sql",
            "migrations/nested/deep.py",
            "./migrations/x.sql",
            "app/../migrations/x.sql",
            ".github/workflows/deploy.yml",
            "Cargo.lock",
        ]:
            for tool in ("patch_file", "create_file"):
                with self.subTest(path=path, tool=tool):
                    with self.assertRaises(SecurityException):
                        gate.assess(self.edit(path, tool))

    def test_absolute_path_inside_workspace_is_blocked(self):
        self.write_gates(GATES)
        gate = PermissionGate(self.ws)
        with self.assertRaises(SecurityException):
            gate.assess(self.edit(os.path.join(self.ws, "migrations", "x.sql")))

    def test_other_paths_allowed(self):
        self.write_gates(GATES)
        gate = PermissionGate(self.ws)
        for path in ["app/migrations_helper.py", "github/workflows/x.yml", "src/lock.py", "docs/a.md"]:
            with self.subTest(path=path):
                gate.assess(self.edit(path))


class TestFailClosed(GatesBase):
    def test_no_file_means_no_extra_gates(self):
        gate = PermissionGate(self.ws)
        gate.assess(self.cmd("make deploy"))

    def test_malformed_files_refuse_to_load(self):
        for bad in [
            "{not json",
            "[]",
            {"commandGates": [{"id": "x"}]},
            {"commandGates": [{"pattern": "(unclosed"}]},
            {"protectedPaths": "migrations/"},
        ]:
            with self.subTest(bad=bad):
                self.write_gates(bad)
                with self.assertRaises(GateConfigError):
                    PermissionGate(self.ws)

    def test_cli_refuses_to_start_with_bad_gates(self):
        self.write_gates("{not json")
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "run.py"), "--mock", "--workspace", self.ws, "--prompt", "x"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("[REFUSED]", proc.stdout)


if __name__ == "__main__":
    unittest.main()

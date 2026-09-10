"""Unit tests for the permission gatekeeper."""

import tempfile
import unittest

from core.models import PermissionLevel, ToolCallRequest
from core.permissions import PermissionGate, SecurityException


class TestPermissions(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.gate = PermissionGate(self.workspace)

    def test_risk_classification(self):
        read_call = ToolCallRequest(
            call_id="1", tool_name="read_file", parameters={"filepath": "main.py"}
        )
        self.assertEqual(self.gate.assess(read_call), PermissionLevel.AUTO_ALLOW)

        patch_call = ToolCallRequest(
            call_id="2", tool_name="patch_file", parameters={"filepath": "main.py"}
        )
        self.assertEqual(self.gate.assess(patch_call), PermissionLevel.CONFIRMATION)

        cmd_call = ToolCallRequest(
            call_id="3",
            tool_name="execute_command",
            parameters={"command": "pytest tests/"},
        )
        self.assertEqual(self.gate.assess(cmd_call), PermissionLevel.RESTRICTED)

    def test_destructive_patterns_throw_security_exception(self):
        bad_calls = [
            "rm -rf /",
            "rm -rf node_modules",
            ":(){ :|:& };:",
            "dd if=/dev/zero of=/dev/sda",
            "format C: /fs:ntfs",
        ]

        for bad_cmd in bad_calls:
            call = ToolCallRequest(
                call_id="evil",
                tool_name="execute_command",
                parameters={"command": bad_cmd},
            )
            with self.assertRaises(SecurityException):
                self.gate.assess(call)

    def test_interactive_authorization_flow(self):
        call = ToolCallRequest(
            call_id="cmd",
            tool_name="execute_command",
            parameters={"command": "git status"},
        )

        # Mock user approving "y"
        approving_gate = PermissionGate(
            self.workspace, input_func=lambda prompt: "y"
        )
        self.assertTrue(approving_gate.authorize(call, PermissionLevel.RESTRICTED))

        # Mock user rejecting "n"
        rejecting_gate = PermissionGate(
            self.workspace, input_func=lambda prompt: "n"
        )
        self.assertFalse(rejecting_gate.authorize(call, PermissionLevel.RESTRICTED))


if __name__ == "__main__":
    unittest.main()

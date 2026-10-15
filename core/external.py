"""Optional external tools: found on PATH at runtime, never required.

The harness runs on the standard library alone. When a stronger tool is
installed (RTK, codebase-memory-mcp), the harness uses it and falls back to its
own implementation when it is missing or fails. Set HARNESS_NO_EXTERNAL=1 to
force the pure-stdlib path (useful for reproducible runs and tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional, Tuple


def external_tools_disabled() -> bool:
    return os.environ.get("HARNESS_NO_EXTERNAL", "").strip() not in ("", "0", "false", "no")


def find_tool(name: str) -> Optional[str]:
    """Returns the absolute path of an executable on PATH, or None."""
    if external_tools_disabled():
        return None
    return shutil.which(name)


def run_tool(argv: List[str], cwd: Optional[str] = None, timeout: int = 60) -> Tuple[int, str, str]:
    """Runs an external tool without a shell. Returns (exit code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{os.path.basename(argv[0])} timed out after {timeout}s"
    except OSError as e:
        return 127, "", f"{os.path.basename(argv[0])} could not run: {e}"

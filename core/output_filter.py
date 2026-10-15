"""Meaning-preserving compression of shell command output.

Test runners, package managers and build tools print far more than an agent
needs: colour codes, progress bars redrawn with carriage returns, one line per
passing test. This module removes that noise and keeps every failure, warning
and summary line. It only touches command output, never file contents or
instructions, which must reach the model verbatim.
"""

from __future__ import annotations

import re
from typing import List, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Lines that only report a passing test. Failures never match these.
PASS_LINE_RES = [
    re.compile(r"^\S+\.py\s+[.sxX]+\s*(\[\s*\d+%\])?$"),          # pytest: tests/a.py ....  [ 40%]
    re.compile(r"^\S+::\S+\s+PASSED(\s+\[\s*\d+%\])?$"),           # pytest -v
    re.compile(r"^\s*(✓|√|✔)\s+.+$"),                              # jest, vitest, mocha
    re.compile(r"^\s*PASS\s+\S+"),                                  # jest file summary
    re.compile(r"^=== RUN\s+\S+$"),                                 # go test -v
    re.compile(r"^\s*--- PASS: \S+ \([\d.]+s\)$"),                  # go test -v
    re.compile(r"^test \S+ \.\.\. ok$"),                            # cargo test
    re.compile(r"^\s*[.]+$"),                                       # bare progress dots
]


def _strip_carriage_returns(line: str) -> str:
    # A progress bar redraws itself with \r; only the last frame matters.
    return line.rsplit("\r", 1)[-1] if "\r" in line else line


def compress(output: str, repeat_threshold: int = 3) -> Tuple[str, int]:
    """Returns (compressed output, number of lines removed)."""
    if not output:
        return output, 0

    lines = [_strip_carriage_returns(ANSI_RE.sub("", line)) for line in output.split("\n")]
    original_count = len(lines)

    kept: List[str] = []
    pass_run = 0

    def flush_pass_run():
        nonlocal pass_run
        if pass_run:
            kept.append(f"[{pass_run} passing-test lines omitted]")
            pass_run = 0

    for line in lines:
        if any(r.match(line) for r in PASS_LINE_RES):
            pass_run += 1
            continue
        flush_pass_run()
        kept.append(line)
    flush_pass_run()

    # Collapse runs of identical lines (retries, repeated warnings).
    collapsed: List[str] = []
    i = 0
    while i < len(kept):
        j = i
        while j + 1 < len(kept) and kept[j + 1] == kept[i]:
            j += 1
        run = j - i + 1
        if run >= repeat_threshold and kept[i].strip():
            collapsed.append(kept[i])
            collapsed.append(f"[previous line repeated {run - 1} more times]")
        else:
            collapsed.extend(kept[i : j + 1])
        i = j + 1

    removed = max(0, original_count - len(collapsed))
    return "\n".join(collapsed), removed

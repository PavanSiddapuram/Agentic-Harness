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


# Lines that must never be folded away as a "group heading".
KEEP_RE = re.compile(
    r"fail|error|✕|×|✗|warn|deprecat|exception|traceback|assert|panic|skip|pending|todo|"
    r"^\s*\d+\)|\d+\s+(passing|failing|passed|failed)",
    re.IGNORECASE,
)


def _is_heading(line: str) -> bool:
    """A short label line (describe block, suite name) with no failure signal."""
    stripped = line.strip()
    return bool(stripped) and len(stripped) <= 120 and not KEEP_RE.search(stripped)


def _fold_passing_groups(items: List[Tuple[str, object]]) -> List[str]:
    """Folds suite headings that only introduce passing tests into one marker.

    items are ("pass", count) or ("line", text). A heading is folded only when
    the next non-blank, non-heading item after it is a passing-test marker, so
    headings that introduce failures, and the failure report at the end, stay.
    """
    n = len(items)
    foldable = [False] * n
    for i, (kind, value) in enumerate(items):
        if kind == "pass":
            foldable[i] = True
        elif _is_heading(value):
            j = i + 1
            while j < n and items[j][0] == "line" and (not items[j][1].strip() or _is_heading(items[j][1])):
                j += 1
            foldable[i] = j < n and items[j][0] == "pass"
    # Blank lines between two foldable items are part of the fold.
    for i, (kind, value) in enumerate(items):
        if kind == "line" and not value.strip():
            prev = next((k for k in range(i - 1, -1, -1) if items[k][0] == "pass" or items[k][1].strip()), None)
            nxt = next((k for k in range(i + 1, n) if items[k][0] == "pass" or items[k][1].strip()), None)
            foldable[i] = prev is not None and nxt is not None and foldable[prev] and foldable[nxt]

    out: List[str] = []
    i = 0
    while i < n:
        if not foldable[i]:
            out.append(items[i][1])
            i += 1
            continue
        passes = groups = 0
        while i < n and foldable[i]:
            kind, value = items[i]
            if kind == "pass":
                passes += value
            elif value.strip():
                groups += 1
            i += 1
        if groups:
            out.append(f"[{passes} passing-test lines omitted, in {groups} groups]")
        else:
            out.append(f"[{passes} passing-test lines omitted]")
    return out


def _strip_carriage_returns(line: str) -> str:
    # A progress bar redraws itself with \r; only the last frame matters.
    return line.rsplit("\r", 1)[-1] if "\r" in line else line


def compress(output: str, repeat_threshold: int = 3) -> Tuple[str, int]:
    """Returns (compressed output, number of lines removed)."""
    if not output:
        return output, 0

    lines = [_strip_carriage_returns(ANSI_RE.sub("", line)) for line in output.split("\n")]
    original_count = len(lines)

    items: List[Tuple[str, object]] = []
    for line in lines:
        if any(r.match(line) for r in PASS_LINE_RES):
            if items and items[-1][0] == "pass":
                items[-1] = ("pass", items[-1][1] + 1)
            else:
                items.append(("pass", 1))
        else:
            items.append(("line", line))
    kept = _fold_passing_groups(items)

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

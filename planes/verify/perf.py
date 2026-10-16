"""Before/after performance check for any command.

    python -m planes.verify.perf baseline --cmd "npm run bench"   # before changing code
    python -m planes.verify.perf compare  --cmd "npm run bench"   # after

Runs the command several times (after a warm-up run) and records wall time,
CPU time and peak memory (Linux/macOS). `compare` reports a regression only
when the change is larger than both a threshold (10%) and the measured noise
(the larger of twice the median absolute deviation and half the range, for
either run set), so jitter is not reported as a slowdown.

Timings are only as good as the machine is quiet; the report says how noisy the
runs were. The command goes through the same owner gates and approval as any
shell command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.models import ToolExecutionResult
from core.output_filter import compress
from core.tools.base import BaseTool

THRESHOLD = 0.10
MEMORY_FLOOR_MB = 5.0
TIME_FLOOR_S = 0.02    # process start-up jitter; smaller differences are not measurable this way
MAX_RUNS = 20


@dataclass
class Sample:
    wall: float
    cpu: Optional[float]
    rss_mb: Optional[float]


class CommandFailed(Exception):
    def __init__(self, code: int, output: str):
        super().__init__(f"exit code {code}")
        self.code = code
        self.output = output


def run_once(command: str, cwd: str, timeout: float) -> Sample:
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out:
        start = time.perf_counter()
        proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
        cpu = rss = None
        if hasattr(os, "wait4"):
            delay, deadline = 0.0005, start + timeout
            while True:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    break
                if time.perf_counter() > deadline:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError(f"run exceeded {timeout:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 0.02)
            wall = time.perf_counter() - start
            code = os.waitstatus_to_exitcode(status)
            proc.returncode = code
            cpu = usage.ru_utime + usage.ru_stime
            # ru_maxrss is KiB on Linux, bytes on macOS; it covers waited-for descendants.
            rss = usage.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        else:  # Windows: wall time only
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise TimeoutError(f"run exceeded {timeout:.0f}s")
            wall = time.perf_counter() - start
        if code != 0:
            out.seek(0)
            raise CommandFailed(code, out.read()[-20000:])
    return Sample(wall, cpu, rss)


def measure(command: str, cwd: str, runs: int = 5, warmup: int = 1, timeout: float = 120) -> List[Sample]:
    for _ in range(warmup):
        run_once(command, cwd, timeout)
    return [run_once(command, cwd, timeout) for _ in range(runs)]


def _stats(values: List[float]) -> Dict[str, float]:
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    return {"median": med, "min": min(values), "max": max(values), "mad": mad}


def summarize(samples: List[Sample]) -> Dict[str, Optional[Dict[str, float]]]:
    out: Dict[str, Optional[Dict[str, float]]] = {"wall": _stats([s.wall for s in samples])}
    out["cpu"] = _stats([s.cpu for s in samples]) if all(s.cpu is not None for s in samples) else None
    out["rss_mb"] = _stats([s.rss_mb for s in samples]) if all(s.rss_mb is not None for s in samples) else None
    return out


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def baseline_path(root: str, command: str) -> str:
    key = hashlib.sha256(command.strip().encode()).hexdigest()[:16]
    return os.path.join(root, ".harness", "perf", f"{key}.json")


def save_baseline(root: str, command: str, samples: List[Sample]) -> Dict[str, Any]:
    record = {
        "command": command,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_head": _git(root, "rev-parse", "--short", "HEAD"),
        "dirty": bool(_git(root, "status", "--porcelain", "--untracked-files=no")),
        "samples": [asdict(s) for s in samples],
        "summary": summarize(samples),
    }
    path = baseline_path(root, command)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    return record


def load_baseline(root: str, command: str) -> Optional[Dict[str, Any]]:
    path = baseline_path(root, command)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _verdict(name: str, unit: str, base: Dict[str, float], new: Dict[str, float], floor: float = 0.0) -> Tuple[str, bool]:
    b, n = base["median"], new["median"]
    if b <= 0:
        return f"{name}: {n:.3g}{unit} (no usable baseline)", False
    rel = (n - b) / b
    def rel_noise(st: Dict[str, float]) -> float:
        # The larger of twice the median absolute deviation and half the full
        # range, relative to the median: wide enough for small, jittery samples.
        med = st["median"]
        return max(2 * st["mad"], (st["max"] - st["min"]) / 2) / med if med > 0 else 0.0
    noise = max(rel_noise(base), rel_noise(new))
    bar = max(THRESHOLD, noise)
    change = f"{name}: {b:.3g}{unit} -> {n:.3g}{unit} ({rel:+.1%})"
    if abs(n - b) < floor:
        return f"{change}: no significant change (below the {floor:g}{unit} measurement floor)", False
    if rel > bar:
        return f"{change}: REGRESSION, beyond the {bar:.0%} bar (threshold or noise)", True
    if rel < -bar:
        return f"{change}: improvement, beyond the {bar:.0%} bar", False
    return f"{change}: no significant change (within {bar:.0%})", False


def compare(baseline: Dict[str, Any], samples: List[Sample]) -> Tuple[List[str], bool]:
    new = summarize(samples)
    base = baseline["summary"]
    lines, regressed = [], False
    for key, name, unit, floor in (("wall", "wall time", "s", TIME_FLOOR_S), ("cpu", "CPU time", "s", TIME_FLOOR_S),
                                   ("rss_mb", "peak memory", " MB", MEMORY_FLOOR_MB)):
        if base.get(key) and new.get(key):
            text, bad = _verdict(name, unit, base[key], new[key], floor)
            lines.append(text)
            regressed = regressed or bad
    return lines, regressed


def _spread(summary: Dict[str, Any]) -> str:
    w = summary["wall"]
    return f"median {w['median']:.3g}s, range {w['min']:.3g}-{w['max']:.3g}s"


class PerfCheckTool(BaseTool):
    name = "perf_check"
    description = (
        "Measure a command's speed and memory before and after a change. Call with mode='baseline' "
        "BEFORE editing (or on a clean checkout), then mode='compare' after. Reports a regression only "
        "when it exceeds both 10% and the run-to-run noise. Use a command that exercises the code you "
        "are changing (a benchmark or a focused test), not the whole suite."
    )

    def __init__(self, root: str):
        self.root = root

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to time, run from the workspace root"},
                        "mode": {"type": "string", "enum": ["baseline", "compare"], "description": "baseline before the change, compare after"},
                        "runs": {"type": "integer", "description": f"Timed runs (default 5, max {MAX_RUNS})"},
                        "timeout": {"type": "integer", "description": "Seconds per run (default 120, max 600)"},
                    },
                    "required": ["command", "mode"],
                },
            },
        }

    def execute(self, parameters: Dict[str, Any]) -> ToolExecutionResult:
        command = (parameters.get("command") or "").strip()
        mode = parameters.get("mode")
        if not command or mode not in ("baseline", "compare"):
            return ToolExecutionResult(call_id="", success=False, output="perf_check needs 'command' and mode 'baseline' or 'compare'.")
        runs = max(1, min(int(parameters.get("runs") or 5), MAX_RUNS))
        timeout = max(1, min(int(parameters.get("timeout") or 120), 600))
        baseline = load_baseline(self.root, command) if mode == "compare" else None
        if mode == "compare" and baseline is None:
            return ToolExecutionResult(call_id="", success=False,
                                       output="No baseline for this exact command. Run perf_check with mode='baseline' first, ideally before editing.")
        try:
            samples = measure(command, self.root, runs=runs, timeout=timeout)
        except CommandFailed as e:
            tail = compress(e.output)[0][-3000:]
            return ToolExecutionResult(call_id="", success=False, output=f"PERF_COMMAND_FAILED ({e}); fix it before measuring.\n{tail}")
        except TimeoutError as e:
            return ToolExecutionResult(call_id="", success=False, output=f"PERF_TIMEOUT: {e}")

        if mode == "baseline":
            record = save_baseline(self.root, command, samples)
            lines = [f"Baseline saved for `{command}`: {runs} runs, {_spread(record['summary'])}"]
            if record["summary"].get("rss_mb"):
                lines.append(f"peak memory median {record['summary']['rss_mb']['median']:.0f} MB")
            if record["dirty"]:
                lines.append("Warning: taken with uncommitted changes, so it may not represent the code before your change.")
            return ToolExecutionResult(call_id="", success=True, output="\n".join(lines))

        lines, regressed = compare(baseline, samples)
        header = f"Performance of `{command}` vs baseline from {baseline['created']}"
        if baseline.get("git_head"):
            header += f" (commit {baseline['git_head']}{', with uncommitted changes' if baseline.get('dirty') else ''})"
        out = [header, f"now: {runs} runs, {_spread(summarize(samples))}"] + [f"  {l}" for l in lines]
        out.append("Verdict: REGRESSION. Find the cause before finishing." if regressed else "Verdict: no regression detected.")
        if runs < 3 or len(baseline["samples"]) < 3:
            out.append("Low confidence: use at least 3 runs on each side.")
        return ToolExecutionResult(call_id="", success=not regressed, output="\n".join(out))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["baseline", "compare"])
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    result = PerfCheckTool(os.path.realpath(args.workspace)).execute(
        {"command": args.cmd, "mode": args.mode, "runs": args.runs, "timeout": args.timeout})
    print(result.output)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())

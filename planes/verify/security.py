"""Security findings in the lines a change adds.

Built-in rules (standard library) catch the common, high-signal problems agents
introduce: committed secrets, code execution on dynamic input, shell injection,
SQL built from strings, disabled TLS checks, unsafe deserialization and HTML
injection. They are pattern rules: fast and dependency-free, but they cannot
follow data flow, so they flag risky constructs rather than prove a bug.

When installed, gitleaks (secrets, 150+ provider formats) and Semgrep (semantic
rules; the repo's own .semgrep.yml, or the registry's p/default) add depth.
Results from every source are limited to added lines. Secret values are always
redacted so they never enter the agent's context.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from core.external import find_tool, run_tool
from planes.verify.diff import ChangeSet, FileChange

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

PY = (".py", ".pyi")
JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")
GO = (".go",)
JVM = (".java", ".kt", ".kts", ".scala")
PHP = (".php",)
RB = (".rb",)
ANY = None


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    message: str
    source: str = "builtin"

    def render(self) -> str:
        return f"[{self.severity}] {self.path}:{self.line} {self.rule}: {self.message} ({self.source})"


# (rule id, severity, extensions or ANY, regex, message). Secret rules match on any file.
SECRET_RULES: List[Tuple[str, str, Optional[Sequence[str]], re.Pattern, str]] = [
    ("private-key", "high", ANY, re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"), "private key committed"),
    ("aws-access-key-id", "high", ANY, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key ID"),
    ("aws-secret-key", "high", ANY, re.compile(r"(?i)aws.{0,24}(?:secret|private).{0,24}[=:]\s*[\"']?(?P<v>[A-Za-z0-9/+=]{40})\b"), "AWS secret access key"),
    ("github-token", "high", ANY, re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b"), "GitHub token"),
    ("slack-token", "high", ANY, re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    ("stripe-live-key", "high", ANY, re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}"), "Stripe live key"),
    ("google-api-key", "high", ANY, re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    ("llm-api-key", "high", ANY, re.compile(r"\bsk-(?:ant-[A-Za-z0-9_-]{20,}|proj-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{48})\b"), "AI provider API key"),
    ("jwt", "medium", ANY, re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "hard-coded JWT"),
    ("hardcoded-credential", "medium", ANY,
     # Letters/digits as boundaries (not \b), so DB_PASSWORD and AWS_SECRET_ACCESS_KEY match.
     re.compile(r"(?i)(?<![a-z0-9])(?:api[_-]?key|secret(?:[_-]?(?:access[_-]?)?key)?|access[_-]?token|auth[_-]?token|passw(?:or)?d|client[_-]?secret)(?![a-z0-9])[\"']?\s*[:=]\s*[\"'](?P<v>[^\"'\s]{12,})[\"']"),
     "credential assigned a literal value; load it from the environment or a secret store"),
]
PLACEHOLDER_RE = re.compile(r"(?i)(example|changeme|placeholder|dummy|your[_-]|xxxx|\*\*\*|<|\$\{|\{\{|redacted|test|fake|sample)")

CODE_RULES: List[Tuple[str, str, Optional[Sequence[str]], re.Pattern, str]] = [
    # Code execution
    ("dynamic-eval", "high", PY + PHP + RB, re.compile(r"(?<![\w.$])(?:eval|exec)\s*\((?!\s*(?:\"[^\"]*\"|'[^']*')\s*\))"), "eval/exec runs arbitrary code if any part of the input is user-controlled"),
    ("dynamic-eval", "high", JS, re.compile(r"(?<![\w.$])eval\s*\((?!\s*(?:\"[^\"]*\"|'[^']*')\s*\))"), "eval runs arbitrary code if any part of the input is user-controlled"),
    ("new-function", "high", JS, re.compile(r"\bnew\s+Function\s*\("), "new Function() compiles code from a string"),
    # Shell injection
    ("shell-injection", "high", PY, re.compile(r"(?:f[\"']|\.format\(|%\s*\(|\+\s*\w)[^#]*shell\s*=\s*True"), "shell=True with a command built from variables: shell injection; pass a list of args"),
    ("shell-true", "medium", PY, re.compile(r"^(?!.*(?:f[\"']|\.format\(|%\s*\(|\+\s*\w)).*shell\s*=\s*True"), "subprocess with shell=True; pass a list of args instead"),
    ("os-system", "medium", PY, re.compile(r"\bos\.(?:system|popen)\s*\("), "os.system/os.popen pass a string to the shell; use subprocess with a list of args"),
    ("child-process-dynamic", "high", JS, re.compile(r"\b(?:exec|execSync)\s*\(\s*(?:`[^`]*\$\{|[^)]*\+)"), "child_process exec with interpolated input: shell injection"),
    ("go-shell", "medium", GO, re.compile(r"exec\.Command\(\s*\"(?:sh|bash|cmd)\"\s*,\s*\"(?:-c|/c)\""), "running a shell with a command string"),
    ("jvm-runtime-exec", "medium", JVM, re.compile(r"Runtime\.getRuntime\(\)\.exec\("), "Runtime.exec with a command string"),
    ("php-shell", "high", PHP, re.compile(r"\b(?:system|shell_exec|passthru|proc_open|popen)\s*\(\s*[^)]*\$"), "shell function with a variable argument"),
    ("ruby-shell-interp", "high", RB, re.compile(r"(?:system|exec|%x)\s*\(?[^)]*#\{|`[^`]*#\{"), "shell command with interpolation"),
    # SQL injection
    ("sql-string-build", "high", ANY,
     re.compile(r"(?i)(?:execute|executemany|query|raw|prepare|exec_query|cursor\.\w+)\s*\(\s*(?:f[\"'][^\"']*\b(?:select|insert|update|delete)\b|[\"'`][^\"'`]*\b(?:select|insert|update|delete)\b[^\"'`]*(?:[\"'`]\s*(?:\+|%|\.format)|\$\{|#\{))"),
     "SQL built from strings; use parameterized queries"),
    # TLS and crypto
    ("tls-verify-off", "medium", ANY,
     re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*[\"']?0|InsecureSkipVerify\s*:\s*true|CURLOPT_SSL_VERIFYPEER\s*,\s*(?:0|false)"),
     "TLS certificate verification disabled"),
    ("weak-hash", "low", PY + JS + GO + JVM + PHP + RB, re.compile(r"(?i)\b(?:md5|sha1)\s*\(|hashlib\.(?:md5|sha1)\b|createHash\(\s*[\"'](?:md5|sha1)|MessageDigest\.getInstance\(\s*\"(?:MD5|SHA-?1)\""), "MD5/SHA-1 must not protect passwords or signatures"),
    # Deserialization
    ("unsafe-deserialize", "medium", PY + PHP + RB + JVM,
     re.compile(r"\bpickle\.loads?\(|\bmarshal\.loads?\(|\bshelve\.open\(|yaml\.load\((?![^)]*Loader\s*=\s*(?:yaml\.)?SafeLoader)|\bunserialize\s*\(|Marshal\.load|YAML\.load\(|ObjectInputStream"),
     "deserializing untrusted data can execute code"),
    # HTML injection
    ("html-injection", "medium", JS,
     re.compile(r"\.(?:innerHTML|outerHTML)\s*=(?!=)|insertAdjacentHTML\s*\(|document\.write\s*\(|dangerouslySetInnerHTML|v-html\s*="),
     "writing HTML from data can enable XSS; prefer textContent or sanitize"),
    # Misconfiguration
    ("debug-enabled", "low", PY, re.compile(r"^\s*DEBUG\s*=\s*True\b"), "DEBUG = True must not reach production"),
    ("cors-wildcard-credentials", "medium", ANY, re.compile(r"(?i)allow[-_]?credentials.{0,40}true.{0,80}\*|origin\s*:\s*[\"']\*[\"'].{0,80}credentials\s*:\s*true"), "CORS allows any origin with credentials"),
    ("world-writable", "low", ANY, re.compile(r"chmod\s+(?:-R\s+)?777|0o?777\b"), "world-writable permissions"),
]
COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|--|<!--)")


def _redact(value: str) -> str:
    return (value[:4] + "…") if len(value) > 4 else "…"


def scan_builtin(changes: ChangeSet) -> List[Finding]:
    findings: List[Finding] = []
    for change in changes.files:
        if change.status == "deleted":
            continue
        for line_no, text in change.added.items():
            for rule, severity, exts, regex, message in SECRET_RULES:
                if exts is not None and change.ext not in exts:
                    continue
                m = regex.search(text)
                if not m:
                    continue
                value = m.groupdict().get("v") or m.group(0)
                if rule == "hardcoded-credential" and PLACEHOLDER_RE.search(value):
                    continue
                findings.append(Finding(severity, rule, change.path, line_no, f"{message} [{_redact(value)}]"))
                break  # one secret finding per line is enough
            if COMMENT_RE.match(text):
                continue
            for rule, severity, exts, regex, message in CODE_RULES:
                if exts is not None and change.ext not in exts:
                    continue
                if regex.search(text):
                    findings.append(Finding(severity, rule, change.path, line_no, message))
    return findings


def scan_gitleaks(changes: ChangeSet) -> Tuple[List[Finding], Optional[str]]:
    binary = find_tool("gitleaks")
    if not binary:
        return [], None
    findings: List[Finding] = []
    for change in changes.files:
        if change.status == "deleted" or not change.added:
            continue
        code, out, err = run_tool(
            [binary, "dir", change.path, "--report-format", "json", "--report-path", "-",
             "--no-banner", "--redact", "--exit-code", "0", "--log-level", "error"],
            cwd=changes.root, timeout=60,
        )
        if code != 0:
            return findings, f"gitleaks failed: {(err or out).strip()[:200]}"
        try:
            results = json.loads(out or "[]")
        except ValueError:
            continue
        for r in results:
            line = int(r.get("StartLine", 0))
            if line in change.added:
                findings.append(Finding("high", r.get("RuleID", "secret"), change.path, line,
                                        f"{r.get('Description', 'secret')} [redacted]", "gitleaks"))
    return findings, None


def _semgrep_config(root: str) -> str:
    explicit = os.environ.get("HARNESS_SEMGREP_CONFIG")
    if explicit:
        return explicit
    for name in (".semgrep.yml", ".semgrep.yaml", ".semgrep"):
        if os.path.exists(os.path.join(root, name)):
            return name
    return "p/default"  # fetched from the Semgrep registry; needs network


# Set once Semgrep fails in this process (most often: no network to fetch the
# registry rules), so later reviews skip it instead of stalling again.
_SEMGREP_DISABLED: Optional[str] = None


def scan_semgrep(changes: ChangeSet) -> Tuple[List[Finding], Optional[str]]:
    global _SEMGREP_DISABLED
    binary = find_tool("semgrep")
    targets = [f for f in changes.files if f.status != "deleted" and f.added]
    if not binary or not targets:
        return [], None
    if _SEMGREP_DISABLED:
        return [], _SEMGREP_DISABLED
    config = _semgrep_config(changes.root)
    remote = config.startswith(("p/", "r/", "http"))
    if remote:
        cached = _remote_failure(config)
        if cached:
            return [], cached
    result, problem = _run_semgrep(binary, changes, targets)
    if problem:
        _SEMGREP_DISABLED = problem + " (skipped for the rest of this session)"
        if remote:
            _remember_remote_failure(config, problem)
    elif remote:
        _remember_remote_failure(config, None)
    return result, problem


# A failed registry download is remembered across runs (one CLI run per commit
# hook or CI job would otherwise stall on every run when offline).
REMOTE_FAILURE_TTL = 6 * 3600


def _failure_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "agentic-harness", "semgrep-remote-failures.json")


def _load_failures() -> Dict[str, Dict]:
    try:
        with open(_failure_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remote_failure(config: str) -> Optional[str]:
    entry = _load_failures().get(config)
    if not entry or time.time() - entry.get("at", 0) > REMOTE_FAILURE_TTL:
        return None
    return (f"semgrep skipped: fetching {config} failed {int((time.time() - entry['at']) // 60)} min ago. "
            f"Retries after 6 h, or delete {_failure_cache_path()}; or add a .semgrep.yml for local rules.")


def _remember_remote_failure(config: str, problem: Optional[str]) -> None:
    data = _load_failures()
    if problem:
        data[config] = {"at": time.time(), "reason": problem[:200]}
    elif config in data:
        del data[config]
    else:
        return
    path = _failure_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # best effort: worst case Semgrep is tried again next run


def _run_semgrep(binary: str, changes: ChangeSet, targets: List[FileChange]) -> Tuple[List[Finding], Optional[str]]:
    by_path: Dict[str, FileChange] = {f.path: f for f in targets}
    config = _semgrep_config(changes.root)
    remote = config.startswith(("p/", "r/", "http"))
    hint = " Add a .semgrep.yml to the repo or set HARNESS_SEMGREP_CONFIG to use local rules." if remote else ""
    env = dict(os.environ, SEMGREP_ENABLE_VERSION_CHECK="0", SEMGREP_SEND_METRICS="off")
    import subprocess  # local: only needed here, and only when semgrep exists
    try:
        proc = subprocess.run(
            [binary, "scan", "--config", config, "--json", "--metrics=off", "--quiet",
             "--disable-version-check", *by_path],
            cwd=changes.root, capture_output=True, text=True, timeout=45 if remote else 300, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"semgrep did not finish with config {config}: {type(e).__name__}.{hint}"
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return [], f"semgrep failed with config {config}: {proc.stderr.strip()[-150:]}{hint}"
    if proc.returncode not in (0, 1) and not data.get("results"):
        return [], f"semgrep failed with config {config}: {proc.stderr.strip()[-150:]}{hint}"
    sev = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    findings = []
    for r in data.get("results", []):
        path, line = r.get("path", ""), int(r.get("start", {}).get("line", 0))
        if path in by_path and line in by_path[path].added:
            rule = r.get("check_id", "semgrep").split(".")[-1]
            message = r.get("extra", {}).get("message", "").strip().split("\n")[0][:200]
            findings.append(Finding(sev.get(r.get("extra", {}).get("severity"), "medium"), rule, path, line, message, "semgrep"))
    return findings, None


def scan(changes: ChangeSet, use_external: bool = True) -> Tuple[List[Finding], List[str]]:
    """Returns (findings sorted most severe first, notes about the scanners that ran)."""
    findings = scan_builtin(changes)
    notes = ["builtin rules"]
    if use_external:
        for name, scanner in (("gitleaks", scan_gitleaks), ("semgrep", scan_semgrep)):
            extra, problem = scanner(changes)
            if problem:
                notes.append(problem)
            elif find_tool(name):
                notes.append(name)
            findings += extra
    # One secret finding per line, whichever source found it; and when a scanner
    # and a builtin rule flag the same line at the same severity, keep the
    # scanner's more specific finding.
    secret_rules = {r[0] for r in SECRET_RULES}
    external_hits = {(f.path, f.line, f.severity) for f in findings if f.source != "builtin"}
    seen, unique = set(), []
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.source == "builtin")):
        secretish = f.rule in secret_rules or f.source == "gitleaks"
        if not secretish and f.source == "builtin" and (f.path, f.line, f.severity) in external_hits:
            continue
        key = (f.path, f.line, "secret") if secretish else (f.path, f.line, f.rule, f.source)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    unique.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.path, f.line))
    return unique, notes

"""Tests for planes/verify: change collection, security, complexity, risk, review."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

import planes.verify.security as security
from planes.verify import complexity, review
from planes.verify.diff import NotAGitRepo, collect_changes
from planes.verify.risk import assess

POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "fake executables are POSIX scripts")


class RepoCase(unittest.TestCase):
    """A throwaway git repo with a base commit; tests then edit the working tree."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.env = mock.patch.dict(os.environ, {"HARNESS_NO_EXTERNAL": "1"})
        self.env.start()
        security._SEMGREP_DISABLED = None

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", "-C", self.root, *args], check=True, capture_output=True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))

    def commit(self, files):
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def changes(self, base="HEAD"):
        return collect_changes(self.root, base)

    def builtin(self, rel, text, base_files=None):
        self.commit(base_files or {"README.md": "x\n"})
        self.write(rel, text)
        return security.scan_builtin(self.changes())


class TestCollectChanges(RepoCase):
    def test_modified_untracked_and_deleted(self):
        self.commit({"a.py": "one\ntwo\nthree\n", "gone.py": "x\n"})
        self.write("a.py", "one\nTWO\nthree\nfour\n")
        self.write("new.py", "n1\nn2\n")
        os.remove(os.path.join(self.root, "gone.py"))
        by = {f.path: f for f in self.changes().files}
        self.assertEqual(by["a.py"].status, "modified")
        self.assertEqual(by["a.py"].added, {2: "TWO", 4: "four"})
        self.assertEqual(by["a.py"].removed_count, 1)
        self.assertEqual(by["a.py"].base_text, "one\ntwo\nthree\n")
        self.assertEqual(by["new.py"].status, "added")
        self.assertEqual(by["new.py"].added, {1: "n1", 2: "n2"})
        self.assertEqual(by["gone.py"].status, "deleted")

    def test_repo_without_commits_treats_everything_as_new(self):
        self.write("a.py", "x = 1\n")
        files = self.changes().files
        self.assertEqual([(f.path, f.status) for f in files], [("a.py", "added")])

    def test_bad_base_and_non_repo(self):
        self.commit({"a.py": "x\n"})
        with self.assertRaises(RuntimeError):
            self.changes(base="no-such-ref")
        outside = tempfile.mkdtemp()
        try:
            with self.assertRaises(NotAGitRepo):
                collect_changes(outside)
        finally:
            shutil.rmtree(outside)

    def test_ignored_and_binary_files(self):
        self.commit({".gitignore": "build/\n"})
        self.write("build/out.js", "eval(x)\n")
        with open(os.path.join(self.root, "img.bin"), "wb") as f:
            f.write(b"\x00\x01eval(x)")
        files = {f.path: f for f in self.changes().files}
        self.assertNotIn("build/out.js", files)
        self.assertEqual(files["img.bin"].added, {})


class TestBuiltinSecurity(RepoCase):
    def rules(self, findings):
        return {f.rule for f in findings}

    def test_secrets_are_found_and_redacted(self):
        token = "ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"
        aws = "AKIA" + "Z7QXMPL4ROWSK2JD"
        findings = self.builtin("cfg.py", f'T = "{token}"\nK = "{aws}"\nDB_PASSWORD = "Tr0ub4dor-horse-9"\n')
        self.assertEqual(self.rules(findings), {"github-token", "aws-access-key-id", "hardcoded-credential"})
        for f in findings:
            self.assertNotIn(token, f.message)
            self.assertNotIn("Tr0ub4dor-horse-9", f.message)

    def test_placeholders_and_env_lookups_are_not_secrets(self):
        findings = self.builtin("cfg.py", 'API_KEY = "your-api-key-here"\npassword = os.environ["DB_PASSWORD"]\nsecret = "${SECRET}"\n')
        self.assertEqual(findings, [])

    def test_python_rules(self):
        findings = self.builtin("app.py", textwrap.dedent('''\
            total = eval(expr)
            subprocess.run(f"ls {path}", shell=True)
            subprocess.run("ls", shell=True)
            data = pickle.loads(blob)
            cur.execute("SELECT * FROM t WHERE id = " + uid)
            requests.get(url, verify=False)
        '''))
        by_line = {f.line: f.rule for f in findings}
        self.assertEqual(by_line, {1: "dynamic-eval", 2: "shell-injection", 3: "shell-true", 4: "unsafe-deserialize",
                                   5: "sql-string-build", 6: "tls-verify-off"})
        self.assertEqual({f.line: f.severity for f in findings}[3], "medium")

    def test_safe_python_is_quiet(self):
        findings = self.builtin("app.py", textwrap.dedent('''\
            value = ast.literal_eval(text)
            cfg = yaml.load(f, Loader=yaml.SafeLoader)
            cur.execute("SELECT * FROM t WHERE id = %s", (uid,))
            subprocess.run(["ls", path])
            # eval(x) is mentioned only in a comment
            result = eval("1 + 1")
        '''))
        self.assertEqual(findings, [])

    def test_javascript_rules_and_exec_is_not_eval(self):
        findings = self.builtin("web.js", textwrap.dedent('''\
            el.innerHTML = name;
            exec(`git log ${ref}`);
            const re = /a/; re.exec(str);
            db.query(`SELECT * FROM t WHERE id = ${id}`);
            el.textContent = name;
        '''))
        self.assertEqual({(f.line, f.rule) for f in findings},
                         {(1, "html-injection"), (2, "child-process-dynamic"), (4, "sql-string-build")})

    def test_only_added_lines_are_scanned(self):
        findings = self.builtin("app.py", "x = eval(a)\ny = 2\n", base_files={"app.py": "x = eval(a)\n"})
        self.assertEqual(findings, [])


FAKE_GITLEAKS = """\
import json, sys
path = sys.argv[2]
print(json.dumps([{"RuleID": "fake-rule", "Description": "Fake secret", "File": path, "StartLine": 1, "Secret": "REDACTED"},
                  {"RuleID": "fake-rule", "Description": "Fake secret", "File": path, "StartLine": 99, "Secret": "REDACTED"}]))
"""

FAKE_SEMGREP = """\
import json, os, sys
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if os.path.exists(sys.argv[0] + ".fail"):
    print("cannot reach registry", file=sys.stderr); sys.exit(2)
files = [a for a in sys.argv[1:] if a.endswith((".py", ".js"))]
print(json.dumps({"results": [
    {"check_id": "rules.py-danger", "path": files[0], "start": {"line": 1}, "extra": {"severity": "ERROR", "message": "danger here"}},
    {"check_id": "rules.py-danger", "path": files[0], "start": {"line": 40}, "extra": {"severity": "ERROR", "message": "old code"}},
]}))
"""


@POSIX_ONLY
class TestExternalScanners(RepoCase):
    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp()
        os.environ.pop("HARNESS_NO_EXTERNAL")
        self.path = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.path.dirname(shutil.which("git"))})
        self.path.start()

    def tearDown(self):
        self.path.stop()
        shutil.rmtree(self.bin, ignore_errors=True)
        super().tearDown()

    def exe(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n" + textwrap.dedent(body))
        os.chmod(path, 0o755)
        return path

    def test_gitleaks_findings_limited_to_added_lines(self):
        self.exe("gitleaks", FAKE_GITLEAKS)
        self.commit({"README.md": "x\n"})
        self.write("cfg.py", "x = 1\n")
        findings, notes = security.scan(self.changes())
        self.assertEqual([(f.rule, f.line, f.source) for f in findings], [("fake-rule", 1, "gitleaks")])
        self.assertIn("gitleaks", notes)

    def test_semgrep_filtered_and_local_config_preferred(self):
        semgrep = self.exe("semgrep", FAKE_SEMGREP)
        self.commit({".semgrep.yml": "rules: []\n"})
        self.write("app.py", "danger()\n")
        findings, _ = security.scan(self.changes())
        self.assertEqual([(f.rule, f.line, f.severity) for f in findings], [("py-danger", 1, "high")])
        with open(semgrep + ".calls") as f:
            call = f.read()
        self.assertIn("--config .semgrep.yml", call)
        self.assertIn("--metrics=off", call)

    def test_semgrep_failure_is_reported_once_then_skipped(self):
        semgrep = self.exe("semgrep", FAKE_SEMGREP)
        open(semgrep + ".fail", "w").close()
        self.commit({"README.md": "x\n"})
        self.write("app.py", "danger()\n")
        _, notes = security.scan(self.changes())
        self.assertTrue(any("p/default" in n and "HARNESS_SEMGREP_CONFIG" in n for n in notes))
        security.scan(self.changes())
        with open(semgrep + ".calls") as f:
            self.assertEqual(len(f.read().splitlines()), 1)


class TestComplexity(RepoCase):
    def test_python_mccabe_counts(self):
        source = textwrap.dedent('''\
            def f(a, b):
                if a and b or a:
                    for x in range(3):
                        pass
                elif b:
                    return [y for y in a if y]
                try:
                    pass
                except ValueError:
                    pass
                def inner():
                    if a: pass
                return 1 if a else 2
        ''')
        by = {f.name: f.ccn for f in complexity.python_functions(source)}
        # 1 + if + 2 boolops + for + elif + comprehension(1 + 1 if) + except + ifexp = 10
        self.assertEqual(by["f"], 10)
        self.assertEqual(by["f.inner"], 2)

    def test_heuristic_for_braces(self):
        src = "function g(a) {\n  if (a && b) { return 1; }\n  for (;;) {}\n  return a ? 1 : 2;\n}\n"
        self.assertEqual([(f.name, f.ccn) for f in complexity.heuristic_functions("x.js", src)], [("g", 5)])

    def test_ratchet_only_reports_increases(self):
        legacy = "def legacy(a):\n" + "".join(f"    if a == {i}: return {i}\n" for i in range(15))
        self.commit({"m.py": legacy + "def small(a):\n    return a\n"})
        grown = "def small(a):\n" + "".join(f"    if a == {i}: return {i}\n" for i in range(12)) + "    return a\n"
        self.write("m.py", legacy.replace("return 0", "return 0  # touched") + grown)
        results, method = complexity.analyze(self.changes())
        self.assertEqual(method, "ast")
        self.assertEqual([(r.function, r.before, r.after) for r in results], [("small", 1, 13)])

    def test_new_complex_function_reported(self):
        self.commit({"README.md": "x\n"})
        self.write("n.py", "def big(a):\n" + "".join(f"    if a == {i}: return {i}\n" for i in range(11)))
        results, _ = complexity.analyze(self.changes())
        self.assertEqual([(r.function, r.before, r.after) for r in results], [("big", None, 12)])


class TestRisk(RepoCase):
    def risk(self):
        changes = self.changes()
        findings, _ = security.scan(changes)
        increases, _ = complexity.analyze(changes)
        return assess(changes, findings, increases)

    def reasons(self, r):
        return " | ".join(f.reason for f in r.factors)

    def test_docs_only_is_low(self):
        self.commit({"README.md": "x\n"})
        self.write("README.md", "x\nmore docs\n")
        self.assertEqual(self.risk().level, "low")

    def test_sensitive_path_without_tests(self):
        self.commit({"src/auth/login.py": "def login(u):\n    return u\n"})
        self.write("src/auth/login.py", "def login(u):\n    return u.strip()\n")
        r = self.risk()
        self.assertIn("auth and sessions", self.reasons(r))
        self.assertIn("no tests changed", self.reasons(r))
        self.assertEqual(r.level, "medium")

    def test_new_dependency_vs_version_bump(self):
        self.commit({"package.json": '{\n  "dependencies": {\n    "qs": "6.1.0"\n  }\n}\n', "test/a.test.js": "x\n"})
        self.write("package.json", '{\n  "dependencies": {\n    "qs": "6.2.0",\n    "lodahs": "^1.0.0"\n  }\n}\n')
        reasons = self.reasons(self.risk())
        self.assertIn("new dependencies: lodahs", reasons)
        self.assertIn("dependency versions changed: qs", reasons)

    def test_removed_public_function(self):
        self.commit({"lib.py": "def keep():\n    pass\ndef api_call():\n    pass\ndef _private():\n    pass\n",
                     "tests/test_lib.py": "x\n"})
        self.write("lib.py", "def keep():\n    return 1\n")
        reasons = self.reasons(self.risk())
        self.assertIn("lib.py:api_call", reasons)
        self.assertNotIn("_private", reasons)

    def test_security_findings_make_it_high(self):
        self.commit({"README.md": "x\n"})
        self.write("app.py", "x = eval(a)\nsubprocess.run(f'rm {p}', shell=True)\n")
        self.assertEqual(self.risk().level, "high")


class TestReviewEntryPoints(RepoCase):
    def test_tool_output_and_no_change(self):
        self.commit({"a.py": "x = 1\n"})
        tool = review.ReviewChangeTool(self.root)
        self.assertIn("No changes against HEAD", tool.execute({}).output)
        self.write("a.py", "x = eval(y)\n")
        out = tool.execute({}).output
        self.assertIn("Risk: ", out)
        self.assertIn("dynamic-eval", out)
        self.assertIn("Next: fix the high-severity findings", out)

    def test_tool_outside_git(self):
        outside = tempfile.mkdtemp()
        try:
            result = review.ReviewChangeTool(outside).execute({})
            self.assertFalse(result.success)
            self.assertIn("REVIEW_UNAVAILABLE", result.output)
        finally:
            shutil.rmtree(outside)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = review.main(["--workspace", self.root, *args])
        return code, out.getvalue()

    def test_cli_exit_codes_and_json(self):
        self.commit({"a.py": "x = 1\n"})
        self.write("a.py", "x = eval(y)\n")
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli("--fail-on", "high")[0], 1)
        code, out = self.run_cli("--json")
        data = json.loads(out)
        self.assertEqual(data["risk"]["level"], "high")
        self.assertEqual(data["findings"][0]["rule"], "dynamic-eval")
        self.write("a.py", "x = 2\n")
        self.assertEqual(self.run_cli("--fail-on", "high")[0], 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for docker-hygiene. Run with: python -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dockerhygiene


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8",
                                                    "newline": "\n"}
    with open(path, mode, **kwargs) as handle:
        handle.write(content)


class MatcherTests(unittest.TestCase):
    def test_basic(self):
        self.assertTrue(dockerhygiene.is_ignored("a.log", ["*.log"]))
        self.assertTrue(dockerhygiene.is_ignored("sub/a.log", ["*.log"]))
        self.assertFalse(dockerhygiene.is_ignored("a.txt", ["*.log"]))

    def test_negation_last_wins(self):
        patterns = ["*.env", "!.env.example"]
        self.assertTrue(dockerhygiene.is_ignored(".env", patterns))
        self.assertFalse(dockerhygiene.is_ignored(".env.example", patterns))

    def test_double_star(self):
        self.assertTrue(
            dockerhygiene.is_ignored("a/b/node_modules", ["**/node_modules"]))
        self.assertTrue(
            dockerhygiene.is_ignored("node_modules", ["**/node_modules"]))

    def test_dir_only(self):
        self.assertTrue(dockerhygiene.is_ignored("build", ["build/"]))
        self.assertTrue(dockerhygiene.is_ignored("build/x.js", ["build/"]))
        self.assertFalse(dockerhygiene.is_ignored("rebuild", ["build/"]))

    def test_anchored(self):
        self.assertTrue(dockerhygiene.is_ignored("a.log", ["/a.log"]))
        self.assertFalse(dockerhygiene.is_ignored("sub/a.log", ["/a.log"]))


class CopyTests(unittest.TestCase):
    def test_copy_sources(self):
        with tempfile.NamedTemporaryFile("w", suffix="Dockerfile",
                                         delete=False) as tmp:
            tmp.write("FROM python:3.12\nCOPY requirements.txt ./\n"
                      "COPY src/ ./app/\nCMD [\"python\", \"app.py\"]\n")
            path = tmp.name
        try:
            self.assertEqual(
                dockerhygiene.dockerfile_copy_sources(path),
                ["requirements.txt", "src/"])
        finally:
            os.unlink(path)

    def test_copy_dot_and_json_form(self):
        with tempfile.NamedTemporaryFile("w", suffix="Dockerfile",
                                         delete=False) as tmp:
            tmp.write('FROM node:22\nCOPY . .\n')
            p1 = tmp.name
        with tempfile.NamedTemporaryFile("w", suffix="Dockerfile",
                                         delete=False) as tmp:
            tmp.write('FROM node:22\nCOPY ["a", "b", "/dest/"]\n')
            p2 = tmp.name
        try:
            self.assertIn(".", dockerhygiene.dockerfile_copy_sources(p1))
            self.assertEqual(dockerhygiene.dockerfile_copy_sources(p2),
                             ["a", "b"])
        finally:
            os.unlink(p1)
            os.unlink(p2)


def make_context(files):
    root = tempfile.mkdtemp(prefix="docker-hygiene-test-")
    for rel, content in files.items():
        write(root, rel, content)
    return root


CLEAN = {
    "Dockerfile": "FROM python:3.12-slim\nCOPY requirements.txt ./\n"
                  "RUN pip install -r requirements.txt\nCOPY src/ ./src/\n",
    ".dockerignore": ".git\nnode_modules\n.env\n*.pem\nDockerfile\n"
                     ".dockerignore\n",
    "requirements.txt": "flask==3.0.0\n",
    "src/app.py": "print('hi')\n",
}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.roots = []

    def tearDown(self):
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def ctx(self, files):
        root = make_context(files)
        self.roots.append(root)
        return root

    def test_clean_context_passes(self):
        findings, stats = dockerhygiene.audit(self.ctx(dict(CLEAN)), 100)
        errors = [f for f in findings if f["severity"] == "error"]
        self.assertEqual(errors, [], json.dumps(findings, indent=1))
        ok, _ = dockerhygiene.evaluate(findings, strict=False)
        self.assertTrue(ok)

    def test_missing_ignore(self):
        files = dict(CLEAN)
        del files[".dockerignore"]
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertTrue(any(f["check"] == "missing-ignore" and
                            f["severity"] == "error" for f in findings))

    def test_secret_sent_without_ignore(self):
        files = {"Dockerfile": CLEAN["Dockerfile"],
                 ".env": "SECRET=abc\n"}
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertTrue(any(f["check"] == "secret-in-context" and
                            ".env" in f["path"] for f in findings))

    def test_secret_excluded_by_ignore_is_quiet(self):
        files = dict(CLEAN)
        files[".env"] = "SECRET=abc\n"
        files[".dockerignore"] += ".env\n"
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertFalse(any(f["check"] == "secret-in-context"
                             for f in findings))

    def test_heavy_node_modules_unignored(self):
        files = {"Dockerfile": CLEAN["Dockerfile"],
                 ".dockerignore": ".git\n",
                 "node_modules/pkg/index.js": "x\n"}
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertTrue(any(f["check"] == "heavy-dir" and
                            "node_modules" in f["path"] for f in findings))

    def test_heavy_node_modules_ignored_is_quiet(self):
        files = dict(CLEAN)
        files["node_modules/pkg/index.js"] = "x\n"
        files[".dockerignore"] += "node_modules\n"
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertFalse(any(f["check"] == "heavy-dir" for f in findings))

    def test_broad_copy_without_ignore(self):
        files = {"Dockerfile": "FROM node:22\nCOPY . .\n",
                 "src/a.js": "x\n"}
        findings, _ = dockerhygiene.audit(self.ctx(files), 100)
        self.assertTrue(any(f["check"] == "broad-copy" for f in findings))

    def test_heavy_context_warns(self):
        files = dict(CLEAN)
        files["data/blob.bin"] = b"0" * (3 * 1024 * 1024)
        findings, _ = dockerhygiene.audit(self.ctx(files), 1)
        self.assertTrue(any(f["check"] == "heavy-context" for f in findings))
        ok, _ = dockerhygiene.evaluate(findings, strict=False)
        self.assertTrue(ok)  # warning only
        ok2, _ = dockerhygiene.evaluate(findings, strict=True)
        self.assertFalse(ok2)

    def test_per_dockerfile_ignore_convention(self):
        files = {"app/Dockerfile": CLEAN["Dockerfile"],
                 "app/Dockerfile.dockerignore": ".git\n",
                 "app/main.py": "x\n"}
        findings, stats = dockerhygiene.audit(self.ctx(files), 100)
        self.assertEqual(stats["ignore_file"],
                         os.path.join("app", "Dockerfile.dockerignore"))
        self.assertFalse(any(f["check"] == "missing-ignore"
                             for f in findings))

    def test_no_dockerfile_warns(self):
        findings, _ = dockerhygiene.audit(self.ctx({"a.txt": "x\n"}), 100)
        self.assertTrue(any(f["check"] == "no-dockerfile" for f in findings))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = make_context(dict(CLEAN))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "dockerhygiene.py")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, self.script, *args],
                              capture_output=True, text=True, cwd=self.root)

    def test_cli_clean_passes(self):
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_cli_fails_missing_ignore(self):
        os.remove(os.path.join(self.root, ".dockerignore"))
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing-ignore", proc.stdout)

    def test_cli_json_valid(self):
        proc = self.run_cli("--root", self.root, "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("top_dirs", payload["stats"])

    def test_cli_bad_root_exit_2(self):
        proc = self.run_cli("--root", os.path.join(self.root, "nope"))
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()

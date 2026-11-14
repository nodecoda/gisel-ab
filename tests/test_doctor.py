"""`gisel-ab doctor` is the preflight for a comparison.

A run builds a runner before it discovers that the tree is not the official
side, so the failure costs minutes and lands far from its cause.  These tests
pin the three things that make the check usable: exit codes (0 / 2), a `fix`
on every failure, and the content-based verdict from check-pin.sh -- not a
HEAD comparison.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_dir(path):
    """`is_dir` for a path that may sit under a directory we cannot enter.

    The dev-machine test names an absolute path that does not exist on CI.
    On some runners `/home/ubuntu` exists but is not readable, and an
    unguarded `is_dir()` there raises PermissionError at import time.
    """
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path):
    try:
        return path.is_file()
    except OSError:
        return False


ROOT_ENVS = ("AB_TREE", "COHORT_TREE", "AB_BUILD", "COHORT_LLVM",
             "AB_LLVM_REPO", "UPSTREAM_SOURCE", "GS_PIN")


def run(*args, env=None):
    # `cohort verify` exports the roots, and they must not leak into a test
    # whose subject is "nothing is configured".
    base = {k: v for k, v in os.environ.items() if k not in ROOT_ENVS}
    base["GISEL_AB_CONFIG"] = str(ROOT / "tests" / ".no-such-config.json")
    base.update(env or {})
    return subprocess.run([sys.executable, str(ROOT / "ab.py"), "doctor", *args],
                          cwd=ROOT, env=base, capture_output=True, text=True)


class EmptyEnvironment(unittest.TestCase):
    def test_it_fails_with_the_setup_lines(self):
        done = run()
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("gisel-ab setup --llvm DIR", done.stdout)
        self.assertIn("gisel-ab setup --build DIR", done.stdout)
        self.assertIn("(or export AB_TREE)", done.stdout)
        self.assertIn("(or export AB_BUILD)", done.stdout)

    def test_json_mode_names_every_failure_and_its_fix(self):
        doc = json.loads(run("--json").stdout)
        self.assertFalse(doc["ok"])
        failed = [c for c in doc["checks"] if c["status"] == "fail"]
        self.assertTrue(failed)
        names = {c["name"] for c in failed}
        self.assertIn("tree", names)
        self.assertIn("build", names)
        for check in failed:
            self.assertIn("fix", check, check)
            self.assertTrue(check["fix"])


class TreeMustMatchThePinByContent(unittest.TestCase):
    def test_a_foreign_checkout_is_rejected_with_a_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "llvm-project"
            (repo / "llvm").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                            "-c", "user.name=t", "commit", "-q", "--allow-empty",
                            "-m", "empty"], check=True)
            done = run("--tree", str(repo))
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("tree", done.stdout)
        self.assertIn("official_base", done.stdout)


class TheRealMachine(unittest.TestCase):
    """"The dev machine passes" is the acceptance criterion for A3.

    The paths come from AB_TREE/AB_BUILD, not from guessing a developer's
    layout.  A cohort clone inherits neither, so this acceptance check skips
    instead of failing on whatever tree happens to sit beside it.
    """

    TREE = Path(os.environ["AB_TREE"]) if os.environ.get("AB_TREE") else None
    BUILD = Path(os.environ["AB_BUILD"]) if os.environ.get("AB_BUILD") else None

    @unittest.skipUnless(TREE is not None and BUILD is not None
                         and _is_dir(TREE / "llvm")
                         and _is_file(BUILD / "build.ninja"),
                         "set AB_TREE and AB_BUILD to the dev machine's tree "
                         "and build")
    def test_dev_machine_is_healthy(self):
        done = run(env={"AB_TREE": str(self.TREE),
                        "AB_BUILD": str(self.BUILD)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("0 failed", done.stdout)

    @unittest.skipUnless(TREE is not None and BUILD is not None
                         and _is_dir(TREE / "llvm")
                         and _is_file(BUILD / "build.ninja"),
                         "set AB_TREE and AB_BUILD to the dev machine's tree "
                         "and build")
    def test_json_reports_the_same_verdict(self):
        doc = json.loads(run("--json", env={
            "AB_TREE": str(self.TREE), "AB_BUILD": str(self.BUILD)}).stdout)
        self.assertTrue(doc["ok"])
        statuses = {c["name"]: c["status"] for c in doc["checks"]}
        self.assertEqual(statuses["tree"], "ok")
        self.assertEqual(statuses["build"], "ok")
        self.assertEqual(statuses["gisel"], "ok")


if __name__ == "__main__":
    unittest.main()

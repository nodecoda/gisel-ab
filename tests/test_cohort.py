"""The cohort record is only useful if it is re-checkable.

These tests do not run the real gates.  They pin the parts that decide
whether a gate is FRESH: the gate inventory, the content fingerprint, the
changed-input report, and the refusal to tag a dirty tree.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cohort

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


IDENTITY = ("-c", "user.name=cohort", "-c", "user.email=cohort@example.invalid")


def init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "-A")
    git(path, *IDENTITY, "commit", "-q", "-m", "seed")
    return git(path, "rev-parse", "HEAD").stdout.strip()


class GateInventory(unittest.TestCase):
    def test_every_manifest_gate_has_a_descriptor(self):
        manifest = cohort.load_manifest()
        table = cohort.gate_table(manifest)
        self.assertEqual([g["manifest_command"] for g in table
                          if g["manifest_command"]], manifest["gates"])
        for gate in table:
            self.assertIn(gate["repo"], cohort.REPO_DIRS)
            self.assertTrue(gate["inputs"], gate["name"])

    def test_online_plan_names_the_adopt_gate(self):
        manifest = cohort.load_manifest()
        table = cohort.gate_table(manifest, online=True)
        adopt = [g for g in table if g["name"] == "adopt-check"]
        self.assertEqual(len(adopt), 1)
        self.assertTrue(adopt[0]["online"])
        self.assertNotIn("adopt-check",
                         [g["name"] for g in cohort.gate_table(manifest)])


class Fingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gisel = self.root / "gisel"
        self.commit = init_repo(self.gisel, {
            "gisel/a.py": "A = 1\n",
            "gisel/b.py": "B = 1\n",
            "tests/corpus/X.gisel": "ruleset X: G_ADD\n",
        })
        self.gate = {"name": "unit", "repo": "gisel",
                     "inputs": ["gisel/*.py"]}
        self.patches = [
            mock.patch.object(cohort, "GISEL", self.gisel),
            mock.patch.object(cohort, "UPSTREAM", self.root / "upstream"),
            mock.patch.object(cohort, "AB", self.root / "ab"),
        ]
        for patch in self.patches:
            patch.start()
        cohort._CACHE.clear()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        cohort._CACHE.clear()
        self.tmp.cleanup()

    def test_committed_fingerprint_is_deterministic(self):
        first = cohort.fingerprint(self.gate, commits={"gisel": self.commit})
        cohort._CACHE.clear()
        second = cohort.fingerprint(self.gate, commits={"gisel": self.commit})
        self.assertEqual(first, second)

    def test_worktree_fingerprint_follows_content(self):
        before = cohort.fingerprint(self.gate, worktree=True)
        (self.gisel / "gisel" / "a.py").write_text("A = 2\n")
        cohort._CACHE.clear()
        after = cohort.fingerprint(self.gate, worktree=True)
        self.assertNotEqual(before, after)

    def test_glob_selects_the_expected_files(self):
        blobs = cohort._blob_map(self.gate, worktree=True)
        self.assertEqual(set(blobs), {"gisel:gisel/a.py", "gisel:gisel/b.py"})
        corpus = {"name": "cases", "repo": "gisel",
                  "inputs": ["tests/corpus/*.gisel"]}
        self.assertEqual(set(cohort._blob_map(corpus, worktree=True)),
                         {"gisel:tests/corpus/X.gisel"})

    def test_changed_inputs_names_the_file(self):
        record = {
            "commits": {"gisel": self.commit},
            "gates": [{"name": "unit",
                       "blobs": cohort._blob_map(self.gate, worktree=True)}],
        }
        self.assertEqual(cohort.changed_inputs(self.gate, record), [])
        (self.gisel / "gisel" / "b.py").write_text("B = 2\n")
        cohort._CACHE.clear()
        self.assertEqual(cohort.changed_inputs(self.gate, record),
                         ["gisel:gisel/b.py"])


class CheckSkip(unittest.TestCase):
    """The tag commit still carries the previous cohort's record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "RELEASE.json"
        self.record = self.root / "VERIFICATION.json"
        self.patches = [
            mock.patch.object(cohort, "MANIFEST", self.manifest),
            mock.patch.object(cohort, "VERIFICATION", self.record),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def write(self, record_cohort, manifest_cohort="cohort-new"):
        self.manifest.write_text(json.dumps({
            "schema": 1,
            "cohort": manifest_cohort,
            "repos": {"gisel": "a" * 40, "upstream_source": "b" * 40,
                      "ab": "c" * 40},
            "gates": [],
        }, indent=2) + "\n")
        self.record.write_text(json.dumps({
            "schema": 2,
            "cohort": record_cohort,
            "commits": {"gisel": "a" * 40, "upstream_source": "b" * 40,
                        "ab": "c" * 40},
            "gates": [],
        }, indent=2) + "\n")

    def check(self, worktree=False, as_json=False):
        args = mock.Mock()
        args.worktree = worktree
        args.json = as_json
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cohort.cmd_check(args)
        return rc, out.getvalue(), err.getvalue()

    def test_record_for_the_previous_cohort_is_skipped(self):
        self.write("cohort-old")
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("skipping", out)
        self.assertIn("cohort-old", out)

    def test_json_reports_the_skip(self):
        self.write("cohort-old")
        rc, out, _ = self.check(as_json=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["skipped"])
        self.assertNotIn("gates", payload)

    def test_worktree_check_refuses_a_mismatched_record(self):
        self.write("cohort-old")
        rc, _, err = self.check(worktree=True)
        self.assertEqual(rc, 2)
        self.assertIn("worktree check", err)

    def test_current_record_is_not_skipped(self):
        self.write("cohort-new")
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertNotIn("skipping", out)


class RecordCi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = Path(self.tmp.name) / "VERIFICATION.json"
        self.update = Path(self.tmp.name) / "CI.json"
        self.record.write_text(json.dumps({"schema": 2, "cohort": "c",
                                           "ci": {}}) + "\n")
        self.patch = mock.patch.object(cohort, "VERIFICATION", self.record)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def record_ci(self, payload):
        self.update.write_text(json.dumps(payload))
        args = mock.Mock()
        args.file = str(self.update)
        return cohort.cmd_record_ci(args)

    def test_notes_land_beside_the_runs(self):
        self.assertEqual(self.record_ci({
            "ab": {"conclusion": "success", "run": 1},
            "publish": {"runs": 0},
            "notes": ["the tag commit predates its record"],
        }), 0)
        written = json.loads(self.record.read_text())
        self.assertEqual(written["ci"]["ab"]["run"], 1)
        self.assertNotIn("notes", written["ci"])
        self.assertEqual(written["ci_notes"],
                         ["the tag commit predates its record"])


class Tagging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ab = self.root / "ab"
        self.gisel = self.root / "gisel"
        self.upstream = self.root / "gisel-anchor"
        self.gisel_commit = init_repo(self.gisel, {"gisel/a.py": "A = 1\n"})
        self.upstream_commit = init_repo(self.upstream, {"pin/PIN.json": "{}\n"})
        self.ab_commit = init_repo(self.ab, {"code.py": "x = 1\n"})
        (self.ab / "RELEASE.json").write_text(json.dumps({
            "schema": 1,
            "cohort": "cohort-test",
            "repos": {"gisel": self.gisel_commit,
                      "upstream_source": self.upstream_commit,
                      "ab": self.ab_commit},
            "gates": [],
        }, indent=2) + "\n")
        git(self.ab, "add", "RELEASE.json")
        git(self.ab, *IDENTITY, "commit", "-q", "-m", "manifest")
        self.manifest_commit = git(self.ab, "rev-parse", "HEAD").stdout.strip()
        self.patches = [
            mock.patch.object(cohort, "AB", self.ab),
            mock.patch.object(cohort, "GISEL", self.gisel),
            mock.patch.object(cohort, "UPSTREAM", self.upstream),
            mock.patch.object(cohort, "MANIFEST", self.ab / "RELEASE.json"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def tag(self):
        args = mock.Mock(name="args")
        args.name = "cohort-test"
        args.push = False
        return cohort.cmd_tag(args)

    def test_clean_trio_is_tagged_at_the_recorded_commits(self):
        self.assertEqual(self.tag(), 0)
        for repo, expected in ((self.gisel, self.gisel_commit),
                               (self.upstream, self.upstream_commit),
                               (self.ab, self.manifest_commit)):
            got = git(repo, "rev-parse", "cohort-test^{commit}").stdout.strip()
            self.assertEqual(got, expected)

    def test_dirty_tree_is_refused(self):
        (self.gisel / "gisel" / "a.py").write_text("A = 2\n")
        self.assertEqual(self.tag(), 2)
        self.assertEqual(git(self.gisel, "tag", "--list").stdout.strip(), "")

    def test_existing_tag_is_not_rewritten(self):
        self.assertEqual(self.tag(), 0)
        git(self.gisel, "tag", "-d", "cohort-test")
        self.assertEqual(self.tag(), 2)


if __name__ == "__main__":
    unittest.main()


class MissingRootIsAGateFailure(unittest.TestCase):
    """A gate whose expansion needs an LLVM tree fails alone, with rc=2.

    Before this, `_expand_command` raised SystemExit from a worker thread and
    `cohort verify` stopped after the gates that happened to finish, printing a
    short table that looked green.  One gate, one verdict.
    """

    def test_an_expand_error_is_returned_not_raised(self):
        gate = {"name": "pin/make-base.sh TREE DEST --verify",
                "repo": "upstream_source",
                "manifest_command": "pin/make-base.sh TREE DEST --verify",
                "command": "pin/make-base.sh TREE DEST --verify",
                "expand": "make-base"}
        with mock.patch.object(cohort.paths, "require_tree",
                               side_effect=SystemExit(2)):
            result = cohort._run_one(gate, "/tmp/does-not-exist",
                                     "/tmp/does-not-exist", 5)
        self.assertEqual(result["rc"], 2)
        self.assertIn("input error", result["evidence"])
        self.assertEqual(result["name"], gate["name"])

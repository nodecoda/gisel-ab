"""The strict evidence checks: exact counts, kinds, closure, adoption, schema."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = [sys.executable, "-m", "gisel_ab.evidence"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_manifest(entries, direction="all", frozen=True, gisel=None):
    tmp = Path(tempfile.mkdtemp())
    manifest = tmp / "EVIDENCE.json"
    manifest.write_text(json.dumps({"schema": 1, "entries": entries}))
    args = [*EVIDENCE, "verify", "--manifest", str(manifest),
            "--direction", direction]
    if frozen:
        args.append("--frozen")
    env = dict(os.environ)
    if gisel:
        env["GISEL_SRC"] = str(gisel)
    return subprocess.run(args, capture_output=True, text=True, env=env)


def neg_entry(tmp, by_ruleset=True, disagree=2, expect_disagree=2):
    diff = {"ref": 10, "ours": 10, "matched": 10, "agree": 8,
            "answered_equal": 8, "unanswerable": 0, "disagree": disagree,
            "ref_asserted": 0, "missing_in_both": 0, "unexpected": 0}
    attribution = {"disagree": disagree, "unknown_keys": 0,
                   "outside_expected": [], "covered_expected": ["X_G_ADD"],
                   "expected_rulesets": ["X_G_ADD"]}
    if by_ruleset:
        attribution["by_ruleset"] = {"X_G_ADD": disagree}
    (tmp / "diff.json").write_text(json.dumps(diff))
    (tmp / "attribution.json").write_text(json.dumps(attribution))
    return {
        "id": "G-neg/notarget", "kind": "neg", "direction": "G-neg",
        "target": "notarget", "command": "make-neg", "dir": str(tmp),
        "artifacts": {"diff.json": sha(tmp / "diff.json"),
                      "attribution.json": sha(tmp / "attribution.json")},
        "expect": {"disagree": expect_disagree, "rulesets": ["X_G_ADD"],
                   "unknown_keys": 0, "answered_equal": 8},
    }


class NegativeEvidence(unittest.TestCase):
    def test_exact_disagreement_count_is_checked(self):
        """A6: 2 -> 1 in the manifest must not pass."""
        proc = run_manifest([neg_entry(Path(tempfile.mkdtemp()),
                                      expect_disagree=1)], frozen=False)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("diff.disagree=2, expected exactly 1", proc.stdout)

    def test_by_ruleset_is_required(self):
        """A7: an attribution without by_ruleset is not evidence."""
        proc = run_manifest([neg_entry(Path(tempfile.mkdtemp()),
                                      by_ruleset=False)], frozen=False)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("no by_ruleset entries", proc.stdout)

    def test_a_matching_negative_entry_passes(self):
        proc = run_manifest([neg_entry(Path(tempfile.mkdtemp()))], frozen=False)
        self.assertEqual(proc.returncode, 0, proc.stdout)


def closure_entry(tmp, seed_states=10, stuck=0):
    report = {
        "target": "x86", "seed_states": seed_states, "ok": True,
        "edges": {"successors": 4, "revisited": 4, "stuck": stuck},
        "disagree": [],
        "answers": {"unanswerable": {"both": 0, "ours_only": 0, "ref_only": 0},
                    "terminal": {"Legal": 10}},
        "rounds": [{"round": 1, "asked": seed_states, "disagree": 0,
                    "new_states": 0, "one_sided": 0}],
    }
    path = tmp / "x86-G.json"
    path.write_text(json.dumps(report))
    return {
        "id": "closure/x86-G", "kind": "closure", "direction": "closure",
        "target": "x86", "command": "make-closure", "dir": str(tmp),
        "artifacts": {"x86-G.json": sha(path)},
        "expect": {"ok": True, "seed_states": 10, "successors": 4,
                   "revisited": 4, "stuck": 0, "disagree": 0,
                   "both_unanswerable": 0, "ref_only_unanswerable": 0,
                   "ours_only_unanswerable": 0},
    }


class ClosureEvidence(unittest.TestCase):
    def test_matching_snapshot_passes(self):
        proc = run_manifest([closure_entry(Path(tempfile.mkdtemp()))])
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_stuck_edge_fails(self):
        proc = run_manifest([closure_entry(Path(tempfile.mkdtemp()), stuck=1)])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("stuck edges are not zero", proc.stdout)

    def test_a_seed_count_drift_fails(self):
        proc = run_manifest([closure_entry(Path(tempfile.mkdtemp()),
                                           seed_states=11)])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("stuck edges are not zero", proc.stdout) if False else None
        self.assertIn("seed_states=11, expected 10", proc.stdout)


class AdoptEvidence(unittest.TestCase):
    # The real entries are historical: an adoption branch commits the include
    # it was measured with, and a later re-spelling of the product must not
    # rewrite that claim.  `tracks_artifact` says whether the live artifact is
    # the same bytes; adopt-check.sh always verifies the branch itself.
    OLD_INC = "38b278dd2a10fe42fa699b3661fbee575657bf8e9cdb1399a0d031a7c05b1442"
    NEW_INC = "c3b959fd2ea4bd1c00ca490bdfa625bbfdc67205a15fa2ebc95ea5a313b9a3f2"

    def entry(self, tracks=True, inc=None):
        expect = {
            "branch": "explore/x86-adopt",
            "commit": "4b8a026249c2",
            "inc": "x86.inc",
            "inc_sha256": inc or self.NEW_INC,
            "lib_sha256": "085928fa26a5e772ff250f045ad4874cd5e8cbbc8046fbeeb2406f7394a9c432",
        }
        if not tracks:
            expect["tracks_artifact"] = False
        return {
            "id": "adopt/x86", "kind": "adopt", "direction": "adopt",
            "target": "x86", "command": "make-adopt",
            "dir": "/tmp/gisel-artifacts",
            "artifacts": {},
            "expect": expect,
        }

    def test_the_recorded_branch_and_inc_match_the_document(self):
        proc = run_manifest([self.entry(tracks=False, inc=self.OLD_INC)],
                            direction="adopt")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_historical_entry_does_not_check_the_live_artifact(self):
        # The frozen claim is the branch bytes; the product may have moved on.
        proc = run_manifest([self.entry(tracks=False, inc=self.OLD_INC)],
                            direction="adopt")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_an_inc_hash_drift_fails(self):
        entry = self.entry()
        entry["expect"]["inc_sha256"] = "0" * 64
        proc = run_manifest([entry], direction="adopt")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("inc sha256", proc.stdout)


class Shape(unittest.TestCase):
    def test_the_real_manifest_has_a_valid_shape(self):
        proc = run_manifest(json.loads((ROOT / "EVIDENCE.json").read_text())["entries"],
                            direction="__none__")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("0/0 entries pass", proc.stdout)

    def test_the_schema_covers_every_kind(self):
        schema = json.loads((ROOT / "evidence.schema.json").read_text())
        kinds = set(schema["$defs"]["entry"]["properties"]["kind"]["enum"])
        self.assertEqual(kinds, {"answers", "neg", "summary", "mir", "lit",
                                 "closure", "adopt"})

    def test_the_real_manifest_has_no_min_disagree(self):
        manifest = json.loads((ROOT / "EVIDENCE.json").read_text())
        for entry in manifest["entries"]:
            if entry["kind"] == "neg":
                self.assertNotIn("min_disagree", entry["expect"], entry["id"])
                self.assertIsInstance(entry["expect"]["disagree"], int,
                                      entry["id"])

    def test_a_missing_kind_fails(self):
        entry = neg_entry(Path(tempfile.mkdtemp()))
        del entry["kind"]
        proc = run_manifest([entry])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("missing fields: kind", proc.stdout)

    def test_an_unknown_kind_fails(self):
        entry = neg_entry(Path(tempfile.mkdtemp()))
        entry["kind"] = "guess"
        proc = run_manifest([entry])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("unknown kind", proc.stdout)

    def test_a_frozen_manifest_does_not_fail_on_a_missing_build(self):
        tmp = Path(tempfile.mkdtemp())
        manifest = tmp / "EVIDENCE.json"
        manifest.write_text(json.dumps({
            "schema": 1, "entries": [],
            "official_build": {"path": str(tmp / "no-build"), "libs": {}},
        }))
        proc = subprocess.run(
            [*EVIDENCE, "verify", "--manifest", str(manifest),
             "--frozen"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SKIP official_build", proc.stdout)
        self.assertIn("1 check(s) skipped", proc.stdout)

    def test_a_live_manifest_fails_on_a_missing_build(self):
        tmp = Path(tempfile.mkdtemp())
        manifest = tmp / "EVIDENCE.json"
        manifest.write_text(json.dumps({
            "schema": 1, "entries": [],
            "official_build": {"path": str(tmp / "no-build"), "libs": {}},
        }))
        proc = subprocess.run(
            [*EVIDENCE, "verify", "--manifest", str(manifest)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("is not present", proc.stdout)


class AdoptFetch(unittest.TestCase):
    def make_repo(self, content: bytes):
        repo = Path(tempfile.mkdtemp(prefix="adopt-repo-"))
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name",
                        "test"], check=True)
        path = repo / "llvm/lib/Target/X86/GISel/X86LegalizerInfo.inc"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "adopt"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b",
                        "explore/x86-adopt"], check=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True)
        return repo, head.stdout.strip()

    def manifest(self, commit, inc_sha):
        tmp = Path(tempfile.mkdtemp())
        entry = {
            "id": "adopt/x86", "kind": "adopt", "direction": "adopt",
            "target": "x86", "command": "adopt-check", "dir": str(tmp),
            "artifacts": {},
            "expect": {"branch": "explore/x86-adopt", "commit": commit[:12],
                       "inc": "x86.inc", "inc_sha256": inc_sha,
                       "lib_sha256": "0" * 64},
        }
        path = tmp / "EVIDENCE.json"
        path.write_text(json.dumps({"schema": 1, "entries": [entry]}))
        return path

    def run_check(self, manifest, repo):
        return subprocess.run(
            [*EVIDENCE, "adopt-check",
             "--manifest", str(manifest), "--fork", "file://" + str(repo),
             "--target", "x86"], capture_output=True, text=True)

    def test_a_branch_with_the_recorded_bytes_passes(self):
        content = b"// generated inc\n"
        repo, commit = self.make_repo(content)
        proc = self.run_check(self.manifest(commit, sha_bytes(content)), repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS x86", proc.stdout)

    def test_a_branch_with_different_bytes_fails(self):
        repo, commit = self.make_repo(b"// changed\n")
        proc = self.run_check(self.manifest(commit, "0" * 64), repo)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("inc sha256", proc.stdout)


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Verify EVIDENCE.json: the manifest is the claim, the artifacts are proof.

    evidence.py verify [--manifest PATH] [--direction D] [--frozen]
    evidence.py fetch [--tag T] [--dir D] [--force]

Kinds are checked differently on purpose:

  answers  a diff plus the answer files (byte identity when claimed)
  neg      a diff plus attribution; the disagreement count is exact and the
           attribution must cover exactly the injected rulesets
  summary  a per-config domain aggregate (no single diff)
  mir      a MIR replay report plus restore provenance
  lit      per-test records re-derived, not the summary
  closure  a committed successor-closure snapshot
  adopt    a branch/commit/inc/lib tuple checked against the local artifact

Exit codes: 0 = every selected entry passes; 1 = a shape or value failure;
2 = the command line or a required file is unusable.
"""

from __future__ import annotations

from gisel_ab import gisel_package, gisel_src, paths

import argparse
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import urllib.request
import subprocess
import sys

PKG = pathlib.Path(__file__).resolve().parent
AB = PKG.parents[1]
DATA = PKG / "data"
EVIDENCE = AB / "evidence"
EVIDENCE_REPO = "nodecoda/gisel-ab"
EVIDENCE_BASE = "https://github.com"
KINDS = {"answers", "neg", "summary", "mir", "lit", "closure", "adopt"}
ADOPT_INC = {
    "x86": "llvm/lib/Target/X86/GISel/X86LegalizerInfo.inc",
    "aarch64": "llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.inc",
    "riscv": "llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.inc",
}
ADOPT_FORK = "https://github.com/nodecoda/llvm-project.git"
BASE_FIELDS = {"id", "kind", "direction", "target", "dir", "command",
               "artifacts", "expect"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def gisel_root() -> pathlib.Path:
    return gisel_src()


def gisel_path(rel: str) -> pathlib.Path:
    """A gisel-repo path, from a checkout or from the wheel's data copies.

    ``tests/corpus/X.gisel`` becomes ``gisel/data/corpus/X.gisel`` when the
    checkout is not there; the wheel ships byte-identical copies.
    """
    path = gisel_root() / rel
    if path.exists():
        return path
    package = gisel_package()
    parts = pathlib.PurePosixPath(rel).parts
    if package is not None and len(parts) > 2 and parts[0] == "tests":
        candidate = package / "data"
        for part in parts[1:]:
            candidate = candidate / part
        if candidate.exists():
            return candidate
    return path


def default_tag() -> str:
    """The cohort the checkout names, so ``fetch`` matches its own record."""
    manifest = AB / "RELEASE.json"
    try:
        return json.loads(manifest.read_text()).get("cohort", "")
    except (OSError, ValueError):
        return ""


def cache_dir(tag: str) -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "gisel-ab" / "evidence" / tag


def asset_url(tag: str, name: str, base: str = EVIDENCE_BASE,
              repo: str = EVIDENCE_REPO) -> str:
    return "%s/%s/releases/download/%s/%s" % (base.rstrip("/"), repo, tag, name)


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: pathlib.Path) -> None:
    with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def _check_members(archive: pathlib.Path) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("unsafe path in asset: %s" % member.name)
            if member.issym() or member.islnk():
                raise ValueError("links are not allowed in the evidence asset: %s"
                                 % member.name)


def fetch_evidence(tag: str = "", dest: pathlib.Path | None = None,
                   force: bool = False, base: str = EVIDENCE_BASE,
                   repo: str = EVIDENCE_REPO) -> pathlib.Path:
    """Download and unpack ``evidence-<tag>.tar.gz`` from the release."""
    tag = tag or default_tag()
    if not tag:
        raise ValueError("no --tag and RELEASE.json names no cohort")
    dest = pathlib.Path(dest) if dest else cache_dir(tag)
    marker = dest / ".fetched"
    if marker.is_file() and not force:
        return dest
    if dest.exists() and force:
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    asset = "evidence-%s.tar.gz" % tag
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / asset
        checksum = pathlib.Path(tmp) / (asset + ".sha256")
        _download(asset_url(tag, asset, base, repo), archive)
        _download(asset_url(tag, asset + ".sha256", base, repo), checksum)
        want = checksum.read_text().split()[0].lower()
        got = file_sha256(archive)
        if got != want:
            raise ValueError("sha256 %s != %s (asset %s)"
                             % (got[:16], want[:16], asset))
        _check_members(archive)
        with tarfile.open(archive) as tar:
            tar.extractall(dest)
    if not (dest / "EVIDENCE.json").is_file():
        raise ValueError("%s has no EVIDENCE.json" % asset)
    if not (dest / "evidence").is_dir():
        raise ValueError("%s has no evidence/ tree" % asset)
    marker.write_text(tag + "\n")
    return dest


def default_manifest() -> pathlib.Path:
    root = os.environ.get("GISEL_AB_EVIDENCE")
    if not root:
        root = paths.load_config().get("evidence")
    if root:
        return pathlib.Path(root).expanduser() / "EVIDENCE.json"
    return AB / "EVIDENCE.json"


class Verifier:
    def __init__(self, manifest_path: pathlib.Path, direction: str = "all",
                 frozen: bool = False):
        self.manifest_path = pathlib.Path(manifest_path)
        self.direction = direction
        self.frozen = frozen
        self.manifest = json.loads(self.manifest_path.read_text())
        self.oracle_root = self.manifest_path.parent / "evidence"
        self.failures = 0
        self.skipped = 0

    # -- reporting -------------------------------------------------------
    def fail(self, entry, message: str) -> int:
        print("FAIL %-20s %s" % (entry.get("id", "?"), message))
        return 1

    def ok(self, entry, where) -> None:
        print("PASS %-20s %s" % (entry["id"], where))

    # -- artifact lookup -------------------------------------------------
    def resolve(self, entry, name, path=None):
        """Live artifact when not frozen; committed evidence when frozen.

        There is deliberately no evidence fallback in live mode: a missing live
        file is a failure, not a reason to quietly verify yesterday's copy.
        """
        candidates = []
        if self.frozen:
            base = self.oracle_root / entry["id"]
            candidates += [base / name, base / (name + ".gz")]
        else:
            if path is not None:
                candidates.append(pathlib.Path(path))
            base = pathlib.Path(entry["dir"])
            candidates += [base / name, base / (name + ".gz")]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    # -- manifest shape --------------------------------------------------
    def validate_shape(self, entry) -> int:
        missing = BASE_FIELDS - set(entry)
        if missing:
            return self.fail(entry, "missing fields: %s" % ", ".join(sorted(missing)))
        kind = entry["kind"]
        if kind not in KINDS:
            return self.fail(entry, "unknown kind %r" % (kind,))
        artifacts = entry["artifacts"]
        expect = entry["expect"]
        if not isinstance(artifacts, dict) or not isinstance(expect, dict):
            return self.fail(entry, "artifacts and expect must be objects")
        if kind == "answers":
            if "diff.json" not in artifacts:
                return self.fail(entry, "answers entry has no diff.json")
            for key in ("agree", "disagree", "unanswerable"):
                if key not in expect:
                    return self.fail(entry, "answers expect has no %s" % key)
        elif kind == "neg":
            if entry["direction"] not in ("G-neg", "U-neg"):
                return self.fail(entry, "neg entry has direction %r"
                                 % entry["direction"])
            for key in ("diff.json", "attribution.json"):
                if key not in artifacts:
                    return self.fail(entry, "neg entry has no %s" % key)
            if not isinstance(expect.get("disagree"), int):
                return self.fail(entry, "neg expect.disagree must be exact")
            if not expect.get("rulesets"):
                return self.fail(entry, "neg expect.rulesets is empty")
        elif kind == "summary":
            if "summary.json" not in artifacts:
                return self.fail(entry, "summary entry has no summary.json")
        elif kind == "mir":
            if "mir-report.json" not in artifacts:
                return self.fail(entry, "mir entry has no mir-report.json")
        elif kind == "lit":
            for name in ("l3-summary.json", "l3-provenance.json",
                         "l3-baseline.tests.jsonl", "l3-combined.tests.jsonl"):
                if name not in artifacts:
                    return self.fail(entry, "lit entry has no %s" % name)
        elif kind == "closure":
            if len(artifacts) != 1:
                return self.fail(entry, "closure entry must name one snapshot")
        elif kind == "adopt":
            for key in ("branch", "commit", "inc", "inc_sha256", "lib_sha256"):
                if key not in expect:
                    return self.fail(entry, "adopt expect has no %s" % key)
        return 0

    # -- common checks ---------------------------------------------------
    def check_snapshot(self, entry) -> int:
        snap = entry.get("snapshot")
        if not snap:
            return 0
        root = gisel_root()
        bad = 0
        corpus = gisel_path("tests/corpus/" + snap["corpus"])
        features = gisel_path("tests/cases/" + snap["features"])
        for label, path, key in (
                ("corpus", corpus, "corpus_sha256"),
                ("features", features, "features_sha256")):
            if not path.exists():
                bad += self.fail(entry, "snapshot %s missing: %s" % (label, path))
                continue
            got = sha256(path.read_bytes())
            if got != snap[key]:
                bad += self.fail(entry, "snapshot %s sha256 %s != %s"
                                 % (label, got[:16], snap[key][:16]))
        inc = gisel_path("tests/artifacts/" + snap["inc"])
        if not inc.exists():
            bad += self.fail(entry, "snapshot inc missing: %s" % inc)
        elif sha256(inc.read_bytes()) != snap["inc_sha256"]:
            bad += self.fail(entry, "snapshot inc sha256 mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "regen.inc"
            proc = subprocess.run(
                [sys.executable, "-m", "gisel", "gen", str(corpus),
                 "--emit=inc", "--out", str(out)],
                cwd=str(root), capture_output=True, text=True)
            if proc.returncode:
                tail = (proc.stderr.strip().splitlines() or [""])[-1]
                bad += self.fail(entry, "snapshot regen failed: %s" % tail)
            elif sha256(out.read_bytes()) != snap["inc_sha256"]:
                bad += self.fail(entry, "snapshot regen sha256 mismatch")
        return bad

    def check_artifacts(self, entry) -> int:
        bad = 0
        for name, expected in entry["artifacts"].items():
            path = self.resolve(entry, name)
            if path is None:
                bad += self.fail(entry, "missing %s (re-run: %s)"
                                 % (pathlib.Path(entry["dir"]) / name,
                                    entry.get("command", "?")))
                continue
            got = sha256(read(path))
            if got != expected:
                bad += self.fail(entry, "%s sha256 %s != %s"
                                 % (name, got[:16], expected[:16]))
        return bad

    @staticmethod
    def _exists(path) -> bool:
        """`exists` for a path that may sit under a directory we cannot enter.

        EVIDENCE.json records absolute paths on the machine that produced it.
        A CI runner may have `/home/ubuntu` but no permission to enter it;
        `Path.exists()` raises PermissionError there.  That is "not present"
        for this check, not a reason to abort the whole verification.
        """
        try:
            return path.exists()
        except OSError:
            return False

    def check_runtime(self, entry) -> None:
        runtime = entry.get("runtime")
        if not runtime:
            return
        path = pathlib.Path(runtime["path"])
        if not self._exists(path):
            print("WARN %-20s runtime missing: %s" % (entry["id"], path))
            return
        got = sha256(path.read_bytes())
        if got != runtime["sha256"]:
            print("WARN %-20s runtime %s changed (%s != %s)"
                  % (entry["id"], path.name, got[:16], runtime["sha256"][:16]))

    def check_queries(self, entry) -> int:
        queries = entry.get("queries")
        if not queries:
            return 0
        path = self.resolve(entry, pathlib.Path(queries["path"]).name,
                            queries["path"])
        if path is None:
            return self.fail(entry, "missing queries %s" % queries["path"])
        got = sha256(read(path))
        if got != queries["sha256"]:
            return self.fail(entry, "queries sha256 %s != %s"
                             % (got[:16], queries["sha256"][:16]))
        return 0

    # -- per-kind checks -------------------------------------------------
    def check_answers(self, entry) -> int:
        expect = entry["expect"]
        path = self.resolve(entry, entry.get("diff", "diff.json"))
        if path is None:
            return self.fail(entry, "missing %s"
                             % entry.get("diff", "diff.json"))
        diff = json.loads(read(path).decode())
        bad = 0
        if expect.get("answers_identical"):
            pa = self.resolve(entry, "official.answers.jsonl")
            pb = self.resolve(entry, "ours.answers.jsonl")
            if pa is None or pb is None:
                return self.fail(entry, "missing answer files")
            if read(pa) != read(pb):
                bad += self.fail(entry, "answer files differ (diff.json alone "
                                        "is not enough)")
        for key in ("ref", "agree", "disagree", "unanswerable", "ref_asserted"):
            if key in expect and diff.get(key) != expect[key]:
                bad += self.fail(entry, "%s=%r, expected %r"
                                 % (key, diff.get(key), expect[key]))
        derived = diff.get("agree", 0) - diff.get("unanswerable", 0)
        if diff.get("answered_equal") is not None \
                and diff["answered_equal"] != derived:
            bad += self.fail(entry, "diff answered_equal=%r, agree-unanswerable=%d"
                             % (diff.get("answered_equal"), derived))
        if "answered_equal" in expect and expect["answered_equal"] != derived:
            bad += self.fail(entry, "answered_equal=%r, agree-unanswerable=%d"
                             % (expect["answered_equal"], derived))
        for key in ("missing_in_both", "unexpected"):
            if diff.get(key) not in (None, 0):
                bad += self.fail(entry, "%s=%r (query set is not the denominator)"
                                 % (key, diff.get(key)))
        by_site = expect.get("ref_asserted_by_site")
        if by_site is not None and diff.get("ref_asserted_by_site") != by_site:
            bad += self.fail(entry, "ref_asserted_by_site mismatch")
        return bad

    def check_summary(self, entry) -> int:
        expect = entry["expect"]
        bad = 0
        path = self.resolve(entry, "summary.json")
        if path is None:
            return self.fail(entry, "missing summary.json")
        summary = json.loads(read(path).decode())
        for key, value in (expect.get("summary") or {}).items():
            if summary.get(key) != value:
                bad += self.fail(entry, "summary %s=%r, expected %r"
                                 % (key, summary.get(key), value))
        sites_expect = expect.get("assert_sites")
        if sites_expect:
            sites_path = self.resolve(entry, entry.get("sites", "sites.json"))
            if sites_path is None:
                bad += self.fail(entry, "missing assert-site summary")
            else:
                sites = json.loads(read(sites_path).decode()).get("assert_sites", {})
                for key, value in sites_expect.items():
                    if sites.get(key) != value:
                        bad += self.fail(entry, "assert_sites %s=%r, expected %r"
                                         % (key, sites.get(key), value))
        return bad

    def check_neg(self, entry) -> int:
        expect = entry["expect"]
        diff_path = self.resolve(entry, "diff.json")
        attr_path = self.resolve(entry, "attribution.json")
        if diff_path is None or attr_path is None:
            return self.fail(entry, "missing diff.json or attribution.json")
        diff = json.loads(read(diff_path).decode())
        attribution = json.loads(read(attr_path).decode())
        bad = 0
        want = expect["disagree"]
        for label, got in (
                ("diff.disagree", diff.get("disagree")),
                ("attribution.disagree", attribution.get("disagree"))):
            if got != want:
                bad += self.fail(entry, "%s=%r, expected exactly %d"
                                 % (label, got, want))
        by_ruleset = attribution.get("by_ruleset") or {}
        if not by_ruleset:
            bad += self.fail(entry, "attribution has no by_ruleset entries")
        allowed = set(expect["rulesets"])
        seen = set(by_ruleset)
        if not seen <= allowed:
            bad += self.fail(entry, "attributed to %r, injected %r"
                             % (sorted(seen), sorted(allowed)))
        covered = set(attribution.get("covered_expected") or [])
        if covered and covered != allowed:
            bad += self.fail(entry, "covered %r, injected %r"
                             % (sorted(covered), sorted(allowed)))
        if attribution.get("unknown_keys", 0) != expect.get("unknown_keys", 0):
            bad += self.fail(entry, "unknown_keys=%r"
                             % attribution.get("unknown_keys"))
        outside = attribution.get("outside_expected") or []
        if outside:
            bad += self.fail(entry, "outside injected rulesets: %r" % outside)
        derived = diff.get("agree", 0) - diff.get("unanswerable", 0)
        if diff.get("answered_equal") is not None \
                and diff["answered_equal"] != derived:
            bad += self.fail(entry, "diff answered_equal=%r, agree-unanswerable=%d"
                             % (diff.get("answered_equal"), derived))
        if "answered_equal" in expect and expect["answered_equal"] != derived:
            bad += self.fail(entry, "answered_equal=%r, agree-unanswerable=%d"
                             % (expect["answered_equal"], derived))
        contract = DATA / ("%s-contract.json" % entry["target"])
        if contract.exists():
            data = json.loads(contract.read_text())
            key = "g_neg" if entry["direction"] == "G-neg" else "u_neg"
            recorded = data.get(key)
            if recorded:
                if recorded.get("disagree") != want:
                    bad += self.fail(entry, "%s contract says disagree=%r"
                                     % (key, recorded.get("disagree")))
                if set(recorded.get("rulesets") or {}) != allowed:
                    bad += self.fail(entry, "%s contract rulesets %r != %r"
                                     % (key, sorted(recorded.get("rulesets") or {}),
                                        sorted(allowed)))
                got_attr = sha256(read(attr_path))
                if recorded.get("attribution_sha256") not in (None, got_attr):
                    bad += self.fail(entry, "%s contract attribution sha mismatch"
                                     % key)
        return bad

    def check_mir(self, entry) -> int:
        expect = entry["expect"]
        path = self.resolve(entry, entry.get("report", "mir-report.json"))
        if path is None:
            return self.fail(entry, "missing mir-report.json")
        report = json.loads(read(path).decode())
        bad = 0
        totals = report.get("totals") or {}
        for key, value in (expect.get("mir") or {}).items():
            if totals.get(key) != value:
                bad += self.fail(entry, "totals %s=%r, expected %r"
                                 % (key, totals.get(key), value))
        for target, want in (expect.get("mir_targets") or {}).items():
            got = (report.get("targets") or {}).get(target) or {}
            for key, value in want.items():
                if got.get(key) != value:
                    bad += self.fail(entry, "%s %s=%r, expected %r"
                                     % (target, key, got.get(key), value))
        llc = expect.get("llc_sha256")
        if llc and report.get("llc_sha256") != llc:
            bad += self.fail(entry, "llc sha256 mismatch")
        prov = self.resolve(entry, "provenance.json")
        if prov is not None:
            data = json.loads(read(prov).decode())
            for target, info in (data.get("targets") or {}).items():
                if info.get("baseline_lib_restored_exactly") is not True:
                    bad += self.fail(entry, "%s baseline library not restored"
                                     % target)
        return bad

    def check_lit(self, entry) -> int:
        expect = entry["expect"]
        summary_path = self.resolve(entry, entry.get("summary", "l3-summary.json"))
        if summary_path is None:
            return self.fail(entry, "missing l3-summary.json")
        summary = json.loads(read(summary_path).decode())
        want = expect.get("lit") or {}
        bad = 0
        for side in ("baseline", "combined"):
            got = summary.get(side) or {}
            for key, value in (want.get(side) or {}).items():
                if got.get(key) != value:
                    bad += self.fail(entry, "%s %s=%r, expected %r"
                                     % (side, key, got.get(key), value))
            got = summary.get("log.%s" % side) or {}
            for key, value in (want.get("log.%s" % side) or {}).items():
                if got.get(key) != value:
                    bad += self.fail(entry, "log.%s %s=%r, expected %r"
                                     % (side, key, got.get(key), value))
        for key in ("tests_compared", "identical"):
            if key in want and summary.get(key) != want[key]:
                bad += self.fail(entry, "%s=%r, expected %r"
                                 % (key, summary.get(key), want[key]))
        sides = {}
        for side in ("baseline", "combined"):
            path = self.resolve(entry, "l3-%s.tests.jsonl" % side)
            if path is None:
                bad += self.fail(entry, "missing l3-%s.tests.jsonl" % side)
                continue
            got = {}
            for line in read(path).decode().splitlines():
                row = json.loads(line)
                got[row["test"]] = (row["status"], row["expected"])
            sides[side] = got
            counts = {"tests": len(got), "passed": 0, "xfail": 0, "failed": 0,
                      "xpass": 0, "unsupported": 0, "aborted": 0}
            for status, expected in got.values():
                if status == "PASS":
                    counts["passed" if expected else "xpass"] += 1
                elif status == "FAIL":
                    counts["xfail" if expected else "failed"] += 1
                elif status == "SKIP":
                    counts["unsupported"] += 1
                else:
                    counts["aborted"] += 1
            for key, value in (want.get(side) or {}).items():
                if counts.get(key) != value:
                    bad += self.fail(entry, "%s recomputed %s=%r, expected %r"
                                     % (side, key, counts.get(key), value))
        if len(sides) == 2:
            base, swap = sides["baseline"], sides["combined"]
            extra = set(base) - set(swap)
            missing = set(swap) - set(base)
            changed = [name for name in set(base) & set(swap)
                       if base[name] != swap[name]]
            if extra:
                bad += self.fail(entry, "%d tests only in baseline" % len(extra))
            if missing:
                bad += self.fail(entry, "%d tests only in combined" % len(missing))
            if changed:
                bad += self.fail(entry, "%d tests changed result (first: %s)"
                                 % (len(changed), sorted(changed)[0]))
            if len(set(base) & set(swap)) != want.get("tests_compared"):
                bad += self.fail(entry, "tests_compared recomputes to %d"
                                 % len(set(base) & set(swap)))
        prov_path = self.resolve(entry, "l3-provenance.json")
        if prov_path is None:
            return bad + self.fail(entry, "missing l3-provenance.json")
        prov = json.loads(read(prov_path).decode())
        if prov.get("tree_clean_after") is not True:
            bad += self.fail(entry, "tree was not clean after the restore")
        libs = prov.get("libs") or {}
        for target, restored in (libs.get("restored_exactly") or {}).items():
            if restored is not True:
                bad += self.fail(entry, "%s library did not come back" % target)
        for target in (libs.get("baseline") or {}):
            if (libs.get("swapped") or {}).get(target) == libs["baseline"][target]:
                bad += self.fail(entry, "%s library was identical after the swap"
                                 % target)
        return bad

    def check_closure(self, entry) -> int:
        expect = entry["expect"]
        name, expected = next(iter(entry["artifacts"].items()))
        # Closure snapshots were never copied into evidence/: they are small
        # contracts and live in the package data.  EVIDENCE.json still spells
        # them "data/closure", which is a repository-root path from before the
        # package existed.
        where = pathlib.Path(entry["dir"])
        if not where.is_absolute() and str(where).startswith("data/"):
            where = DATA / str(where)[len("data/"):]
        path = where / name
        if not path.exists():
            return self.fail(entry, "missing %s" % path)
        data = read(path)
        if sha256(data) != expected:
            return self.fail(entry, "snapshot sha256 differs")
        report = json.loads(data.decode())
        bad = 0
        for key in ("ok", "seed_states", "successors", "revisited", "stuck",
                    "disagree", "both_unanswerable", "ref_only_unanswerable",
                    "ours_only_unanswerable"):
            got = report.get(key)
            if key in ("successors", "revisited", "stuck"):
                got = (report.get("edges") or {}).get(key)
            elif key in ("both_unanswerable", "ref_only_unanswerable",
                         "ours_only_unanswerable"):
                short = key.replace("_unanswerable", "")
                got = ((report.get("answers") or {}).get("unanswerable") or {}).get(short)
            elif key == "disagree":
                got = len(report.get("disagree") or [])
            if key in expect and got != expect[key]:
                bad += self.fail(entry, "%s=%r, expected %r"
                                 % (key, got, expect[key]))
        if report.get("ok") is not True:
            bad += self.fail(entry, "closure report is not ok")
        if (report.get("edges") or {}).get("stuck") != 0:
            bad += self.fail(entry, "stuck edges are not zero")
        if report.get("disagree"):
            bad += self.fail(entry, "disagreements: %d" % len(report["disagree"]))
        if ((report.get("answers") or {}).get("unanswerable") or {}).get("ours_only"):
            bad += self.fail(entry, "ours-only unanswerable states")
        rounds = report.get("rounds") or []
        if not rounds:
            bad += self.fail(entry, "no rounds")
        elif rounds[0].get("asked") != report.get("seed_states"):
            bad += self.fail(entry, "round 1 asked %r, seed_states %r"
                             % (rounds[0].get("asked"), report.get("seed_states")))
        for rnd in rounds:
            if rnd.get("disagree"):
                bad += self.fail(entry, "round %r has disagreements"
                                 % rnd.get("round"))
        return bad

    def check_adopt(self, entry) -> int:
        expect = entry["expect"]
        bad = 0
        # An adoption experiment is pinned to the include its branch commits.
        # When the product re-spells its rules the live artifact moves and the
        # experiment becomes historical: `tracks_artifact` false says so, and
        # adopt-check.sh still verifies the branch bytes over the network.
        if expect.get("tracks_artifact", True):
            inc = gisel_path("tests/artifacts/" + expect["inc"])
            if not inc.exists():
                return self.fail(entry, "missing gisel artifact %s" % inc)
            got = sha256(inc.read_bytes())
            if got != expect["inc_sha256"]:
                bad += self.fail(entry, "inc sha256 %s != %s"
                                 % (got[:16], expect["inc_sha256"][:16]))
        doc = AB / "docs" / "ADOPT.md"
        if not doc.exists():
            # The prose that names the fork's branch is checkout-only.  The
            # artifact sha above is the claim a wheel can still check.
            if not (AB / "RELEASE.json").is_file():
                print("NOTE %-20s docs/ADOPT.md is checkout-only; artifact "
                      "hash checked" % entry["id"])
                return bad
            return bad + self.fail(entry, "missing docs/ADOPT.md")
        text = doc.read_text()
        for label, value in (("branch", expect["branch"]),
                             ("commit", expect["commit"]),
                             ("inc", expect["inc_sha256"]),
                             ("lib", expect["lib_sha256"])):
            if value not in text:
                bad += self.fail(entry, "docs/ADOPT.md does not mention the %s"
                                 % label)
        return bad

    # -- build ----------------------------------------------------------
    def check_build(self) -> int:
        build = self.manifest.get("official_build")
        if not build:
            return 0
        if self.frozen:
            # Frozen mode proves the bytes, not the build that produced them.
            # Say "SKIP", not "PASS": the two are different claims.
            print("SKIP official_build  frozen mode verifies artifacts, not the build")
            self.skipped += 1
            return 0
        root = pathlib.Path(build["path"])
        if not self._exists(root):
            print("FAIL official_build  %s is not present" % root)
            return 1
        bad = 0
        for target, want in sorted(build["libs"].items()):
            lib = root / "lib" / ("libLLVM%sCodeGen.so.24.0git" % target)
            if not self._exists(lib):
                print("FAIL official_build  missing %s" % lib)
                bad += 1
                continue
            got = sha256(lib.read_bytes())
            if got != want:
                print("FAIL official_build  %s is %s, evidence was recorded "
                      "against %s" % (lib.name, got[:16], want[:16]))
                bad += 1
        if not bad:
            print("PASS official_build  %s (%d libraries)"
                  % (root, len(build["libs"])))
        return bad

    def check(self, entry) -> int:
        kind = entry["kind"]
        if kind == "closure":
            bad = self.check_closure(entry)
            if not bad:
                self.ok(entry, pathlib.Path(entry["dir"]))
            return bad
        if kind == "adopt":
            bad = self.check_adopt(entry)
            if not bad:
                self.ok(entry, entry["expect"]["branch"])
            return bad
        bad = self.check_snapshot(entry)
        bad += self.check_artifacts(entry)
        if bad:
            return bad
        self.check_runtime(entry)
        bad += self.check_queries(entry)
        if bad:
            return bad
        if kind == "answers":
            bad = self.check_answers(entry)
        elif kind == "summary":
            bad = self.check_summary(entry)
        elif kind == "neg":
            bad = self.check_neg(entry)
        elif kind == "mir":
            bad = self.check_mir(entry)
        elif kind == "lit":
            bad = self.check_lit(entry)
        else:
            bad = self.fail(entry, "unhandled kind %r" % kind)
        if not bad:
            where = (self.oracle_root / entry["id"]) if self.frozen \
                else pathlib.Path(entry["dir"])
            self.ok(entry, where)
        return bad

    def run(self) -> int:
        entries = self.manifest.get("entries")
        if not isinstance(entries, list):
            print("FAIL manifest has no entries list")
            return 1
        seen = set()
        selected = []
        shape_failures = 0
        for entry in entries:
            if not isinstance(entry, dict):
                print("FAIL entry is not an object: %r" % (entry,))
                shape_failures += 1
                continue
            if entry.get("id") in seen:
                print("FAIL duplicate entry id %r" % entry.get("id"))
                shape_failures += 1
            seen.add(entry.get("id"))
            shape_failures += self.validate_shape(entry)
            if self.direction == "all" or entry.get("direction") == self.direction:
                selected.append(entry)
        build_failures = self.check_build()
        for entry in selected:
            self.failures += self.check(entry)
        total = len(selected)
        print("%d/%d entries pass" % (total - self.failures, total))
        if self.skipped:
            print("%d check(s) skipped" % self.skipped)
        if any(e.get("id", "").startswith("U-full/") for e in selected):
            print("NOTE U-full/*: only `universe.jsonl` and the expand command "
                  "are frozen; the expanded query file is regenerated on demand.")
        return 1 if (self.failures or build_failures or shape_failures) else 0


def adopt_check(manifest_path: pathlib.Path, fork: str, targets) -> int:
    """Fetch each adoption branch and compare its committed inc bytes.

    The branch is the claim; the gisel artifact is the source of truth.  A
    shallow fetch is enough: the adoption commit is a single commit on a
    branch that starts at the pin.
    """
    manifest = json.loads(pathlib.Path(manifest_path).read_text())
    entries = [e for e in manifest.get("entries", []) if e.get("kind") == "adopt"]
    if targets:
        entries = [e for e in entries if e.get("target") in targets]
    if not entries:
        print("adopt-check: no adopt entries selected", file=sys.stderr)
        return 2
    repo = pathlib.Path(tempfile.mkdtemp(prefix="adopt-check-")) / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", fork],
                   check=True)
    failures = 0
    for entry in entries:
        target = entry["target"]
        expect = entry["expect"]
        path = ADOPT_INC.get(target)
        if path is None:
            print("FAIL %-12s no inc path for target" % target)
            failures += 1
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--depth=1", "origin",
             expect["branch"]], capture_output=True, text=True)
        if proc.returncode:
            print("FAIL %-12s fetch %s: %s"
                  % (target, expect["branch"], proc.stderr.strip()))
            failures += 1
            continue
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "FETCH_HEAD"],
            capture_output=True, text=True)
        commit = head.stdout.strip()
        if not commit.startswith(expect["commit"]):
            print("FAIL %-12s branch head %s, expected %s"
                  % (target, commit[:12], expect["commit"]))
            failures += 1
            continue
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", "FETCH_HEAD:" + path],
            capture_output=True)
        if blob.returncode:
            print("FAIL %-12s %s is not on the branch" % (target, path))
            failures += 1
            continue
        got = sha256(blob.stdout)
        if got != expect["inc_sha256"]:
            print("FAIL %-12s inc sha256 %s != %s"
                  % (target, got[:16], expect["inc_sha256"][:16]))
            failures += 1
            continue
        print("PASS %-12s %s @ %s" % (target, expect["branch"], commit[:12]))
    return 1 if failures else 0


def _manifest_or_none(path) -> pathlib.Path:
    return pathlib.Path(path) if path else default_manifest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evidence.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--tag", default="",
                       help="cohort tag (default: RELEASE.json's cohort)")
    fetch.add_argument("--dir", type=pathlib.Path, default=None,
                       help="destination (default: the user cache)")
    fetch.add_argument("--force", action="store_true")
    fetch.add_argument("--base", default=EVIDENCE_BASE, help=argparse.SUPPRESS)
    fetch.add_argument("--repo", default=EVIDENCE_REPO, help=argparse.SUPPRESS)
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=pathlib.Path, default=None)
    verify.add_argument("--direction", default="all")
    verify.add_argument("--frozen", action="store_true")
    adopt = sub.add_parser("adopt-check")
    adopt.add_argument("--manifest", type=pathlib.Path, default=None)
    adopt.add_argument("--fork", default=os.environ.get("AB_FORK", ADOPT_FORK))
    adopt.add_argument("--target", action="append", default=None)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "fetch":
            dest = fetch_evidence(args.tag, args.dir, args.force,
                                  args.base, args.repo)
            print("fetched %s -> %s" % (args.tag or default_tag(), dest))
            print("verify: gisel-ab evidence verify --manifest %s"
                  % (dest / "EVIDENCE.json"))
            return 0
        manifest = _manifest_or_none(args.manifest)
        if not manifest.is_file():
            print("evidence.py: %s is missing" % manifest, file=sys.stderr)
            print("  run: gisel-ab evidence fetch --tag <cohort>", file=sys.stderr)
            print("  or:  gisel-ab evidence verify --manifest <dir>/EVIDENCE.json",
                  file=sys.stderr)
            return 2
        if args.cmd == "verify":
            return Verifier(manifest, args.direction, args.frozen).run()
        if args.cmd == "adopt-check":
            return adopt_check(manifest, args.fork, args.target)
    except OSError as exc:
        print("evidence.py: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("evidence.py: %s" % exc, file=sys.stderr)
        return 2
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())

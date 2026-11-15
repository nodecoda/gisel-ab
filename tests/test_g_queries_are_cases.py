"""The frozen G queries are `gisel cases` at the recorded commit.

`evidence/G/<target>/queries.jsonl.gz` is not an arbitrary input: it is the
rule-aware query set that gisel emits for that corpus.  The release manifest
already records the cases golden it was taken from, so the two can be compared
without running gisel: a corpus or generator change that moves the query set
must move the evidence in the same cohort, or this test fails.
"""

import gzip
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "G"


class FrozenQueries(unittest.TestCase):
    def setUp(self):
        self.release = json.loads((ROOT / "RELEASE.json").read_text())
        self.cases = self.release["cases_sha256"]

    def test_every_frozen_query_file_is_the_recorded_cases_golden(self):
        frozen = sorted(EVIDENCE.glob("*/queries.jsonl.gz"))
        self.assertTrue(frozen, "evidence/G has no frozen query files")
        drift = []
        for path in frozen:
            target = path.parent.name
            name = "%s.cases.jsonl" % target
            want = self.cases.get(name)
            if want is None:
                drift.append("%s: no cases_sha256 entry for %s" % (target, name))
                continue
            with gzip.open(path, "rb") as stream:
                got = hashlib.sha256(stream.read()).hexdigest()
            if got != want:
                drift.append("%s: frozen %s, cases %s" % (target, got[:12],
                                                          want[:12]))
        self.assertEqual(drift, [], "frozen G queries drifted from gisel cases")

    def test_every_cases_golden_has_frozen_queries(self):
        have = {path.parent.name for path in EVIDENCE.glob("*/queries.jsonl.gz")}
        want = {name[:-len(".cases.jsonl")] for name in self.cases}
        self.assertEqual(sorted(want - have), [],
                         "cases goldens without frozen G queries")


if __name__ == "__main__":
    unittest.main()

"""The out-of-domain probe: shape-preserving rewrites and honest counting.

The probe rewrites the frozen `G` queries into types no rule declares.  Two
properties matter more than any particular number: a rewrite keeps the query's
shape (a scalar stays scalar, a vector stays vector, a pointer and an IEEE
float keep the width LLVM can build), and the classifier never folds a refusal
into `agree`.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gisel import jsonl as J  # noqa: E402
from gisel_ab.l0 import outside  # noqa: E402


def query(name, types, config="C0", imms=(), mem=None):
    return {"config": config, "name": name, "types": list(types),
            "mem": mem, "imms": list(imms)}


def answer(row, action="Legal", type_idx=0, new_type="LLT_invalid"):
    return dict(row, action=action, type_idx=type_idx, new_type=new_type)


def write(path, rows):
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                            for row in rows))
    return path


class RewriteKeepsTheShape(unittest.TestCase):
    def test_a_scalar_width_changes_and_the_kind_stays(self):
        self.assertEqual(outside.rewrite(("scalar", 32), width=7), ("scalar", 7))
        self.assertEqual(outside.rewrite(("integer", 32), width=31),
                         ("integer", 31))

    def test_a_vector_keeps_its_lane_count_unless_asked(self):
        t = ("vector", 4, False, ("scalar", 32))
        self.assertEqual(outside.rewrite(t, width=7),
                         ("vector", 4, False, ("scalar", 7)))
        self.assertEqual(outside.rewrite(t, lanes=3),
                         ("vector", 3, False, ("scalar", 32)))
        self.assertEqual(outside.rewrite(t, width=7, lanes=3),
                         ("vector", 3, False, ("scalar", 7)))

    def test_pointers_and_floats_keep_constructible_widths(self):
        pointer = ("pointer", 0, 64)
        self.assertEqual(outside.rewrite(pointer, width=7), pointer)
        self.assertEqual(outside.rewrite(("float", 32), width=7), ("float", 32))


class ApplyingAProbe(unittest.TestCase):
    def test_a_rewrite_that_changes_nothing_is_dropped(self):
        base = query("G_ADD", ["p0:*", "p0:*"])
        self.assertIsNone(outside.apply_probe(base, {"width": 7}))

    def test_mem_is_rewritten_with_the_same_numbers(self):
        base = query("G_LOAD", ["s32", "p0:*"], mem=["s32", 4])
        out = outside.apply_probe(base, {"width": 7})
        self.assertEqual(out["types"], ["s7", "p0:*"])
        self.assertEqual(out["mem"], ["s7", 4])

    def test_the_menu_covers_widths_and_odd_lane_counts(self):
        widths = {probe["width"] for probe in outside.probe_plan()
                  if "width" in probe}
        self.assertEqual(widths, set(outside.WIDTHS))
        lanes = {probe["lanes"] for probe in outside.probe_plan()
                 if "lanes" in probe}
        self.assertEqual(lanes, set(outside.LANES))


class CountingRefusals(unittest.TestCase):
    def classify(self, queries, official, ours):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            return outside.classify(
                queries, write(tmp / "official.jsonl", official),
                write(tmp / "ours.jsonl", ours))

    def test_both_refusing_is_agreement_without_the_same_words(self):
        q = [query("G_ICMP", ["s7", "p0:*"])]
        official = [dict(q[0], status="unanswerable", reason="asserted",
                         assert_site="LowLevelType.h:470")]
        ours = [dict(q[0], status="unanswerable", reason="shape: no vector")]
        counts = self.classify(q, official, ours)
        self.assertEqual(counts["both_unanswerable"], 1)
        self.assertEqual(counts["disagree"], 0)
        self.assertEqual(counts["official_asserts"], 1)
        self.assertEqual(counts["assert_sites"], {"LowLevelType.h:470": 1})

    def test_one_side_refusing_is_its_own_bucket(self):
        q = [query("G_OP", ["s7"])]
        official = [dict(q[0], status="unanswerable", reason="asserted")]
        ours = [answer(q[0])]
        counts = self.classify(q, official, ours)
        self.assertEqual(counts["official_only_unanswerable"], 1)
        self.assertEqual(counts["disagree"], 0)

    def test_a_different_action_is_a_disagreement(self):
        q = [query("G_OP", ["s7"])]
        counts = self.classify(q, [answer(q[0], action="WidenScalar")],
                               [answer(q[0], action="Legal")])
        self.assertEqual(counts["disagree"], 1)
        self.assertEqual(counts["by_class"], {"action": 1})


class CleanRun(unittest.TestCase):
    def counts(self, **overrides):
        base = {"disagree": 0, "only_official": 0, "only_eval": 0,
                "official_only_unanswerable": 0, "eval_only_unanswerable": 0}
        base.update(overrides)
        return base

    def test_identical_answers_are_clean(self):
        self.assertTrue(outside.clean_run(self.counts()))

    def test_a_one_sided_refusal_is_not_clean(self):
        self.assertFalse(outside.clean_run(
            self.counts(official_only_unanswerable=1)))
        self.assertFalse(outside.clean_run(
            self.counts(eval_only_unanswerable=1)))

    def test_a_disagreement_is_not_clean(self):
        self.assertFalse(outside.clean_run(self.counts(disagree=1)))


if __name__ == "__main__":
    unittest.main()

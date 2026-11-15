#!/usr/bin/env python3
"""Probe the legalizer with types no rule declares.

Everything else in this workbench starts from a cut the corpus declares, so
the first question is always "given s8/s16/...".  ``graph`` has the same
boundary: the closure is over the declared domain.  This verb asks the
neighbouring question once: what do the two sides do when the first type is a
width the rules never wrote down (``s7``, ``s31``, ``v3s7``, ...)?

It is a probe, not a cohort gate:

* the query set is generated here from the frozen ``G`` queries' shapes, so it
  is reproducible and does not depend on ``gisel cases`` learning a new cut;
* the official side is the same runner the A/B uses; ours is ``gisel eval`` on
  the generated core document, i.e. the same rules without a build;
* ``unanswerable`` is counted on each side, never folded into ``agree``.

Both sides refusing counts as agreement: the runner says "asserted" and the
model names the shape it cannot build, but what the probe compares is the first
step's observable outcome, and here it is "no answer" twice.  A refusal on one
side only is a hole and is counted apart from a disagreement.

Exit codes: 0 every query answered identically, 1 a disagreement or a one-sided
refusal, 2 usage or a broken environment.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from .run import (GISEL, SCRIPTS, HarnessError, answer, build_ctor_runners,
                  build_swap_runner, ensure_baseline, generate_core,
                  generate_inc, load_target, run_eval, swap_source)
from gisel import jsonl as J  # noqa: E402  (run.py puts gisel on sys.path)


# Widths and lane counts the nine corpora do not declare.  `63`/`65` straddle
# the 64-bit boundary, `127` the 128-bit one; 3/5/7 lanes are the odd counts a
# `pow2` rule has never seen.  A probe rewrites *every* slot, keeping each
# slot's kind (scalar/integer/float/pointer) and the vector-ness, so the query
# still looks like the opcode's real shape -- only the numbers are new.
WIDTHS = (7, 31, 63, 65, 127)
LANES = (3, 5, 7)


def rewrite(t, width=None, lanes=None):
    """Same shape, new numbers: kinds, pointers, floats and lanes preserved."""
    kind = t[0]
    if kind in ("scalar", "integer"):
        return (kind, width if width is not None else t[1])
    if kind == "vector":
        return ("vector", lanes if lanes is not None else t[1], t[2],
                rewrite(t[3], width, None))
    return t  # pointers and IEEE floats keep the widths LLVM can construct


def probe_plan():
    plan = [{"width": w} for w in WIDTHS]
    plan += [{"lanes": n} for n in LANES]
    plan += [{"width": w, "lanes": n} for w in (7, 31) for n in (3, 5)]
    return plan


def apply_probe(row: dict, probe: dict) -> dict | None:
    try:
        types = [rewrite(J.parse_type(text), probe.get("width"),
                         probe.get("lanes")) for text in row["types"]]
    except (ValueError, KeyError, IndexError):
        return None
    out = {"config": row["config"], "name": row["name"],
           "types": [J.format_type(value) for value in types],
           "mem": None, "imms": list(row.get("imms") or [])}
    if row.get("mem") and len(row["mem"]) >= 2:
        try:
            mem_ty = rewrite(J.parse_type(row["mem"][0]), probe.get("width"))
            out["mem"] = [J.format_type(mem_ty), int(row["mem"][1])]
        except (ValueError, KeyError, IndexError):
            out["mem"] = list(row["mem"])
    if out["types"] == list(row["types"]) and out["mem"] == row.get("mem"):
        return None  # the probe did not change a width or a lane count
    return out


def base_rows(target: dict) -> list[dict]:
    path = Path(__file__).resolve().parents[3] / "evidence" / "G" / target["name"] / "queries.jsonl.gz"
    if not path.is_file():
        raise HarnessError("no frozen G queries at %s" % path)
    rows = []
    with gzip.open(path, "rt") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def make_queries(target: dict, out: Path, limit: int, per_base: int) -> list[dict]:
    """Rotate a probe menu over the frozen shapes until `limit` queries."""
    rows, seen, plan = [], set(), probe_plan()
    bases = base_rows(target)
    for index, base in enumerate(bases):
        if not base.get("types"):
            continue
        for step in range(per_base):
            probe = plan[(index * per_base + step) % len(plan)]
            candidate = apply_probe(base, probe)
            if candidate is None:
                continue
            try:
                key = J.key(candidate)
            except (KeyError, TypeError, ValueError, J.EvalError):
                continue
            if key in seen:
                continue
            seen.add(key)
            rows.append(candidate)
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    path = out / "queries.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    return rows


def _classify(ref, ours):
    if ref[0] != ours[0]:
        return "status"
    if ref[0] == "unanswerable":
        return "unanswerable-reason"
    for index, name in ((1, "action"), (2, "type_idx"), (3, "new_type")):
        if ref[index] != ours[index]:
            return name
    return None


def _assert_sites(path: Path) -> dict:
    """Map a query key to the assertion site the official runner reported."""
    sites = {}
    with open(path) as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("status") != "unanswerable" or not row.get("assert_site"):
                    continue
                sites[J.key(row)] = row["assert_site"]
            except (KeyError, TypeError, ValueError, J.EvalError):
                continue
    return sites


def classify(queries: list[dict], official: Path, ours: Path) -> dict:
    ref, errors_ref = J.load(official)
    mine, errors_ours = J.load(ours)
    if errors_ref or errors_ours:
        raise HarnessError("unreadable answers: %d ref, %d ours"
                           % (errors_ref, errors_ours))
    counts = {"agree": 0, "both_unanswerable": 0, "disagree": 0,
              "only_official": 0, "only_eval": 0, "queries": len(queries),
              "official_unanswerable": 0, "eval_unanswerable": 0,
              "official_asserts": 0, "by_class": {}, "samples": [],
              "refusals": {}, "official_only_unanswerable": 0,
              "eval_only_unanswerable": 0}
    sites = _assert_sites(official)
    sites_seen = {}
    for row in queries:
        key = J.key(row)
        a, b = ref.get(key), mine.get(key)
        if a is not None and a[0] == "unanswerable":
            counts["official_unanswerable"] += 1
            if key in sites:
                counts["official_asserts"] += 1
                sites_seen[sites[key]] = sites_seen.get(sites[key], 0) + 1
        if b is not None and b[0] == "unanswerable":
            counts["eval_unanswerable"] += 1
        if a is None:
            counts["only_eval"] += 1
            continue
        if b is None:
            counts["only_official"] += 1
            continue
        if a[0] == "unanswerable" and b[0] == "unanswerable":
            # Both sides refuse.  They need not use the same words: the
            # runner says "asserted" and the model names the shape it cannot
            # build.  What the probe compares is the first step's observable
            # outcome, and here it is "no answer" on both sides.
            counts["both_unanswerable"] += 1
            reason = "%s | %s" % (a[1], b[1])
            counts["refusals"][reason] = counts["refusals"].get(reason, 0) + 1
            continue
        if a == b:
            counts["agree"] += 1
            continue
        if a[0] == "unanswerable" or b[0] == "unanswerable":
            # One side refused and the other answered: that is the hole the
            # probe is looking for, kept apart from a rule disagreement.
            name = ("official_only_unanswerable"
                    if a[0] == "unanswerable" else "eval_only_unanswerable")
            counts[name] += 1
            if len(counts["samples"]) < 20:
                counts["samples"].append({"query": row, "official": list(a),
                                          "eval": list(b), "class": name})
            continue
        cls = _classify(a, b)
        counts["disagree"] += 1
        counts["by_class"][cls] = counts["by_class"].get(cls, 0) + 1
        if len(counts["samples"]) < 20:
            counts["samples"].append({"query": row, "official": list(a),
                                      "eval": list(b), "class": cls})
    counts["assert_sites"] = sites_seen
    return counts


def clean_run(counts: dict) -> bool:
    """True when every query was answered the same way by both sides.

    A one-sided refusal is a hole, not agreement: the official side asserting
    and the model answering means the probe never compared a rule.
    """
    return (counts["disagree"] == 0 and counts["only_official"] == 0
            and counts["only_eval"] == 0
            and counts["official_only_unanswerable"] == 0
            and counts["eval_only_unanswerable"] == 0)


def cmd_outside(args) -> int:
    target = load_target(args.target)
    tree = Path(args.tree).expanduser().resolve()
    build = Path(args.build).expanduser().resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    state = Path(args.state or (out / "state")).resolve()

    queries = make_queries(target, out, args.limit, args.per_base)
    if not queries:
        raise HarnessError("no probes were generated")
    print("%s: %d outside-domain queries" % (target["name"], len(queries)))

    source = (GISEL / target["source"]).resolve()
    features = (GISEL / target["features"]).resolve()

    _baseline, _prov = ensure_baseline(target, tree, build, state)
    if target["strategy"] == "swap-source":
        swap_source("restore", target, tree)
        official_runner = build_swap_runner(target, tree, build, out,
                                            "runner.official")
    elif target["strategy"] == "generated-ctor":
        inc = generate_inc(target, source, out)
        official_runner, _ours = build_ctor_runners(target, tree, build, inc,
                                                    out)
    else:
        raise HarnessError("unknown strategy %r" % target["strategy"])

    official = out / "official.answers.jsonl"
    answer(official_runner, out / "queries.jsonl", official)

    core = generate_core(target, source, out)
    ours, eval_rc = run_eval(core, features, out / "queries.jsonl", out)
    if eval_rc not in (0, 1):
        raise HarnessError("gisel eval failed with rc=%d" % eval_rc)

    result = classify(queries, official, ours)
    result.update({"target": target["name"], "tree": str(tree),
                   "build": str(build)})
    (out / "summary.json").write_text(json.dumps(result, indent=2,
                                                sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("queries=%d agree=%d both-unanswerable=%d disagree=%d "
              "official-unanswerable=%d eval-unanswerable=%d "
              "official-asserts=%d"
              % (result["queries"], result["agree"],
                 result["both_unanswerable"], result["disagree"],
                 result["official_unanswerable"], result["eval_unanswerable"],
                 result["official_asserts"]))
        if result["by_class"]:
            print("by class: %s" % json.dumps(result["by_class"], sort_keys=True))
        for site, n in sorted(result["assert_sites"].items(),
                              key=lambda item: -item[1]):
            print("official assert x%d at %s" % (n, site))
        for reason, n in sorted(result["refusals"].items(),
                                key=lambda item: -item[1])[:3]:
            print("both refused x%d: %s" % (n, reason))
        for sample in result["samples"][:5]:
            print("  %s %s: official=%s eval=%s"
                  % (sample["query"]["name"],
                     ",".join(sample["query"]["types"]),
                     sample["official"], sample["eval"]))
    return 0 if clean_run(result) else 1


def build_parser():
    ap = argparse.ArgumentParser(prog="ab.py outside", description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--build", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--limit", type=int, default=900,
                    help="maximum probes (default 900)")
    ap.add_argument("--per-base", type=int, default=3,
                    help="probes per base query (default 3)")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return cmd_outside(args)
    except HarnessError as exc:
        print("outside: %s" % exc, file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print("outside: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

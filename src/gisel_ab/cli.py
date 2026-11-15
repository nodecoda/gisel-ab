"""One verb table for the workbench: ``ab.py <verb> ...``.

Before this module each tool was its own script with its own parser, and the
documentation had to name six different entry points.  There is still one
implementation per verb; this module only routes.  Imports are lazy so
``--help`` works in an environment without LLVM, gisel, or the evidence tree.

Verbs that need a checkout rather than an install say so in their own error
message; nothing here reads the tree.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

# verb -> (module, callable, prepend the verb to argv)
#
# ``run``/``mir``/``verify`` share one parser in ``l0/run.py``: the harness
# builds, answers and diffs in one process, so they must stay in one module.
VERBS = {
    "doctor": (("gisel_ab.doctor", "main", False),
               "check tree/build/gisel/state before a run (read-only)"),
    "bug-report": (("gisel_ab.bugreport", "main", False),
                   "collect versions, cohort and doctor into one JSON"),
    "setup": (("gisel_ab.setup", "main", False),
              "record the LLVM tree/build/evidence paths (no export)"),
    "run": (("gisel_ab.l0.run", "main", True),
            "build both sides, answer, diff, restore"),
    "mir": (("gisel_ab.l0.run", "main", True),
            "L2: answerless queries -> MIR -> replay both libraries"),
    "verify": (("gisel_ab.l0.run", "main", True),
               "check the build is back on the baseline library"),
    "diff": (("gisel_ab.l0.diff", "main", False),
             "compare two answer files (action or full)"),
    "l3": (("gisel_ab.l3", "main", False),
           "run check-llvm twice and compare it test by test"),
    "suite": (("gisel_ab.suite", "main", False),
              "check/refresh the per-target answer snapshots"),
    "vertical": (("gisel_ab.vertical", "main", False),
                 "run or verify one target's whole slice"),
    "cohort": (("gisel_ab.cohort", "main", False),
               "plan, check, tag and verify a frozen cohort"),
    "evidence": (("gisel_ab.evidence", "main", False),
                 "verify EVIDENCE.json against live or frozen artifacts"),
    "plan": (("gisel_ab.domain_plan", "main", True),
             "print one target's domain plan (no build)"),
    "outside": (("gisel_ab.l0.outside", "main", False),
                "probe types no rule declares (undeclared widths, odd lanes)"),
}

# Not in the front page but still reachable: they are the internals the ten
# verbs call, and keeping them addressable is how a failure gets reproduced.
INTERNALS = {
    "attribute": ("gisel_ab.l0.attribute", "main"),
    "closure": ("gisel_ab.l0.closure", "main"),
    "queries": ("gisel_ab.l0.queries", "main"),
    "swap": ("gisel_ab.l0.swap", "main"),
    "inject": ("gisel_ab.l0.inject_predicate", "main"),
    "baseline": ("gisel_ab.baseline", "main"),
    "replay-frozen": ("gisel_ab.replay_frozen", "main"),
    "refresh-counts": ("gisel_ab.refresh_counts", "main"),
    "mir-from-query": ("gisel_ab.mir.from_query", "main"),
    "mir-replay": ("gisel_ab.mir.replay", "main"),
    "mir-config-check": ("gisel_ab.mir.config_check", "main"),
}


def _version() -> str:
    """The checkout's version when there is one, else the installed metadata.

    The source wins so `python3 ab.py --version` reflects the tree being
    edited, even when a stale editable install is still on the path.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = pyproject.read_text()
    except OSError:
        text = ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if match:
        return match.group(1)
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("gisel-ab")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    return "unknown"


# What a verb needs before it can run; the help groups by this so a reader
# can start with the offline half.
NEEDS_BUILD = {"run", "mir", "verify", "l3", "vertical", "outside"}
NEEDS_TREE = {"cohort"}


def _usage() -> str:
    width = max(len(name) for name in VERBS)
    groups = (
        ("without LLVM", [name for name in VERBS
                          if name not in NEEDS_BUILD | NEEDS_TREE]),
        ("with an LLVM tree and build", [name for name in VERBS
                                         if name in NEEDS_BUILD]),
        ("with a git tree (no build)", [name for name in VERBS
                                        if name in NEEDS_TREE]),
    )
    chunks = []
    for title, names in groups:
        chunks.append("  %s:" % title)
        for name in names:
            chunks.append("    %-*s  %s" % (width, name, VERBS[name][1]))
    return (
        "usage: gisel-ab <verb> [options]\n"
        "       (checkout: python3 ab.py <verb>)\n"
        "\n"
        "verbs (each takes --help; gisel-ab --version):\n"
        + "\n".join(chunks)
        + "\n\n"
        "Internals stay callable for reproduction: %s\n"
        % ", ".join(sorted(INTERNALS))
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version"):
        print("gisel-ab %s" % _version())
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage(), end="")
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    if verb in INTERNALS:
        module, name = INTERNALS[verb]
        return getattr(importlib.import_module(module), name)(rest) or 0
    entry = VERBS.get(verb)
    if entry is None:
        print("ab: unknown verb %r\n\n%s" % (verb, _usage()), file=sys.stderr,
              end="")
        return 2
    (module, name, prepend), _ = entry
    args = [verb, *rest] if prepend else rest
    return getattr(importlib.import_module(module), name)(args) or 0


if __name__ == "__main__":
    sys.exit(main())

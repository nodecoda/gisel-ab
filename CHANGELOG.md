# Changelog

This file records shape changes to the workbench, not results.  Counts,
commits and gate outcomes live in `RELEASE.json` and `VERIFICATION.json`,
which are generated and must not be hand-edited.

## 0.9.1 — cohort tooling

- `cohort diff NEW OLD [--json]` compares two recorded cohorts without
  re-running them: `RELEASE.json.repos`, gate blobs and `EVIDENCE.json` counts,
  reported as changed / unchanged / added / removed.  A missing tag is rc=2.
- `cohort verify --jobs N` runs the gates' slow legs in parallel; the record
  keeps the serial order, so the draft is byte-identical.
- `cohort record-ci` accepts extra top-level keys (for example
  `gisel-install`), so a new consuming CI can be recorded without changing the
  tool.
- Tests read the pinned gisel version from `RELEASE.json` (and, where present,
  from `repos.gisel`'s own `__init__.py`) instead of hardcoding it, so a gisel
  patch release no longer turns an ab build red.

## Unreleased — usability (cohort r12)

- `tests/test_g_queries_are_cases.py` binds every frozen
  `evidence/G/<target>/queries.jsonl.gz` to `RELEASE.json.cases_sha256`, so a
  corpus or `cases` change can no longer leave the A/B input behind.  The
  riscv and mips evidence were re-frozen under that gate.
- `gisel-ab setup --llvm DIR --build DIR [--evidence DIR]` records the paths in
  `$XDG_CONFIG_HOME/gisel-ab/config.json` (or `~/.config/...`; `GISEL_AB_CONFIG`
  moves it), so a shell no longer needs `export AB_TREE=... AB_BUILD=...`.
  Resolution is unchanged at the bottom: flag > environment > config > sibling.
- `setup` writes `llvm` and the resolver reads the same key; `run`, `mir` and
  `verify` now accept an omitted `--tree`/`--build` and fall back to the
  config (an explicit flag still wins, and keeps the old no-shape-check
  behaviour).  This closes the gap where `setup` only helped `vertical` and
  `doctor`.
- `doctor` prints where each path came from (`(from AB_TREE)`, `(from
  /path/config.json)`) and its fix line now names `gisel-ab setup` first.
  Failures with nothing configured stay rc=2.
- `gisel-ab --help` groups verbs by what they need: without LLVM, with an LLVM
  tree and build, with a git tree.  `--version` reports 0.9.0.
- `doctor` gains `gisel-pin`: the installed gisel must match the cohort's
  `RELEASE.json` version.  A site-packages mismatch fails with the exact
  `pip install gisel==...` line; a checkout mismatch only warns.
- `gisel graph` is unchanged in this cohort.
- `gisel-ab bug-report [--out FILE]` collects the three versions, the cohort
  record, the doctor verdict and the last run's ledger line into one small
  JSON file (no evidence tree), so an issue can carry the facts.
- Distribution: a composite action (`lint` + `cases` + `suite check`), a
  pre-commit hook in gisel (`gisel-lint`), and
  `scripts/time-to-first-answer.sh` (fresh venv -> installed tools -> an
  answer, 300 s limit) wired into CI.
- The landing page and the eight-task cookbook live in gisel
  (`docs/OVERVIEW-zh.md`, `docs/COOKBOOK-zh.md`, `docs/GLOSSARY-zh.md`);
  this README links to them instead of duplicating the story.

## Unreleased — productization (cohorts r8, r9)

- One entry point: `python3 ab.py <verb>` (or `gisel-ab` after
  `pip install .`).  The verb table is `doctor run mir verify diff l3 suite
  vertical cohort evidence plan`; the old per-script entry points are gone.
- One package: every module moved under `src/gisel_ab/` (`l0/` for the
  answer/diff layer, `mir/` for the MIR layer).  Flat imports and the
  `sys.path` shims that made them work are gone; `tests/conftest.py` keeps
  the old names only as test aliases.
- Shell entry points moved under `scripts/` (`runners/`, `domain/`, `e2e/`,
  `negative/`, `inc/`).  `scripts/lib.sh` is the one place that defines the
  roots and the negative-control exit-code contract.
- C++ query runners moved to `src/gisel_ab/cpp/`; the target registry, the
  contracts, the domain table and the offline suite snapshots moved to
  `src/gisel_ab/data/`.
- The frozen evidence tree moved from `oracle/` to `evidence/`.  Contents and
  hashes are unchanged; `EVIDENCE.json` is byte-identical.
- Packaging: `pyproject.toml` (src layout, `gisel-ab` console script, no
  third-party dependencies) and `MANIFEST.in`.  The wheel carries code, the
  target registry, the suite snapshots and the C++ sources; the 102 MB
  evidence tree is repository-only.
- CI gains a packaging job (install the wheel, run `--help` and the offline
  suite, assert `evidence/` is absent) and a cohort check job.
- Installed state: `suite` resolves `gisel` and its `data/corpus`/`data/cases`
  without a sibling checkout, `doctor` falls back to the pin the
  `gisel-anchor` wheel ships, and `gisel-ab --version` exists.  The packaging
  smoke no longer sets `GISEL_SRC`, so the install-only path is what CI tests.
- `cohort check` treats a record that names an older cohort as "not written
  yet" (rc=0, one line saying so) instead of drift.  The record commit is a
  child of the tag, so at the tag the manifest is already the new cohort while
  `VERIFICATION.json` still names the previous one.  `--worktree` keeps
  failing in that state: a drift check needs a record for this cohort.
  `r9` is the cohort that carries this fix and the refreshed anchor pin; its
  evidence tree is byte-identical to `r8`.

## Earlier

See the git history for r1–r7.  Each cohort's gates and commits are in
`RELEASE.json`; the recorded verification of each tag is in
`VERIFICATION.json`.

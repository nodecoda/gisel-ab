# gisel-ab — differential harness

This is the workbench entry point for the LLVM legalizer differential loop.
It is **not** part of gisel; the boundary is `SCOPE.md`.  It consumes a
`.gisel` source (or a prebuilt `.inc`) and an answerless query JSONL, and
compares the official legalizer with one that includes the generated fragment.

## Layout and entry points

One command surface, one package, three layers of files:

```text
ab.py                       the checkout entry point: python3 ab.py <verb>
src/gisel_ab/               the package (cli, l0/, mir/, cpp/, data/)
scripts/                    shell entry points (runners, domain, e2e, negative, inc)
evidence/                   frozen artifacts; repo-only, never in the wheel
tests/  docs/               the suite and the write-ups
```

`ab.py --help` is the verb table: `doctor setup bug-report run mir verify diff l3
suite vertical outside cohort evidence plan`.  Each verb keeps its own parser and its own
exit codes; `ab.py` only routes.  `pip install gisel-ab` (or `pip install .`
from a checkout) gives the same table, and the installed wheel carries the code, the target registry,
the suite snapshots and the C++ runner sources -- but not `evidence/`, which
a verifier must be able to see in a checkout.

The internals stay addressable for reproduction: `ab.py closure`,
`ab.py attribute`, `ab.py queries`, `ab.py swap`, `ab.py inject`,
`ab.py baseline`, `ab.py replay-frozen`, `ab.py refresh-counts`,
`ab.py mir-from-query`, `ab.py mir-replay`, `ab.py mir-config-check`.

## Start here

The toolchain is three repositories: `gisel` (DSL + questions), `gisel-anchor`
(pinned upstream facts), and this one (differential harness).  The Chinese
landing page is
[gisel/docs/OVERVIEW-zh.md](https://github.com/nodecoda/gisel/blob/main/docs/OVERVIEW-zh.md);
eight copy-paste tasks are in
[gisel/docs/COOKBOOK-zh.md](https://github.com/nodecoda/gisel/blob/main/docs/COOKBOOK-zh.md).

## Ten minutes

`docs/TUTORIAL-m68k.md` walks the whole loop on M68k -- 13 rules, 35
queries, one configuration, about 65 seconds of compute: doctor, questions,
include, comparison, negative control, and the check that the tree was put
back.  X86 is the same loop with 3,014 queries; M68k is the one you can
finish while the kettle boils.

`docs/QUICKSTART-with-llvm.md` is the shorter decision page: which layer
answers which question (L0 `ab.py run`, MIR replay, L3 `check-llvm`), what
each costs, and what a green result does and does not prove.

## Offline check

`ab.py suite check --all` compares our answers with a frozen official snapshot
per target -- no LLVM, no build, no network.  It is the cheapest way to ask
"do the rules still say what the official legalizer said?" and it refuses to
run when the snapshot's shas no longer match the corpus.  Format and how to
add a target: `src/gisel_ab/data/suite/README.md`.

## Two layers

- **L0 (`ab.py run` / `ab.py diff`)** compares `getAction()` for one query:
  `(opcode, types, mem, imms) -> (action, type_idx, new_type)`.
- **MIR/llc (`ab.py mir-replay`)** replays real `.mir` through
  `llc -run-pass=legalizer` and diffs the official and replaced libraries.
- **L3 (`ab.py l3`)** runs the whole `check-llvm` twice (baseline, then with the
  three constructors pointing at gisel's `.inc`) and compares lit's per-test
  results, not just the totals.  `ab.py l3 compare` fails on a test that exists
  on one side only or whose result changed.

L0 is the rule-translation gate. The MIR layer is the behavior gate: it also
covers multi-step legalization, custom actions and the downstream verifier.
A pass in one is not a pass in the other; the contracts are `SCOPE.md` §3 and
§6, the report schema is `CONTRACTS.md` §8.

## Roots

There is no default checkout: a default pointing at the machine the harness
was written on either fails obscurely or swaps files in someone else's tree.
Record the paths once:

```sh
gisel-ab setup --llvm /path/to/llvm-project --build /path/to/build
gisel-ab doctor
```

The config lives in `$XDG_CONFIG_HOME/gisel-ab/config.json` (usually
`~/.config/gisel-ab/config.json`); `--show` prints it, `--unset build` removes
one key.  Precedence is flag > environment > config, so an existing shell keeps
working:

```sh
export AB_TREE=/path/to/llvm-project      # pin-compatible checkout
export AB_BUILD=/path/to/build            # its ninja build
```

`ab.py cohort` additionally accepts `COHORT_TREE` and `COHORT_LLVM`.  A missing
root is an input error (rc=2) with the exact `export` line to run.

Start here, before any run:

```sh
python3 ab.py doctor          # read-only; needs no arguments
python3 ab.py doctor --json   # {ok, checks:[{name,status,detail,fix}]}
```

`doctor` checks the interpreter, `gisel`, `PIN.json`, the tree, the build
(`build.ninja` and `bin/llc`), `ninja`, free disk, the last ledger entry and
any held lock.  The tree check is the same `check-pin.sh` the run uses: it
compares the twelve files `PIN.json` freezes by content, so a `main` that
merely contains the pin commit is rejected before a runner is built.  Exit
codes: 0 all checks passed (warnings allowed), 2 something failed.

## One command

```sh
python3 ab.py run --target x86-ctor \
  --tree "$AB_TREE" \
  --build "$AB_BUILD" \
  --queries /tmp/x86.domain.jsonl \
  --out /tmp/ab-x86 --state /tmp/ab-state-x86 \
  --compare full
```

`scripts/e2e/run-x86-end-to-end.sh TREE BUILD x86.inc OUT STATE` does the same
without the ledger: official from the pinned constructor, ours after swapping
the body, then `diff.json`.

## Cohort

`ab.py cohort` turns a release cut into five verbs:

```sh
python3 ab.py cohort plan                 # gates, repos, inputs, online gates
python3 ab.py cohort tag cohort-YYYY-MM-DD-rN
python3 ab.py cohort verify --clone /tmp/cohort --jobs 2
python3 ab.py cohort check [--worktree]   # FRESH / STALE per gate, no re-run
python3 ab.py cohort diff NEW OLD         # repos, frozen hashes, gate blobs, evidence
```

`RELEASE.json` names the commits and gate commands.  `ab.py cohort` adds the
repo and input files for each gate and stores a git-blob fingerprint in
`VERIFICATION.json`.  `check` compares the record with the tagged commits;
`check --worktree` compares it with what is on disk now, which is how a
changed corpus shows up before anyone tags again.

The record is a child of the tag: the tag commit still carries the previous
cohort's `VERIFICATION.json`, so `check` prints `record is ...; skipping`
and exits 0 there, and `test_verification` skips too.  The check is only
evidence once the record names the same cohort as the manifest; `--worktree`
has no such grace state and still fails on a mismatch.  Do not edit
`RELEASE.json` after its manifest commit; the record shape is in
`CONTRACTS.md` §11.

## Full domain

`scripts/domain/run-domain.sh TARGET TREE BUILD INC OUT` asks every point the declared value
space enumerates, per config, for the six adapters without a dedicated script
(x86, aarch64 and riscv keep their own).  The shape is pinned in
`data/domain.json`: per config, how many queries, how many both sides refuse,
how many only the reference refuses.  A drift fails; it does not absorb.

`scripts/negative/run-g-negative.sh TARGET OPCODE OUT` is the G-side negative control:
replace one builder chain with `.unsupported()`, run the target script, and
require every disagreement to be attributed to that builder.

## Frozen replay

`ab.py replay-frozen TARGET --tree TREE --build BUILD --out OUT --direction both`
rebuilds the target's include and runner, answers the frozen `G`/`U` query
sets, and requires the same numbers as the committed official answers.  This
is the gate to run after editing a `.gisel` file: `scripts/verify-evidence.sh` only
proves the old artifacts did not change.  See `docs/REPLAY-FROZEN.md`.

## Adoption branches

The three L4 targets have fork branches that actually build from the gisel
fragment (`explore/{x86,aarch64,riscv}-adopt` in `nodecoda/llvm-project`).
Each one is backed by the same three checks: the built CodeGen library hash
equals the MIR-L3 "swapped" hash, all 80,596 lit results are identical, and the
frozen `G`/`U` replays disagree zero times.  Branch list, provenance hashes,
the inc-generation policy and the rollback rule are in `docs/ADOPT.md`.

## Multi-step closure

`ab.py closure` answers the question a one-step A/B leaves open: does the whole
`legalize` loop agree?  It feeds every non-terminal answer's `new_type` back in
as a new query, asks both builds, and stops at a fixed point.  Because the
resulting state set is closed under the successor, one-step equality there
implies loop equality by induction.  Seeds are `cases` (rule-aware, `p0:*`) and
`domain` (declared product, `p0:64`); the two keys spell pointers differently,
so both are needed.  Numbers, the pinned AArch64 asserts and the boundary are in
`docs/CLOSURE.md`.

## Vertical slice

`ab.py vertical` is the end-to-end slice for one target: G/U questions, both
negative controls, the full domain, MIR L1/L2, and optionally L3, all checked
against `data/<target>-contract.json`.

```sh
python3 ab.py vertical --target riscv   --tree TREE --build BUILD --out /tmp/rv [--l3]
python3 ab.py vertical --target x86     --out /tmp/x --verify-only [--l3]
python3 ab.py vertical --target aarch64 --out /tmp/a --verify-only --frozen
```

`--record` writes the contract from the run that just finished; it is for
adding a target, not for refreshing one.

`VERIFY_FROZEN=1 ./scripts/verify-evidence.sh` reads only the committed `evidence/`
and `data/closure/` copies, so CI proves the repo alone replays the recorded
evidence.  `./scripts/adopt-check.sh` fetches the three adoption branches and
compares the committed `.inc` bytes with the gisel artifacts.

`--frozen` rebuilds the artifact layout from the committed copies under
`evidence/` (G/U and both negative controls come from `evidence/<id>/`, the domain
summary and the two MIR reports from `evidence/<target>-vertical/`). It needs no
build tree and no `/tmp` state, so CI runs it for all three targets on every
push (`.github/workflows/ci.yml`).

Evidence: `docs/EVIDENCE-riscv.md`, `docs/EVIDENCE-domain.md`.

## Outside the declared domain

`ab.py outside` asks the neighbouring question once: what do the two sides do
when the first type is a width no rule wrote down (`s7`, `s31`, `s63`, `s65`,
`s127`, odd lane counts)?  The queries are rewritten from the frozen `G`
shapes, keeping each slot's kind, vector-ness, pointer and IEEE-float width;
the official runner and `gisel eval` answer the same file.  Both sides refusing
is counted as agreement (`both-unanswerable`); a refusal on one side only is a
hole and keeps the exit code at 1.

```sh
python3 ab.py outside --target aarch64 --tree TREE --build BUILD --out /tmp/o9 \
    [--limit 200000] [--per-base 12] [--json]
```

Numbers and the two model defects it found are in `docs/EVIDENCE-O9.md`.

## MIR/llc regression

```sh
python3 ab.py mir-replay --layer upstream \
  --target x86 --target aarch64 --target riscv \
  --tree /home/ubuntu/llvm/trees/explore \
  --build /home/ubuntu/llvm/builds/explore \
  --out /tmp/mir-l1 --prepare --json
```

`--prepare` snapshots the official `libLLVM<T>CodeGen.so`, applies the
generated `.inc` through `ab.py swap`, rebuilds only that library,
snapshots the replaced one, restores the source and rebuilds the baseline.
Replay then runs one `bin/llc` against both snapshots via `LD_LIBRARY_PATH`,
so the tree is never left patched while tests run.

`--negative-control OPCODE` rebuilds a mutant include with
`ab.py inject` and requires the replay to report `diff > 0`;
otherwise the control fails and the command exits 1.

### L2: queries become MIR

```sh
python3 ab.py mir --target x86 --queries /tmp/x86.cases.jsonl \
  --tree /home/ubuntu/llvm/trees/explore \
  --build /home/ubuntu/llvm/builds/explore \
  --out /tmp/l2-x86 [--negative-control G_ADD] [--limit N] --json
```

`ab.py mir-from-query` turns each answerless query into a minimal `.mir` (one
`# RUN:` line each) using `data/mir-shapes.jsonc` and `data/mir-configs.json`;
queries that cannot be spelled as real MIR are counted, not dropped.  Every
def of the instruction under test gets a `COPY` sink, because the legalizer
erases trivially dead instructions before it looks at them.  Files are
preflighted with `-run-pass=none` (parse + machine verifier only), then
replayed with the same buckets as L1.  Runs that abort on both sides are
retried with `-global-isel-abort=0` so the unlegalized MIR is still compared.

`ab.py mir-config-check --target T --features F` checks that every gisel config
has a realizable llc spelling; `--mutate CONFIG` is its negative control.

`--compare action` is the coarse smoke gate; `full` (default) compares
`(action, type_idx, new_type)`.  `--dry-run` validates the adapter, query file
and baseline without touching the build.  `ab.py verify` checks that the build
is back on the baseline include.

## Targets

| adapter | strategy | notes |
|---|---|---|
| `x86` | swap the constructor body for `#include` and rebuild | official is the pinned hand-written constructor |
| `x86-ctor` | generated constructor object | retired with the `.td` path; kept only for old logs |
| `aarch64` | generated constructor object | 12,559 queries; 9 configs; does not modify the build |
| `m68k` | generated constructor object | 35 queries; single `default` config |
| `ppc` | generated constructor object | 94 queries; one `default` config; 1 query asserts on both sides (upstream `bitcastIf`) |
| `riscv` | generated constructor object | 6,086 queries; 8 configs |
| `wasm` | generated constructor object | 894 queries; `wasm32|wasm64` × `±sign-ext` |
| `arm` | generated constructor object | 1,614 queries; 7 configs across soft-float / VFP2 / VFP4 / NEON / V5T / hardware divide / AEABI; Thumb1 excluded (constructor registers no rule) |
| `mips` | generated constructor object | 1,656 queries; 5 configs across MSA / MIPS32r2 / unaligned access; 9 queries abort upstream (`--allow-ref-asserts 9`) |
| `spirv` | generated constructor object | 5,689 queries; 6 configs across kernel/shader × spirv32/64 × extension sets; 4 queries abort on both sides (`--expect-unanswerable 4`, upstream `changeElementSizeTo` overload) |

Adapters live in `src/gisel_ab/data/targets/*.json`: include path, library, ninja
target, runner builder, feature table, optional axes, query-count contract.

`query_count` is the current generator's output, not a hand-written constant:
`ab.py refresh-counts --check` recomputes every adapter and exits 1 on drift.
A frozen `EVIDENCE.json` entry keeps the count of *its own* query file, so the
two can differ after the corpus changes and both are still correct.

A new target's runner is a ~30 line file: include its subtarget and legalizer
headers, alias `TargetSubtarget`/`TargetLegalizer`, include
`query_runner_common.h`, then return the config table, the target
initializers, and the legalizer constructor.  `scripts/runners/build-runner.sh` derives
the include paths and the `libLLVM<TARGET>{CodeGen,Desc,Info}` list from the
target name; `scripts/runners/build-m68k-runner.sh` is the thin per-target wrapper the adapter
names.

## Artifacts

```text
OUT/official.answers.jsonl
OUT/ours.answers.jsonl
OUT/diff.json
STATE/baseline/           baseline inc + library + provenance
STATE/ledger.jsonl        one line per run (input hashes, counts, rc)
```

Assertions do not kill the batch: the runner arms a `sigsetjmp` recovery point
around `getAction()`, writes `status:"unanswerable"` for a query that aborts,
and keeps answering (AArch64's full domain aborts 39,056 times per config).
`ab.py queries` still restarts on a real crash (SIGSEGV) and marks that query
the same way, so the answer file stays parallel to the query file.  A one-sided
assertion is a difference (`ref_asserted`), not agreement.

## Triage

`bundle.json` groups differences by `owner`:

| owner | meaning | action |
|---|---|---|
| `ours` | translation / ours assertion / evaluator | fix the rule, importer, emitter or evaluator |
| `ours-or-upstream` | rule answer differs | check the source revision before deciding |
| `llvm-or-query` | official asserts, a rule did fire | validate the query first, then record upstream |
| `query` | official asserts, no rule fired | fix the generator/axes |

A confirmed LLVM defect is recorded under
`/home/ubuntu/admin/upstream-issues/` first, per `admin/AGENTS.md` §4.1; do not
open a GitHub issue directly.

## Status

First smoke (2026-11-08): X86 400 queries, full compare, 0 disagreements;
AArch64 10,271 queries, 52 differences (45 one-sided assertions, 7 `type_idx`)
awaiting three-way attribution.  See `docs/REPORT-ab.md` §3.

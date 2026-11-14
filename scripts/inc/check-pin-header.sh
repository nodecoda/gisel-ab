#!/usr/bin/env bash
set -euo pipefail

# check-pin-header.sh --tree DIR --build DIR [--target T]... [--self-test]
#
# Compile every generated target include against a pin-only LLVM tree: the pin
# commit plus the PRs listed in PIN.json, with none of the local deviations.
#
# The A/B build already compiles the include, but inside our frozen tree, and
# that tree carries the `core*` builders.  So the A/B build cannot tell whether
# the include would compile upstream.  This gate can.  It is what keeps a
# symbol that only exists in `pin/deviations/` out of a shipped `.inc`: the
# generated file must name nothing but the public LegalizerInfo API.
#
#   --tree DIR    a pin-only llvm-project checkout
#   --build DIR   that tree's ninja build (generated headers, TargetGen*.inc)
#   --target T    x86 | aarch64 | riscv (repeatable; default: all three)
#   --self-test   inject a `coreLegalIf` call and require the gate to fail
#
# rc: 0 = every include compiles; 1 = a compile failed; 2 = input error.

AB_ROOT=${AB_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
. "$AB_ROOT/scripts/lib.sh"

TREE=${AB_TREE:-}
BUILD=${AB_BUILD:-}
TARGETS=()
SELF_TEST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tree) TREE=${2:?}; shift 2 ;;
    --build) BUILD=${2:?}; shift 2 ;;
    --target) TARGETS+=("${2:?}"); shift 2 ;;
    --self-test) SELF_TEST=1; shift ;;
    -h|--help) sed -n '3,22p' "$0"; exit 0 ;;
    *) echo "usage: $0 --tree DIR --build DIR [--target T]... [--self-test]" >&2; exit 2 ;;
  esac
done

if ! require_roots "$TREE" "$BUILD"; then
  exit 2
fi
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  TARGETS=(x86 aarch64 riscv)
fi

CXX=${CXX:-c++}
OUT=$(mktemp -d "${TMPDIR:-/tmp}/gisel-pin-header.XXXXXX")
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$OUT"' EXIT

# target -> (corpus name, C++ target directory)
corpus_name() {
  case "$1" in
    x86) echo X86 ;;
    aarch64) echo AArch64 ;;
    riscv) echo RISCV ;;
    *) echo "input error: unknown target '$1'" >&2; return 2 ;;
  esac
}
target_dir() {
  case "$1" in
    x86) echo X86 ;;
    aarch64) echo AArch64 ;;
    riscv) echo RISCV ;;
  esac
}

compile_one() {  # compile_one TARGET INC
  local target=$1 inc=$2 dir
  dir=$(target_dir "$target")
  "$CXX" -fsyntax-only \
    -D_DEBUG -D_GLIBCXX_ASSERTIONS -D_GNU_SOURCE \
    -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS \
    -I"$OUT" \
    -I"$BUILD/lib/Target/$dir" -I"$TREE/llvm/lib/Target/$dir" \
    -I"$BUILD/include" -I"$TREE/llvm/include" \
    -std=c++17 -fno-exceptions -fno-rtti \
    "$AB_CPP/${target}_generated_ctor.cpp"
}

status=0
for target in "${TARGETS[@]}"; do
  dir=$(target_dir "$target") || exit 2
  name=$(corpus_name "$target") || exit 2
  inc="$OUT/${dir}LegalizerInfo.inc"
  python3 -m gisel gen "$GISEL/tests/corpus/${name}LegalizerInfo.gisel" \
    --emit=inc --out "$inc" >/dev/null
  if compile_one "$target" "$inc" >"$OUT/$target.log" 2>&1; then
    echo "pin header: $target compiles"
  else
    echo "pin header: $target DOES NOT compile against the pin-only header" >&2
    sed -n '1,40p' "$OUT/$target.log" >&2
    status=1
  fi
done

if [[ $SELF_TEST -eq 1 ]]; then
  target=${TARGETS[0]}
  dir=$(target_dir "$target")
  name=$(corpus_name "$target")
  inc="$OUT/${dir}LegalizerInfo.inc"
  # A call to a builder that exists only in pin/deviations/.  If this compiles,
  # the gate is compiling against the wrong header and the run above is blind.
  {
    echo '  getActionDefinitionsBuilder(G_ADD).coreLegalIf([](const LegalityQuery &) { return true; });'
    cat "$inc"
  } >"$OUT/${dir}SelfTest.inc"
  cp "$OUT/${dir}SelfTest.inc" "$inc"
  if compile_one "$target" "$inc" >"$OUT/$target.selftest.log" 2>&1; then
    echo "self-test: BLIND -- a core* builder compiled against $TREE" >&2
    exit 2
  fi
  if ! grep -q 'coreLegalIf' "$OUT/$target.selftest.log"; then
    echo "self-test: failed for the wrong reason" >&2
    sed -n '1,20p' "$OUT/$target.selftest.log" >&2
    exit 2
  fi
  echo "self-test: a core* builder is rejected (gate is live)"
fi

exit "$status"

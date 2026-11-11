#!/usr/bin/env bash
# check-slow.sh - the engine's SLOW gate: every pytest test marked `slow`
# (tests/conftest.py) - the real-tool tests that need the eda image (sby,
# yosys, verilator, mcy, cocotb). Split out of tests/check-engine.sh because
# CLAUDE.md keeps tests/check.sh under two minutes and this set alone ran
# past that once M3's formal/cover/synth suites landed. Not wired into
# tests/check.sh - run this one by hand, or from a slower CI lane.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

eda_bin() {
  if [ -x "$REPO_ROOT/bin/eda" ]; then
    echo "$REPO_ROOT/bin/eda"
  elif [ -n "${EDA:-}" ] && [ -x "$EDA" ]; then
    echo "$EDA"
  else
    echo "check-slow.sh: no ./bin/eda and \$EDA is not set to an " \
         "executable launcher" >&2
    exit 2
  fi
}

EDA_BIN="$(eda_bin)"
cd "$REPO_ROOT"
# The cap catches a hang, not a slow box: M1-M3's own marked set (mutate is
# the slowest single gate there, ~20-25 s per run, several times over)
# measured ~146 s of wall time under load (uptime 20-30 on 6 cores). M4
# added one real end-to-end proof (test_check_harden.py's
# test_harden_job_killed_halfway_..., two full real LibreLane harden runs
# plus five real signoff gates) that alone measured ~815 s under load - the
# old 900 s cap left the rest of the slow set almost no room and one busy
# box away from a false-red timeout kill, not a real hang. Tests also
# marked `harden_faults` (whole corpus rungs, several top hardens each) run
# in tests/check-harden-faults.sh instead, so they never eat this cap.
# tests/test_corpus_faults.py's other rungs run in `bin/chip-flow test`
# (one CI job per corpus shard, each under its own 1800 s cap, and a fast
# test there keeps that matrix naming every shard), not here: the
# comparator rung alone measured about 1340 s on CI, so with them this
# set ran past 1800 s on its own.
# The set outgrew one 1800 s cap: timed on the tower (load 15-35) it ran
# 2532 s, with the harden proof failing early rather than taking its
# ~815 s. So it runs in parts, each under its own 1800 s cap, as
# bin/chip-flow test splits the corpus step: `harden` (the real LibreLane
# harden tests), `charlib` (cell characterisation and the real optimise
# runs), and `rest` (every other slow file, so a new file is never left
# out). `check-slow.sh PART` runs one part; no argument runs every part.
# Measured there: harden ~640 s (~1320 s with the proof passing),
# charlib ~1220 s, rest ~670 s.
SHARDS="harden charlib rest"
shard_files() {
  case "$1" in
    harden) echo "tests/test_check_harden.py tests/test_check_top_harden.py" ;;
    charlib) echo "tests/test_charlib.py tests/test_optimise.py" ;;
    rest)
      printf 'tests/ '
      for f in $(shard_files harden) $(shard_files charlib); do
        printf -- '--ignore=%s ' "$f"
      done ;;
    *) return 1 ;;
  esac
}
if [ $# -gt 1 ] || { [ $# -eq 1 ] && ! shard_files "$1" >/dev/null; }; then
  echo "check-slow.sh: unknown part '$*'; name one of: $SHARDS," \
       "or none to run every part" >&2
  exit 2
fi
# Scratch in memory and fsync off: tests/lib/scratch.sh.
# shellcheck source=tests/lib/scratch.sh
. "$REPO_ROOT/tests/lib/scratch.sh"
test_scratch
if [ -n "$TEST_SCRATCH" ]; then trap 'rm -rf "$TEST_SCRATCH"' EXIT; fi
rc=0
for shard in ${1:-$SHARDS}; do
  start=$(date +%s)
  part_rc=0
  # shellcheck disable=SC2046
  timeout 1800 "$EDA_BIN" python -m pytest -q -p no:cacheprovider \
    --ignore=tests/test_corpus_faults.py -m "slow and not harden_faults" \
    $(shard_files "$shard") || part_rc=$?
  echo "check-slow.sh: $shard: $(($(date +%s) - start)) s of its 1800 s cap," \
       "exit $part_rc" >&2
  if [ "$part_rc" -ne 0 ] && [ "$rc" -eq 0 ]; then rc=$part_rc; fi
done
exit "$rc"

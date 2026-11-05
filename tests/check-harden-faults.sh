#!/usr/bin/env bash
# check-harden-faults.sh [RUNG] - the corpus rungs whose planted faults need
# full LibreLane hardens (tests/test_corpus_faults.py's HARDEN_RUNGS,
# marked `harden_faults`), run end to end through faults.py's run_rung.
# Split out of tests/check-slow.sh so their hardens (several per rung)
# never push that lane past its 1800 s cap. RUNG, when given, runs only
# that rung (a pytest -k match on its id, e.g. dac_tile); CI runs one job
# per rung (.github/workflows/harden-faults.yml). The cap catches a hang:
# CHIP_FLOW_HARDEN_FAULTS_TIMEOUT seconds, 3000 unless set.
#
# faults.py's run_rung writes each step it starts (the untouched
# reference's gates, then each fault by name) to $CHIP_FLOW_FAULTS_PROGRESS;
# this script streams that file as it grows and, on a timeout or a failure,
# names the step the rung was on, which pytest's own output (held until the
# test ends, and lost when timeout kills it) cannot (#196).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

eda_bin() {
  if [ -x "$REPO_ROOT/bin/eda" ]; then
    echo "$REPO_ROOT/bin/eda"
  elif [ -n "${EDA:-}" ] && [ -x "$EDA" ]; then
    echo "$EDA"
  else
    echo "check-harden-faults.sh: no ./bin/eda and \$EDA is not set to an " \
         "executable launcher" >&2
    exit 2
  fi
}

EDA_BIN="$(eda_bin)"
cap="${CHIP_FLOW_HARDEN_FAULTS_TIMEOUT:-3000}"
cd "$REPO_ROOT"
args=(tests/test_corpus_faults.py -v -rf -m harden_faults)
if [ -n "${1:-}" ]; then args+=(-k "$1"); fi

progress="$(mktemp "${TMPDIR:-/tmp}/harden-faults.XXXXXX")"
tail --pid=$$ -n +1 -f "$progress" | sed -u 's/^/faults: /' &
tailer=$!
trap 'kill "$tailer" 2>/dev/null || true; rm -f "$progress"' EXIT

rc=0
CHIP_FLOW_FAULTS_PROGRESS="$progress" timeout "$cap" "$EDA_BIN" python -m pytest "${args[@]}" || rc=$?
if [ "$rc" -ne 0 ]; then
  last="$(tail -n 1 "$progress")"
  if [ "$rc" -eq 124 ]; then
    why="timed out after ${cap}s"
  else
    why="failed (exit $rc)"
  fi
  echo "check-harden-faults.sh: ${1:-every harden rung} ${why}; last step started: ${last:-none (before the first rung step)}" >&2
fi
exit "$rc"

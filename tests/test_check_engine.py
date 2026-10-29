"""tests/check-engine.sh holds the suite to a CPU budget, not a wall clock.

Each case copies the script into its own repo root beside a stub bin/eda
that plays the suite: one that passes, one that fails, a planted slow suite
(burning CPU in children it reaps, and in a grandchild still running), a
planted hang (sleeping, no CPU) and a trickle (a little CPU every second,
for longer than the stall window). The budgets are shrunk through the
script's own env knobs so a case costs about a second of CPU. A slow suite
must come back 125 saying "slow", a hang 124 saying "hang", never the
other, and neither may leave its processes behind. A hang is the suite's
CPU not advancing, not a wall clock: the trickle outlives the stall window
and still passes, and the wall backstop is only there when asked for."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

STUB = r"""#!/usr/bin/env bash
echo "$*" > "$REPO_ROOT/args"
case "$STUB_MODE" in
  pass) exit 0 ;;
  fail) exit 1 ;;
  work)
    # a bounded bit of CPU in a reaped child, then a pass
    bash -c 'i=0; while [ $i -lt 300000 ]; do i=$((i+1)); done'; exit 0 ;;
  slow-reaped)
    # short CPU-bound children, each reaped: their time is in our cutime
    while :; do bash -c 'i=0; while [ $i -lt 100000 ]; do i=$((i+1)); done'; done ;;
  slow-live)
    # one CPU-bound grandchild that never exits
    bash -c 'while :; do :; done' &
    echo $! > "$REPO_ROOT/pid"; wait ;;
  hang)
    sleep 1000 &
    echo $! > "$REPO_ROOT/pid"; wait ;;
  trickle)
    # working slowly: a short burst of CPU each second, for 8 s
    for _ in 1 2 3 4 5 6 7 8; do
      bash -c 'i=0; while [ $i -lt 20000 ]; do i=$((i+1)); done'; sleep 1
    done; exit 0 ;;
esac
"""


def run_gate(tmp_path: Path, mode: str, cpu_s: int = 1, wall_s: int = 0,
             stall_s: int = 300):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy(REPO / "tests" / "check-engine.sh", root / "tests")
    shutil.copytree(REPO / "tests" / "lib", root / "tests" / "lib")
    eda = root / "bin" / "eda"
    eda.write_text(STUB)
    eda.chmod(0o755)
    env = dict(os.environ, STUB_MODE=mode, REPO_ROOT=str(root),
               CHECK_ENGINE_CPU_S=str(cpu_s), CHECK_ENGINE_WALL_S=str(wall_s),
               CHECK_ENGINE_STALL_S=str(stall_s), CHECK_ENGINE_POLL_S="1")
    proc = subprocess.run(["bash", str(root / "tests" / "check-engine.sh")],
                          env=env, capture_output=True, text=True, timeout=900)
    return proc, root


def gone(root: Path) -> bool:
    """The planted process is gone (a zombie awaiting init counts as gone)."""
    stat = Path("/proc") / (root / "pid").read_text().strip() / "stat"
    try:
        return stat.read_text().rsplit(") ", 1)[1].startswith("Z")
    except FileNotFoundError:
        return True


def test_pass_runs_the_not_slow_suite_and_reports_cpu(tmp_path):
    proc, root = run_gate(tmp_path, "pass", cpu_s=120)
    assert proc.returncode == 0, proc.stderr
    assert (root / "args").read_text().strip() == \
        "python -m pytest tests/ -q -m not slow"
    assert "CPU of its 120 s budget" in proc.stderr
    assert "FAIL" not in proc.stderr


def test_pass_counts_the_cpu_the_finished_suite_used(tmp_path):
    proc, _ = run_gate(tmp_path, "work", cpu_s=120)
    assert proc.returncode == 0, proc.stderr
    used = float(proc.stderr.split("suite used ")[1].split(" s CPU")[0])
    assert used > 0, proc.stderr


def test_pytest_failure_passes_through_as_itself(tmp_path):
    proc, _ = run_gate(tmp_path, "fail", cpu_s=120)
    assert proc.returncode == 1, proc.stderr
    assert "FAIL slow" not in proc.stderr and "FAIL hang" not in proc.stderr


def test_planted_slow_suite_fails_as_slow(tmp_path):
    proc, _ = run_gate(tmp_path, "slow-reaped")
    assert proc.returncode == 125, proc.stderr
    assert "FAIL slow" in proc.stderr and "hang" not in proc.stderr


def test_planted_slow_grandchild_is_counted_and_ended(tmp_path):
    proc, root = run_gate(tmp_path, "slow-live")
    assert proc.returncode == 125, proc.stderr
    assert "FAIL slow" in proc.stderr and "hang" not in proc.stderr
    assert gone(root)


def test_planted_hang_fails_as_hang_not_slow(tmp_path):
    proc, root = run_gate(tmp_path, "hang", cpu_s=120, stall_s=3)
    assert proc.returncode == 124, proc.stderr
    assert "FAIL hang" in proc.stderr and "slow" not in proc.stderr
    assert "stopped advancing for 3 s" in proc.stderr
    assert gone(root)


def test_suite_still_working_past_the_stall_window_is_not_a_hang(tmp_path):
    # 8 s of wall, CPU moving every second, under a 3 s stall window and
    # no wall backstop (the default): a slow box, not a hang.
    proc, _ = run_gate(tmp_path, "trickle", cpu_s=120, stall_s=3)
    assert proc.returncode == 0, proc.stderr
    assert "FAIL" not in proc.stderr


def test_wall_backstop_when_asked_for_still_ends_a_hang(tmp_path):
    proc, root = run_gate(tmp_path, "hang", cpu_s=120, wall_s=3, stall_s=600)
    assert proc.returncode == 124, proc.stderr
    assert "FAIL hang" in proc.stderr and "wall-clock backstop" in proc.stderr
    assert gone(root)


def test_bad_budget_is_an_error_not_a_pass(tmp_path):
    proc, _ = run_gate(tmp_path, "pass", cpu_s=0)
    assert proc.returncode == 2
    assert "positive whole seconds" in proc.stderr


def test_bad_stall_window_is_an_error_not_a_pass(tmp_path):
    proc, _ = run_gate(tmp_path, "pass", cpu_s=120, stall_s=0)
    assert proc.returncode == 2
    assert "positive whole seconds" in proc.stderr

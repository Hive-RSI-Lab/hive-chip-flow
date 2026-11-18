"""engine/lib/simrecord.py (#360): the per-test output record a passing sim
keeps and the first divergence a later failure is told. Hermetic: no
simulator; the real-sim round trip is in test_check_sim.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))

import simrecord  # noqa: E402
import wavelib  # noqa: E402

SPEC = {"top": "counter8", "clock": {"domains": ["clk"]},
        "ports": {"clk": {"dir": "input", "width": 1},
                  "rst": {"dir": "input", "width": 1},
                  "count": {"dir": "output", "width": 8},
                  "done": {"dir": "output", "width": 1}}}


def _bits(n, w=8):
    return format(n, f"0{w}b")


def counter_changes(t0_ps, n, wrap_at, period_ps=20000):
    """The monitor's lines for a counter test starting at t0_ps: one per
    edge (count changes every edge), done high on the wrap."""
    out, c = [], 0
    for i in range(n):
        c = 0 if c == wrap_at else c + 1
        out.append((t0_ps + (i + 1) * period_ps,
                    [_bits(c), "1" if c == 0 else "0"]))
    return out


def test_plan_takes_the_clock_and_every_output():
    p = simrecord.plan(SPEC)
    assert p == {"on": True, "clock": "clk",
                 "outputs": [("count", 8), ("done", 1)]}
    no_clk = {"ports": {"a": {"dir": "input"}, "y": {"dir": "output"}}}
    assert not simrecord.plan(no_clk)["on"]


def test_monitor_samples_after_the_edge_and_writes_on_change(tmp_path):
    src = simrecord.monitor_verilog(simrecord.plan(SPEC), "tt_counter8",
                                    tmp_path / "outputs.txt")
    assert f"module {simrecord.MODULE};" in src
    assert "always @(posedge tt_counter8.clk) tick <= ~tick;" in src
    assert "#0;" in src
    assert "{tt_counter8.count, tt_counter8.done} !== last" in src
    assert str((tmp_path / "outputs.txt").resolve()) in src


def test_read_changes_skips_lines_of_the_wrong_shape(tmp_path):
    f = tmp_path / "outputs.txt"
    f.write_text("20000 00000001 0\ngarbage\n40000 00000010\n"
                 "60000 xxxxxxxx z\n")
    assert simrecord.read_changes(f, 2) == [
        (20000, ["00000001", "0"]), (60000, ["xxxxxxxx", "z"])]
    assert simrecord.take(f, 2) and not f.exists()
    assert simrecord.read_changes(f, 2) == []


def test_slice_is_relative_to_the_test_start_and_carries_its_first_state():
    ch = [(10000, ["00000000", "0"]), (30000, ["00000001", "0"]),
          (50000, ["00000010", "0"]), (70000, ["00000011", "0"])]
    rows = simrecord.slice_test(ch, start_ns=40.0, stop_ns=70.0)
    assert rows == [[0, ["00000001", "0"]], [10000, ["00000010", "0"]],
                    [30000, ["00000011", "0"]]]
    assert simrecord.slice_test(ch, None, 70.0) is None


def test_first_divergence_is_the_wrap_edge():
    outs = simrecord.plan(SPEC)["outputs"]
    was = simrecord.slice_test(counter_changes(0, 300, 255), 0.0, 6000.0)
    now = simrecord.slice_test(counter_changes(0, 260, 254), 0.0, 5210.0)
    fd = simrecord.first_divergence(was, now, outs, 5210000)
    # edge 255 (t = 255 * 20 ns) is where the early wrap reads 0
    assert fd == {"test_ns": 5100.0, "signal": "count", "was": "255",
                  "now": "0"}


def test_agreeing_runs_and_x_values():
    outs = simrecord.plan(SPEC)["outputs"]
    rows = simrecord.slice_test(counter_changes(0, 50, 255), 0.0, 1000.0)
    assert simrecord.first_divergence(rows, rows, outs, 10 ** 9) is None
    x = [[0, ["xxxxxxxx", "0"]]]
    fd = simrecord.first_divergence([[0, [_bits(0), "0"]]], x, outs, 0)
    assert fd["was"] == "0" and fd["now"] == "xxxxxxxx", fd
    # a difference after the shorter run ended is not one
    late = [[0, [_bits(0), "0"]], [5000, [_bits(9), "0"]]]
    assert simrecord.first_divergence([[0, [_bits(0), "0"]]], late, outs,
                                      4000) is None


def test_save_load_keeps_only_the_same_bench_clock_and_outputs(tmp_path):
    p = simrecord.plan(SPEC)
    rows = [[0, [_bits(1), "0"]]]
    simrecord.save(tmp_path, "test_a", 7, p, "tbA", "rtlA", rows, 1000)
    body = simrecord.load(tmp_path, "test_a", 7, p, "tbA")
    assert body["rows"] == rows and body["rtl"] == "rtlA"
    assert body["duration_ps"] == 1000
    assert simrecord.load(tmp_path, "test_a", 7, p, "tbB") is None
    assert simrecord.load(tmp_path, "test_a", 8, p, "tbA") is None
    other = dict(p, outputs=[("count", 8)])
    assert simrecord.load(tmp_path, "test_a", 7, other, "tbA") is None
    path = simrecord.record_path(tmp_path, "test_a", 7)
    path.write_text("{not json")
    assert simrecord.load(tmp_path, "test_a", 7, p, "tbA") is None
    path.write_text(json.dumps({"version": 0}))
    assert simrecord.load(tmp_path, "test_a", 7, p, "tbA") is None


def test_digest_moves_with_any_file(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    d1 = simrecord.digest(tmp_path)
    (tmp_path / "a.v").write_text("module a; wire w; endmodule\n")
    assert simrecord.digest(tmp_path) != d1


def test_format_window_marks_the_divergence_row():
    win = {"signals": ["count"], "rows": [
        {"t_ns": 10.0, "count": "254"},
        {"t_ns": 20.0, "count": "0", "first_divergence": True},
        {"t_ns": 25.0, "count": "0", "at_failure": True}]}
    lines = wavelib.format_window(win).splitlines()
    assert lines[2].endswith("<- first divergence")
    assert lines[3].endswith("<- failure")
    win["rows"][2]["first_divergence"] = True
    assert wavelib.format_window(win).splitlines()[3].endswith(
        "<- failure, first divergence")

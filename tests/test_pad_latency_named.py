"""A bench that ignores the tile's pad reset latency fails by name: sim says
tb_ignores_pad_latency and holdout says holdout_ignores_pad_latency when a
test fails through the frame, passes driving the block directly, and the
bench never reads the latency (cocotblib.reads_pad_reset_latency). A real RTL
bug fails both runs and stays a plain failed test. The real-sim cases plant
a hand-driven reset in corpus/vde/counter8's own benches and run REAL
cocotb-on-Icarus."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import cluster_violations  # noqa: E402
import cocotblib  # noqa: E402
import faults  # noqa: E402
import gate as gate_mod  # noqa: E402

RUNG = REPO / "corpus" / "vde" / "counter8"
IMPORT = "from padreset import pad_reset\n"
# what a bench written for a bare block does: release reset and carry on,
# as if the block left reset on that edge
BY_HAND = '''
async def pad_reset(dut, rst, cycles):
    rst.value = 1
    for _ in range(cycles):
        await FallingEdge(dut.clk)
    rst.value = 0
'''


def write(d: Path, **modules: str) -> list[Path]:
    for name, text in modules.items():
        (d / f"{name}.py").write_text(text, encoding="utf-8")
    return [d]


@pytest.mark.parametrize("text", [
    "import padreset\n",
    "from padreset import pad_reset\n",
    "def f(dut):\n    return dut.tt_pad_reset_latency.value\n",
    "NET = 'tt_pad_reset_latency'\n",
])
def test_a_bench_that_reads_the_latency_is_seen(tmp_path, text):
    dirs = write(tmp_path, test_x=text)
    assert cocotblib.reads_pad_reset_latency(["test_x"], dirs)


def test_a_helper_module_the_bench_imports_counts(tmp_path):
    dirs = write(tmp_path, test_x="from common import reset\n",
                 common="from deeper import reset\n",
                 deeper="from padreset import pad_reset as reset\n")
    assert cocotblib.reads_pad_reset_latency(["test_x"], dirs)


@pytest.mark.parametrize("text", [
    "import cocotb\n",
    "# from padreset import pad_reset, tt_pad_reset_latency\nimport cocotb\n",
    "from common import reset\n",  # not on the search path: nothing to read
    "def broken(:\n",
])
def test_a_bench_that_does_not_read_it_is_seen(tmp_path, text):
    dirs = write(tmp_path, test_x=text)
    assert not cocotblib.reads_pad_reset_latency(["test_x"], dirs)


def test_a_helper_import_cycle_ends(tmp_path):
    dirs = write(tmp_path, test_x="import a\n", a="import b\n", b="import a\n")
    assert not cocotblib.reads_pad_reset_latency(["test_x"], dirs)


def test_both_kinds_route_to_the_writer_of_that_bench():
    assert cluster_violations.fixer_for("tb_ignores_pad_latency") == "testbench"
    assert cluster_violations.fixer_for(
        "holdout_ignores_pad_latency") == "holdout_stimulus"


def hand_reset_workspace(tmp_path: Path) -> Path:
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    for bench in (ws / "tb" / "test_counter8.py",
                  ws / "holdout" / "test_counter8_holdout.py"):
        text = bench.read_text(encoding="utf-8")
        assert IMPORT in text, bench
        bench.write_text(text.replace(IMPORT, BY_HAND), encoding="utf-8")
    return ws


def run(ws: Path, name: str) -> dict:
    row = gate_mod.load_gates(gate_mod.DEFAULT_GATES)["vde"][name]
    return gate_mod.evaluate(name, row, gate_mod.run_report_for_gate(row, ws))


def kinds(result: dict) -> set:
    return {v.get("kind") for v in result.get("failing") or []}


def test_a_tb_that_ignores_the_latency_fails_by_name(tmp_path):
    result = run(hand_reset_workspace(tmp_path), "sim")
    assert result["status"] == "fail"
    assert "tb_ignores_pad_latency" in kinds(result), result["failing"]
    named = [v for v in result["failing"]
             if v["kind"] == "tb_ignores_pad_latency"]
    assert all("padreset.pad_reset" in v["msg"] for v in named)


def test_a_holdout_that_ignores_the_latency_fails_by_name(tmp_path):
    result = run(hand_reset_workspace(tmp_path), "holdout")
    assert result["status"] == "fail"
    assert kinds(result) == {"holdout_ignores_pad_latency"}, result["failing"]
    for v in result["failing"]:
        assert v["msg"].startswith("a held-out test for requirement(s) REQ-")
        assert "test_" not in v["msg"]  # the fixer never sees a test name


def test_a_real_rtl_bug_under_such_a_bench_stays_a_failed_test(tmp_path):
    """the count is wrong through the pad and driving the block directly"""
    ws = hand_reset_workspace(tmp_path)
    rtl = ws / "rtl" / "counter8.v"
    good = "count <= count + 8'd1;"
    text = rtl.read_text(encoding="utf-8")
    assert good in text
    rtl.write_text(text.replace(good, "count <= count + 8'd2;"),
                   encoding="utf-8")
    result = run(ws, "sim")
    assert result["status"] == "fail"
    assert kinds(result) == {"test_failed"}, result["failing"]

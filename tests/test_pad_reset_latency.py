"""The tile's pad reset latency is a contract the benches honour exactly:
corpus/vde/counter8's, blocks/counter's and blocks/r2r_dac/digital's sim
and holdout run
through the generated wrapper (ttlib.write_sim_frame) and reset through
engine/lib/padreset.py, which waits ttlib.PAD_RESET_LATENCY edges after the
release. A wrapper whose synchroniser is one flop short or one flop long
must fail both, so a bench that only checked "within at most N" would not
pass here. r2r_dac's per-cycle model takes its reset chain from the same
latency, so its random test must be among the failures, and the same
benches still pass driving the block directly (latency 0). The wrapper's
synchroniser asserts reset asynchronously: one that asserts it on the clock
(a plain flop chain) must fail blocks/counter's test_async_reset, whose spec
clears the count with no clock edge. Every workspace in the repo whose tile
reads rst_n is in SWEEP, so a new one can't miss the sweep. Runs REAL
cocotb-on-Icarus."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import faults  # noqa: E402
import gate as gate_mod  # noqa: E402
import ttlib  # noqa: E402

import speclib  # noqa: E402

RUNG = REPO / "corpus" / "vde" / "counter8"
R2R = REPO / "blocks" / "r2r_dac" / "digital"
COUNTER = REPO / "blocks" / "counter"
R2R_RANDOM = "test_random_mode_switching_against_model"
COUNTER_ASYNC = "test_async_reset"
N = ttlib.PAD_RESET_LATENCY
Q = ttlib.PAD_RESET_SYNC_FLOP

# Every workspace whose tile reads the rst_n pad, and how a change to the
# pad reset is checked on it: "plants" runs the plants below, "sweep" is a
# row of the before/after sim+holdout+cdc table in the change's PR, "framed"
# runs through the frame by another route.
SWEEP = {
    "blocks/counter": "plants",
    "blocks/r2r_dac/digital": "plants",
    "corpus/vde/counter8": "plants",
    "blocks/pll/digital": "sweep (opt-out: pad_reset_sync off, #212)",
    "corpus/msde/dac_tile/digital": "sweep",
    "corpus/msde/sensor_counted/digital": "sweep",
    "corpus/vde/riscv": "sweep",
    "corpus/vde/spi_fifo": "sweep",
    "corpus/vde/uart": "sweep",
    "blocks/r2r_dac/top": "framed in its cosim bench (check_cosim, #208)",
    "evals/fixtures/P3/counter8_tb/ws": "framed in its bench (bench.py)",
    "evals/fixtures/P4/counter8_rtl/ws": "framed in its bench (bench.py)",
    "evals/fixtures/P4/uart_rtl/ws": "framed in its bench (bench.py)",
    "evals/fixtures/P4/uart_weak_spec/ws": "framed in its bench (bench.py)",
}


def _short(v: str) -> str:
    """the block's reset taken one flop early: latency N-1 (an active-high
    reset reads the chain inverted, an active-low one as it is)"""
    return (v.replace(f"= ~{Q}{N};", f"= ~{Q}{N - 1};")
             .replace(f"= {Q}{N};", f"= {Q}{N - 1};"))


def _long(v: str) -> str:
    """one extra flop in the chain: latency N+1"""
    return (v.replace(f"  reg {Q}1;\n", f"  reg {Q}1;\n  reg {Q}x;\n")
             .replace(f"      {Q}{N} <= 1'b0;\n",
                      f"      {Q}{N} <= 1'b0;\n      {Q}x <= 1'b0;\n")
             .replace(f"      {Q}{N} <= {Q}{N - 1};\n",
                      f"      {Q}x <= {Q}{N - 1};\n      {Q}{N} <= {Q}x;\n"))


def _sync_assert(v: str) -> str:
    """the same N flops as a plain chain on clk, so the assertion as well as
    the release waits N edges"""
    flops = [f"{Q}{i}" for i in range(1, N + 1)]
    real = "\n".join(ttlib._pad_reset_sync(N)[2 + N:]) + "\n"
    plain = "\n".join(["  always @(posedge clk) begin",
                       f"    {flops[0]} <= rst_n;",
                       *(f"    {b} <= {a};" for a, b in zip(flops, flops[1:])),
                       "  end"]) + "\n"
    return v.replace(real, plain)


def workspace(tmp_path: Path, block: str) -> Path:
    if block == "counter8":
        return faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    ws = tmp_path / "ws"
    shutil.copytree(R2R if block == "r2r_dac" else COUNTER, ws,
                    ignore=shutil.ignore_patterns("*.lock", "log", "release"))
    return ws


def run(tmp_path: Path, block: str) -> dict:
    """{gate: evaluate()'s result} for sim and holdout"""
    ws = workspace(tmp_path, block)
    rows = gate_mod.load_gates(gate_mod.DEFAULT_GATES)["vde"]
    out = {}
    for g in ("sim", "holdout"):
        report = gate_mod.run_report_for_gate(rows[g], ws)
        out[g] = gate_mod.evaluate(g, rows[g], report)
    return out


def statuses(results: dict) -> dict:
    return {g: r["status"] for g, r in results.items()}


BLOCKS = ["counter8", "r2r_dac", "counter"]


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
def test_passes_through_the_real_synchroniser(tmp_path, block):
    assert statuses(run(tmp_path, block)) == {"sim": "pass", "holdout": "pass"}


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
def test_the_same_benches_pass_driving_the_block_directly(tmp_path, monkeypatch,
                                                          block):
    monkeypatch.setattr(ttlib, "write_sim_frame", lambda *a, **k: None)
    assert statuses(run(tmp_path, block)) == {"sim": "pass", "holdout": "pass"}


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize("fn", [_short, _long], ids=["short", "long"])
def test_a_wrong_pad_latency_fails_sim_and_holdout(tmp_path, monkeypatch,
                                                   fn, block):
    plant(monkeypatch, fn)
    results = run(tmp_path, block)
    assert statuses(results) == {"sim": "fail", "holdout": "fail"}
    # these benches read the latency, so a wrong wrapper is a failed test,
    # never blamed on the bench
    assert {v.get("kind") for r in results.values() for v in r["failing"]
            } <= {"test_failed", "holdout_failed", "holdout_stimulus_fault"}
    if block == "r2r_dac":
        assert R2R_RANDOM in failed_tests(results), failed_tests(results)


def failed_tests(results: dict) -> set:
    return {f.get("module") for f in results["sim"].get("failing") or []}


def plant(monkeypatch, fn):
    real = ttlib.generate_tt_wrapper

    def planted(spec, *a, **k):
        v = real(spec, *a, **k)
        out = fn(v)
        assert out != v, "plant did not apply"
        return out
    monkeypatch.setattr(ttlib, "generate_tt_wrapper", planted)


@pytest.mark.slow
def test_a_synchronous_assert_fails_the_async_reset_test(tmp_path,
                                                         monkeypatch):
    plant(monkeypatch, _sync_assert)
    results = run(tmp_path, "counter")
    assert results["sim"]["status"] == "fail"
    assert COUNTER_ASYNC in failed_tests(results), failed_tests(results)


def test_the_plants_apply_to_the_real_wrapper():
    v = ttlib.generate_tt_wrapper(speclib.load_spec(COUNTER / "spec"
                                                    / "spec.yaml"))
    for fn in (_short, _long, _sync_assert):
        assert fn(v) != v, fn.__name__
    assert "negedge rst_n" not in _sync_assert(v)


def _reads_the_pad(spec: dict) -> bool:
    for pin in (spec.get("tt_pins") or {}).values():
        try:
            if ttlib.parse_pin_expr(pin)["base"] == "rst_n":
                return True
        except ttlib.TTError:
            pass
    return False


def test_every_tile_that_reads_rst_n_is_in_the_sweep():
    found = set()
    for p in REPO.rglob("spec.yaml"):
        rel = p.relative_to(REPO)
        if rel.parts[0] not in ("blocks", "corpus", "evals") or \
                {"log", "release", "faults"} & set(rel.parts):
            continue
        spec = speclib.load_spec(p)
        if isinstance(spec, dict) and _reads_the_pad(spec):
            ws = rel.parent.parent if rel.parent.name == "spec" else rel.parent
            found.add(ws.as_posix())
    assert found == set(SWEEP), (
        f"not in SWEEP: {sorted(found - set(SWEEP))}; "
        f"in SWEEP but gone: {sorted(set(SWEEP) - found)}")

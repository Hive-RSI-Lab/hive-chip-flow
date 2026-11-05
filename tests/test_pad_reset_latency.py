"""The tile's pad reset latency is a contract the benches honour exactly:
corpus/vde/counter8's and blocks/r2r_dac/digital's sim and holdout run
through the generated wrapper (ttlib.write_sim_frame) and reset through
engine/lib/padreset.py, which waits ttlib.PAD_RESET_LATENCY edges after the
release. A wrapper whose synchroniser is one flop short or one flop long
must fail both, so a bench that only checked "within at most N" would not
pass here. r2r_dac's per-cycle model takes its reset chain from the same
latency, so its random test must be among the failures, and the same
benches still pass driving the block directly (latency 0). Runs REAL
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

RUNG = REPO / "corpus" / "vde" / "counter8"
R2R = REPO / "blocks" / "r2r_dac" / "digital"
R2R_RANDOM = "test_random_mode_switching_against_model"
N = ttlib.PAD_RESET_LATENCY
Q = ttlib.PAD_RESET_SYNC_FLOP


def _short(v: str) -> str:
    """the block's reset taken one flop early: latency N-1 (an active-high
    reset reads the chain inverted, an active-low one as it is)"""
    return (v.replace(f"= ~{Q}{N};", f"= ~{Q}{N - 1};")
             .replace(f"= {Q}{N};", f"= {Q}{N - 1};"))


def _long(v: str) -> str:
    """one extra flop in the chain: latency N+1"""
    return (v.replace(f"  reg {Q}1;\n", f"  reg {Q}1;\n  reg {Q}x;\n")
             .replace(f"    {Q}{N} <= {Q}{N - 1};\n",
                      f"    {Q}x <= {Q}{N - 1};\n    {Q}{N} <= {Q}x;\n"))


def workspace(tmp_path: Path, block: str) -> Path:
    if block == "counter8":
        return faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    ws = tmp_path / "ws"
    shutil.copytree(R2R, ws, ignore=shutil.ignore_patterns("*.lock"))
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


BLOCKS = ["counter8", "r2r_dac"]


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
def test_passes_through_the_real_synchroniser(tmp_path, block):
    assert statuses(run(tmp_path, block)) == {"sim": "pass", "holdout": "pass"}


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
def test_the_same_benches_pass_driving_the_block_directly(tmp_path, block):
    with ttlib.block_level_sim("test: no pad in front of the block"):
        assert statuses(run(tmp_path, block)) == {"sim": "pass",
                                                  "holdout": "pass"}


@pytest.mark.slow
@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize("plant", [_short, _long], ids=["short", "long"])
def test_a_wrong_pad_latency_fails_sim_and_holdout(tmp_path, monkeypatch,
                                                   plant, block):
    real = ttlib.generate_tt_wrapper

    def planted(spec, *a, **k):
        v = real(spec, *a, **k)
        out = plant(v)
        assert out != v, "plant did not apply"
        return out
    monkeypatch.setattr(ttlib, "generate_tt_wrapper", planted)
    results = run(tmp_path, block)
    assert statuses(results) == {"sim": "fail", "holdout": "fail"}
    if block == "r2r_dac":
        failed = {f.get("module") for f in results["sim"].get("failing") or []}
        assert R2R_RANDOM in failed, failed

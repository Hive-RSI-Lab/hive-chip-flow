"""The tile's pad reset latency is a contract the benches honour exactly:
corpus/vde/counter8's sim and holdout run through the generated wrapper
(ttlib.write_sim_frame) and reset through engine/lib/padreset.py, which
waits ttlib.PAD_RESET_LATENCY edges after the release. A wrapper whose
synchroniser is one flop short or one flop long must fail both, so a bench
that only checked "within at most N" would not pass here. Runs REAL
cocotb-on-Icarus."""
from __future__ import annotations

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
N = ttlib.PAD_RESET_LATENCY
Q = ttlib.PAD_RESET_SYNC_FLOP


def _short(v: str) -> str:
    """the block's reset taken one flop early: latency N-1"""
    return v.replace(f"= ~{Q}{N};", f"= ~{Q}{N - 1};")


def _long(v: str) -> str:
    """one extra flop in the chain: latency N+1"""
    return (v.replace(f"  reg {Q}1;\n", f"  reg {Q}1;\n  reg {Q}x;\n")
             .replace(f"    {Q}{N} <= {Q}{N - 1};\n",
                      f"    {Q}x <= {Q}{N - 1};\n    {Q}{N} <= {Q}x;\n"))


def statuses(tmp_path: Path) -> dict:
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    rows = gate_mod.load_gates(gate_mod.DEFAULT_GATES)["vde"]
    out = {}
    for g in ("sim", "holdout"):
        report = gate_mod.run_report_for_gate(rows[g], ws)
        out[g] = gate_mod.evaluate(g, rows[g], report)["status"]
    return out


@pytest.mark.slow
def test_counter8_passes_through_the_real_synchroniser(tmp_path):
    assert statuses(tmp_path) == {"sim": "pass", "holdout": "pass"}


@pytest.mark.slow
@pytest.mark.parametrize("plant", [_short, _long], ids=["short", "long"])
def test_a_wrong_pad_latency_fails_sim_and_holdout(tmp_path, monkeypatch, plant):
    real = ttlib.generate_tt_wrapper

    def planted(spec, *a, **k):
        v = real(spec, *a, **k)
        out = plant(v)
        assert out != v, "plant did not apply"
        return out
    monkeypatch.setattr(ttlib, "generate_tt_wrapper", planted)
    assert statuses(tmp_path) == {"sim": "fail", "holdout": "fail"}

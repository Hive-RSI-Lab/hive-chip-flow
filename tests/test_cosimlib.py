"""engine/lib/cosimlib.py: cocotbext-ams's bridge with the sync-rounding
and write-on-change fixes (found on the PLL's transistor cosim). The fast
tests drive the two overridden methods with stand-ins - no ngspice, no
simulator; the slow one runs corpus/msde/ring_osc_div's bench on
CosimBridge, at a 1 ns sync, through the cosim gate."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

pytest.importorskip("cocotbext.ams")

import cosimlib  # noqa: E402
from cocotbext.ams._ngspice import NgspiceInterface  # noqa: E402


def _ngspice(cls, next_sync: float):
    """An NgspiceInterface of class `cls` with only what the sync callback
    reads: no shared library is loaded."""
    sim = object.__new__(cls)
    sim._simulation_done = False
    sim._crossing_detected = False
    sim._next_sync_time = next_sync
    sim._spice_time = 0.0
    sim.synced = []
    sim._on_sync_point = lambda: sim.synced.append(sim._spice_time)
    return sim


def _sync(sim, ckttime: float, step: float) -> list[float]:
    p_delta = [step]
    sim._on_get_sync_data(ckttime, p_delta, step, 0, 0, 0, None)
    return p_delta


# a float rounding short of a 4 ns sync point: the remainder is ~4e-25 s
SHORT = 3.9999999999999996e-09


def test_the_stock_callback_hands_ngspice_a_sub_femtosecond_step():
    # the bug the fix is for: ngspice is clamped to ~1e-25 s and aborts
    # with "Timestep too small"
    sim = _ngspice(NgspiceInterface, 4e-9)
    p_delta = _sync(sim, SHORT, 1e-10)
    assert 0 < p_delta[0] < 1e-20
    assert sim.synced == []


def test_a_remainder_under_a_femtosecond_counts_as_the_sync_point():
    sim = _ngspice(cosimlib.SyncSafeNgspice, 4e-9)
    p_delta = _sync(sim, SHORT, 1e-10)
    assert sim.synced == [SHORT]
    assert p_delta == [1e-10]


def test_a_remainder_past_a_femtosecond_is_still_clamped():
    sim = _ngspice(cosimlib.SyncSafeNgspice, 4e-9)
    p_delta = _sync(sim, 3.99e-9, 1e-10)
    assert sim.synced == []
    assert p_delta[0] == pytest.approx(1e-11)


def test_ref_grid_adds_a_sync_just_after_each_reference_edge():
    sim = _ngspice(cosimlib.SyncSafeNgspice, 100e-9)
    sim.ref_grid = (0.0, 160e-9, 2e-12)
    assert sim.next_ref_sync(10e-9) == pytest.approx(160.002e-9)
    p_delta = _sync(sim, 159.5e-9, 1e-9)
    assert sim._next_sync_time == pytest.approx(100e-9)  # sooner one stands
    sim._next_sync_time = 1e-6
    p_delta = _sync(sim, 159.5e-9, 1e-9)
    assert p_delta[0] == pytest.approx(0.502e-9)
    # at the edge's own sync, the next one is a period on
    assert sim.next_ref_sync(160.002e-9) == pytest.approx(320.002e-9)


class _Pin:
    direction = "output"

    def analog_to_digital(self, volts, prev_value=None):
        return int(volts[0] > 1.65)


class _Signal:
    def __init__(self):
        self.writes = []

    @property
    def value(self):
        return self.writes[-1]

    @value.setter
    def value(self, v):
        self.writes.append(v)


def test_an_analog_output_is_written_once_per_change_and_never_forced():
    bridge = object.__new__(cosimlib.CosimBridge)
    bridge._written = {}
    bridge._block_output_nodes = {"osc": {"out": ["x1.out"]}}
    volts = {"x1.out": 0.0}
    sim = type("Sim", (), {})()
    sim.get_node_voltage = lambda n: volts[n]
    sim._prev_digital_values = {}
    bridge._sim = sim
    sig = _Signal()
    bridge._resolve_signal = lambda block, pin: sig
    block = type("Block", (), {"name": "osc",
                               "digital_pins": {"out": _Pin()}})()
    for v in (0.0, 0.1, 3.3, 3.2, 3.3, 0.0, 0.0):
        volts["x1.out"] = v
        bridge._read_analog_outputs(block)
    assert sig.writes == [0, 1, 0]
    assert all(isinstance(w, int) for w in sig.writes)
    bridge._release_outputs(block)  # nothing forced, nothing to release
    assert sig.writes == [0, 1, 0]


@pytest.mark.slow
def test_ring_osc_div_passes_cosim_on_cosim_bridge_at_a_1ns_sync(
        tmp_path, capsys):
    """The ring_osc_div bench on CosimBridge, its osc_out a reg, syncing
    every 1 ns (700 syncs - the stock bridge Forces osc_out at each one,
    what crashed vvp on the PLL), imported the way any bench imports it:
    from the cosim gate's own engine/lib."""
    import check_cosim

    rung = REPO / "corpus" / "msde" / "ring_osc_div"
    ws = tmp_path / "ws"
    (ws / "log").mkdir(parents=True)
    (ws / "reports").mkdir(parents=True)
    shutil.copytree(rung / "tb", ws / "tb")
    shutil.copy2(rung / "interface.yaml", ws / "interface.yaml")
    top = ws / "tb" / "ring_osc_div_top.v"
    src = top.read_text(encoding="utf-8")
    assert src.count("output wire osc_out") == 1
    top.write_text(src.replace("output wire osc_out", "output reg  osc_out"),
                   encoding="utf-8")
    bench = ws / "tb" / "test_ring_osc_div.py"
    src = bench.read_text(encoding="utf-8")
    src, n = re.subn(r"bridge = MixedSignalBridge\(\s*dut, \[block\], "
                     r"max_sync_interval_ns=20\.0,",
                     "bridge = CosimBridge(dut, [block], "
                     "max_sync_interval_ns=1.0,", src)
    assert n == 1
    stock = "from cocotbext.ams import AnalogBlock, DigitalPin, MixedSignalBridge\n"
    assert src.count(stock) == 1
    bench.write_text(src.replace(
        stock, stock + "from cosimlib import CosimBridge\n"), encoding="utf-8")

    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["measures"]["digital_toggles"] >= 8
    assert not out["ngspice_signatures"]

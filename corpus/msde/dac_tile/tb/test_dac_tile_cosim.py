"""Cosim bench for dac_tile: the digital side (Icarus), inside its TT frame,
drives bmsb/blsb into the 2-bit ladder simulated by ngspice through
cosimlib's CosimBridge (ideal switches, real ladder:
dac_tile_ideal.sp).

The frame puts the pad reset synchroniser in front of rst_n, so the code
register stays in reset for latency(dut.digital) clocks after the bench
releases rst_n. The bench waits that out, then checks the first code it
loads comes straight through: a bench that ignores the latency loads into
a register still held in reset, and code_mismatches says so.

Measures, with bounds from the specs, not the design:
  code_mismatches = 0           [digital REQ-LOAD: the code register takes
      code_in at each clock]
  vout_00 in [2.45, 3.25], vout_01 in [1.85, 2.52],
  vout_10 in [0.48, 0.72], vout_11 <= 0.05   [analog spec's own bounds]
  monotonic_falling = 1         [vout is high for bits low, spec.md]
reports/cosim_measures.json is written before any assertion can raise.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer, ValueChange
from cocotb.utils import get_sim_time
from cocotbext.ams import AnalogBlock, DigitalPin
from cosimlib import CosimBridge
from padreset import latency

HERE = Path(__file__).resolve().parent
LIBNGSPICE = os.environ["LIBNGSPICE_PATH"]

VDD = 3.3
CLK_NS = 20
RESET_CYCLES = 4
CODE_HOLD_NS = 200      # ~ 30 tau of the 5k x 1 pF pad pole
TOGGLES = 3
SYNC_NS = 20.0
DURATION_NS = (RESET_CYCLES + 12) * CLK_NS \
    + (4 + 2 * TOGGLES) * (CODE_HOLD_NS + 2 * CLK_NS) + 500


async def load_code(dut, code: int) -> None:
    """Present `code` on code_in; the register takes it at the next rising
    edge (REQ-LOAD)."""
    await FallingEdge(dut.clk)
    dut.code_in.value = code
    await RisingEdge(dut.clk)


def loaded(dut) -> int:
    return (int(dut.bmsb.value) << 1) | int(dut.blsb.value)


@cocotb.test()
async def test_dac_tile_cosim(dut):
    dut.rst_n.value = 0
    dut.code_in.value = 3
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())

    block = AnalogBlock(
        name="dac",
        spice_file=str(HERE / "dac_tile_ideal.sp"),
        subcircuit="dac_tile_ideal",
        digital_pins={
            "code": DigitalPin(direction="input", width=2, vdd=VDD, vss=0.0),
            "vout": DigitalPin(direction="output", vdd=VDD, vss=0.0,
                               hysteresis=0.05),
        },
        vdd=VDD,
        vss=0.0,
        tran_step="1n",
    )
    bridge = CosimBridge(
        dut, [block], max_sync_interval_ns=SYNC_NS, simulator_lib=LIBNGSPICE)
    sim_task = cocotb.start_soon(bridge.start(duration_ns=DURATION_NS))

    edges_ns: list[float] = []

    async def watch_vout_sync():
        prev = None
        while True:
            await ValueChange(dut.vout_sync)
            if not dut.vout_sync.value.is_resolvable:
                continue
            cur = int(dut.vout_sync.value)
            if prev is not None and cur != prev:
                edges_ns.append(float(get_sim_time(unit="ns")))
            prev = cur

    cocotb.start_soon(watch_vout_sync())

    vcode: list[float] = []
    mismatches = 0
    measures: dict = {}
    try:
        for _ in range(RESET_CYCLES):
            await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        # rst_n reaches the code register through the tile's pad reset
        # synchroniser (the frame instance `digital`): wait out its latency.
        for _ in range(latency(dut.digital)):
            await FallingEdge(dut.clk)

        # The first load after reset must come straight through.
        await load_code(dut, 3)
        await FallingEdge(dut.clk)
        if loaded(dut) != 3:
            mismatches += 1

        # Every code, held to settle, then sampled.
        for code in range(4):
            await load_code(dut, code)
            await Timer(CODE_HOLD_NS, unit="ns")
            if loaded(dut) != code:
                mismatches += 1
            vcode.append(bridge.get_analog_voltage("dac", "vout"))

        # Full-scale steps: the digital side's toggle evidence.
        for _ in range(TOGGLES):
            for code in (0, 3):
                await load_code(dut, code)
                await Timer(CODE_HOLD_NS, unit="ns")
    finally:
        if len(vcode) == 4:
            measures.update({
                "vout_00": vcode[0], "vout_01": vcode[1],
                "vout_10": vcode[2], "vout_11": vcode[3],
                "monotonic_falling": int(all(vcode[k] < vcode[k - 1]
                                             for k in range(1, 4))),
            })
        measures.update({
            "code_mismatches": mismatches,
            "codes_sampled": len(vcode),
            "digital_toggles": len(edges_ns),
            "edge_times_ns": list(edges_ns),
            "spice_time_reached_ns": bridge._last_sync_spice_time * 1e9,
            "analog_model": "dac_tile_ideal.sp (ideal switches, nominal 5k/10k ladder, 1 pF pad)",
        })
        reports_dir = HERE.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "cosim_measures.json").write_text(
            json.dumps(measures, indent=2), encoding="utf-8")
        await sim_task
        await bridge.stop()

    assert len(vcode) == 4
    assert mismatches == 0, f"{mismatches} code(s) did not load from code_in"
    assert measures["monotonic_falling"] == 1
    assert edges_ns, "vout_sync never toggled"

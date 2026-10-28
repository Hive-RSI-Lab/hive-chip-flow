"""Cosim bench for r2r_dac: the released digital front end (Icarus) drives
dac_code into the R-2R ladder simulated by ngspice through cocotbext-ams's
MixedSignalBridge (behavioral drivers, real resistor ladder: r2r_ideal.sp).

Measures, with bounds from the brief (../brief/spec.md), not the design:
  dnl_max_abs, inl_max_abs <= 0.5 LSB, monotonic = 1   [brief targets]
  fs_gain_err_lsb in +-0.5 LSB                         [brief asks for the
      full-scale range; bound = the brief's own 0.5 LSB accuracy target,
      as analog/spec/spec.yaml derives it]
  t_settle_rise/fall <= 1.5 us to 0.5 LSB              [brief said one
      50 MHz period; the owner's 2026-09-25 round-2 decision, recorded in
      analog/spec/spec.yaml, replaced it with 1.5 us]
  dac_code_mismatches = 0                              [join check: in mode
      00 dac_code is the ui_in loaded the edge before (digital REQ-CAPTURE)]

The DNL/INL sweep loads every code through ui_in in register mode and
samples the ngspice vout after the code has settled. Settling uses full-
scale 0 <-> 255 steps, timed from the clk edge that changed dac_code, in
ngspice's own time. reports/cosim_measures.json is written before any
assertion can raise.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer, ValueChange
from cocotb.utils import get_sim_time
from cocotbext.ams import AnalogBlock, DigitalPin, MixedSignalBridge

HERE = Path(__file__).resolve().parent
LIBNGSPICE = os.environ["LIBNGSPICE_PATH"]

VDD = 3.3
LSB = VDD / 256.0
CLK_NS = 20
RESET_CYCLES = 5
CODE_HOLD_NS = 1000      # 1-LSB step: ~5 tau of the 190 ns pad pole
STEP_HOLD_NS = 2500      # full-scale step: > the 1.5 us bound
STEP_REPEATS = 3
POLL_NS = 10
SYNC_NS = 20.0
DURATION_NS = (RESET_CYCLES + 4) * CLK_NS + 256 * (CODE_HOLD_NS + 2 * CLK_NS) \
    + 2 * STEP_REPEATS * (STEP_HOLD_NS + 2 * CLK_NS) + 2000


def spice_ns(bridge) -> float:
    return bridge._sim._spice_time * 1e9


async def load_code(dut, code: int) -> float:
    """Present `code` on ui_in and return the time of the clk edge that
    loads it (mode 00: dac_code takes ui_in at the next rising edge)."""
    await FallingEdge(dut.clk)
    dut.ui_in.value = code
    await RisingEdge(dut.clk)
    return float(get_sim_time(unit="ns"))


def settle_time_ns(trace, t0_ns, v_final):
    """Time from t0 to the last sample outside +-0.5 LSB of v_final."""
    last_out = t0_ns
    for t, v in trace:
        if t >= t0_ns and abs(v - v_final) > 0.5 * LSB:
            last_out = t
    return last_out - t0_ns


@cocotb.test()
async def test_r2r_dac_cosim(dut):
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.mode.value = 0
    dut.slow.value = 0
    dut.ui_in.value = 0
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())

    block = AnalogBlock(
        name="r2r",
        spice_file=str(HERE / "r2r_ideal.sp"),
        subcircuit="r2r_dac_ideal",
        digital_pins={
            "dac_code": DigitalPin(direction="input", width=8, vdd=VDD, vss=0.0),
            "vout": DigitalPin(direction="output", vdd=VDD, vss=0.0,
                               hysteresis=0.05),
        },
        vdd=VDD,
        vss=0.0,
        tran_step="1n",
    )
    bridge = MixedSignalBridge(
        dut, [block], max_sync_interval_ns=SYNC_NS, simulator_lib=LIBNGSPICE)
    sim_task = cocotb.start_soon(bridge.start(duration_ns=DURATION_NS))

    edges_ns: list[float] = []

    async def watch_vout_sync():
        # Only a resolved 0<->1 change counts; the X->0 power-up settle at
        # t=0 is not something the analog side did.
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
    rise_ns: list[float] = []
    fall_ns: list[float] = []
    measures: dict = {}
    try:
        for _ in range(RESET_CYCLES):
            await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1

        # DNL / INL / monotonic / full scale: every code through ui_in.
        for code in range(256):
            await load_code(dut, code)
            await Timer(CODE_HOLD_NS, unit="ns")
            if int(dut.dac_code.value) != code:
                mismatches += 1
            vcode.append(bridge.get_analog_voltage("r2r", "vout"))

        # Full-scale settling, both directions, in ngspice time.
        for _ in range(STEP_REPEATS):
            for target, sink in ((255, rise_ns), (0, fall_ns)):
                t0 = await load_code(dut, target)
                trace = []
                t_end = t0 + STEP_HOLD_NS
                while float(get_sim_time(unit="ns")) < t_end:
                    await Timer(POLL_NS, unit="ns")
                    trace.append((spice_ns(bridge),
                                  bridge.get_analog_voltage("r2r", "vout")))
                if int(dut.dac_code.value) != target:
                    mismatches += 1
                sink.append(settle_time_ns(trace, t0, trace[-1][1]))
    finally:
        if len(vcode) == 256:
            lsb_ep = (vcode[255] - vcode[0]) / 255.0
            dnl = [(vcode[k] - vcode[k - 1]) / lsb_ep - 1.0
                   for k in range(1, 256)]
            inl = [(vcode[k] - (vcode[0] + k * lsb_ep)) / lsb_ep
                   for k in range(256)]
            measures.update({
                "dnl_max_abs": max(abs(x) for x in dnl),
                "inl_max_abs": max(abs(x) for x in inl),
                "monotonic": int(all(vcode[k] > vcode[k - 1]
                                     for k in range(1, 256))),
                "v_zero": vcode[0],
                "v_full_scale": vcode[255],
                "fs_gain_err_lsb": ((vcode[255] - vcode[0])
                                    - 255.0 / 256.0 * VDD) / LSB,
            })
        if rise_ns:
            measures["t_settle_rise"] = max(rise_ns) * 1e-9
        if fall_ns:
            measures["t_settle_fall"] = max(fall_ns) * 1e-9
        measures.update({
            "dac_code_mismatches": mismatches,
            "codes_sampled": len(vcode),
            "digital_toggles": len(edges_ns),
            "edge_times_ns": list(edges_ns),
            "spice_time_reached_ns": bridge._last_sync_spice_time * 1e9,
            "analog_model": "r2r_ideal.sp (ideal drivers, nominal 27k ladder, 100 ohm + 7 pF pad)",
        })
        reports_dir = HERE.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "cosim_measures.json").write_text(
            json.dumps(measures, indent=2), encoding="utf-8")
        await sim_task
        await bridge.stop()

    assert len(vcode) == 256
    assert mismatches == 0, f"{mismatches} dac_code value(s) differed from ui_in"
    assert measures["monotonic"] == 1
    assert edges_ns, "vout_sync never toggled"

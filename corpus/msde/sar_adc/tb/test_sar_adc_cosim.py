"""Cosim bench for sar_adc: the SAR logic (Icarus, sar_adc_digital.v) runs
its successive approximation against the S/H, DAC and comparator simulated
by ngspice through cosimlib's CosimBridge (sar_adc_ideal.sp), on a sine
input, and the bench measures the converter's dynamic performance from the
codes it returns.

The SAR converts back to back, CONV_CLOCKS clocks per conversion, so it
samples at fs = 1 / (CONV_CLOCKS * CLK_NS). The tone frequency is the .sp's
own `.param fin` (one source of truth); convlib.dynamic refuses a record
whose tone is not coherent with fs and N, and the bench then reports no
enob, which fails the bound.

Measures, bounds in sar_adc.bounds.json:
  conversions >= 64        the record convlib measures
  enob >= 7.5              an ideal 8-bit SAR gives ~8.1 on this record;
      a DAC weight 3% off (the plant cosim_dac_msb_weight_low_enob) ~6.4
  sndr, snr, sfdr (dB), thd (dBc)   reported, not bounded
reports/cosim_measures.json is written before any assertion can raise.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer, ValueChange
from cocotb.utils import get_sim_time
from cocotbext.ams import AnalogBlock, DigitalPin
from convlib import Refused, coherent_tone, dynamic, dynamic_measures
from cosimlib import CosimBridge

HERE = Path(__file__).resolve().parent
LIBNGSPICE = os.environ["LIBNGSPICE_PATH"]
NETLIST = HERE / "sar_adc_ideal.sp"

VDD = 3.3
NBITS = 8
CLK_NS = 50
CONV_CLOCKS = 4 + 2 * NBITS          # TRACK + SET/CMP per bit
FS = 1e9 / (CONV_CLOCKS * CLK_NS)    # 1 MHz
N = 64
WARMUP = 1                           # the first conversion is discarded
RESET_CYCLES = 4
SYNC_NS = 10.0
CLOCK_OFFSET_NS = 5                  # clock edges fall between sync points
DURATION_NS = CLOCK_OFFSET_NS + (RESET_CYCLES + 2) * CLK_NS \
    + (N + WARMUP + 1) * CONV_CLOCKS * CLK_NS + 500


def netlist_fin() -> float:
    m = re.search(r"^\.param\b.*\bfin\s*=\s*([0-9.eE+-]+)",
                  NETLIST.read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        raise ValueError(f"{NETLIST.name}: no `.param fin=`")
    return float(m.group(1))


@cocotb.test()
async def test_sar_adc_cosim(dut):
    dut.rst_n.value = 0
    dut.start.value = 0

    block = AnalogBlock(
        name="sar",
        spice_file=str(NETLIST),
        subcircuit="sar_adc_ideal",
        digital_pins={
            "dac_code": DigitalPin(direction="input", width=NBITS, vdd=VDD,
                                   vss=0.0),
            "sample": DigitalPin(direction="input", vdd=VDD, vss=0.0),
            "comp_clk": DigitalPin(direction="input", vdd=VDD, vss=0.0),
            "cmp_out": DigitalPin(direction="output", vdd=VDD, vss=0.0,
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

    async def watch_cmp_out():
        prev = None
        while True:
            await ValueChange(dut.cmp_out)
            if not dut.cmp_out.value.is_resolvable:
                continue
            cur = int(dut.cmp_out.value)
            if prev is not None and cur != prev:
                edges_ns.append(float(get_sim_time(unit="ns")))
            prev = cur

    cocotb.start_soon(watch_cmp_out())

    codes: list[int] = []
    measures: dict = {}
    refused = None
    fin = netlist_fin()
    try:
        await Timer(CLOCK_OFFSET_NS, unit="ns")
        cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
        for _ in range(RESET_CYCLES):
            await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        await FallingEdge(dut.clk)
        dut.start.value = 1
        while len(codes) < WARMUP + N:
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            if int(dut.done.value) == 1:
                codes.append(int(dut.result.value))
        dut.start.value = 0
    finally:
        record = codes[WARMUP:WARMUP + N]
        if len(record) == N:
            try:
                measures.update(dynamic_measures(
                    dynamic(record, FS, fin, NBITS)))
            except Refused as e:
                refused = str(e)
                measures["dynamic_refused"] = refused
        measures.update({
            "conversions": len(record),
            "fs_hz": FS,
            "fin_hz": fin,
            "coherent_fin_hz": coherent_tone(FS, N, fin)["fin"],
            "codes": list(record),
            "digital_toggles": len(edges_ns),
            "edge_times_ns": list(edges_ns),
            "spice_time_reached_ns": bridge._last_sync_spice_time * 1e9,
            "analog_model": "sar_adc_ideal.sp (ideal S/H, binary-weighted "
                            "DAC, strobed comparator)",
        })
        reports_dir = HERE.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "cosim_measures.json").write_text(
            json.dumps(measures, indent=2), encoding="utf-8")
        await sim_task
        await bridge.stop()

    assert len(record) == N, f"{len(record)} of {N} conversions"
    assert refused is None, refused
    assert edges_ns, "cmp_out never toggled"

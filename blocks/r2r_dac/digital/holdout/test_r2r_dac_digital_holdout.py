"""Held-out tests for r2r_dac_digital (never given to the rtl-writer). Edge
cases the visible tests do not isolate: a one-clock reset mid-run in slow
sine mode must clear the prescaler too, and a hold after a ramp/sine excursion
that started from a captured 0xFF."""
import math

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

SINE = [int(math.floor(127.5 + 127.5 * math.sin(2 * math.pi * i / 256) + 0.5)) for i in range(256)]


# req: REQ-RESET REQ-SLOW
@cocotb.test()
async def test_one_clock_reset_clears_prescaler(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.mode.value = 2
    dut.slow.value = 1
    dut.rst_n.value = 0
    for _ in range(3):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(100):
        await FallingEdge(dut.clk)
    assert int(dut.dac_code.value) == SINE[0]
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)  # rst_n is registered: the reset acts on the next edge
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)
    assert int(dut.dac_code.value) == 0
    seq = []
    for _ in range(260):
        await FallingEdge(dut.clk)
        seq.append(int(dut.dac_code.value))
    assert seq[:256] == [SINE[0]] * 256, "prescaler not cleared by a one-clock reset"
    assert seq[256] == SINE[1]


# req: REQ-HOLD
@cocotb.test()
async def test_hold_after_excursion_from_ff(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 0
    dut.slow.value = 0
    dut.mode.value = 0
    dut.ui_in.value = 0
    dut.rst_n.value = 0
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)  # rst_n is registered: release acts one clk late
    dut.ui_in.value = 0xFF
    await FallingEdge(dut.clk)
    dut.mode.value = 2
    dut.ui_in.value = 0x01
    for _ in range(40):
        await FallingEdge(dut.clk)
    dut.mode.value = 3
    await FallingEdge(dut.clk)
    assert int(dut.dac_code.value) == 0xFF
    assert int(dut.uo_out.value) == 0xFF

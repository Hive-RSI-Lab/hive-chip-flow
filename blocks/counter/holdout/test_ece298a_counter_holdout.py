"""Held-out tests for counter (docs/design.md section 2): never in tb/, never
given to the rtl-writer or a fixer, run only by the `holdout` gate. Each
looks where tb/ does not: a reset with no clock running at all, a load of
every value, a full lap with the bus driven, and OE turned around on every
edge. Inputs change right after a FallingEdge and are read 10 ns after a
RisingEdge, as in tb/."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

CLK_PERIOD_NS = 100
SETTLE_NS = 10
LOAD, ENABLE, OE = 0b001, 0b010, 0b100


async def _up(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1


async def _edge(dut):
    await RisingEdge(dut.clk)
    await Timer(SETTLE_NS, unit="ns")


# req: REQ-RESET
@cocotb.test()
async def test_reset_with_no_clock(dut):
    """Count up, stop the clock entirely, then pull rst_n: the count clears
    with no edge at all, and counting resumes from 0 once released."""
    dut.ena.value = 1
    dut.uio_in.value = 0
    dut.clk.value = 0
    dut.rst_n.value = 0
    await Timer(50, unit="ns")
    dut.rst_n.value = 1
    dut.ui_in.value = ENABLE
    for _ in range(9):
        await Timer(50, unit="ns")
        dut.clk.value = 1
        await Timer(50, unit="ns")
        dut.clk.value = 0
    await Timer(SETTLE_NS, unit="ns")
    assert int(dut.uo_out.value) == 9
    await Timer(500, unit="ns")
    dut.rst_n.value = 0
    await Timer(SETTLE_NS, unit="ns")
    assert int(dut.uo_out.value) == 0, "rst_n must clear the count with no clock"
    assert int(dut.uio_out.value) == 0
    await Timer(500, unit="ns")
    dut.rst_n.value = 1
    await Timer(50, unit="ns")
    dut.clk.value = 1
    await Timer(SETTLE_NS, unit="ns")
    assert int(dut.uo_out.value) == 1


# req: REQ-LOAD
@cocotb.test()
async def test_load_every_value(dut):
    """Every one of the 256 values loads exactly, each from a different count."""
    await _up(dut)
    values = list(range(256))
    random.Random(298).shuffle(values)
    for v in values:
        await FallingEdge(dut.clk)
        dut.uio_in.value = v
        dut.ui_in.value = LOAD | (ENABLE if v & 1 else 0)
        await _edge(dut)
        assert int(dut.uo_out.value) == v, f"load of 0x{v:02X} gave 0x{int(dut.uo_out.value):02X}"
        await FallingEdge(dut.clk)
        dut.ui_in.value = ENABLE
        dut.uio_in.value = v ^ 0xFF
        await _edge(dut)
        assert int(dut.uo_out.value) == (v + 1) & 0xFF


# req: REQ-COUNT REQ-BUS
@cocotb.test()
async def test_full_lap_on_the_bus(dut):
    """With OE high for a whole lap of 256 edges, uio_out and uo_out step
    together from 0 back to 0 and uio_oe never leaves 0xFF."""
    await _up(dut)
    await FallingEdge(dut.clk)
    dut.ui_in.value = ENABLE | OE
    for i in range(1, 257):
        await _edge(dut)
        assert int(dut.uio_oe.value) == 0xFF
        assert int(dut.uo_out.value) == i & 0xFF, f"edge {i}: 0x{int(dut.uo_out.value):02X}"
        assert int(dut.uio_out.value) == i & 0xFF


# req: REQ-LOAD-OE REQ-BUS REQ-HOLD
@cocotb.test()
async def test_oe_turned_around_every_edge(dut):
    """LOAD held high while OE toggles every cycle: the count loads only on
    the edges where OE was low, and uio_oe follows OE between edges."""
    await _up(dut)
    rng = random.Random(42)
    model = 0
    for _ in range(64):
        await FallingEdge(dut.clk)
        oe = rng.random() < 0.5
        en = rng.random() < 0.5
        d = rng.randrange(256)
        dut.uio_in.value = d
        dut.ui_in.value = LOAD | (OE if oe else 0) | (ENABLE if en else 0)
        await Timer(SETTLE_NS, unit="ns")
        assert int(dut.uio_oe.value) == (0xFF if oe else 0x00)
        assert int(dut.uo_out.value) == model, "count moved between edges"
        await _edge(dut)
        if not oe:
            model = d
        elif en:
            model = (model + 1) & 0xFF
        assert int(dut.uo_out.value) == model, (
            f"oe={oe} en={en} d=0x{d:02X}: 0x{int(dut.uo_out.value):02X} != 0x{model:02X}")

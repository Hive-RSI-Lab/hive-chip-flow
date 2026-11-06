"""Visible tests for counter8. Drive and sample on FallingEdge only; the
count read at a falling edge is the value the previous rising edge set."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, Timer
from padreset import pad_reset

from refmodel import Counter8Model

CLK_PERIOD_NS = 10


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, 2)
    # the block has just left reset: count is 0, next rising edge gives 1
    assert int(dut.count.value) == 0, f"count after reset: {int(dut.count.value)}"


async def run_checked(dut, model, n):
    for _ in range(n):
        await FallingEdge(dut.clk)
        exp = model.step(0)
        got = int(dut.count.value)
        assert got == exp, f"expected {exp}, got {got}"


# req: REQ-INC
@cocotb.test()
async def test_increments_by_one(dut):
    await start(dut)
    await run_checked(dut, Counter8Model(), 40)


# req: REQ-WRAP
@cocotb.test()
async def test_wraps_255_to_0_and_continues(dut):
    await start(dut)
    model = Counter8Model()
    saw_255 = saw_wrap = False
    prev = 0
    for _ in range(600):
        await FallingEdge(dut.clk)
        exp = model.step(0)
        got = int(dut.count.value)
        assert got == exp, f"expected {exp}, got {got}"
        if prev == 255:
            assert got == 0, f"after 255 expected 0, got {got}"
            saw_wrap = True
        saw_255 |= got == 255
        prev = got
    assert saw_255 and saw_wrap


# req: REQ-RST-SYNC
@cocotb.test()
async def test_reset_is_synchronous_and_holds_zero(dut):
    await start(dut)
    model = Counter8Model()
    await run_checked(dut, model, 17)
    before = int(dut.count.value)
    assert before != 0

    # rst rises just after a falling edge: no clock edge has seen it, so
    # count must not move between edges.
    dut.rst.value = 1
    await Timer(1, unit="ns")
    assert int(dut.count.value) == before, \
        f"count changed {before} -> {int(dut.count.value)} between edges (async reset?)"
    await Timer(3, unit="ns")
    assert int(dut.count.value) == before, "count changed before the rising edge"

    # held across several edges: 0 after each
    for i in range(4):
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == 0, \
            f"hold edge {i}: expected 0, got {int(dut.count.value)}"
    await pad_reset(dut, dut.rst, 1)
    assert int(dut.count.value) == 0
    model.count = 0
    await run_checked(dut, model, 5)


# req: REQ-RST-PULSE REQ-INC
@cocotb.test()
async def test_one_clock_reset_pulse_midrun(dut):
    await start(dut)
    model = Counter8Model()
    for run_len in (5, 100, 255):
        await run_checked(dut, model, run_len)
        before = int(dut.count.value)
        assert before != 0, "need a non-zero count before the pulse"
        await pad_reset(dut, dut.rst, 1)
        model.count = 0
        got = int(dut.count.value)
        assert got == 0, f"pulse at count {before}: expected 0, got {got}"
        await run_checked(dut, model, 3)
        assert int(dut.count.value) == 3

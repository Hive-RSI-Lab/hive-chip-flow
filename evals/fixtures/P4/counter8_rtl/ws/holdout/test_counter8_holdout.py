"""Held-out tests for counter8: corners the visible suite skips. Self
contained; FallingEdge-anchored; reset only through pad_reset."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, Timer
from padreset import pad_reset

CLK_PERIOD_NS = 10


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, 1)
    assert int(dut.count.value) == 0


async def run_to(dut, exp, target):
    """Free-run until the model value `target`, checking every edge."""
    while exp[0] != target:
        await FallingEdge(dut.clk)
        exp[0] = (exp[0] + 1) % 256
        got = int(dut.count.value)
        assert got == exp[0], f"expected {exp[0]}, got {got}"


# req: REQ-RST-PULSE REQ-WRAP
@cocotb.test()
async def test_pulse_at_255_and_right_after_wrap(dut):
    await start(dut)
    exp = [0]
    await run_to(dut, exp, 255)
    await pad_reset(dut, dut.rst, 1)  # on the very edge that would wrap
    assert int(dut.count.value) == 0
    exp[0] = 0
    for want in (1, 2, 3):
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == want, f"expected {want}"
    # second lap: wrap normally, then pulse at count 1 just after the wrap
    exp[0] = 3
    await run_to(dut, exp, 255)
    await FallingEdge(dut.clk)
    assert int(dut.count.value) == 0, "no wrap on second lap"
    await FallingEdge(dut.clk)
    assert int(dut.count.value) == 1
    await pad_reset(dut, dut.rst, 1)
    assert int(dut.count.value) == 0


# req: REQ-RST-PULSE REQ-RST-SYNC
@cocotb.test()
async def test_back_to_back_pulses_with_minimal_gap(dut):
    await start(dut)
    exp = [0]
    await run_to(dut, exp, 40)
    for gap in (0, 1, 2, 1, 0, 3):
        await pad_reset(dut, dut.rst, 1)
        assert int(dut.count.value) == 0, "pulse did not clear count"
        for k in range(1, gap + 1):
            await FallingEdge(dut.clk)
            assert int(dut.count.value) == k, f"gap {gap}: expected {k}"
    await pad_reset(dut, dut.rst, 1)
    for k in (1, 2):
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == k


# req: REQ-RST-PULSE REQ-INC
@cocotb.test()
async def test_random_pulses_against_model(dut):
    await start(dut)
    rng = random.Random(0xC0FFEE)
    model = 0
    for _ in range(40):
        for _ in range(rng.randrange(1, 300)):
            await FallingEdge(dut.clk)
            model = (model + 1) % 256
            got = int(dut.count.value)
            assert got == model, f"expected {model}, got {got}"
        await pad_reset(dut, dut.rst, rng.choice((1, 1, 2, 5)))
        model = 0
        assert int(dut.count.value) == 0, "reset did not clear count"


# req: REQ-RST-SYNC
@cocotb.test()
async def test_no_async_effect_at_any_phase_and_value(dut):
    await start(dut)
    exp = [0]
    for target in (1, 127, 128, 254, 255):
        await run_to(dut, exp, target)
        before = int(dut.count.value)
        dut.rst.value = 1  # just after a falling edge, before any rising edge
        for dt in (1, 1, 2, 0.5):
            await Timer(dt, unit="ns")
            got = int(dut.count.value)
            assert got == before, \
                f"count {before} -> {got} between edges with rst high"
        await pad_reset(dut, dut.rst, 1)
        assert int(dut.count.value) == 0
        exp[0] = 0

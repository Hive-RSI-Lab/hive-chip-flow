"""Visible tests for r2r_dac_digital, from spec/spec.md alone. Inputs are driven
and outputs sampled right after a FallingEdge (half a period away from the
active edge)."""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, Timer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dac_model import SINE, Model  # noqa: E402

CLK_NS = 20


async def setup(dut, mode=0, slow=0, ui=0, cycles=3):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = ui
    dut.mode.value = mode
    dut.slow.value = slow
    dut.rst_n.value = 0
    for _ in range(cycles):
        await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)  # rst_n is registered: release acts one clk late


def val(sig):
    return int(sig.value)


# req: REQ-RESET
@cocotb.test()
async def test_reset_clears(dut):
    await setup(dut, mode=0, ui=0xA5)
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0xA5
    dut.rst_n.value = 0
    await FallingEdge(dut.clk)  # registered reset: takes effect one clk later
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0
    assert val(dut.uo_out) == 0
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0
    dut.rst_n.value = 1
    dut.mode.value = 1
    await FallingEdge(dut.clk)  # release also acts one clk later
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0, "counter must restart from 0 after reset"
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 1
    dut.mode.value = 3
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0, "code register must be 0 after reset"


# req: REQ-CAPTURE
@cocotb.test()
async def test_register_capture(dut):
    await setup(dut, mode=0)
    for v in [0x00, 0xFF, 0x01, 0x80, 0x7F, 0x55, 0xAA, 0x12]:
        dut.ui_in.value = v
        await FallingEdge(dut.clk)
        assert val(dut.dac_code) == v


# req: REQ-REGISTERED
@cocotb.test()
async def test_registered_output(dut):
    await setup(dut, mode=0, ui=0x55)
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0x55
    dut.ui_in.value = 0xAA
    dut.mode.value = 1
    await Timer(5, unit="ns")
    assert val(dut.dac_code) == 0x55, "dac_code moved between clock edges"
    dut.mode.value = 0
    await Timer(3, unit="ns")
    assert val(dut.dac_code) == 0x55
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0xAA


# req: REQ-HOLD
@cocotb.test()
async def test_hold_keeps_captured_code(dut):
    await setup(dut, mode=0, ui=0x3C)
    await FallingEdge(dut.clk)
    dut.mode.value = 3
    for v in [0x00, 0xFF, 0x81, 0x42]:
        dut.ui_in.value = v
        await FallingEdge(dut.clk)
        assert val(dut.dac_code) == 0x3C
    dut.mode.value = 1
    for _ in range(5):
        await FallingEdge(dut.clk)
    dut.mode.value = 2
    for _ in range(5):
        await FallingEdge(dut.clk)
    dut.mode.value = 3
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == 0x3C


# req: REQ-RAMP
@cocotb.test()
async def test_ramp_all_codes_and_wrap(dut):
    await setup(dut, mode=1, slow=0)
    seen = []
    for _ in range(300):
        await FallingEdge(dut.clk)
        seen.append(val(dut.dac_code))
    assert seen[0] == 0
    for i, s in enumerate(seen):
        assert s == i % 256, f"step {i}: got {s}"
    assert set(seen) == set(range(256))


# req: REQ-SLOW
@cocotb.test()
async def test_slow_prescale(dut):
    await setup(dut, mode=1, slow=1)
    samples = []
    for _ in range(3 * 256 + 4):
        await FallingEdge(dut.clk)
        samples.append(val(dut.dac_code))
    changes = [i for i in range(1, len(samples)) if samples[i] != samples[i - 1]]
    assert len(changes) >= 2
    assert samples[0] == 0
    assert changes[0] == 256, f"first slow step after {changes[0]} clocks, not 256"
    assert samples[changes[0]] == 1
    for a, b in zip(changes, changes[1:]):
        assert b - a == 256, f"counter stepped after {b - a} clocks, not 256"
    for i in changes:
        assert samples[i] == (samples[i - 1] + 1) % 256
    # back to fast
    dut.slow.value = 0
    await FallingEdge(dut.clk)
    a = val(dut.dac_code)
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert val(dut.dac_code) == (a + 2) % 256


# req: REQ-SINE
@cocotb.test()
async def test_sine_table_all_entries(dut):
    await setup(dut, mode=2, slow=0)
    assert SINE[0] == 128 and SINE[64] == 255 and SINE[192] == 0
    for i in range(256 + 8):
        await FallingEdge(dut.clk)
        got = val(dut.dac_code)
        assert got == SINE[i % 256], f"sine entry {i % 256}: got {got} want {SINE[i % 256]}"


# req: REQ-SWITCH REQ-ECHO
@cocotb.test()
async def test_random_mode_switching_against_model(dut):
    rng = random.Random(298)
    await setup(dut, mode=0)
    m = Model()
    for _ in range(3):
        m.edge(0, 0, 0, 0)
    m.edge(1, 0, 0, 0)  # setup's extra edge after release
    prev_src = None
    for cyc in range(3000):
        mode = rng.randrange(4) if rng.random() < 0.3 else int(dut.mode.value)
        slow = 1 if (cyc // 700) % 2 else 0
        ui = rng.randrange(256)
        rst_n = 0 if rng.random() < 0.005 else 1
        dut.mode.value = mode
        dut.slow.value = slow
        dut.ui_in.value = ui
        dut.rst_n.value = rst_n
        dut.ena.value = rng.randrange(2)
        old_src = m.sources(ui)
        in_reset = not m.rst_q
        want = m.edge(rst_n, ui, mode, slow)
        await FallingEdge(dut.clk)
        got = val(dut.dac_code)
        assert val(dut.uo_out) == got, "uo_out must echo dac_code"
        assert got == want, f"cycle {cyc}: mode {mode} got {got} want {want}"
        if not in_reset and prev_src is not None:
            assert got in (old_src[mode], old_src[prev_src])
        prev_src = mode

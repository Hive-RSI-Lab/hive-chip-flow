"""Held-out tests for uart: corners the visible suite does not drive
(every byte value, back-to-back frames, resets and start pulses at every
frame offset, held start, random stimulus). Self-contained: own reference
model, written from spec/spec.md alone. Inputs driven and outputs sampled
on FallingEdge."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
from padreset import pad_reset

CPB = 4
FRAME = 11 * CPB


class Model:
    def __init__(self):
        self.bits = None
        self.k = 0

    def step(self, rst, start, data):
        if rst:
            self.bits = None
        elif self.bits is None:
            if start:
                d = [(data >> i) & 1 for i in range(8)]
                self.bits = [0] + d + [sum(d) & 1, 1]
                self.k = 0
        else:
            self.k += 1
            if self.k == FRAME:
                self.bits = None
        return (1, 0) if self.bits is None else (self.bits[self.k // CPB], 1)


async def reset(dut, cycles=3):
    dut.start.value = 0
    dut.data.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, cycles)


async def tick(dut, m, start=0, data=0, what=""):
    dut.start.value = start
    dut.data.value = data
    await FallingEdge(dut.clk)
    exp = m.step(0, start, data)
    got = (int(dut.tx.value), int(dut.busy.value))
    assert got == exp, f"{what}: expected (tx,busy)={exp}, got {got}"
    if got[1] == 0:
        assert got[0] == 1, f"{what}: tx low while busy low"


async def reset_checked(dut, m, cycles=1, start=0, data=0):
    """Pad reset (start/data held through it), then expect idle."""
    dut.start.value = start
    dut.data.value = data
    await pad_reset(dut, dut.rst, cycles)
    dut.start.value = 0
    m.bits = None
    got = (int(dut.tx.value), int(dut.busy.value))
    assert got == (1, 0), f"after reset expected (1,0), got {got}"


def clock(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())


# req: REQ-PARITY-EVEN REQ-DATA-LSB-FIRST REQ-START-BIT REQ-STOP-BIT REQ-BIT-HOLD REQ-BUSY-FRAME
@cocotb.test()
async def test_all_bytes_back_to_back(dut):
    clock(dut)
    await reset(dut)
    m = Model()
    for byte in range(256):
        # start is driven on the clock busy has just dropped for the
        # previous frame (accepted: busy is 0 on that edge).
        await tick(dut, m, 1, byte, f"byte {byte:#x} start")
        for i in range(1, FRAME):
            await tick(dut, m, 0, (~byte) & 0xFF, f"byte {byte:#x} clk {i}")
        if byte == 255:
            await tick(dut, m, 0, 0, "tail")
    assert int(dut.busy.value) == 0


# req: REQ-RESET REQ-IDLE
@cocotb.test()
async def test_one_clock_reset_at_every_offset(dut):
    clock(dut)
    await reset(dut)
    for off in range(FRAME + 2):
        m = Model()
        byte = (off * 37 + 5) & 0xFF
        await tick(dut, m, 1, byte, "start")
        for i in range(off):
            await tick(dut, m, 0, byte, f"off {off} pre {i}")
        await reset_checked(dut, m, 1)
        for i in range(3):
            await tick(dut, m, 0, 0xFF, f"off {off} idle {i}")
        nb = (~byte) & 0xFF
        await tick(dut, m, 1, nb, f"off {off} new start")
        for i in range(FRAME):
            await tick(dut, m, 0, byte, f"off {off} new frame {i}")


# req: REQ-RESET
@cocotb.test()
async def test_start_on_reset_edge_is_lost(dut):
    clock(dut)
    await reset(dut)
    m = Model()
    await reset_checked(dut, m, 1, start=1, data=0xFF)
    for i in range(FRAME + 4):
        await tick(dut, m, 0, 0xFF, f"lost start {i}")
    assert int(dut.busy.value) == 0 and int(dut.tx.value) == 1
    # reset in the middle of a frame, with a start also pending
    await tick(dut, m, 1, 0x42)
    for _ in range(9):
        await tick(dut, m)
    await reset_checked(dut, m, 2, start=1, data=0x24)
    for i in range(FRAME + 4):
        await tick(dut, m, 0, 0x24, f"post-reset idle {i}")


# req: REQ-START-WHILE-BUSY REQ-LATCH-ON-START
@cocotb.test()
async def test_start_pulse_at_every_offset(dut):
    clock(dut)
    await reset(dut)
    for off in range(1, FRAME + 2):
        m = Model()
        await tick(dut, m, 1, 0xB3, f"off {off} first")
        for i in range(off - 1):
            await tick(dut, m, 0, 0x00, f"off {off} pre {i}")
        # off 1..43 land while busy (ignored); off 44 is accepted (busy low)
        await tick(dut, m, 1, 0x4D, f"off {off} second start")
        for i in range(2 * FRAME + 3):
            await tick(dut, m, 0, 0xFF, f"off {off} after {i}")
        assert int(dut.busy.value) == 0 and int(dut.tx.value) == 1


# req: REQ-START-WHILE-BUSY REQ-LATCH-ON-START REQ-BUSY-FRAME
@cocotb.test()
async def test_start_held_high_changing_data(dut):
    clock(dut)
    await reset(dut)
    m = Model()
    for i in range(5 * FRAME + 7):
        await tick(dut, m, 1, (i * 29 + 3) & 0xFF, f"held start clk {i}")
    for i in range(FRAME + 2):
        await tick(dut, m, 0, 0, f"drain {i}")


# req: REQ-IDLE REQ-RESET REQ-TX-IDLE-HIGH-WHEN-NOT-BUSY REQ-START-WHILE-BUSY REQ-LATCH-ON-START
@cocotb.test()
async def test_random_stimulus_vs_model(dut):
    clock(dut)
    await reset(dut)
    m = Model()
    rng = random.Random(0x5EED)
    for i in range(6000):
        r = rng.random()
        if r < 0.004:
            await reset_checked(dut, m, rng.choice((1, 1, 2)),
                                start=rng.getrandbits(1), data=rng.getrandbits(8))
            continue
        # bursts of idle make frames complete; start is otherwise sparse
        start = 1 if rng.random() < 0.04 else 0
        await tick(dut, m, start, rng.getrandbits(8), f"random clk {i}")

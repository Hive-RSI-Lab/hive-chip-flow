"""Held-out tests for uart: cases the visible suite does not run. Self-
contained (own model, own helpers); FallingEdge-anchored throughout."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
from padreset import pad_reset

CPB = 4  # spec.yaml params.CLKS_PER_BIT default
FLEN = 11 * CPB


def want(byte, i):
    k = i // CPB
    if k == 0:
        return 0
    if k <= 8:
        return (byte >> (k - 1)) & 1
    if k == 9:
        return None  # undefined in this revision
    return 1


async def reset(dut, cycles=3):
    dut.start.value = 0
    dut.data.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, cycles)


async def frame(dut, byte):
    """Start driven on the current falling edge; checks all FLEN clocks;
    returns on the last frame clock's falling edge."""
    dut.data.value = byte
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    for i in range(FLEN):
        if i:
            await FallingEdge(dut.clk)
        assert int(dut.busy.value) == 1, f"{byte:#04x} clk {i}: busy expected 1, got {dut.busy.value}"
        e = want(byte, i)
        if e is not None:
            assert int(dut.tx.value) == e, f"{byte:#04x} clk {i}: tx expected {e}, got {dut.tx.value}"


async def idle(dut, n):
    for j in range(n):
        await FallingEdge(dut.clk)
        assert int(dut.tx.value) == 1, f"idle clk {j}: tx expected 1, got {dut.tx.value}"
        assert int(dut.busy.value) == 0, f"idle clk {j}: busy expected 0, got {dut.busy.value}"


# req: REQ-DATA-LSB-FIRST REQ-START-BIT REQ-STOP-BIT REQ-BIT-HOLD REQ-BUSY-FRAME
@cocotb.test()
async def test_back_to_back_frames_all_bits(dut):
    """Frames with no idle gap beyond the single clock where busy has
    dropped, over walking-one, walking-zero and edge bytes."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    await idle(dut, 2)
    bytes_ = [1 << b for b in range(8)] + [0xFF ^ (1 << b) for b in range(8)] + [0x00, 0xFF, 0xAA, 0x55]
    for n, byte in enumerate(bytes_):
        await frame(dut, byte)
        if n != len(bytes_) - 1:
            # first clock after the stop hold: idle, start driven right here
            await FallingEdge(dut.clk)
            assert int(dut.busy.value) == 0, f"after {byte:#04x}: busy expected 0 at end of frame, got {dut.busy.value}"
            assert int(dut.tx.value) == 1, f"after {byte:#04x}: tx expected 1 at end of frame, got {dut.tx.value}"
    await idle(dut, 4)


# req: REQ-IDLE REQ-BUSY-FRAME
@cocotb.test()
async def test_data_activity_without_start_stays_idle(dut):
    """data toggles while idle and start is never pulsed: no frame."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    for j in range(2 * FLEN):
        dut.data.value = (0xFF if j % 2 else 0x00) ^ (j & 0xFF)
        await FallingEdge(dut.clk)
        assert int(dut.tx.value) == 1, f"clk {j}: tx expected 1, got {dut.tx.value}"
        assert int(dut.busy.value) == 0, f"clk {j}: busy expected 0, got {dut.busy.value}"


# req: REQ-RESET-INIT REQ-IDLE
@cocotb.test()
async def test_one_clock_reset_while_idle_then_frame(dut):
    """A single-clock reset taken between frames (busy is 0) leaves the
    block idle and able to send a correct frame afterwards."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)
    await idle(dut, 2)
    await frame(dut, 0xC3)
    await idle(dut, 3)
    await pad_reset(dut, dut.rst, 1)
    await idle(dut, 6)
    await frame(dut, 0x3D)
    await idle(dut, 3)
    # and a longer power-up style reset while idle
    await pad_reset(dut, dut.rst, 6)
    await idle(dut, 3)
    await frame(dut, 0x96)
    await idle(dut, 3)


# req: REQ-RESET-INIT REQ-START-BIT
@cocotb.test()
async def test_start_on_first_clock_after_reset(dut):
    """start pulsed on the very first clock the block is out of reset."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut, cycles=2)
    await frame(dut, 0xE1)
    await idle(dut, 3)

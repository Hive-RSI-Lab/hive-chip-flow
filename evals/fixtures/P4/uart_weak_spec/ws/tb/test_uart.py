"""Visible tests for uart. Everything is driven and sampled on FallingEdge.
A start driven after a falling edge is sampled by the next rising edge N; at
the following falling edge the registered outputs already show frame clock 0
(spec: tx/busy registered, first clock of the frame is the one after N)."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
from padreset import pad_reset
from uartmodel import CLKS_PER_BIT, expected_tx, frame_len

CLK_PERIOD_NS = 10


async def reset(dut):
    dut.start.value = 0
    dut.data.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, 4)


async def idle_clocks(dut, n):
    """n falling edges, each checked idle (tx=1, busy=0)."""
    for j in range(n):
        await FallingEdge(dut.clk)
        assert int(dut.tx.value) == 1, f"idle clock {j}: tx expected 1, got {dut.tx.value}"
        assert int(dut.busy.value) == 0, f"idle clock {j}: busy expected 0, got {dut.busy.value}"


async def send_frame(dut, byte):
    """Call on a falling edge with the block idle. Pulses start for one clock
    and checks every clock of the frame against the model. Returns on the
    falling edge of the frame's last clock."""
    dut.data.value = byte
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    for i in range(frame_len()):
        if i > 0:
            await FallingEdge(dut.clk)
        assert int(dut.busy.value) == 1, \
            f"byte {byte:#04x} clock {i}: busy expected 1, got {dut.busy.value}"
        exp = expected_tx(byte, i)
        if exp is not None:
            assert int(dut.tx.value) == exp, \
                f"byte {byte:#04x} clock {i} (bit {i // CLKS_PER_BIT}): tx expected {exp}, got {dut.tx.value}"


# req: REQ-IDLE REQ-RESET-INIT
@cocotb.test()
async def test_idle_after_reset(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await idle_clocks(dut, 3 * frame_len() // 2)


# req: REQ-START-BIT REQ-DATA-LSB-FIRST REQ-STOP-BIT REQ-BIT-HOLD REQ-BUSY-FRAME
@cocotb.test()
async def test_frame_matches_model(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    for byte in (0xA5, 0x01, 0x80, 0x5A, 0x00, 0xFF):
        await idle_clocks(dut, 2)
        await send_frame(dut, byte)
        # first clock after the stop hold completes: back to idle
        await idle_clocks(dut, 5)


# req: REQ-BUSY-FRAME REQ-BIT-HOLD
@cocotb.test()
async def test_busy_exact_length_and_rise_with_start(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)
    await idle_clocks(dut, 3)
    dut.data.value = 0x69
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    high = 0
    while int(dut.busy.value) == 1:
        high += 1
        assert high <= frame_len(), "busy stayed high beyond 11*CLKS_PER_BIT clocks"
        await FallingEdge(dut.clk)
    assert high == frame_len(), f"busy high {high} clocks, expected {frame_len()}"
    assert int(dut.tx.value) == 1, "tx must be 1 once busy returns to 0"

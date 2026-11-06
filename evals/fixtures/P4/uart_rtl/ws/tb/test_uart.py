"""Visible tests for uart. Inputs are driven on FallingEdge and outputs
sampled on FallingEdge; a start driven at falling edge F is sampled by the
next rising edge N, and the sample taken at the following falling edge is
frame clock 0 (start bit)."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge
from padreset import pad_reset

from uartmodel import CPB, FRAME, UartModel, frame_bits


async def reset(dut, cycles=3):
    dut.start.value = 0
    dut.data.value = 0
    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, cycles)


async def tick(dut, model, start=0, data=0):
    """Drive one clock of inputs, compare (tx, busy) to the model."""
    dut.start.value = start
    dut.data.value = data
    await FallingEdge(dut.clk)
    exp = model.step(0, start, data)
    got = (int(dut.tx.value), int(dut.busy.value))
    assert got == exp, f"expected (tx,busy)={exp}, got {got}"
    return got


async def send(dut, model, byte, tail=CPB + 2):
    """start pulse then the whole frame and a short idle tail, every clock
    checked against the model. Returns the sampled tx trace of the frame."""
    await tick(dut, model, 1, byte)
    trace = [int(dut.tx.value)]
    for _ in range(FRAME - 1):
        await tick(dut, model, 0, byte ^ 0xFF)
        trace.append(int(dut.tx.value))
    for _ in range(tail):
        await tick(dut, model)
    return trace


def decode(trace):
    """Bits of the frame read independently of the model, with each bit
    required to be constant for CPB clocks."""
    out = []
    for b in range(11):
        chunk = trace[b * CPB:(b + 1) * CPB]
        assert len(set(chunk)) == 1, f"bit {b} not held for {CPB} clocks: {chunk}"
        out.append(chunk[0])
    return out


def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())


# req: REQ-IDLE
@cocotb.test()
async def test_idle_after_reset(dut):
    start_clock(dut)
    await reset(dut)
    assert int(dut.tx.value) == 1 and int(dut.busy.value) == 0, \
        f"after reset expected tx=1 busy=0, got tx={dut.tx.value} busy={dut.busy.value}"
    m = UartModel()
    for _ in range(20):
        await tick(dut, m)


# req: REQ-START-BIT REQ-DATA-LSB-FIRST REQ-STOP-BIT REQ-BIT-HOLD REQ-BUSY-FRAME
@cocotb.test()
async def test_frame_matches_model(dut):
    start_clock(dut)
    await reset(dut)
    m = UartModel()
    for byte in (0xA5, 0x3C, 0x81, 0x6E):
        trace = await send(dut, m, byte)
        bits = decode(trace)
        assert bits[0] == 0, f"{byte:#x}: start bit {bits[0]}"
        got_data = sum(bits[1 + i] << i for i in range(8))
        assert got_data == byte, f"expected data {byte:#x}, decoded {got_data:#x}"
        assert bits[10] == 1, f"{byte:#x}: stop bit {bits[10]}"


# req: REQ-BUSY-FRAME REQ-BIT-HOLD
@cocotb.test()
async def test_busy_exact_length(dut):
    start_clock(dut)
    await reset(dut)
    dut.data.value = 0x0F
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    high = 0
    while int(dut.busy.value) == 1 and high < 2 * FRAME:
        high += 1
        await FallingEdge(dut.clk)
    assert high == FRAME, f"busy high for {high} clocks, expected {FRAME}"
    assert int(dut.tx.value) == 1, "tx must be 1 on the clock busy returns to 0"


# req: REQ-PARITY-EVEN
@cocotb.test()
async def test_parity_even(dut):
    start_clock(dut)
    await reset(dut)
    m = UartModel()
    for byte in (0x00, 0x01, 0x03):
        bits = decode(await send(dut, m, byte))
        expected = bin(byte).count("1") & 1
        assert bits[9] == expected, f"{byte:#x}: parity expected {expected}, got {bits[9]}"
        assert (sum(bits[1:9]) + bits[9]) % 2 == 0, f"{byte:#x}: odd number of ones"


# req: REQ-LATCH-ON-START
@cocotb.test()
async def test_data_latched_on_start(dut):
    start_clock(dut)
    await reset(dut)
    m = UartModel()
    # data changes every clock after the start clock (send() inverts it).
    bits = decode(await send(dut, m, 0x96))
    assert bits == frame_bits(0x96), f"expected {frame_bits(0x96)}, got {bits}"


# req: REQ-RESET REQ-IDLE
@cocotb.test()
async def test_reset_mid_frame(dut):
    start_clock(dut)
    await reset(dut)
    m = UartModel()
    await tick(dut, m, 1, 0x00)
    for _ in range(16):
        await tick(dut, m)
    assert int(dut.busy.value) == 1
    await pad_reset(dut, dut.rst, 1)
    m = UartModel()
    assert (int(dut.tx.value), int(dut.busy.value)) == (1, 0), \
        f"after mid-frame reset expected tx=1 busy=0, got tx={dut.tx.value} busy={dut.busy.value}"
    for _ in range(FRAME):
        await tick(dut, m)
    bits = decode(await send(dut, m, 0xC3))
    assert bits == frame_bits(0xC3), f"frame after reset expected {frame_bits(0xC3)}, got {bits}"


# req: REQ-START-WHILE-BUSY
@cocotb.test()
async def test_start_while_busy_ignored(dut):
    start_clock(dut)
    await reset(dut)
    m = UartModel()
    await tick(dut, m, 1, 0xA5)
    for _ in range(4):
        await tick(dut, m)
    await tick(dut, m, 1, 0x5A)  # ignored
    for _ in range(FRAME + 2 * CPB):
        await tick(dut, m)
    assert int(dut.busy.value) == 0, "ignored start must not queue a second frame"

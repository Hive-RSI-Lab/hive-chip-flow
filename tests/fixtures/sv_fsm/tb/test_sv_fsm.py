import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge


# req: REQ-RUN
@cocotb.test()
async def test_start_runs_ten_then_done(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.start.value = 0
    dut.rst.value = 1
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.busy.value) == 0 and int(dut.done.value) == 0
    dut.start.value = 1
    await FallingEdge(dut.clk)
    dut.start.value = 0
    for i in range(10):
        assert int(dut.busy.value) == 1, f"busy low at step {i}"
        assert int(dut.count.value) == i
        await FallingEdge(dut.clk)
    assert int(dut.done.value) == 1 and int(dut.busy.value) == 0
    assert int(dut.count.value) == 10
    await FallingEdge(dut.clk)
    assert int(dut.done.value) == 0 and int(dut.busy.value) == 0

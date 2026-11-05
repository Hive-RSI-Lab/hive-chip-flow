"""Reset a block through its tile's pad, from tb/ and holdout/.

A TinyTapeout tile's rst_n pin reaches the block through the generated
wrapper's synchroniser (ttlib.PAD_RESET_LATENCY flops on clk), so a reset's
assertion and its release both reach the block that many clock edges after
the bench drives them. RTL sim (ttlib.write_sim_frame) and glsim both run
the bench through that wrapper and publish the latency on the harness net
ttlib.PAD_RESET_LATENCY_NET; a run with no wrapper in front of the block
has no such net, and the latency is 0.

pad_reset() drives the reset, holds it, releases it, then waits out exactly
that latency. When it returns, the block has seen exactly `cycles` clocks of
reset and has just left it: the state a bench with no pad in front of the
block sees right at its release. A bench's own cycle-exact checks after it
then still catch a synchroniser with a flop too many or too few, or a
block that takes an extra clock to reset."""
from cocotb.triggers import FallingEdge

from ttlib import PAD_RESET_LATENCY_NET


def latency(dut) -> int:
    """The pad reset latency of the run dut is in: the harness's
    PAD_RESET_LATENCY_NET, or 0 when the block is driven directly."""
    try:
        net = getattr(dut, PAD_RESET_LATENCY_NET)
    except AttributeError:
        return 0
    return int(net.value)


async def pad_reset(dut, rst, cycles: int, active: int = 1, clk=None,
                    edge=FallingEdge):
    """Drive rst to `active` now, hold it for `cycles` edges of clk (dut.clk
    by default; falling edges unless `edge` names another trigger, such as
    RisingEdge for a bench that drives its inputs there), release it, then
    wait latency(dut) more of the same edges. Call it on the edge the bench
    drives its inputs on."""
    clk = dut.clk if clk is None else clk
    rst.value = active
    for _ in range(cycles):
        await edge(clk)
    rst.value = 1 - active
    for _ in range(latency(dut)):
        await edge(clk)

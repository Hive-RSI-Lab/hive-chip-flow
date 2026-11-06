# tb_ignores_pad_latency (sim)

A visible test failed through the tile's pad frame and passed driving the
block directly, and the bench never reads the pad's reset latency. The
generated wrapper holds reset on the block for `ttlib.PAD_RESET_LATENCY`
clocks after `rst_n` rises, so a bench that counts cycles from its own
release is that many clocks early. No RTL is at fault. Routes to
`testbench`, which is the tb-writer's.

**Cheapest fix first:** reset through the helper, in the bench's one reset
helper, and keep every check as it is:

    from padreset import pad_reset

    await FallingEdge(dut.clk)
    await pad_reset(dut, dut.rst, cycles)

It waits out the latency and returns on the edge a block-level bench sees
its release, and with no pad in front of the block it waits nothing.
`corpus/vde/counter8/tb/test_counter8.py` uses it.

**Never:** add idle clocks after the reset to absorb the latency, or loosen
a cycle-exact check. A bench that hard-codes the latency misses a wrapper
with a flop too few or too many.

**Trap:** a reference model ticked during reset has to agree with
`pad_reset`'s timing. Reset the model when `pad_reset` returns, not while
the bench still counts its own reset clocks.

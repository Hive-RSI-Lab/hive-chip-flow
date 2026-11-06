# holdout_ignores_pad_latency (holdout)

A held-out test failed through the tile's pad frame and passed driving the
block directly, and the held-out bench never reads the pad's reset latency
(`tb_ignores_pad_latency` is the visible suite's form of this). The finding
names the requirement id(s), never the test. No RTL is at fault. Routes to
`holdout_stimulus`, which is the tb-writer's.

**Cheapest fix first:** change how the bench resets and nothing else:
`from padreset import pad_reset`, then `await FallingEdge(dut.clk)` and
`await pad_reset(dut, dut.rst, cycles)` in its reset helper.

**Never:** change an assert, a `check_*`/`expect_*`/`verify_*` call, a
bound, an expected value, a loop's case list, a decorator, a `# req:` tag,
or a plain helper that computes expected values. Run
`check_holdout_edit.py --workspace <ws> --baseline <pre-fix label>` before
you declare the edit; `state.py edit --class holdout_stimulus_edit
--baseline <label>` refuses the same things.

**Trap:** if a reset helper also computes expected values, the edit won't
pass that check. Change nothing and say so in OPEN; the orchestrator
escalates it.

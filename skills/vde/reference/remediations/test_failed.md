# test_failed (sim)

A visible cocotb test ran and its assertion failed. Since the tests are
written first, from the spec alone, before any RTL exists (`docs/
design.md` section 2), this almost always means the DESIGN does not do
what the test - and therefore the spec - says it should. Routes to `rtl`.

**False positive class:** a genuine test bug (an off-by-one in the test's
own reference model, a race between drive and sample not anchored to the
same clock edge - see `corpus/vde/counter8/tb/test_counter8.py`'s own
`FallingEdge`-only convention). Real, but less common than a real design
bug - read the assertion's expected-vs-actual message before assuming the
test is wrong.

**Cheapest fix first:** reproduce the exact failing case by hand against
the spec text - does the RTL's actual behavior match what `spec.md`/
`spec.yaml`'s requirement says, or does the test's expectation? Fix
whichever one is actually wrong; if it's the test, this finding is not
yours to close (report it in OPEN, the orchestrator re-routes to
tb-writer, never silently edit `tb/` from the rtl domain).

**Trap:** a failure right after a reset edge is almost always a one-cycle
timing assumption mismatch (registered vs. combinational reset release) -
check exactly which edge the RTL's reset takes effect on against exactly
which edge the test samples.

**Failed at an extra seed:** a message saying the test "passed at seed A
and failed at extra seed B" comes from spec `sim.seeds`: only some random
draws reach the bug. Replay it as one run with the `--seed B` line in the
message and debug that draw; a fix is done when the full multi-seed run
passes, not just seed B.

**Read the waves before re-running anything (#93):** the finding carries
`window`, a text table of the block's ports and the signals the failing
test reads, sampled just before each of the last 16 rising clock edges,
with a last row at the failure time (`<- failure`). A counter that wraps
one early reads `253, 254, 0` down its column. `waves` is the whole dump
(VCD) and `failure_ns` the failure time: widen the window or add an
internal register with `chip-flow wave <waves> --signals count,dut.u_x.state
--at <failure_ns> --window 64`. `signature` is the assert's tb file:line
with its numbers masked; several tests with one signature are one bug,
clustered as one, so fix it once. `window_note` says why a finding has no
window (for example, the test passes when run alone, so the failure needs
the tests before it).

**First divergence (#360):** a test that once passed at the same seed with
the same testbench carries `first_divergence`: the first rising edge after
which one of the block's outputs went another way than on that passing
run (`signal`, `was`, `now`, `t_ns`, and `record_rtl`, the RTL digest it
passed on). That edge is usually the bug's cause and the failure its
symptom, often many cycles later; the window row after it is marked
`<- first divergence`. Start there. Records live in `log/sim_record/`,
one per test and seed, refreshed on every pass; a testbench edit retires
them, so no field means no comparable passing run, not no divergence.

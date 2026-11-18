# sim_mismatch (cover)

The same test, with the same seed, passed on one simulator and failed on
the other (`cover` runs the visible tests on Verilator for coverage and
again on Icarus to compare). The finding names the test and gives each
simulator's result. Routes to `rtl`: a result that depends on which
simulator ran it depends on event order, and that is a race.

**Usual cause:** a blocking assignment (`a = ...`) in one clocked always
block that another always block reads on the same edge. Which block runs
first is not defined by the language, and the two simulators pick
differently (a block clocked through a buffered or assigned copy of the
clock is a common way to tip it). Gate-level sim and silicon won't follow
either.

**Cheapest fix first:** use non-blocking assignments (`<=`) for every
register written in a clocked block, and clock every block from the same
net, not a copy made with `assign`. Keep blocking assignments to
temporaries local to one block.

**Trap:** don't fix it in the bench by moving when it samples. If the
bench drives inputs on the clock's active edge, though, the bench is the
race: drive on the opposite edge (`FallingEdge`) and sample there too.

**False positive class:** none known. A test that is flaky on its own (it
draws random values without the seed) can look like this; run `sim` twice
with the same seed to rule it out.

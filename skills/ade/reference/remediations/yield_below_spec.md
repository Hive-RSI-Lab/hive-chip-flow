# yield_below_spec (mc)

The Monte Carlo yield (samples with every measure in bound) is below
`mc.yield_min` at tt or at a bench's worst sim_pvt corner (the finding
says which; `worst_corners` names the corner). Routes to `sizing`.

**Cheapest fix first:** mismatch shrinks with device area. Larger W*L on
the matched pair (a mirror, a differential input) is the textbook fix,
before any topology change. Read which measure fails in the failing
samples: it points at the matched pair. A miss only at the worst corner
wants margin there (the nominal value sits near a bound at that corner),
not only less mismatch.

**Trap:** lowering `yield_min` or `runs` is a spec change for the person.
A higher `runs` count does not raise yield; it only tightens the estimate.

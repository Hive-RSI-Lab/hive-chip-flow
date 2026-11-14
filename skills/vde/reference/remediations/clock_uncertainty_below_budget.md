# clock_uncertainty_below_budget (timing)

spec.yaml's `clock.jitter` declares a clock's source jitter, and the setup
uncertainty the signoff SDC leaves on that clock is less than the budget
asks for: `ps_rms x sigma x cycles` (correlated accumulation, the default)
or `x sqrt(cycles)` (random), in ns (engine/lib/clock_budget.py). Slack
can be positive everywhere and this still fails: the margin that slack
was measured against does not cover the jitter the clock really has.
Routes to `harden`.

**Cheapest fix first:** set `CLOCK_UNCERTAINTY_CONSTRAINT` in
`harden/config.override.json` to at least the required value the finding
names (round up), then re-run `harden` and `timing`. That value is what
harden's flow SDC applies to every clock, after it sources
`harden/constraints.sdc`.

**Trap:** a `set_clock_uncertainty` in `harden/constraints.sdc` does not
stick - harden's flow SDC sets the uncertainty again, over every clock,
after it sources the design's. Do not lower the budget to make it pass
either: `ps_rms`, `cycles` and `sigma` are the measurement and the
coverage the spec owner chose, and the budget's `source` (a file under
`spec/jitter/`) is the evidence. If the measurement really changed, that
is a spec edit with the new evidence beside it, not a timing fix. A
budget that genuinely needs more uncertainty than the period can carry
is a `review` call on the clock, not a harden knob.

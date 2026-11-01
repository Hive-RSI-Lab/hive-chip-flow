# macro_clock_disagrees (top_harden)

A clock the SDC creates on the analog macro's pin has a period that does not
follow the analog side's recorded ceiling for that process, from
`macro_timing.clocks` in `analog/spec/spec.yaml`. No harden ran.

**Cheapest fix first:** drop the typed number. `top_harden` puts a proc at the
head of the top SDC, so a `top.sdc` can say `create_clock -period
[macro_clock_period <pin>] [get_pins u_analog/<pin>]`. A digital SDC that also
runs standalone has no such proc: change its period table to the finding's
`ceiling_ns`.

**Trap:** a period longer than the ceiling times slower than the macro can run,
so never lengthen it to get timing to pass. A period well under the ceiling
is safe but stops following the record. If that is deliberate, state
`period_margin` for the clock with the reason beside it: the analog spec's
owner decides. Do not edit the record.

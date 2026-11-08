# mismatch_measure_no_mc (spec_lint)

A matching-sensitive measure (an offset, a mirror or UP/DN ratio, a
mismatch, INL/DNL; by a name token, or marked `statistical: mismatch`) sits
in a spec with no `mc: {enabled: true}`. sim_tt and sim_pvt run with the
PDK's `sw_stat_mismatch=0`, so they check only its systematic part; the mc
gate is the one gate that turns device mismatch on.

**Cheapest fix first:** add `mc: {enabled: true, yield_min: ...}` (the
gate sizes the run count from it) and make sure the measure is scored at
tt (the corner MC always runs; its second run is each bench's worst
sim_pvt corner).

**Trap:** a waiver is only for a measure mismatch truly cannot move (a
digital divide ratio; resistors, which GF180's ngspice models give no
per-instance mismatch). Write `statistical: none` and an `mc_waiver` that
says why; the report lists it under `mc_waived`. "MC is slow" is not a
reason.

**Also:** a multiplied device (`m=N`) gets the right sigma in the mc gate
(check_mc adds `par=N`, and carries an `m=N` on a user subckt down to the
devices inside it); you need not expand it by hand. A multiplied subckt defined
outside the netlist and bench stays pessimistic: the report lists it as
`unhandled` under `par_added`.

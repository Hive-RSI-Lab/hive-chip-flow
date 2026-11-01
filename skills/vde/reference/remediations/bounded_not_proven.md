# bounded_not_proven (formal)

Info severity - never fails the gate by itself. smtbmc's basecase found no
counterexample within the spec's own `formal: {depth}`, but neither complete
engine closed the proof: the k-induction step did not converge and abc pdr
gave up (UNKNOWN). A property abc pdr proves is reported proven
(`proven_by: pdr`), not bounded. Routes to `formal` if it is ever
dispatched (rare, since it does not fail the gate on its own).

**What to do:** nothing is required. If a human wants a full proof rather
than a bounded one, the property likely needs an inductive invariant
(an auxiliary assert capturing an intermediate truth the induction step
can use) or a deeper `formal: {depth}` - never claim "proven" in a digest
or a summary for a requirement that only reached "bounded."

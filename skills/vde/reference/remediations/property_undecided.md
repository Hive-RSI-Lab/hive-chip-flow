# property_undecided (formal)

No engine reached a verdict on this assert. smtbmc's basecase stopped at
another assert's counterexample before it reached `formal.depth`, and abc pdr
ran out of time or crashed writing a trace before it settled this one. Nothing
disproved the assert, and no bound was reached. Routes to `formal`.

**Cheapest fix first:** fix the counterexample in the same report
(`property_failed` or `engine_disagreement`) and rerun. This verdict usually
goes with it.

**Trap:** do not edit this assert to clear the finding. It was not shown to be
wrong. If it stays undecided once the other asserts hold, raise
`formal.timeout_s`.

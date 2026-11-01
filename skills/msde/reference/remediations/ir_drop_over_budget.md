# ir_drop_over_budget (harden)

`ir__drop__worst` is over the budget, by default 5% of the rail (gates.yaml's
`harden_metrics`). Cells at the far end of the grid are slower than the timing
corners assume. Routes to `harden`.

**Cheapest fix first:** find the worst region in the IR drop step's report.
Spread the switching (lower utilisation, a smaller clock tree) or strengthen the
grid before anything else. `harden_signoff.ir_budget_pct` in spec.yaml moves the
budget, only with a timing margin that covers the bigger drop: say so in the
spec. Re-check timing afterwards.

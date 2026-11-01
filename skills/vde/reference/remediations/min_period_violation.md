# min_period_violation (timing)

A clock's period is under a clocked cell's liberty `min_period` (the
fastest it may toggle), even if setup and hold are met. Routes to
`review`: the period is spec.yaml's clock.

**Cheapest fix first:** compare the spec's clock period with the
limit the finding names; a prescaler stage at the full VCO rate is the
typical case. Divide earlier, use a faster cell, or lower the spec's
frequency.

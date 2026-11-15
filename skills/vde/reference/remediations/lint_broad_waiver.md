# lint_broad_waiver (lint, info)

An entry in `rtl/lint_allow.yaml` has only a `rule`, so it waives that rule
everywhere in the design, including any new warning of it. Reported at
info: it never fails the gate. **Fix:** add the `file` (e.g. `rtl/top.v`)
and the `signal` the warning quotes (or its `line`), one entry per waived
warning.

# lint_stale_waiver (lint)

An entry in `rtl/lint_allow.yaml` matches no lint warning: the warning it
was written for is gone, or its `file`, `signal` or `line` never matched
it. A stale waiver would silently cover the next, unrelated warning of
that rule. **Fix:** delete the entry, or correct its `file`/`signal`/`line`
against the warning lint now prints. Do not drop the narrowing fields to
make it match: that only turns it into a `lint_broad_waiver`.

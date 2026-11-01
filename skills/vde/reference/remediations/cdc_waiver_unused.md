# cdc_waiver_unused (cdc)

A waiver in `spec/cdc.yaml` matches no finding: stale or misnamed, and it would
hide whatever matches it next. **Fix:** remove it, or fix its names against the
report's `crossings` and `clock_gates`. Do not widen it with wildcards.

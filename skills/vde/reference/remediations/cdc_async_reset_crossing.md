# cdc_async_reset_crossing (cdc)

A flop's async set or reset is driven from a flop in another domain, so its
release is not timed against this flop's clock. Routes to `rtl`.

**Fix:** drive the reset from this domain through a reset synchroniser. Where
the crossing is the function (a PFD's mutual clear), waive it with a
`{from, to, reason}` in `spec/cdc.yaml` and say what bounds the pulse.

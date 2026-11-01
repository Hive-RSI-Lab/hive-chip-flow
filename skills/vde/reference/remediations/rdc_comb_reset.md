# rdc_comb_reset (cdc)

Combinational logic drives a flop's async set or reset, so a glitch on that
logic resets the flop. Routes to `rtl`.

**Fix:** register the logic in that domain and use the result, or use a
synchronous clear. Where the pulse is the function (a PFD's clear), waive it
with `{reset, reason}` in `spec/cdc.yaml` and say what bounds the pulse. The
raw resets in the cone are judged separately, as `rdc_unsynced_release`.

# cdc_comb_before_sync (cdc)

A synchroniser has logic ahead of its first flop. The logic can glitch and the
flop can catch the glitch as a real pulse. Routes to `rtl`.

**Fix:** register the signal in the source domain and feed that flop straight
to the synchroniser; do the logic before that register or after the
synchroniser. Never waive this to save the flop.

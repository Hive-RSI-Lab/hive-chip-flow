# cdc_glitchy_clock_mux (cdc)

A mux on a clock path. Changing its select can cut a clock pulse short; this
gate cannot prove any mux safe from its structure. Routes to `rtl`.

**Fix:** a glitch-free mux with a synchronised select per clock, or drop the
mux. If the select only moves with both clocks stopped or the output is
observation only, waive it with `{clock, reason}` in `spec/cdc.yaml`.

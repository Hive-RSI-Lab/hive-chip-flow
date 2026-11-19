# requirement_cover_label_missing (cover)

A requirement in `spec.yaml` names a cover label (`cover: [LABEL]`) that
no cover statement in `rtl/` or `cover/*.sv` carries, so there is nothing
to hit. Without this error, a label that names nothing would pass for
free. Routes to `testbench`.

**Fix:** write the cover point with that statement label, for example
`LABEL: cover property (@(posedge clk) ...);` in a module in `cover/`,
bound into the DUT with `bind <top> <module> u_cover (...)`. Or correct
the spelling in `spec.yaml`. Labels are case-sensitive and cannot contain
dashes.

# pex_bench_not_corner_aware (pex_sim)

The post-layout bench hard-codes something this run's corners move: the
`.lib` section, the temperature, the supply, or a passive section. Every corner
would simulate as typical and pass unseen, so nothing was run. Routes to
`testbench`.

**Fix:** use the token the finding names, in `layout_ref/<block>_pex_tb.cir`:

    .lib '{pdk}/libs.tech/ngspice/sm141064.spice' {corner}
    .lib '{pdk}/libs.tech/ngspice/sm141064.spice' {res_corner}
    .lib '{pdk}/libs.tech/ngspice/sm141064.spice' {mim_corner}
    .temp {temp_c}
    vdd_src vdd 0 dc {vdd}

Anything that was a fraction of 3.3 V (stimulus levels, a measure's threshold)
must follow `{vdd}` too. Then check the bounds still mean what they should
at every corner: a bound written for typical usually needs a `corners` scope,
or a list of bounds per measure (`check_pex_sim.py`'s docstring). Say which
measures you changed in SUMMARY.

**Trap:** `pex_corners: [tt]` also clears this finding, and it puts the block
back to typical-only. That is the person's scope call, recorded in the spec,
not a fix for this finding.

# requirement_cover_not_hit (cover)

A requirement in `spec.yaml` names an SV cover point (`cover: [LABEL]`),
and no visible test under `tb/` ever hit it. The finding names the
requirement, the label and the instance it is bound into (the cover can be
in `rtl/` or in a `cover/*.sv` file bound into the DUT). If the label is
bound into several instances, each one must be hit. Routes to `testbench`.

**Cheapest fix first:** read the cover's sequence and write, or extend,
a test tagged `# req: <ID>` that drives the design through it. A cover on
a wrap or overflow needs a test that runs long enough to reach it. A
`disable iff (rst)` cover also needs reset to be released.

**Trap:** do not weaken the cover property to make it easier to hit, and
do not drop the label from the requirement. `rtl/cover_exclude.yaml`
does not remove a requirement's cover point. If the point really cannot
be reached, the requirement or the design is wrong: fix that instead.

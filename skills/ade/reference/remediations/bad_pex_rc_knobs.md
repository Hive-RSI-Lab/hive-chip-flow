# bad_pex_rc_knobs (spec_lint)

spec.yaml's `pex_rc_knobs` is not `"all"` or a non-empty list of knob names, it
names a knob `engine/reference/pex_rc_knobs.yaml` does not pin, or it names a
knob whose paired process the spec's `corners` do not sweep (a `_high` knob runs
at ss, a `_low` knob at ff). Routes to the spec's author.

**Fix:** name knobs from the table, add the paired process to `corners`, or
delete the field: absent means the wires as extracted, with no extra runs.

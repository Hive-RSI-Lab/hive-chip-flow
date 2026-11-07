# bad_pex_rc_corners (spec_lint)

spec.yaml's `pex_rc_corners` is not `"all"` or a non-empty list of variant names,
it names a variant `engine/reference/pex_rc_variants.yaml` does not pin, or it
names a variant whose paired process the spec's `corners` do not sweep (hrhc
runs at ss, lrlc at ff, hrlc at sf, lrhc at fs). A grid of corners has no sf or
fs, so it can ask for hrhc and lrlc only. Routes to the spec's author.

**Fix:** name variants from the table, add the paired process to `corners`, or
delete the field: absent means the wires as extracted, with no extra extraction.

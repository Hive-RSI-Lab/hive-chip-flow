# sigma_unmeasured (mc)

A spec measure carries a `sigma_max`, but no bench the mc gate judges
measures it. The sigma bound is never checked, so it is a refusal, not a
pass. Routes to `spec`.

**Cheapest fix first:** add a `tb/` bench that measures it (a `.measure`
of the same name, with a bounds file scored at tt), or check that the
measure is not marked `statistical: none`, which leaves it out of MC.

**Trap:** deleting `sigma_max` to clear the finding removes the spec's
only mismatch bound on that measure. That is a spec change for the person.

# feedback_no_stability (spec_lint)

spec.yaml says `feedback: true` but no measure named `pm_deg` has a `min`
bound. A loop with no phase margin bound can ring or oscillate at a corner
and still pass every DC and transient gate (issue #100).

**Cheapest fix first:** add a stability bench from
`skills/ade/templates/stb_tb.cir` (Tian's loop gain, broken at the
feedback node; it prints `pm_deg`, `gm_db` and `ugf_hz`) and bound the
margin in spec.yaml:

    - name: pm_deg
      bounds: {min: 60}

with the same bound in the bench's `.bounds.json`. 60 degrees is the
usual floor for an amplifier in unity-gain feedback; 45 is the least a
spec should accept. Bound `gm_db` (min 10) and `ugf_hz` too.

**Trap:** a bench that only runs `op` and a step cannot see a margin. A
step that settles at tt says nothing about ss or ff.

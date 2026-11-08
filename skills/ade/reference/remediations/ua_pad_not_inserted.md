# ua_pad_not_inserted

spec.yaml lists `ua_pins`, so every bench runs twice, `no_pad` (as written)
and `pad` (with the Tiny Tapeout ua pad model, `engine/lib/padlib.py`, on
each ua port of every instance of `top`). The pad could not be put into
this bench, so the gate refuses: with no pad in the deck, a "pad" run would
only be a second bare run.

The message names why. Usually the bench has no instance of `top`, the
instance calls the top under another name, or its node count does not match
the top's `.subckt` ports. Fix the bench so it instantiates the top's
`.subckt` by name with every port connected, in the order the netlist
declares them. Triage it to the bench-writer in WORK-ORDER MODE.

**Trap:** do not take the pin out of `ua_pins` to make the refusal go away.
If the port leaves the tile on a `ua[k]` pin, silicon sees the pad whether
the spec says so or not.

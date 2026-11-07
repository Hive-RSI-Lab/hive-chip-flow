# lvs_mismatch (lvs)

netgen did not report a clean, unique match between the generated layout
(extracted with magic) and `netlist/<block>.cir` at `sizing/sizing.yaml`'s
current values. Routes to `layout` (the layout-fixer) - the layout is
checked AGAINST the netlist, never the other way round.

**Cheapest fix first:** the finding's `msg` carries netgen's own log tail
(the last 40 lines) - it names the specific mismatch: a device sized
differently in the generator than in the netlist+sizing (the most common
planted shape - `corpus/ade/mirror`'s own `plant_lvs_sized_differently.py`
fault), a swapped or floating net, or a missing device. Compare the
generator's own W/L constants against `netlist/<block>.cir`'s subckt line
with `sizing/sizing.yaml`'s overrides applied - a stale literal in
`layout/gen_<block>.py` left over from an earlier sizing pass is the
common case.

**Where it is:** the gate runs the PDK's klayout LVS deck as well, and its
own `lvs_mismatch` finding (`source: klayout`) carries `locations`: every
unmatched net with its drawn shapes (`layer`, `bbox` in um, metals first)
and every unmatched device with its `position` and both sides' parameters.
An open shows as one reference net split over two layout nets, a short as
one layout net named after two (`iout,vss`) - the shape that joins them is
among its shapes. The full cross-reference is the `lvsdb` file it names
(KLayout's netlist browser opens it).

**Trap:** never edit `netlist/` or `sizing/` to match a wrong layout - LVS
exists to catch the layout drifting from the design of record, and "fixing"
it by moving the reference defeats the entire point (docs/design.md
section 2's "the record of what ran is the release", applied to the
fabrication-correctness check). If the mismatch persists after matching
every constant, escalate rather than keep guessing at netgen's output.

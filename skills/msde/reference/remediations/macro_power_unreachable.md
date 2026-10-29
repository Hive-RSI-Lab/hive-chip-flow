# macro_power_unreachable (top_harden)

The tile's power stripes are vertical Metal4, and they join a macro only
where they cross one of its Metal3 supply shapes. The finding comes in two
forms; `top_harden` ran no harden for either, since LibreLane would stop
at `openroad-generatepdn` with PDN-0232 and PDN-0233.

- **"supply pin ... has no Metal3 shape"**: this pin could never reach
  VPWR or VGND. Fix it as below.
- **"no place ... lets the tile's Metal4 power stripes reach both"**: the
  supplies have Metal3, but the cell's own Metal4 is in the way everywhere.
  OpenROAD cuts a stripe at each of the macro's obstructions, grown by the
  PDN halo (10 um) and the Metal4 spacing (0.3 um). `-pinonly` writes all
  of the cell's Metal4 as obstruction, its own supply nets' too. So a
  stripe reaches a strap only through a column about 22 um wide (the 1.6 um
  stripe plus 10.3 um a side) with no Metal4, running from the cell's top
  or bottom edge to that strap. `top_harden` already slides the macro
  sideways by whole sites, up to one stripe pitch, to put a VPWR stripe in
  vdd's column and the VGND beside it (3.3 um right) in vss's. The finding
  means no such place exists. A supply Metal3 shape that Metal4 wholly
  covers is never offered as a pin: OpenROAD stops on one with PDN-0006.

**Cheapest fix first:** in `analog/layout/gen_<block>.py`, draw vdd and vss
each as a horizontal Metal3 strap across the cell's full width, joined by
Via2 to the Metal2 or Metal1 supply they already have, with the pin label
on the strap. `corpus/msde/dac_tile/analog/layout/gen_dac_tile_analog.py`
does this. A strap at least one stripe pitch long (38.87 um on GF180) is
sure to cross a stripe. Then run `analog/`'s own layout gates and
`top_harden` again.

For the second form, keep one stretch of a vdd strap and of a vss strap
reachable from the same edge with no Metal4 above it, and none within
10.3 um either side of a column from there to the edge (route the cell's
Metal4 wires and cap plates elsewhere). One Metal4-free column at least
25.5 um wide (22.2 um for a stripe, plus the 3.3 um from VPWR to VGND),
running in from one edge across a vdd strap and a vss strap, always
fits, since the macro can slide a whole pitch.

**Trap:** keep other Metal3 off the straps' rows. Keep Metal4 and above
off the columns the stripes take, because the tile's stripes run over the
macro there.

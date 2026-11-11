# bad_ua_pad

spec.yaml's `ua_pad` (the optional board load on every ua pin) is malformed.
It may set only `board_c` (farads) and/or `board_r` (ohms), each a positive
number, and only when the spec lists `ua_pins`.

The pad's own 500 ohm, 5 pF and ESD diodes are fixed from Tiny Tapeout's
published analog spec and the GF180 pad cell, and no field changes them.
The page states those figures (under 500 ohm, under 5 pF, 4 mA max for the
path from the analog pads to the `ua[]` pins) for sky130 only; no GF180 table
was found there, so for GF180 they are the only published numbers, likely
similar but not confirmed. `engine/lib/padlib.py` cites the source.
To model a probe, header or scope input, set `board_c`/`board_r`. For no
board load, leave `ua_pad` out.

**Trap:** do not try to lower the pad's R or C through `ua_pad`. They are
TT's published worst case, and the field only adds load.

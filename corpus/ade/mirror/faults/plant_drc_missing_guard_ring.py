"""plant_drc_missing_guard_ring.py - ade/mirror `drc` fault (docs/design.md
1.5, "### M9."): removes both substrate tap placements (one at each end of
the row) from layout/gen_mirror.py, leaving the rest of the VSS routing
(every source and the bus) intact - a real "missing guard ring": no P+ tap
anywhere near any NMOS. klayout's real GF180 signoff deck catches this as
DF.14 ("max distance of a substrate tap from NCOMP is 20um"); magic's own DRC does not
enforce it at all in this repo's check_analog_drc.py (docs/spikes/glayout.md
already found magic silently hides a DIFFERENT class of bug here - this
fault exercises a distinct rule, but the same "klayout is the DRC that
counts" reasoning is why analog_drc runs klayout only).
"""
from __future__ import annotations

from pathlib import Path

TAP_LINES = (
    '    tap = top.add_ref(layoutlib.psub_tap(TAP_SIZE))\n'
    '    tap.move((tap_x, tap_y))\n'
)
LEFT_TAP_LINES = (
    '    tapl = top.add_ref(layoutlib.psub_tap(TAP_SIZE))\n'
    '    tapl.move((tap_xl, tap_y))\n'
)


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if TAP_LINES not in text or LEFT_TAP_LINES not in text:
        raise RuntimeError(
            f"{gen_path}: guard-ring tap lines not found as expected - "
            "gen_mirror.py's own source shape changed")
    text = text.replace(TAP_LINES, "").replace(LEFT_TAP_LINES, "")
    gen_path.write_text(text, encoding="utf-8")

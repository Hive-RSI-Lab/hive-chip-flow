"""plant_pex_asymmetric_output.py - ade/comparator `pex_sim` fault for
spec.yaml's symmetric_nets (#121): one output routed the long way round.
The outn pin moves from its latch column to the far end of a 50um metal1
detour (0.40um, the twisted pair's own track width) off outn's left-half
metal2 track ("outn<"), reached through one more via1 past the left clk
column. outp keeps its route. DRC and LVS stay clean - the same net,
legally drawn - but outn now carries the detour's C and its pin-to-gate R
and outp does not, so pex_sim's symmetry check names the pair with both
values before the bench runs.
"""
from __future__ import annotations

from pathlib import Path

DETOUR_UM = 50.0
# the outn label leaves the latch column ...
OLD_LBL = ('        labels.append((out_net, (dx0 + dx1) / 2,\n'
           '                       (mid_y["outn"] + mid_y["outp"]) / 2, L1LBL))\n')
NEW_LBL = ('        if out_net != "outn":\n'
           '            labels.append((out_net, (dx0 + dx1) / 2,\n'
           '                           (mid_y["outn"] + mid_y["outp"]) / 2, L1LBL))\n')
# ... for the far end of the detour, drawn once every clk column is known
OLD_TRK = '    # --- the metal2 tracks, each spanning its own net\'s landings only\n'
NEW_TRK = ('    line_y = mid_y["outn"]\n'
           '    line_x = round(-clk_x - 1.5, 3)\n'
           '    via_at("mid", "outn<", line_x)\n'
           f'    layoutlib.rect(top, line_x - {DETOUR_UM}, line_y - TRACK / 2,\n'
           '                   line_x, line_y + TRACK / 2, L1)\n'
           f'    labels.append(("outn", line_x - {DETOUR_UM} + 0.2, line_y, L1LBL))\n'
           + OLD_TRK)


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_comparator.py"
    text = gen_path.read_text(encoding="utf-8")
    if text.count(OLD_LBL) != 1 or text.count(OLD_TRK) != 1:
        raise RuntimeError(
            f"{gen_path}: the line this fault edits was not found once - "
            "gen_comparator.py's own source shape changed")
    gen_path.write_text(text.replace(OLD_LBL, NEW_LBL).replace(OLD_TRK, NEW_TRK),
                        encoding="utf-8")

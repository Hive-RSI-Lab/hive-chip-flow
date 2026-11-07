"""gen_mirror.py - layout generator for the ade/mirror corpus rung
(docs/design.md 5, "### M9."; docs/spikes/glayout.md's fallback).

Draws M8's own `current_mirror` (netlist/mirror.cir): xmref diode-connected
at W=4u, xmout at W=8u, both L=0.5u, sources and bulk on vss, pins
iref_node/iout/vdd/vss. vdd is a pin of M8's subckt that no device uses, so
here it is a labelled pad on its own.

The two devices are a matched pair (spec.yaml `matching:`), so they are
drawn as one row of equal unit transistors: xmref is one unit (A), xmout
two (B) of MOUT_W / 2, in the common-centroid row ROW, with a dummy unit (D)
at each end so neither end unit sees a different neighbour. netgen merges
xmout's parallel units into one W=8u device, and a dummy has every
terminal on vss, so LVS sees M8's two devices; layout_match is the gate
that reads the row itself.

Every unit is GF180's draw_nfet(). Its pads are read off the drawn cell
(layoutlib.fet_pads), so the wiring follows whatever W/L the constants
below say. The sizes are this generator's own, typed in by the
layout-writer; LVS is what checks them against netlist/mirror.cir.

Wiring, unit i of ROW at x = i * PITCH, channels vertical:
  iref_node  a bus joining the A and B bottom gate pads, plus each A
             drain dropped onto it (the diode connection)
  iout       each B drain up a via1 to one metal2 line that ends on a
             metal1 pad right of the row
  vss        every source pad, and a dummy's drain and top gate pad, run
             up to a bus above the row that lands on a P+ substrate
             tap at each end (DF.14 wants a tap within 20um; two taps
             keep the body resistance short and symmetric)
  vdd        a lone pad right of the tap
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "engine" / "lib"))
import layoutlib  # noqa: E402

L1 = layoutlib.GF180_LAYER["metal1"]
L1LBL = layoutlib.GF180_LAYER["metal1_label"]
L2 = layoutlib.GF180_LAYER["metal2"]

CELL = "current_mirror"
PINS = ["iref_node", "iout", "vdd", "vss"]
ROW = "DBABD"
MREF_W = 4.0
# xmout's total W; each of its two units is half of it
MOUT_W = 8.0
GATE_L = 0.5
PITCH = 3.0
TAP_SIZE = 1.0


def shifted(box, dx):
    return (box[0] + dx, box[1], box[2] + dx, box[3])


def generate():
    import gdsfactory as gf

    draw_fet, _draw_res = layoutlib.gf180_cells()
    top = gf.Component()

    width = {"A": MREF_W, "B": MOUT_W / 2, "D": MREF_W}
    cells = {k: draw_fet.draw_nfet(l_gate=GATE_L, w_gate=w)
             for k, w in width.items()}
    units = []
    for i, kind in enumerate(ROW):
        x = i * PITCH
        top.add_ref(cells[kind]).move((x, 0.0))
        pads = {k: shifted(v, x) for k, v in layoutlib.fet_pads(cells[kind]).items()}
        units.append((kind, pads))
    act = [p for k, p in units if k != "D"]
    ref = next(p for k, p in units if k == "A")
    outs = [p for k, p in units if k == "B"]
    out = outs[-1]

    labels = []

    # --- iref_node: A and B bottom gate pads joined, each A drain dropped on
    g0, g1 = act[0]["gate_bot"], act[-1]["gate_bot"]
    layoutlib.rect(top, g0[0], g0[1], g1[2], g0[3], L1)
    for k, p in units:
        if k == "A":
            d = p["d"]
            layoutlib.rect(top, d[0], g0[1], d[2], d[1] + 0.2, L1)
    d = ref["d"]
    labels.append(("iref_node", (d[2] + g1[0]) / 2, (g0[1] + g0[3]) / 2, L1LBL))

    # --- iout: each B drain up a via1, one metal2 line to a pad past the row
    cy = round((max(p["d"][1] for p in outs) + min(p["d"][3] for p in outs)) / 2, 2)
    row_x1 = max(b[2] for _, p in units for b in p.values())
    px = round(row_x1 + 0.7, 2)
    for p in outs:
        layoutlib.via1(top, round((p["d"][0] + p["d"][2]) / 2, 3), cy)
    layoutlib.via1(top, px, cy)
    x0 = (outs[0]["d"][0] + outs[0]["d"][2]) / 2
    layoutlib.rect(top, x0 - 0.2, cy - 0.2, px + 0.2, cy + 0.2, L2)
    iout_pad = (px - 0.25, cy - 0.25, px + 0.25, cy + 0.25)
    layoutlib.rect(top, *iout_pad, L1)
    d = iout_pad
    labels.append(("iout", (d[0] + d[2]) / 2, (d[1] + d[3]) / 2, L1LBL))

    # --- vss: every source, and a dummy's drain and gate, up to a bus, a tap
    # at each end
    bus_y0 = max(p["gate_top"][3] for _, p in units) + 0.6
    bus_y1 = bus_y0 + 0.5
    for k, p in units:
        runs = [p["s"]] + ([p["d"]] if k == "D" else [])
        for s in runs:
            layoutlib.rect(top, s[0], s[3] - 0.2, s[2], bus_y1, L1)
        if k == "D":
            g = p["gate_top"]
            layoutlib.rect(top, g[0], g[1], g[2], bus_y1, L1)
    tap_x = row_x1 + 2.0
    tap_y = (bus_y0 + bus_y1) / 2 - TAP_SIZE / 2
    tap = top.add_ref(layoutlib.psub_tap(TAP_SIZE))
    tap.move((tap_x, tap_y))
    row_x0 = min(b[0] for _, p in units for b in p.values())
    tap_xl = row_x0 - 2.0 - TAP_SIZE
    tapl = top.add_ref(layoutlib.psub_tap(TAP_SIZE))
    tapl.move((tap_xl, tap_y))
    layoutlib.rect(top, tap_xl + 0.2, bus_y0, tap_x + 0.8, bus_y1, L1)
    labels.append(("vss", (ref["s"][2] + ref["d"][0]) / 2,
                   (bus_y0 + bus_y1) / 2, L1LBL))

    # --- vdd: M8's pin with nothing on it inside the mirror
    vdd_x = tap_x + TAP_SIZE + 1.0
    layoutlib.rect(top, vdd_x, bus_y0, vdd_x + 1.0, bus_y1, L1)
    labels.append(("vdd", vdd_x + 0.5, (bus_y0 + bus_y1) / 2, L1LBL))

    # magic numbers a cell's ports in the order their labels were written,
    # and a bench instantiates the extracted cell positionally: M8's order.
    labels.sort(key=lambda lbl: PINS.index(lbl[0]))
    return layoutlib.finalize(top, CELL, labels)


if __name__ == "__main__":
    comp = generate()
    out_path = sys.argv[1] if len(sys.argv) > 1 else "mirror.gds"
    comp.write_gds(out_path)
    print(f"wrote {out_path}")

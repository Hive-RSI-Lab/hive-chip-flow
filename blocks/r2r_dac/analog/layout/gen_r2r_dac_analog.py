"""gen_analog.py - layout generator for r2r_dac_analog (netlist/r2r_dac.cir).

The workspace's state.json names the block "analog", so layout_gen.py loads
this file as gen_analog.py; the cell it draws is the netlist's own subckt,
r2r_dac_analog (spec.yaml's `top`).

Devices, sizes from sizing/sizing.yaml (ru_w 2u, ru_l 150u, rb_l 148.36u):
  25 ppolyf_u units and 4 dummies (draw_ppolyf_res, res_type ppolyf_u),
     one per row, bodies horizontal, left ends aligned at x=0, each with
     the vendor cell's own P+ substrate tap on its left (the resistor's
     3rd terminal, vss). Two matched rows (spec.yaml `matching:`), each a
     layoutlib.matched_row of one cell, so every unit of a row is one
     size and one way round, with a dummy (both ends on vss) at each end:
     top-down, the 17 full units (a_k then rs_(k-1) for k=7..0, then the
     termination rta, rtb), then the 8 trim units b_7..b_0.
  8 gf180mcu_fd_sc_mcu7t5v0__buf_20, one per bit, stacked in rows that
     share rails (odd rows mirrored), a filltie at both ends of every row
     for the well and substrate taps (DF.13/DF.14 within 20um).

Routing, one scheme for every net:
  - every resistor terminal gets a via1 and a metal2 stub running right to
    its net's vertical metal1 track, right of the array (the left-end stub
    rides over its own body at the pad's upper half, the right-end stub at
    the lower half, so the two never meet);
  - a dummy's two ends are stubbed to the vss track the same way;
  - every internal net is labelled on its metal1 track (34/0 text: a net
    name for the extraction, never a pin);
  - each buffer's Z gets Z_CUTS via1s along its Z metal1 band (its edges
    peak near 2 mA) and a metal2 stub left to its d_k track; the buffer stack sits below the resistor rows so no
    Z stub crosses a resistor stub;
  - supplies for the tile's PDN (its Metal4 stripes join a macro only on
    Metal3): vss is a metal3 strap along the bottom, vdd one along the
    top, each the full cell width and STRAP_H tall. The resistors'
    substrate taps are joined by one metal1 strip at their x, which drops
    to the vss strap; the vss track (rtb's end) drops to it too, each
    through a via1 and via2 array.
  - the buffers' rails: each VDD and VSS rail is carried right of the row
    on a RAIL_H metal1 extension, drawn from the right filltie's left
    edge (its own rail necks down to a 0.34um tap stalk), to a metal2 pad of the same size, joined
    by a via1 array; a via2 array lands it on a metal3 riser, vdd's
    running up into the top strap, vss's down into the bottom one. The
    rails carry the bit drivers' switching current (the em_ir transient
    bench), so every hop is wide and many-cut. Nothing else is on metal3,
    nothing above it.
Pins are labelled in the subckt's own order. Every signal pin is a metal2
track out to the cell's RIGHT edge, labelled on metal2 at that edge (the
tile router lands there, never on buried metal): each buffer's I gets a
via1 at its right end and a metal2 wire right, at the I strip's own y,
past the row's end; vout's top-left stub is carried on right the same way.
The wires dip to metal1 for 4.4um between the bridge via1s (BR_X0,
BR_X1), clear of the rails' pads, then come back up to a short metal2
pin stub at the edge. Each Z stub
runs in the lower Z metal1 band, 0.4um below the I wire. vdd and vss are
labelled on their metal3 straps.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "engine" / "lib"))
import layoutlib  # noqa: E402

L1 = layoutlib.GF180_LAYER["metal1"]
L2 = layoutlib.GF180_LAYER["metal2"]
L2LBL = layoutlib.GF180_LAYER["metal2_label"]
VIA1 = layoutlib.GF180_LAYER["via1"]
VIA2 = (38, 0)
L3 = (42, 0)
L3LBL = (42, 10)
VIA2_SIZE = 0.26
STRAP_H = 2.8          # metal3 supply strap height (the vss one clear of row 0)
CUT_PITCH = 0.52       # via1/via2 array pitch (0.26 cut, 0.26 space)

CELL = "r2r_dac_analog"
PINS = [f"dac_code[{k}]" for k in range(7, -1, -1)] + ["vout", "vdd", "vss"]

# sizing/sizing.yaml values, um
RU_W = 2.0
RU_L = 150.0
RB_L = 148.36

ROW_PITCH = 4.0        # resistor rows
TRACK_PITCH = 1.0      # vertical metal1 tracks
W = 0.4                # stub/track width (the via1 pad size)
STD_LIB = "gf180mcu_fd_sc_mcu7t5v0"
BUF = f"{STD_LIB}__buf_20"
TIE = f"{STD_LIB}__filltie"
CELL_H = 3.92
BUF_W = 34.72
TIE_W = 1.12
# via positions inside buf_20 (its LEF): Z's lower metal1 band at x
# 12.565-32.955, y 0.865-1.51; I's metal1 at x 0.63-9.90, y 1.74-2.15.
# The Z via's metal2 pad (y 0.90-1.30) sits 0.445um below the I wire's
# (1.745-2.145), over M2.2a's 0.28um.
Z_VIA = (22.68, 1.10)
Z_CUTS = 4             # via1s along Z's band: its edges peak near 2 mA
I_VIA = (9.50, 1.945)
# right of the rows: the bridge's via1s (at the I wires' y), the rails'
# pads (at the rails' y, 0.945um clear of the I wires) and two metal3
# risers over both, vdd's left of vss's
BR_X0 = 0.6            # first bridge via1, past the row's end
BR_X1 = 5.0            # second bridge via1, past the row's end
EDGE_DX = 6.0          # the cell's right edge, past the row's end
RAIL_H = 1.6           # rail extension and pad height
PAD_X = (0.3, 5.7)     # rail pads, past the row's end
VDD_RISER = (0.0, 2.7)  # metal3 vdd riser, past the row's end
VSS_RISER = (3.0, 6.0)  # metal3 vss riser, past the row's end


def resistor_nets():
    """{refdes: (left net, right net)} for every unit resistor."""
    nets = {}
    for k in range(7, -1, -1):
        n = "vout" if k == 7 else f"n{k}"
        nets[f"xrb{k}a"] = (f"d{k}", f"mb{k}")
        nets[f"xrb{k}b"] = (n, f"mb{k}")
        if k > 0:
            nets[f"xrs{k-1}"] = (n, f"n{k-1}")
    nets["xrta"] = ("n0", "mt")
    nets["xrtb"] = ("mt", "vss")
    return nets


def matched_rows():
    """[(unit length, refdes top-down)]: spec.yaml's two matching groups,
    the full units in bit order (each 2R branch's 'a' half next to the
    series R below its rung) and the trim units."""
    full = []
    for k in range(7, -1, -1):
        full.append(f"xrb{k}a")
        if k > 0:
            full.append(f"xrs{k-1}")
    full += ["xrta", "xrtb"]
    trims = [f"xrb{k}b" for k in range(7, -1, -1)]
    return [(RU_L, full), (RB_L, trims)]


def cut_array(top, x0, y0, x1, y1, layer, enc=0.07):
    """Fill the box (x0, y0, x1, y1) with 0.26 cuts at CUT_PITCH, each
    `enc` inside it, centred on the 5nm grid; returns the cut count."""
    h = VIA2_SIZE / 2
    span_x = round(x1 - x0 - 2 * (enc + h), 6)
    span_y = round(y1 - y0 - 2 * (enc + h), 6)
    if span_x < 0 or span_y < 0:
        raise layoutlib.LayoutError(f"no room for a cut in {x0, y0, x1, y1}")
    nx, ny = int(span_x // CUT_PITCH) + 1, int(span_y // CUT_PITCH) + 1
    cx0 = (x0 + x1) / 2 - (nx - 1) * CUT_PITCH / 2
    cy0 = (y0 + y1) / 2 - (ny - 1) * CUT_PITCH / 2
    for i in range(nx):
        for j in range(ny):
            cx = round((cx0 + i * CUT_PITCH) / 0.005) * 0.005
            cy = round((cy0 + j * CUT_PITCH) / 0.005) * 0.005
            layoutlib.rect(top, cx - h, cy - h, cx + h, cy + h, layer)
    return nx * ny


def drop_to_strap(top, cx, cy):
    """A via1 and via2 stack rising from (cx, cy): metal1 and metal2 pads
    one wire (W) wide, 1.2um tall, landing on the metal3 strap above them;
    as wide as the wire, so the cell's outline stays the bus's."""
    x0, y0, x1, y1 = cx - W / 2, cy - W / 2, cx + W / 2, cy + 1.2 - W / 2
    for layer in (L1, L2):
        layoutlib.rect(top, x0, y0, x1, y1, layer)
    cut_array(top, x0, y0, x1, y1, VIA1)
    cut_array(top, x0, y0, x1, y1, VIA2)


def std_cell(name):
    import gdsfactory as gf

    gds = (layoutlib.pdk_root() / "libs.ref" / STD_LIB / "gds" /
           f"{STD_LIB}.gds")
    return gf.import_gds(gds, cellname=name)


def generate():
    import gdsfactory as gf

    _draw_fet, draw_res = layoutlib.gf180_cells()
    top = gf.Component()
    labels = []
    edge_pins = []  # (pin, x where its metal2 wire starts, y)

    # ---------------------------------------------------------- buffers
    buf = std_cell(BUF)
    tie = std_cell(TIE)
    n_bufs = 8
    buf_block_h = n_bufs * CELL_H
    y_res0 = buf_block_h + 5.0          # lowest resistor row
    nets = resistor_nets()
    groups = matched_rows()
    n_rows = sum(len(devs) + 2 for _l, devs in groups)

    # net -> list of y where a stub lands on its track; side stubs drawn later
    pts: dict[str, list[float]] = {}
    stubs = []  # (net, y, x_from) metal2 stub from x_from to the track

    max_right = 0.0
    sub_x = None
    pads = []
    y_row = y_res0 + (n_rows - 1) * ROW_PITCH   # the top row
    for length, devs in groups:
        rc = draw_res.draw_ppolyf_res(l_res=length, w_res=RU_W,
                                      res_type="ppolyf_u")
        row = layoutlib.matched_row(top, rc, dict.fromkeys(devs, 1),
                                    pitch=-ROW_PITCH, pattern="interdigitate",
                                    dummies=1, axis="y", origin=(0.0, y_row))
        y_row -= len(row["sequence"]) * ROW_PITCH
        pads += row["pads"]
    for pad in pads:
        m1 = pad["boxes"]
        if len(m1) != 3:
            raise layoutlib.LayoutError(
                f"expected 3 metal1 pads on a ppolyf_u cell, got {len(m1)}")
        sub, lp, rp = m1
        ln, rn = nets[pad["device"]] if pad["device"] else ("vss", "vss")
        sub_x = ((sub[0] + sub[2]) / 2, sub[0], sub[2])
        max_right = max(max_right, rp[2])
        # left end: via at the pad's upper half; right end: lower half
        ly = lp[3] - 0.25
        ry = rp[1] + 0.25
        lx = round((lp[0] + lp[2]) / 2 / 0.005) * 0.005
        rx = round((rp[0] + rp[2]) / 2 / 0.005) * 0.005
        layoutlib.via1(top, lx, ly)
        layoutlib.via1(top, rx, ry)
        stubs.append((ln, ly, lx))
        stubs.append((rn, ry, rx))
        pts.setdefault(ln, []).append(ly)
        pts.setdefault(rn, []).append(ry)
    y_res_top = y_res0 + n_rows * ROW_PITCH

    # buffer rows: row r at y r*CELL_H, odd rows mirrored about x so rails
    # are shared; filltie at both ends. Bit order: row 0 = bit 0.
    x_tie0 = None  # set after the track field is sized
    # --- vss track also reaches the bus below everything
    y_bus = -3.0
    pts.setdefault("vss", []).append(y_bus)

    # Z points: computed with row geometry (independent of x)
    def row_tf(r):
        """(mirror, dy): local y -> dy - y when mirrored else dy + y."""
        if r % 2 == 0:
            return False, r * CELL_H
        return True, (r + 1) * CELL_H

    def ty(r, y):
        mir, dy = row_tf(r)
        return dy - y if mir else dy + y

    for k in range(n_bufs):
        pts.setdefault(f"d{k}", []).append(ty(k, Z_VIA[1]))

    # ---------------------------------------------------------- tracks
    # left-edge interval packing of each net's y span onto metal1 tracks
    spans = sorted(((min(v), max(v), net) for net, v in pts.items()
                    if len(v) > 1 or net.startswith("d") or net == "vss"),
                   key=lambda s: s[0])
    tracks: list[float] = []  # last y used per track
    track_of: dict[str, int] = {}
    for lo, hi, net in spans:
        for t, last in enumerate(tracks):
            if lo > last + 1.5:
                tracks[t] = hi
                track_of[net] = t
                break
        else:
            tracks.append(hi)
            track_of[net] = len(tracks) - 1
    x_t0 = max_right + 2.0
    track_x = {net: x_t0 + t * TRACK_PITCH for net, t in track_of.items()}
    for net, x in track_x.items():
        lo, hi = min(pts[net]), max(pts[net])
        layoutlib.rect(top, x - W / 2, lo - W / 2, x + W / 2, hi + W / 2, L1)
    for net, y, x_from in stubs:
        x = track_x[net]
        layoutlib.rect(top, min(x_from, x) - W / 2, y - W / 2,
                       max(x_from, x) + W / 2, y + W / 2, L2)
        layoutlib.via1(top, x, y)
    x_track_end = x_t0 + len(tracks) * TRACK_PITCH

    # ---------------------------------------------------------- buffers
    x_tie0 = round((x_track_end + 3.0) / 0.005) * 0.005
    x_buf = x_tie0 + TIE_W
    x_tie1 = x_buf + BUF_W
    x_row_end = x_tie1 + TIE_W
    for r in range(n_bufs):
        mir, dy = row_tf(r)
        for comp, x in ((tie, x_tie0), (buf, x_buf), (tie, x_tie1)):
            ref = top.add_ref(comp)
            if mir:
                ref.dmirror_y(0)
            ref.move((x, dy))
        k = r  # bit k in row k
        zx, zy = x_buf + Z_VIA[0], ty(r, Z_VIA[1])
        for j in range(Z_CUTS):     # leftward, under the wire to the track
            layoutlib.via1(top, zx - j * CUT_PITCH, zy)
        dx = track_x[f"d{k}"]
        layoutlib.rect(top, dx - W / 2, zy - W / 2, zx + W / 2, zy + W / 2, L2)
        layoutlib.via1(top, dx, zy)
        iy = ty(r, I_VIA[1])
        layoutlib.via1(top, x_buf + I_VIA[0], iy)
        edge_pins.append((f"dac_code[{k}]", x_buf + I_VIA[0], iy))

    # supply rails: VSS at y = 0, 2H, 4H, ...; VDD at H, 3H, ...; each
    # carried right on a RAIL_H metal1 extension, drawn over the right
    # filltie (whose own 0.6 rail narrows to its 0.34 tap stalk) to a
    # metal2 pad, via1 array between, and a via2 array to its supply's
    # metal3 riser
    vdd_ys = [CELL_H * j for j in range(1, n_bufs + 1, 2)]
    vss_ys = [CELL_H * j for j in range(0, n_bufs + 1, 2)]
    px0, px1 = x_row_end + PAD_X[0], x_row_end + PAD_X[1]
    risers = {"vdd": (x_row_end + VDD_RISER[0], x_row_end + VDD_RISER[1]),
              "vss": (x_row_end + VSS_RISER[0], x_row_end + VSS_RISER[1])}
    rh = RAIL_H / 2
    for net, ys in (("vdd", vdd_ys), ("vss", vss_ys)):
        r0, r1 = risers[net]
        for y in ys:
            layoutlib.rect(top, x_tie1, y - rh, px1, y + rh, L1)
            layoutlib.rect(top, px0, y - rh, px1, y + rh, L2)
            cut_array(top, px0, y - rh, px1, y + rh, VIA1)
            cut_array(top, max(px0, r0), y - rh, min(px1, r1), y + rh, VIA2)

    # ---------------------------------------------------------- vss
    # the substrate taps' strip and the vss track drop to the vss strap
    sx, s0, s1 = sub_x
    layoutlib.rect(top, s0, y_bus - W / 2, s1, y_res_top, L1)
    sxv = round(sx / 0.005) * 0.005
    drop_to_strap(top, sxv, y_bus)
    drop_to_strap(top, track_x["vss"], y_bus)

    # ---------------------------------------------------------- vout
    # carried right from its track along its top stub's y (that stub's own
    # row has no other metal2 right of the track at that y)
    edge_pins.append(("vout", track_x["vout"], max(pts["vout"])))

    # ---------------------------------------------------------- edge pins
    xb0, xb1 = x_row_end + BR_X0, x_row_end + BR_X1
    x_edge = x_row_end + EDGE_DX
    for pin, x0, y in edge_pins:
        layoutlib.rect(top, x0 - W / 2, y - W / 2, xb0 + W / 2, y + W / 2, L2)
        layoutlib.via1(top, xb0, y)
        layoutlib.rect(top, xb0 - W / 2, y - W / 2, xb1 + W / 2, y + W / 2, L1)
        layoutlib.via1(top, xb1, y)
        layoutlib.rect(top, xb1 - W / 2, y - W / 2, x_edge, y + W / 2, L2)
        labels.append((pin, x_edge - 0.25, y, L2LBL))

    # ---------------------------------------------------------- metal3
    bb = top.dbbox()
    x_l, x_r = bb.left, bb.right
    # vss: strap along the bottom, its lower edge at the cell's (the
    # drops' pads); its riser comes down into it from the top VSS rail
    vss_y0 = bb.bottom
    vss_y1 = vss_y0 + STRAP_H
    if vss_y1 > -0.3:
        raise layoutlib.LayoutError("the vss strap reaches the buffer rows")
    layoutlib.rect(top, x_l, vss_y0, x_r, vss_y1, L3)
    r0, r1 = risers["vss"]
    layoutlib.rect(top, r0, vss_y0, r1, max(vss_ys) + rh, L3)
    labels.append(("vss", x_l + 1.0, (vss_y0 + vss_y1) / 2, L3LBL))
    # vdd: strap along the top; its riser goes up into it from the lowest
    # VDD rail
    vdd_y1 = bb.top
    vdd_y0 = vdd_y1 - STRAP_H
    layoutlib.rect(top, x_l, vdd_y0, x_r, vdd_y1, L3)
    r0, r1 = risers["vdd"]
    layoutlib.rect(top, r0, min(vdd_ys) - rh, r1, vdd_y1, L3)
    labels.append(("vdd", x_l + 1.0, (vdd_y0 + vdd_y1) / 2, L3LBL))

    labels.sort(key=lambda lbl: PINS.index(lbl[0]))
    # every internal net named on its own metal1 track (a drawing-layer
    # text: a plain magic label, never a pin, out of the abstract), so the
    # extraction names the nets layout_match fits each unit by
    for net in sorted(set(track_x) - set(PINS)):
        labels.append((net, track_x[net], min(pts[net]), L1))
    return layoutlib.finalize(top, CELL, labels)


if __name__ == "__main__":
    comp = generate()
    out_path = sys.argv[1] if len(sys.argv) > 1 else "r2r_dac_analog.gds"
    comp.write_gds(out_path)
    print(f"wrote {out_path}")

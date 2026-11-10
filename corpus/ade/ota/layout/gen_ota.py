"""gen_ota.py - layout generator for the ade/ota corpus rung: the
two_stage_miller_ota of netlist/ota.cir, through layoutlib.

Every transistor is drawn as equal unit devices (GF180's draw_nfet()/
draw_pfet(), one finger, L = 1um), in four rows stacked bottom to top, each
row centred on the same x:

  N1  nfet 4u  the bias mirror, one matched_row (spec.yaml `matching:`,
               common_centroid): xmb 1 unit, xmt 2, xm7 20, the odd xmb in
               the middle, a dummy at each end
  N2  nfet 5u  the input pair, matched_row ABBA: xm1 2, xm2 2, dummies
  P1  pfet 2u  the mirror load, matched_row ABBA: xm3 2, xm4 2, dummies
  P2  pfet 8u  xm6, 10 units side by side (not matched: one device)

netgen merges each device's parallel units back into the netlist's one
device, and a dummy has every terminal on its row's rail (netlist/ota.cir
lists them), so LVS sees the netlist; layout_match reads the rows.

Wiring is metal1 stubs, metal2 tracks and metal3 spines. Between two rows
is a channel of horizontal metal2 tracks, one per net, each with a column
of via1 cuts across its width on every stub (or group of close same-net
stubs) that ends on it: one cut carries 0.18 mA, so out (1.0um) gets two
and the 2um rails four. A stub runs straight up or down from a unit's pad
(source, drain, or one of its two gate pads) to its track, so a stub from
the row below and one from the row above that share an x must end on
tracks in that order (below's lower) unless they are the same net: the
generator checks this and refuses a crossing. A net with tracks in two
channels (vss, vdd, out, d2) gets a metal3 spine left of the rows, a via2
at each of its tracks (two or more cuts on the nets that carry the
supply current).

  channel c0 (below N1)     vss, a wide rail, with substrate taps below
  channel c1 (N1 to N2)     ibias, out, tail | tail, inp, inn, vss
  channel c2 (N2 to P1)     d1, d2
  channel c3 (P1 to P2)     vdd, a wide rail | d2
  channel c4 (above P2)     out | vdd, with nwell taps above

Substrate taps (P+) sit below N1 every 10um or less and at both ends of
N2; nwell taps (N+) at both ends of P1 and along the top of P2; one nwell
rectangle covers P1, P2 and their taps. Each row has one pplus/nplus
across all its units: at a 3um pitch neighbouring units' implants would sit
0.36um apart, under PP.2/NP.2's 0.4.

xcc, the Miller cap, is a 26um MIM-B (metal4 bottom, metal5 top) left of
the spines: its top plate is d2 (the netlist's first terminal) and comes
out on metal5 to a via4+via3 stack on the d2 spine. xrz, its nulling
resistor (a 2um x 25.5um ppolyf_u, #297), lies below the cap: its left
terminal goes up a via1/via2/via3 stack to a metal4 strip into the bottom
plate (dz), its right terminal on metal2 to the out spine, landing on a
2x2 via2 (em_ir puts out's whole current through it). Every strap
stays as short as the old cap's were, so each spine end of a wire is
within reach of a node of magic's FastHenry geometry (em_ir). xrz carries
its own substrate contact; the substrate is vss. It is a ppolyf_u, not the
1 kohm/sq ppolyf_u_1k: it was drawn when pex_sim's second extractor (kpex)
refused the high-Rs layer 62/0. kpex now takes 62/0 around a poly resistor
body (engine/lib/kpexlib.py), so this is history and no longer a constraint.

Pins (metal1/metal2 label layers, PINS order: magic numbers ports in label
order) sit on their tracks; tail, d1, d2 and dz are labelled on metal2 drawing,
a name and not a port, so layout_match can tell the pair's units apart.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "engine" / "lib"))
import layoutlib  # noqa: E402

LY = dict(layoutlib.GF180_LAYER)
LY.update({"via3": (40, 0), "metal4": (46, 0), "via4": (41, 0),
           "metal5": (81, 0)})
M1, M2, M3 = LY["metal1"], LY["metal2"], LY["metal3"]
M4, M5 = LY["metal4"], LY["metal5"]

CELL = "two_stage_miller_ota"
PINS = ["inp", "inn", "out", "vdd", "vss", "ibias"]

L_GATE = 1.0
PITCH = 3.0       # every row's unit pitch
TAP = 1.0         # tap comp side
GAP = 1.2         # a row-end tap's comp to the row's end comp
TAP_STEP = 10.0   # substrate/nwell taps no further apart than this
CH_IN = 1.0       # a row's pad edge to its channel's nearest track edge
TRK_SP = 0.4      # metal2 track edge to edge
TRK_W = 0.4
RAIL_W = 2.0      # the wide vss/vdd rails
OUT_W = 1.0       # out carries the second stage's 200uA: two via1 cuts
SPINE_W = {"vss": 1.0, "vdd": 1.0, "out": 1.0, "d2": 0.4}
SPINE_SP = 0.6    # metal3 spine edge to edge
SPINE_ORDER = ["vss", "vdd", "out", "d2"]   # nearest the rows first
NW_ENC = 0.8      # nwell margin round the nwell taps
CUT, CUT_SP, CUT_ENC = 0.26, 0.26, 0.07
VIA_GAP = 0.8     # same-net stub ends closer than this share one via1

CC_SIDE = 26.0    # xcc: c_width = c_length = cc_w
RZ_W, RZ_L = 2.0, 25.5  # xrz: r_width = rz_w, r_length = rz_l
RZ_SP = 1.0       # xrz's cell edge to the d2 spine's metal3
RZ_DROP = 1.5     # xrz's cell top below the cap's bottom plate (its metal2
                  # to out then clears the out track's at the spine)
RZ_A_IN = 1.5     # xrz's left terminal this far inside the plate's left edge
MIM_ENC = 0.6     # draw_cap_mim's own bottom-plate margin
BOT_EXT = 2.0     # bottom plate widened right of the top plate, for via4
M4_KEEP = 2.0     # other metal4 kept this far off the cap's bottom plate
STRAP_SEP = 4.0   # the two plate straps' y apart

# unit width (um), the matched groups or the plain unit list per row
ROWS = [
    ("N1", "n", 4.0, {"xmb": 1, "xmt": 2, "xm7": 20}, "common_centroid"),
    ("N2", "n", 5.0, {"xm1": 2, "xm2": 2}, "ABBA"),
    ("P1", "p", 2.0, {"xm3": 2, "xm4": 2}, "ABBA"),
    ("P2", "p", 8.0, {"xm6": 10}, None),
]
# device: (drain, gate, source) nets
DEV = {"xmb": ("ibias", "ibias", "vss"), "xmt": ("tail", "ibias", "vss"),
       "xm7": ("out", "ibias", "vss"), "xm1": ("d1", "inn", "tail"),
       "xm2": ("d2", "inp", "tail"), "xm3": ("d1", "d1", "vdd"),
       "xm4": ("d2", "d1", "vdd"), "xm6": ("out", "d2", "vdd")}
# channel tracks, bottom to top: (net, width)
CHANNELS = [
    [("vss", RAIL_W)],
    [("ibias", TRK_W), ("out", OUT_W), ("tail", TRK_W), ("inp", TRK_W),
     ("inn", TRK_W), ("vss", TRK_W)],
    [("d1", TRK_W), ("d2", TRK_W)],
    [("vdd", RAIL_W), ("d2", TRK_W)],
    [("out", OUT_W), ("vdd", TRK_W)],
]
# which channel each row's terminals go to: (down channel, up channel)
ROW_CH = {"N1": (0, 1), "N2": (1, 2), "P1": (2, 3), "P2": (3, 4)}
RAIL = {"n": "vss", "p": "vdd"}


def r5(v):
    """Round onto the 5 nm grid."""
    return round(round(v / 0.005) * 0.005, 3)


def rect(top, x0, y0, x1, y1, layer):
    layoutlib.rect(top, r5(min(x0, x1)), r5(min(y0, y1)),
                   r5(max(x0, x1)), r5(max(y0, y1)), layer)


def via(top, x, y, cut, lower, upper, land=TRK_W):
    h, e = CUT / 2, land / 2
    rect(top, x - h, y - h, x + h, y + h, LY[cut])
    rect(top, x - e, y - e, x + e, y + e, lower)
    rect(top, x - e, y - e, x + e, y + e, upper)


def via_array(top, x0, y0, x1, y1, cut):
    """As many cuts as fit in the box (x0..x1, y0..y1), CUT_ENC inside it
    (the box is both metals' overlap, already drawn). Returns the count."""
    def axis(a, b):
        n = max(1, int(math.floor((b - a - 2 * CUT_ENC + CUT_SP)
                                  / (CUT + CUT_SP) + 1e-9)))
        span = n * CUT + (n - 1) * CUT_SP
        s = r5((a + b) / 2 - span / 2)
        return [r5(s + i * (CUT + CUT_SP)) for i in range(n)]
    n = 0
    for cx in axis(x0, x1):
        for cy in axis(y0, y1):
            rect(top, cx, cy, cx + CUT, cy + CUT, LY[cut])
            n += 1
    return n


def shift(b, dx, dy):
    return (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)


def cx_of(b):
    return (b[0] + b[2]) / 2


def res_cell(draw_res):
    """xrz: a flat ppolyf_u (RZ_W x RZ_L), drawn as r2r_dac's units are.
    Returns (cell, pads): pads 'sub', 'a' (left terminal), 'b' (right),
    each a metal1 box in the cell's frame."""
    import gdsfactory as gf

    cell = gf.Component()
    cell.add_ref(draw_res.draw_ppolyf_res(l_res=RZ_L, w_res=RZ_W,
                                          res_type="ppolyf_u"))
    cell.flatten()
    boxes = layoutlib.layer_boxes(cell, M1)
    if len(boxes) != 3:
        raise layoutlib.LayoutError(
            f"expected 3 metal1 pads on xrz, got {len(boxes)}")
    sub, a, b = sorted(boxes, key=lambda r: r[0])
    return cell, {"sub": sub, "a": a, "b": b}


def generate():
    import gdsfactory as gf

    draw_fet, draw_res = layoutlib.gf180_cells()
    from cells.draw_cap_mim import draw_cap_mim

    top = gf.Component()
    cells, local = {}, {}
    for name, kind, w, _u, _p in ROWS:
        f = draw_fet.draw_nfet if kind == "n" else draw_fet.draw_pfet
        cells[name] = f(l_gate=L_GATE, w_gate=w)
        local[name] = layoutlib.fet_pads(cells[name])
    nslots = {name: (sum(u.values()) + (2 if pat else 0))
              for name, _k, _w, u, pat in ROWS}
    # the cell's own comp centre: rows are centred on N1's centre comp
    comp_cx = {n: cx_of(layoutlib.layer_boxes(c, LY["comp"])[0])
               for n, c in cells.items()}
    row_cx = (nslots["N1"] - 1) / 2 * PITCH + comp_cx["N1"]

    def row_x0(name):
        return r5(row_cx - (nslots[name] - 1) / 2 * PITCH - comp_cx[name])

    stubs = []   # (net, x0, x1, y_pad_edge, "up"|"down", channel)
    labels = []
    chan_lo = [None] * len(CHANNELS)   # each channel's lowest track edge

    def tracks_of(ci):
        """Track centres of channel ci, its first track edge at chan_lo."""
        y, out = chan_lo[ci], []
        for _net, w in CHANNELS[ci]:
            out.append(r5(y + w / 2))
            y += w + TRK_SP
        return out

    def chan_top(ci):
        ys = tracks_of(ci)
        return ys[-1] + CHANNELS[ci][-1][1] / 2

    # ---- R0: substrate taps under N1, then c0 ----
    n1_x0 = row_x0("N1")
    n1_comp = layoutlib.layer_boxes(cells["N1"], LY["comp"])[0]
    span0 = n1_x0 + n1_comp[0]
    span1 = n1_x0 + (nslots["N1"] - 1) * PITCH + n1_comp[2]
    nt = int(math.ceil((span1 - span0 - TAP) / TAP_STEP)) + 1
    for i in range(nt):
        tx = r5(span0 + i * (span1 - span0 - TAP) / (nt - 1))
        top.add_ref(layoutlib.psub_tap(TAP)).move((tx, 0.0))
        pad = (tx + 0.2, 0.2, tx + 0.8, 0.8)
        stubs.append(("vss", pad[0], pad[2], pad[3], "up", 0))
    chan_lo[0] = 0.8 + CH_IN

    rows = {}
    y_edge = chan_top(0) + CH_IN
    for ri, (name, kind, w, units, pat) in enumerate(ROWS):
        lp = local[name]
        oy = r5(y_edge - lp["gate_bot"][1])
        ox = row_x0(name)
        if pat:
            mr = layoutlib.matched_row(top, cells[name], units, pitch=PITCH,
                                       pattern=pat, origin=(ox, oy))
            slots = [(p["device"], p["origin"]) for p in mr["pads"]]
        else:
            (dev, n), = units.items()
            slots = []
            for i in range(n):
                at = (r5(ox + i * PITCH), oy)
                top.add_ref(cells[name]).move(at)
                slots.append((dev, at))
        dn, up = ROW_CH[name]
        rail = RAIL[kind]
        for dev, (sx, sy) in slots:
            p = {k: shift(v, sx, sy) for k, v in lp.items()}
            if dev is None:
                # a dummy: every terminal on the rail, on the rail's side
                ch, d = (dn, "down") if kind == "n" else (up, "up")
                g = p["gate_bot"] if kind == "n" else p["gate_top"]
                for b in (p["s"], p["d"], g):
                    stubs.append((rail, b[0], b[2],
                                  b[1] if d == "down" else b[3], d, ch))
                continue
            dnet, gnet, snet = DEV[dev]
            plan = {
                "N1": [("s", snet, "down"), ("gate_top", gnet, "up"),
                       ("d", dnet, "up")],
                "N2": [("s", snet, "down"), ("gate_bot", gnet, "down"),
                       ("d", dnet, "up")],
                "P1": [("s", snet, "up"), ("d", dnet, "down"),
                       ("gate_bot", gnet, "down")],
                "P2": [("s", snet, "down"), ("gate_bot", gnet, "down"),
                       ("d", dnet, "up")],
            }[name]
            for pad, net, d in plan:
                b = p[pad]
                stubs.append((net, b[0], b[2], b[1] if d == "down" else b[3],
                              d, dn if d == "down" else up))
        # one implant across the row: neighbouring units' pplus/nplus sit
        # 0.36 apart at this pitch, under PP.2/NP.2's 0.4, so bridge them
        imp = LY["pplus" if kind == "p" else "nplus"]
        ib = layoutlib.layer_boxes(cells[name], imp)
        if ib:
            ix0 = min(b[0] for b in ib)
            ix1 = max(b[2] for b in ib)
            iy0 = min(b[1] for b in ib)
            iy1 = max(b[3] for b in ib)
            top.add_polygon([(slots[0][1][0] + ix0, oy + iy0),
                             (slots[-1][1][0] + ix1, oy + iy0),
                             (slots[-1][1][0] + ix1, oy + iy1),
                             (slots[0][1][0] + ix0, oy + iy1)], layer=imp)
        first = shift(lp["s"], *slots[0][1])
        y_bot = oy + lp["gate_bot"][1]
        y_top = oy + lp["gate_top"][3]
        x_lo = slots[0][1][0] + layoutlib.layer_boxes(
            cells[name], LY["comp"])[0][0]
        x_hi = slots[-1][1][0] + layoutlib.layer_boxes(
            cells[name], LY["comp"])[0][2]
        rows[name] = dict(slots=slots, oy=oy, y_bot=y_bot, y_top=y_top,
                          x_lo=x_lo, x_hi=x_hi, first=first)
        chan_lo[up] = y_top + CH_IN
        y_edge = chan_top(up) + CH_IN

    # ---- row-end taps: P+ at N2's ends (down to c1 vss), N+ at P1's ends
    # (up to c3 vdd); N+ taps along P2's top (down to c4 vdd) ----
    nw_taps = []
    for name, maker, net, d, ch in (("N2", layoutlib.psub_tap, "vss",
                                     "down", 1),
                                    ("P1", layoutlib.nwell_tap, "vdd",
                                     "up", 3)):
        r = rows[name]
        ty = r5((r["first"][1] + r["first"][3]) / 2 - TAP / 2)
        for tx in (r5(r["x_lo"] - GAP - TAP), r5(r["x_hi"] + GAP)):
            top.add_ref(maker(TAP)).move((tx, ty))
            pad = (tx + 0.2, ty + 0.2, tx + 0.8, ty + 0.8)
            stubs.append((net, pad[0], pad[2],
                          pad[1] if d == "down" else pad[3], d, ch))
            if name == "P1":
                nw_taps.append((tx, ty))
    r = rows["P2"]
    ty = r5(y_edge - 0.2)
    nt = int(math.ceil((r["x_hi"] - r["x_lo"] - TAP) / TAP_STEP)) + 1
    for i in range(nt):
        # taps between P2's drain columns: they share x with nothing below
        tx = r5(r["x_lo"] + i * (r["x_hi"] - r["x_lo"] - TAP) / (nt - 1))
        top.add_ref(layoutlib.nwell_tap(TAP)).move((tx, ty))
        stubs.append(("vdd", tx + 0.2, tx + 0.8, ty + 0.2, "down", 4))
        nw_taps.append((tx, ty))

    # ---- nwell: one rectangle over P1, P2 and their taps ----
    nwb = []
    for name in ("P1", "P2"):
        wb = layoutlib.layer_boxes(cells[name], LY["nwell"])[0]
        for _dev, (sx, sy) in rows[name]["slots"]:
            nwb.append(shift(wb, sx, sy))
    for tx, ty in nw_taps:
        nwb.append((tx - NW_ENC, ty - NW_ENC, tx + TAP + NW_ENC,
                    ty + TAP + NW_ENC))
    rect(top, min(b[0] for b in nwb), min(b[1] for b in nwb),
         max(b[2] for b in nwb), max(b[3] for b in nwb), LY["nwell"])

    # ---- tracks: every stub's net on its channel ----
    tracks = {}   # (channel, net) -> y, width
    for ci, chan in enumerate(CHANNELS):
        for (net, w), y in zip(chan, tracks_of(ci)):
            tracks[(ci, net)] = (y, w)
    index = {(ci, net): i for ci, chan in enumerate(CHANNELS)
             for i, (net, _w) in enumerate(chan)}
    for s in stubs:
        if (s[5], s[0]) not in tracks:
            raise layoutlib.LayoutError(
                f"a {s[0]} stub has no track in channel c{s[5]}")
    # a stub from below and one from above that share x (with metal1's
    # spacing) must end on tracks in that order, unless one net
    for a in stubs:
        for b in stubs:
            if (a[4], b[4]) != ("up", "down") or a[5] != b[5] or a[0] == b[0]:
                continue
            if a[1] - 0.25 < b[2] and b[1] - 0.25 < a[2]:
                if index[(a[5], a[0])] >= index[(b[5], b[0])]:
                    raise layoutlib.LayoutError(
                        f"c{a[5]}: a {a[0]} stub from below and a {b[0]} "
                        f"stub from above cross at x={a[1]:.2f}")
    # one net's stubs that land closer than a via pitch on one track (a
    # tap beside a source, P1's source over P2's) share one via1, their
    # ends joined by a metal1 bridge along the track
    xs = {}
    ends = {}
    for net, x0, x1, ye, d, ci in stubs:
        y, _w = tracks[(ci, net)]
        rect(top, x0, ye, x1, y + (0.2 if d == "up" else -0.2), M1)
        ends.setdefault((ci, net), []).append((x0, x1))
    for key, spans in ends.items():
        y, _w = tracks[key]
        spans.sort()
        groups = [[spans[0]]]
        for sp in spans[1:]:
            if sp[0] - groups[-1][-1][1] < VIA_GAP:
                groups[-1].append(sp)
            else:
                groups.append([sp])
        for g in groups:
            gx0 = min(a for a, _b in g)
            gx1 = max(b for _a, b in g)
            # a column of cuts across the track's width: one via1 cut
            # carries 0.18 mA (EM), and out and the rails carry more; the
            # bridge is as tall as that landing, or the two leave a notch
            w = tracks[key][1]
            if len(g) > 1:
                rect(top, gx0, y - w / 2, gx1, y + w / 2, M1)
            cx = r5((g[0][0] + g[0][1]) / 2)
            rect(top, cx - 0.2, y - w / 2, cx + 0.2, y + w / 2, M1)
            via_array(top, cx - 0.2, y - w / 2, cx + 0.2, y + w / 2, "via1")
            xs.setdefault(key, []).append(cx)
    for key in tracks:
        if key not in xs:
            raise layoutlib.LayoutError(
                f"track {key[1]} in channel c{key[0]} has no stub")

    # ---- spines: metal3, left of everything, for nets on two channels ----
    multi = sorted({n for (_c, n) in tracks
                    if sum(1 for (_c2, n2) in tracks if n2 == n) > 1},
                   key=SPINE_ORDER.index)
    bb = top.dbbox()
    spine_x = {}
    x = bb.left - 2.0
    for net in multi:
        w = SPINE_W[net]
        spine_x[net] = r5(x - w / 2)
        x -= w + SPINE_SP
    spine_y = {n: [] for n in multi}
    for (ci, net), (y, w) in tracks.items():
        x0 = min(xs[(ci, net)]) - 0.2
        x1 = max(xs[(ci, net)]) + 0.2
        if net in spine_x:
            sx, sw = spine_x[net], SPINE_W[net]
            x0 = sx - sw / 2
            via_array(top, sx - sw / 2, y - w / 2, sx + sw / 2, y + w / 2,
                      "via2")
            spine_y[net] += [y - w / 2, y + w / 2]
        rect(top, x0, y - w / 2, x1, y + w / 2, M2)

    # ---- xcc then xrz: the MIM-B left of the spines, the resistor below it
    d2_ys = [tracks[k][0] for k in tracks if k[1] == "d2"]
    y_d2s = r5(sum(d2_ys) / len(d2_ys))
    y_outs = r5(y_d2s - STRAP_SEP)
    px = {"d2": spine_x["d2"], "out": spine_x["out"]}
    cap_xr = min(px.values()) - 0.2 - M4_KEEP
    fx1 = r5(cap_xr - MIM_ENC - BOT_EXT)
    fx0 = r5(fx1 - CC_SIDE)
    fy0 = r5((y_d2s + y_outs) / 2 - CC_SIDE / 2)
    fy1 = fy0 + CC_SIDE
    top.add_ref(draw_cap_mim(mim_option="MIM-B", metal_level="M5",
                             lc=CC_SIDE, wc=CC_SIDE)).move((fx0, fy0))
    rect(top, fx1, fy0 - MIM_ENC, fx1 + MIM_ENC + BOT_EXT, fy1 + MIM_ENC, M4)
    if not fy0 + 1.0 <= y_d2s <= fy1 - 1.0:
        raise layoutlib.LayoutError(
            f"xcc.top: strap y={y_d2s:.2f} is off the plate")
    # top plate (d2): metal5 right to a via4+via3 stack on the d2 spine
    rect(top, fx1 - 1.5, y_d2s - 0.5, px["d2"] + 0.5, y_d2s + 0.5, M5)
    via(top, px["d2"], y_d2s, "via4", M4, M5)
    via(top, px["d2"], y_d2s, "via3", M3, M4)
    spine_y["d2"] += [y_d2s - 0.5, y_d2s + 0.5]
    # xrz below the bottom plate: its left terminal (a) RZ_A_IN inside the
    # plate's left edge, its cell's top RZ_DROP below the plate
    rz, rp = res_cell(draw_res)
    rbb = rz.dbbox()
    plate_y0 = fy0 - MIM_ENC
    rx = r5(fx0 - MIM_ENC + RZ_A_IN - cx_of(rp["a"]))
    ry = r5(plate_y0 - RZ_DROP - rbb.top)
    if rx + rbb.right > px["d2"] - 0.2 - RZ_SP:
        raise layoutlib.LayoutError("xrz runs into the d2 spine")
    top.add_ref(rz).move((rx, ry))
    pa = shift(rp["a"], rx, ry)
    pb = shift(rp["b"], rx, ry)
    y_rz = r5((pa[1] + pa[3]) / 2)
    # a (dz): via1..via3 up to metal4, metal4 up into the bottom plate
    ax = r5(cx_of(pa))
    for cut, lo, hi in (("via1", M1, M2), ("via2", M2, LY["metal3"]),
                        ("via3", M3, M4)):
        via(top, ax, y_rz, cut, lo, hi)
    rect(top, ax - 0.5, y_rz - 0.5, ax + 0.5, plate_y0 + 0.5, M4)
    # b (out): via1 on its pad, metal2 right to a via2 on the out spine
    bx = r5(cx_of(pb))
    via(top, bx, y_rz, "via1", M1, M2)
    rect(top, bx - 0.2, y_rz - 0.2, px["out"], y_rz + 0.2, M2)
    # the via2 there carries out's full current in em_ir (0.205 mA, over
    # one cut's 0.18): a spine-wide landing hung below the strap, clear of
    # the out track above, takes a 2x2 array
    ow = SPINE_W["out"]
    lb = (px["out"] - ow / 2, y_rz + 0.2 - ow, px["out"] + ow / 2, y_rz + 0.2)
    rect(top, *lb, M2)
    rect(top, *lb, M3)
    via_array(top, *lb, "via2")
    spine_y["out"] += [lb[1], lb[3]]
    for net, sx in spine_x.items():
        w = SPINE_W[net]
        rect(top, sx - w / 2, min(spine_y[net]), sx + w / 2,
             max(spine_y[net]), M3)

    # ---- labels: pins on the label layers, internal nets on drawing ----
    def on_track(ci, net, x=None):
        y, _w = tracks[(ci, net)]
        if x is None:
            x = sum(xs[(ci, net)]) / len(xs[(ci, net)])
            x = min(xs[(ci, net)], key=lambda v: abs(v - x))
        return r5(x), y
    xmb = next(o for d, o in rows["N1"]["slots"] if d == "xmb")
    x_vss = r5(xmb[0] + cx_of(local["N1"]["s"]))
    p2 = rows["P2"]["slots"]
    x_vdd = r5(p2[len(p2) // 2][1][0] + cx_of(local["P2"]["s"]))
    for net, ci, x, layer in (
            ("inp", 1, None, "metal2_label"), ("inn", 1, None, "metal2_label"),
            ("out", 1, None, "metal2_label"),
            ("vdd", 3, x_vdd, "metal2_label"),
            ("vss", 0, x_vss, "metal2_label"),
            ("ibias", 1, None, "metal2_label"),
            ("tail", 1, None, "metal2"), ("d1", 2, None, "metal2"),
            ("d2", 2, None, "metal2")):
        lx, ly = on_track(ci, net, x)
        labels.append((net, lx, ly, LY[layer]))
    # dz (Cc bottom plate to xrz) holds no transistor: label it so the
    # extraction keeps its own node and its plate C to the substrate.
    labels.append(("dz", ax, y_rz, LY["metal2"]))
    labels.sort(key=lambda lbl: (PINS.index(lbl[0]) if lbl[0] in PINS
                                 else len(PINS)))
    return layoutlib.finalize(top, CELL, labels)


if __name__ == "__main__":
    comp = generate()
    out_path = sys.argv[1] if len(sys.argv) > 1 else f"{CELL}.gds"
    comp.write_gds(out_path)
    print(f"wrote {out_path} bbox={comp.dbbox()}")

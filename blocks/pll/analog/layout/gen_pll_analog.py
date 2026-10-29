"""gen_pll_analog.py - layout generator for blocks/pll/analog (pll_analog,
the analog half of the GF180 charge-pump PLL). Layout is code: every shape
below is drawn by this file from GF180's own primitive cells.

Draws netlist/pll_analog.cir at sizing/sizing.yaml. Sizes are typed in
here as constants named after the sizing.yaml entries; LVS checks them.

Floorplan, bottom to top: the VCO block, then five rows, each with a
private routing channel above it:

  ring     the VCO ring block (ring_block): stages in the order
           3 2 4 1 5, then NAND2 and output inverter, flush right under
           the vco_out pin. NMOS half below one shared channel, PMOS half
           mirrored above it. Each stage is one column: its inverter
           (2 fingers, drain in the middle) next to the channel, its
           starve device behind it, n_k/p_k joined by a metal2 strap.
           Sources tie in metal1 to a rail per half. The starve gates
           run on their own metal3 track behind the inverters, so the
           channel carries only r1..r5, nb, vco_out and pll_en, as short
           metal3 hops between 0.3 um stubs. The rails and the
           vbn_vco/vbp_vco tracks rise in metal4 at the block's left end
           to the rows above.
  mix_n    buffer NMOS pair (ABBA) and mirror load (ABBA), the vref
           mirror bank (FLR BUFT BUFT B1 BUFT BUFT), MN_B2, startup,
           standby clamps, starve masters MN_V2IM/MN_ENV, V2I, ENS
  buf_p    buffer PMOS pair (ABBAABBA), PMOS mirror load (ABBA), the
           inverters' PMOS, MP_STL, UP switches, starve master PMOS pair
           MP_V2I/MP_V2IB (ABBA), MP_ENV
  res      ppolyf_u serpentines: R1, R_V2I, RBIAS_TOP, RBIAS_INT
  cp_n     pump NMOS mirror CPREF/CPREF2/DNS (ABCCBA), DN and trim
           switches, inverter NMOS, MN_CL
  cp_p     pump PMOS mirror CPREF/UPS (ABBA, 25 um units) and the
           14-unit vbp bank, common centroid about its middle:
           BUFT T4 BUFT T4 T2 T1A B2 B1 T1B T2 T4 BUFT T4 BUFT

The ring block is drawn for speed, not equal wire lengths: post-layout
fmax is set by the wire and drain-junction C on r1..r5, so each node gets
the shortest wiring the folded order allows. No metal4 and no cap plate
is near it.

C1 (20 pF, 100 x 100 um) sits over res/cp_n/cp_p and C2 (2 pF) over
mix_n/buf_p, both MIM-B (metal4 bottom plate on vss, metal5 top). No
metal4 trunk runs under a cap (MIMTM.1); nets of the covered rows take
their trunks in a strip right of C1. Both plates come out in metal5 to a
via4/via3 stack on their own net's track beside the cap.

Outside the ring block each device is one or more single-finger units
(draw_nfet/draw_pfet, gate contact on top). netgen merges parallel units (W add) and series
resistor segments (r_length add), so a W=50 device is 2 x 25 here.

Routing ("row + channel"): in a row every unit has three metal2 stubs,
all going UP into the row's own channel: left diffusion, right
diffusion, and the gate, whose metal1 jog carries it to a via right of
the right diffusion pad (so the three stubs never sit closer than metal2
spacing, even at L=0.28). A diode-connected unit (gate = right pad net)
ties the jog to its right pad in metal1. Channel tracks are metal3, one
net per track span (left-edge packing). A net seen in more than one
channel gets a vertical metal4 trunk with a via3 on each of its tracks.
Metal4 is kept off the caps (MIMTM.1). Pins: digital-side pins (vco_out,
pfd_up, pfd_dn, pll_en, cp_trim0, cp_trim1) are metal3 tracks run out to
the RIGHT edge - the edge facing the digital half and its prescaler;
vctrl, bias_ref, vdd, vss are metal4 trunks run to the BOTTOM edge. Each
pin ends in a via stack on a metal2 pad carrying its metal2 pin label.

Taps: a P+ substrate tap (vss) at each end of every NMOS row and at
most ~10 um apart inside it (DF.14), an N+ tap (vdd) likewise in every
PMOS row under one nwell per row (DF.13). Every resistor segment carries
the PDK cell's own substrate tap, strapped to vss.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# layout_gen.py puts engine/lib on sys.path itself; this is for running
# the file directly.
sys.path.insert(0, str(Path(os.environ.get(
    "CHIP_FLOW_HOME", Path.home() / ".claude/skills/chip-flow"))
    / "engine" / "lib"))
import layoutlib  # noqa: E402

LY = dict(layoutlib.GF180_LAYER)
LY.update({"via2": (38, 0), "metal3": (42, 0), "via3": (40, 0),
           "metal4": (46, 0), "via4": (41, 0), "metal5": (81, 0),
           "metal3_label": (42, 10), "metal4_label": (46, 10)})
M1, M2, M3, M4, M5 = (LY[k] for k in
                      ("metal1", "metal2", "metal3", "metal4", "metal5"))

CELL = "pll_analog"
PINS = ["vco_out", "pfd_up", "pfd_dn", "pll_en", "cp_trim0", "cp_trim1",
        "vctrl", "bias_ref", "vdd", "vss"]
RIGHT_PINS = ["vco_out", "pfd_up", "pfd_dn", "pll_en", "cp_trim0",
              "cp_trim1"]
BOTTOM_PINS = ["vctrl", "bias_ref", "vdd", "vss"]

# ---- sizing (um), sizing/sizing.yaml values ----------------------------
W_BP, L_BP = 4.0, 2.0
W_B1, L_B1 = 2.0, 2.0
W_B2, L_B2 = 4.0, 1.0
RB_L, RBT_L = 366.0, 180.0
W_CPN, L_CPN = 20.0, 2.0
W_CPP, L_CPP = 50.0, 2.0
W_SWN, W_SWP, L_SW = 2.0, 4.0, 0.5
R1_L = 150.0
C1_W, C2_W = 100.0, 32.0
W_V2I, L_V2I, RV2I_L = 10.0, 1.0, 35.0
W_FLR, L_FLR = 2.0, 2.0
W_V2IP, W_V2IN, L_V2I2 = 10.0, 4.0, 0.5
W_SP, W_SN, L_S = 10.0, 4.0, 0.5
W_RP, W_RN, L_R = 6.0, 3.0, 0.28
W_GP, W_GN = 4.0, 4.0
W_OP, W_ON = 12.0, 6.0
M_BUFT = 4
W_BUFP, L_BUFP, W_BUFN, L_BUFN = 20.0, 1.0, 10.0, 1.0
W_BUFLN, W_BUFLP, L_BUFL = 4.0, 8.0, 2.0
W_CL, L_CL = 40.0, 2.0
LMIN = 0.28

# ---- devices: name -> (type, drain, gate, source, W total, L, units) ---
# W total includes the netlist's m; units = how many equal single-finger
# pieces it is drawn as.
DEV = {
    "MP_ENI": ("p", "en_b", "pll_en", "vdd", 2.0, LMIN, 1),
    "MN_ENI": ("n", "en_b", "pll_en", "vss", 1.0, LMIN, 1),
    "MP_B2": ("p", "vbp", "vbp", "vdd", W_BP, L_BP, 1),
    "MP_B1": ("p", "va", "vbp", "vdd", W_BP, L_BP, 1),
    "MN_B1": ("n", "va", "vref", "vss", W_B1, L_B1, 1),
    "MN_B2": ("n", "vbp", "va", "vref", W_B2, L_B2, 1),
    "MP_STL": ("p", "vst", "en_b", "vdd", 0.5, 10.0, 1),
    "MN_STD": ("n", "vst", "vref", "vss", 10.0, 1.0, 2),
    "MN_ST": ("n", "vbp", "vst", "vss", 0.5, 2.0, 1),
    "MN_STE": ("n", "vst", "en_b", "vss", 1.0, LMIN, 1),
    "MP_ENB": ("p", "vbp", "pll_en", "vdd", 1.0, LMIN, 1),
    "MN_ENA": ("n", "va", "en_b", "vss", 1.0, LMIN, 1),
    "MN_ENB": ("n", "vref", "en_b", "vss", 1.0, LMIN, 1),
    "MP_T4": ("p", "vcp_n", "vbp", "vdd", 4 * W_BP, L_BP, 4),
    "MP_T1A": ("p", "t1a", "vbp", "vdd", W_BP, L_BP, 1),
    "MN_SW0": ("n", "vcp_n", "cp_trim0", "t1a", 2.0, LMIN, 1),
    "MP_T2": ("p", "t2", "vbp", "vdd", 2 * W_BP, L_BP, 2),
    "MN_SW1": ("n", "vcp_n", "cp_trim1", "t2", 2.0, LMIN, 1),
    "MP_T1B": ("p", "t1b", "vbp", "vdd", W_BP, L_BP, 1),
    "MN_SW0B": ("n", "t1c", "cp_trim0", "t1b", 2.0, LMIN, 1),
    "MN_SW1B": ("n", "vcp_n", "cp_trim1", "t1c", 2.0, LMIN, 1),
    "MN_CPREF": ("n", "vcp_n", "vcp_n", "vss", W_CPN, L_CPN, 2),
    "MN_CPREF2": ("n", "vcp_p", "vcp_n", "vss", W_CPN, L_CPN, 2),
    "MP_CPREF": ("p", "vcp_p", "vcp_p", "vdd", W_CPP, L_CPP, 2),
    "MP_UPS": ("p", "up_s", "vcp_p", "vdd", W_CPP, L_CPP, 2),
    "MP_UP": ("p", "vctrl", "up_b", "up_s", W_SWP, L_SW, 1),
    "MP_UPD": ("p", "vdump", "pfd_up", "up_s", W_SWP, L_SW, 1),
    "MP_UPI": ("p", "up_b", "pfd_up", "vdd", 2.0, LMIN, 1),
    "MN_UPI": ("n", "up_b", "pfd_up", "vss", 1.0, LMIN, 1),
    "MN_DNS": ("n", "dn_s", "vcp_n", "vss", W_CPN, L_CPN, 2),
    "MN_DN": ("n", "vctrl", "pfd_dn", "dn_s", W_SWN, L_SW, 1),
    "MN_DND": ("n", "vdump", "dn_b", "dn_s", W_SWN, L_SW, 1),
    "MP_DNI": ("p", "dn_b", "pfd_dn", "vdd", 2.0, LMIN, 1),
    "MN_DNI": ("n", "dn_b", "pfd_dn", "vss", 1.0, LMIN, 1),
    "MP_ENP": ("p", "vcp_p", "pll_en", "vdd", 1.0, LMIN, 1),
    "MN_ENN": ("n", "vcp_n", "en_b", "vss", 1.0, LMIN, 1),
    "MP_BUFT": ("p", "vbt", "vbp", "vdd", M_BUFT * W_BP, L_BP, M_BUFT),
    "MP_BUF1": ("p", "vbl", "vctrl", "vbt", W_BUFP, L_BUFP, 4),
    "MP_BUF2": ("p", "vdump", "vdump", "vbt", W_BUFP, L_BUFP, 4),
    "MN_BUFL1": ("n", "vbl", "vbl", "vss", W_BUFLN, L_BUFL, 2),
    "MN_BUFL2": ("n", "vdump", "vbl", "vss", W_BUFLN, L_BUFL, 2),
    "MN_BUFT": ("n", "vnt", "vref", "vss", M_BUFT * W_B1, L_B1, M_BUFT),
    "MN_BUF1": ("n", "vbh", "vctrl", "vnt", W_BUFN, L_BUFN, 2),
    "MN_BUF2": ("n", "vdump", "vdump", "vnt", W_BUFN, L_BUFN, 2),
    "MP_BUFL1": ("p", "vbh", "vbh", "vdd", W_BUFLP, L_BUFL, 2),
    "MP_BUFL2": ("p", "vdump", "vbh", "vdd", W_BUFLP, L_BUFL, 2),
    "MN_V2I": ("n", "vi", "vctrl", "vs", W_V2I, L_V2I, 2),
    "MN_CL": ("n", "vcl", "vref", "vss", W_CL, L_CL, 4),
    "MN_FLR": ("n", "vi", "vref", "vss", W_FLR, L_FLR, 1),
    "MN_ENS": ("n", "vbp_vco", "pll_en", "vi", 10.0, LMIN, 2),
    "MP_V2I": ("p", "vbp_vco", "vbp_vco", "vdd", W_V2IP, L_V2I2, 2),
    "MP_V2IB": ("p", "vbn_vco", "vbp_vco", "vdd", W_V2IP, L_V2I2, 2),
    "MN_V2IM": ("n", "vbn_vco", "vbn_vco", "vss", W_V2IN, L_V2I2, 1),
    "MP_ENV": ("p", "vbp_vco", "pll_en", "vdd", 1.0, LMIN, 1),
    "MN_ENV": ("n", "vbn_vco", "en_b", "vss", 1.0, LMIN, 1),
    "MP_G1": ("p", "nb", "r5", "vdd", W_GP, LMIN, 1),
    "MP_G2": ("p", "nb", "pll_en", "vdd", W_GP, LMIN, 1),
    "MN_G1": ("n", "nb", "r5", "ng", W_GN, LMIN, 1),
    "MN_G2": ("n", "ng", "pll_en", "vss", W_GN, LMIN, 1),
    "MP_O": ("p", "vco_out", "nb", "vdd", W_OP, LMIN, 2),
    "MN_O": ("n", "vco_out", "nb", "vss", W_ON, LMIN, 1),
}
for _k in range(1, 6):
    _prev = f"r{5 if _k == 1 else _k - 1}"
    DEV[f"MP_S{_k}"] = ("p", f"p{_k}", "vbp_vco", "vdd", W_SP, L_S, 1)
    DEV[f"MP_I{_k}"] = ("p", f"r{_k}", _prev, f"p{_k}", W_RP, L_R, 1)
    DEV[f"MN_I{_k}"] = ("n", f"r{_k}", _prev, f"n{_k}", W_RN, L_R, 1)
    DEV[f"MN_S{_k}"] = ("n", f"n{_k}", "vbn_vco", "vss", W_SN, L_S, 1)

# ---- resistors: name -> (end a, end b, width, total length, segments) --
RES = {
    "R_V2I": ("vs", "vcl", 1.0, RV2I_L, 2),
    "R1": ("vctrl", "vfilt", 2.0, R1_L, 4),
    "RBIAS_TOP": ("vref", "bias_ref", 1.0, RBT_L, 6),
    "RBIAS_INT": ("bias_ref", "vss", 1.0, RB_L, 10),
}
# ---- MIM caps: name -> (top plate net, bottom plate net, side) ---------
CAPS = {"C1": ("vfilt", "vss", C1_W), "C2": ("vctrl", "vss", C2_W)}


RING_ORDER = [3, 2, 4, 1, 5]   # every hop spans 1-2 stages; 5 next to NAND
# the ring block: columns left to right, each (NMOS group, PMOS group).
# A group's devices abut, sharing the diffusion between them (a stage's
# starve device and inverter share n_k / p_k; the NAND's series pair
# shares ng; its parallel PMOS pair shares nb).
RING_COLS = (
    [("stage", k) for k in RING_ORDER]
    + [("row", ["MN_G2", "MN_G1"], ["MP_G2", "MP_G1"]),
       ("row", ["MN_O"], ["MP_O"]), ("row", [], ["MP_O"])])
# nets that live only between a stage's (or the NAND's) own devices,
# joined by a metal2 strap under them: no channel stub
INTERNAL = {f"n{k}" for k in range(1, 6)} | {f"p{k}" for k in range(1, 6)} \
    | {"ng"}

# rows bottom to top: (name, kind, items). An item is a device name (its
# next unit) or "T" (a tap). Taps are also added wherever a row runs more
# than TAP_RUN um without one.
ROWS = [
    ("ring", "ring", None),
    ("mix_n", "n", ["MN_BUF1", "MN_BUF2", "MN_BUF2", "MN_BUF1",
                    "MN_BUFL1", "MN_BUFL2", "MN_BUFL2", "MN_BUFL1",
                    "MN_FLR", "MN_BUFT", "MN_BUFT", "MN_B1", "MN_BUFT",
                    "MN_BUFT", "MN_B2", "MN_STD", "MN_STD", "MN_ST",
                    "MN_STE", "MN_ENA", "MN_ENB",
                    "MN_V2IM", "MN_ENV", "MN_V2I", "MN_V2I", "MN_ENS",
                    "MN_ENS"]),
    ("buf_p", "p", ["MP_BUF1", "MP_BUF2", "MP_BUF2", "MP_BUF1",
                    "MP_BUF1", "MP_BUF2", "MP_BUF2", "MP_BUF1",
                    "MP_BUFL1", "MP_BUFL2", "MP_BUFL2", "MP_BUFL1",
                    "MP_UPI", "MP_DNI", "MP_ENP", "MP_ENB", "MP_ENI",
                    "MP_STL", "MP_UP", "MP_UPD",
                    "MP_V2I", "MP_V2IB", "MP_V2IB", "MP_V2I", "MP_ENV"]),
    ("res", "r", None),
    ("cp_n", "n", ["MN_CPREF", "MN_CPREF2", "MN_DNS", "MN_DNS",
                   "MN_CPREF2", "MN_CPREF",
                   "MN_DN", "MN_DND", "MN_SW0", "MN_SW1", "MN_SW0B",
                   "MN_SW1B", "MN_UPI", "MN_DNI", "MN_ENN", "MN_ENI",
                   "MN_CL", "MN_CL", "MN_CL", "MN_CL"]),
    ("cp_p", "p", ["MP_CPREF", "MP_UPS", "MP_UPS", "MP_CPREF",
                   "MP_BUFT", "MP_T4", "MP_BUFT", "MP_T4", "MP_T2",
                   "MP_T1A", "MP_B2", "MP_B1", "MP_T1B", "MP_T2",
                   "MP_T4", "MP_BUFT", "MP_T4", "MP_BUFT"]),
]
RES_ORDER = ["R1", "R_V2I", "RBIAS_TOP", "RBIAS_INT"]

# ---- geometry (um) -----------------------------------------------------
GAP = 1.2         # COMP to COMP between neighbouring units/taps in a row
GOFF = 0.75       # gate via: this far right of the right diffusion pad
TAP = 1.0         # tap COMP size
TAP_RUN = 15.0    # longest stretch of a row without a tap
TRK_W = 0.4       # metal2/3/4 wire width (= a via landing)
TRK_P = 0.8       # metal3 track pitch, metal4 column pitch
RING_TRK_P = 1.0  # the ring block's shared channel: wider pitch and
RING_LW = 0.3     # thinner wires (0.4 via landings kept) - less coupling
CH_IN = 1.0       # row's top shape to the channel's first track
ROW_SP = 1.6      # channel's last track to the next row's lowest shape
RES_P = {1.0: 3.0, 2.0: 4.0}   # resistor segment pitch by width
RES_GAP = 3.0     # extra space between two resistor chains
M4_KEEP = 2.0     # other metal4 kept this far off a cap's bottom plate


# ======================================================================
def r5(v):
    """Round onto the 5 nm grid."""
    return round(round(v / 0.005) * 0.005, 3)


def rect(top, x0, y0, x1, y1, layer):
    layoutlib.rect(top, r5(min(x0, x1)), r5(min(y0, y1)),
                   r5(max(x0, x1)), r5(max(y0, y1)), layer)


def via(top, x, y, cut, lower, upper, land=TRK_W):
    h, e = 0.13, land / 2
    rect(top, x - h, y - h, x + h, y + h, LY[cut])
    rect(top, x - e, y - e, x + e, y + e, lower)
    rect(top, x - e, y - e, x + e, y + e, upper)


def tbox(b, dx, dy):
    return (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)


def cx(b):
    return (b[0] + b[2]) / 2


def cy(b):
    return (b[1] + b[3]) / 2


class Router:
    """Collects terminals per channel, metal4 ports, then draws tracks,
    stubs, trunks."""

    def __init__(self):
        self.ch = []          # channel dicts, bottom to top
        self.ports = []       # (net, x, y): metal4 points a net must reach
        self.keepout = []     # metal4 keep-out boxes (caps)

    def channel(self, name, base_y):
        c = {"name": name, "base": base_y, "terms": [], "tracks": {},
             "ntracks": 0}
        self.ch.append(c)
        return c


def fet_cell(draw_fet, kind, w, l):
    fn = draw_fet.draw_nfet if kind == "n" else draw_fet.draw_pfet
    return fn(l_gate=l, w_gate=w, gate_con_pos="top")


def fet_geom(cell):
    comp = layoutlib.layer_boxes(cell, LY["comp"])[0]
    m1 = layoutlib.layer_boxes(cell, M1)
    if len(m1) != 3:
        raise layoutlib.LayoutError(f"expected 3 metal1 pads, got {m1}")
    by_y = sorted(m1, key=cy)
    gate = by_y[-1]
    left, right = sorted(by_y[:2], key=lambda b: b[0])
    return comp, {"l": left, "r": right, "g": gate}


def build_row(top, draw_fet, name, kind, items, y0, used):
    """Place one row with its COMP bottom at y0. Returns (terms, top_y,
    bottom_y, x_end, nwell box or None). terms: (net, x, y_start)."""
    terms = []
    x = 0.0
    since_tap = 1e9
    tops, bots = [], []
    lefts, rights = [], []

    def place_tap(x):
        cell = (layoutlib.psub_tap(TAP) if kind == "n"
                else layoutlib.nwell_tap(TAP))
        top.add_ref(cell).move((r5(x), r5(y0)))
        net = "vss" if kind == "n" else "vdd"
        via(top, x + TAP / 2, y0 + TAP / 2, "via1", M1, M2)
        terms.append((net, x + TAP / 2, y0 + TAP / 2))
        tops.append(y0 + TAP + 0.2)
        bots.append(y0 - 0.2)
        lefts.append(x)
        rights.append(x + TAP)
        return x + TAP + GAP

    seq = list(items)
    if seq[0] != "T":
        seq.insert(0, "T")
    if seq[-1] != "T":
        seq.append("T")
    for it in seq:
        if it == "T":
            x = place_tap(x)
            since_tap = 0.0
            continue
        typ, d, g, s, wt, l, nu = DEV[it]
        assert typ == kind, (it, kind)
        w = wt / nu
        cell = fet_cell(draw_fet, kind, w, l)
        comp, pads = fet_geom(cell)
        cw = comp[2] - comp[0]
        if since_tap + cw > TAP_RUN:
            x = place_tap(x)
            since_tap = 0.0
        dx, dy = r5(x - comp[0]), r5(y0 - comp[1])
        top.add_ref(cell).move((dx, dy))
        used[it] = used.get(it, 0) + 1
        p = {k: tbox(v, dx, dy) for k, v in pads.items()}
        bb = cell.dbbox()
        tops.append(bb.top + dy)
        bots.append(bb.bottom + dy)
        lefts.append(x)
        rights.append(x + cw)
        # left pad = source, right pad = drain (gate==drain ties right)
        lp, rp, gp = p["l"], p["r"], p["g"]
        for net, pad in ((s, lp), (d, rp)):
            via(top, cx(pad), pad[3] - 0.2, "via1", M1, M2)
            terms.append((net, cx(pad), pad[3] - 0.2))
        xg = cx(rp) + GOFF
        rect(top, gp[0], gp[1], xg + TRK_W / 2, gp[3], M1)
        if g == d:
            rect(top, rp[0], rp[3] - 0.3, rp[2], gp[3], M1)
        else:
            via(top, xg, cy(gp), "via1", M1, M2)
            terms.append((g, xg, cy(gp)))
        tops.append(gp[3] + 0.3)
        x = x + cw + GAP
        since_tap += cw + GAP
    nw = None
    if kind == "p":
        nwb = layoutlib.layer_boxes(top, LY["nwell"])
        nw = (min(lefts) - 1.1, y0 - 0.8, max(rights) + 1.1,
              max(b[3] for b in nwb if b[1] >= y0 - 1.0))
        rect(top, *nw, LY["nwell"])
        bots.append(nw[1])
        tops.append(nw[3])
    return terms, max(tops), min(bots), x - GAP


def build_res_row(top, draw_res, y0):
    """Every resistor as a serpentine of vertical ppolyf_u segments, body
    bottom at y0. Even segment counts put both chain ends on top heads.
    Returns (terms, top_y, bottom_y, x_end)."""
    terms = []
    x = 2.0                      # room for the substrate strap's stub
    subs = []
    tops, bots = [], []
    for rname in RES_ORDER:
        a, b, w, ltot, nseg = RES[rname]
        assert nseg % 2 == 0
        lseg = ltot / nseg
        cell = draw_res.draw_ppolyf_res(l_res=lseg, w_res=w,
                                        res_type="ppolyf_u")
        m1 = layoutlib.layer_boxes(cell, M1)
        heads0 = [bb for bb in m1 if -1.0 < bb[0] < 0]
        heads1 = [bb for bb in m1 if bb[0] > lseg]
        subp = [bb for bb in m1 if bb[2] < -1.0]
        pitch = RES_P[w]
        heads = []
        for i in range(nseg):
            # rotate +90: (x, y) -> (-y, x); body lands at x-w..x
            ref = top.add_ref(cell)
            ref.rotate(90)
            ox, oy = r5(x + w), r5(y0)
            ref.move((ox, oy))
            rot = lambda bb: (-bb[3] + ox, bb[0] + oy, -bb[1] + ox, bb[2] + oy)
            h0 = [rot(bb) for bb in heads0]
            h1 = [rot(bb) for bb in heads1]
            hb = (min(q[0] for q in h0), min(q[1] for q in h0),
                  max(q[2] for q in h0), max(q[3] for q in h0))
            ht = (min(q[0] for q in h1), min(q[1] for q in h1),
                  max(q[2] for q in h1), max(q[3] for q in h1))
            heads.append((hb, ht))
            subs += [rot(bb) for bb in subp]
            bb = ref.dbbox()
            tops.append(bb.top)
            bots.append(bb.bottom)
            x += pitch
        for i in range(nseg - 1):
            k = 0 if i % 2 == 0 else 1   # bottom strap after even segs
            p, q = heads[i][k], heads[i + 1][k]
            rect(top, p[0], p[1], q[2], p[3], M1)
        for net, hd in ((a, heads[0][1]), (b, heads[-1][1])):
            # head pads are 0.38 tall: widen the via landing's metal1
            via(top, cx(hd), cy(hd), "via1", M1, M2)
            terms.append((net, cx(hd), cy(hd)))
        x += RES_GAP
    # substrate straps: one metal1 bar over every segment's own P+ tap
    sy0 = min(s[1] for s in subs)
    sy1 = max(s[3] for s in subs)
    rect(top, 0.6, sy0, max(s[2] for s in subs), sy1, M1)
    via(top, 0.8, cy((0, sy0, 0, sy1)), "via1", M1, M2)
    terms.append(("vss", 0.8, cy((0, sy0, 0, sy1))))
    return terms, max(tops), min(bots), x - RES_GAP


RAIL = (-1.7, -1.1)     # a ring-block metal1 supply rail, from S COMP bottom
RTAP_Y = -2.2           # its taps' COMP bottom, from S COMP bottom
RTAP_P = 8.0            # tap pitch along a rail
RWELL_Y = -2.6          # PMOS half nwell bottom, from S COMP bottom
GOFF_R = 1.1            # ring block gate via, right of the right pad
NEXT_X = 1.2            # COMP right edge to the next stage's left pad:
                        # the gate jog (ends 1.37 past COMP) clears the
                        # next same-height gate pad (0.54 in) by 0.37
NEXT_XR = 1.8           # the same for NAND/output columns, whose taller
                        # pads and rail drops reach the jog's height
STRAP_Y = -0.6          # metal2 strap joining an inverter's n_k/p_k pads
MINI_Y = -1.6           # metal3 starve-gate track (vbn_vco / vbp_vco)
RING_CH_IN = 0.6        # inverter-row top shape to the first ring track
S_TOP = -3.2            # starve-device COMP top (inverter COMP bottom = 0)

# ring-block devices drawn as multi-finger cells: the stage inverter
# (n_k | r_k | n_k) halves each ring node's drain diffusion, and every
# inverter-row device is about the same height, so the shared channel is
# close to all of them
RING_NF = {f"M{t}_{d}{k}": 2 for t in "NP" for d in "SI" for k in range(1, 6)}
RING_NF.update({d: 2 for d in ("MP_G1", "MP_G2", "MN_G1", "MN_G2", "MP_O",
                               "MN_O")})


def fet_geom_nf(cell, nf):
    """COMP box, S/D pads left to right, gate pads, of a top-contacted
    draw_nfet/draw_pfet with nf fingers."""
    comp = layoutlib.layer_boxes(cell, LY["comp"])[0]
    m1 = layoutlib.layer_boxes(cell, M1)
    if len(m1) != 2 * nf + 1:
        raise layoutlib.LayoutError(f"expected {2 * nf + 1} pads: {m1}")
    by_y = sorted(m1, key=cy)
    gates = sorted(by_y[nf + 1:], key=lambda b: b[0])
    sd = sorted(by_y[:nf + 1], key=lambda b: b[0])
    return comp, sd, gates


def ring_dev_cell(draw_fet, dev):
    kind, _d, _g, _s, wt, l, nu = DEV[dev]
    nf = RING_NF.get(dev, 1)
    fn = draw_fet.draw_nfet if kind == "n" else draw_fet.draw_pfet
    # inner S/D long enough for its contacts (CO.7) and for neighbouring
    # top gate pads to clear each other (PL.3a); the cell's 0.24 is not
    return fn(l_gate=l, w_gate=wt / nu / nf, nf=nf, gate_con_pos="top",
              inter_sd_l=0.52), nf


def dev_width(draw_fet, dev):
    cell, nf = ring_dev_cell(draw_fet, dev)
    comp, _sd, _g = fet_geom_nf(cell, nf)
    return comp[2] - comp[0]


def ring_place(top, draw_fet, dev, xl, ybot, used):
    """Place dev with its left pad at x=xl and COMP bottom at ybot; draw
    its gate jog and gate via (right of the right pad). Returns (pads,
    nets per pad, gate net, gate via (x, y), COMP box)."""
    typ, d, g, s, wt, l, nu = DEV[dev]
    cell, nf = ring_dev_cell(draw_fet, dev)
    comp, sd, gps = fet_geom_nf(cell, nf)
    dx, dy = r5(xl - sd[0][0]), r5(ybot - comp[1])
    top.add_ref(cell).move((dx, dy))
    used[dev] = used.get(dev, 0) + 1
    sd = [tbox(b, dx, dy) for b in sd]
    gps = [tbox(b, dx, dy) for b in gps]
    nets = [s if k % 2 == 0 else d for k in range(nf + 1)]
    gy0, gy1 = gps[0][1], gps[0][3]
    xg = cx(sd[-1]) + GOFF_R
    rect(top, gps[0][0], gy0, xg + TRK_W / 2, gy1, M1)
    via(top, xg, (gy0 + gy1) / 2, "via1", M1, M2)
    return sd, nets, g, (xg, (gy0 + gy1) / 2), tbox(comp, dx, dy)


def ring_half(top, draw_fet, kind, cols, used):
    """One half of the ring block in its own frame, gates toward the
    shared channel above:
      inverter row   COMP bottom y=0: stage inverters, NAND, output
      n_k/p_k strap  metal2 at STRAP_Y under each stage inverter
      starve track   metal3 at MINI_Y: vbn_vco (or vbp_vco)
      starve row     COMP top at S_TOP, under its stage's inverter
      rail           metal1 + taps under the starve row
    cols: [(x, starve dev or None, [inverter-row devs])]. Returns (terms
    [(net, x, y)] for the shared channel, inverter row top y, x end,
    rail (x0, y), starve track (x0, y), bottom y)."""
    rail_net = "vss" if kind == "n" else "vdd"
    sdev = next(c[1] for c in cols if c[1])
    s_w = DEV[sdev][4] / DEV[sdev][6] / RING_NF[sdev]
    s_bot = S_TOP - s_w
    rail_y = (s_bot + RAIL[0], s_bot + RAIL[1])
    terms, tops, xmax = [], [0.0], 0.0
    mini_xs = []
    straps = {}                    # internal net -> pad xs on its strap

    def tie(net, pad):
        if net == rail_net:
            rect(top, pad[0], rail_y[1], pad[2], pad[1] + 0.2, M1)
            return True
        return False

    for x, sdv, idevs in cols:
        xi = x
        for dev in idevs:
            sd, nets, g, (xg, yg), comp = ring_place(top, draw_fet, dev,
                                                     xi, 0.0, used)
            for net, pad in zip(nets, sd):
                if tie(net, pad):
                    continue
                if net in INTERNAL:
                    via(top, cx(pad), pad[1] + 0.2, "via1", M1, M2)
                    rect(top, cx(pad) - TRK_W / 2, STRAP_Y - TRK_W / 2,
                         cx(pad) + TRK_W / 2, pad[1] + 0.2, M2)
                    straps.setdefault(net, []).append(cx(pad))
                else:
                    via(top, cx(pad), pad[3] - 0.2, "via1", M1, M2)
                    terms.append((net, cx(pad), pad[3] - 0.2))
            terms.append((g, xg, yg))
            if sdv is not None:
                # the starve device's n_k/p_k pad(s) join this strap
                ssd, snets, sg, (sxg, syg), scomp = ring_place(
                    top, draw_fet, sdv, xi, s_bot, used)
                for net, pad in zip(snets, ssd):
                    if tie(net, pad):
                        continue
                    assert net in INTERNAL, (sdv, net)
                    via(top, cx(pad), pad[3] - 0.2, "via1", M1, M2)
                    rect(top, cx(pad) - TRK_W / 2, pad[3] - 0.2,
                         cx(pad) + TRK_W / 2, STRAP_Y + TRK_W / 2, M2)
                    straps.setdefault(net, []).append(cx(pad))
                assert sg in ("vbn_vco", "vbp_vco"), sdv
                rect(top, sxg - RING_LW / 2, syg, sxg + RING_LW / 2,
                     MINI_Y + TRK_W / 2, M2)
                via(top, sxg, MINI_Y, "via2", M2, M3)
                mini_xs.append(sxg)
                xmax = max(xmax, scomp[2], sxg + 0.2)
            tops.append(max(b[3] for b in sd) + 0.9)
            xmax = max(xmax, comp[2], xg + 0.2)
            xi = comp[2] + NEXT_XR
    for xs in straps.values():
        rect(top, min(xs) - TRK_W / 2, STRAP_Y - TRK_W / 2,
             max(xs) + TRK_W / 2, STRAP_Y + TRK_W / 2, M2)
    x0 = min(c[0] for c in cols) - 3.0
    rect(top, x0, rail_y[0], xmax + 1.0, rail_y[1], M1)
    tap = layoutlib.psub_tap(TAP) if kind == "n" else layoutlib.nwell_tap(TAP)
    xt = x0 + 0.4
    while xt + TAP <= xmax + 1.0:
        top.add_ref(tap).move((r5(xt), r5(s_bot + RTAP_Y)))
        xt += RTAP_P
    rect(top, min(mini_xs) - TRK_W / 2, MINI_Y - RING_LW / 2,
         max(mini_xs) + TRK_W / 2, MINI_Y + RING_LW / 2, M3)
    bottom = s_bot + (RWELL_Y if kind == "p" else RTAP_Y - 0.2)
    if kind == "p":
        nwb = layoutlib.layer_boxes(top, LY["nwell"])
        rect(top, x0 - 0.8, bottom, xmax + 1.8, max(b[3] for b in nwb),
             LY["nwell"])
    # inverter-row gate pads are the tallest shapes facing the channel
    return (terms, max(tops), xmax, (x0, (rail_y[0] + rail_y[1]) / 2),
            (min(mini_xs), MINI_Y), bottom)


def ring_block(top, draw_fet, y0, x0, ntracks, used):
    """The VCO: NMOS half (gates up) under one shared channel, PMOS half
    mirrored above it (gates down). Each stage is one column: inverter
    next to the channel, starve device behind it, so stages are narrow,
    every ring hop is a short metal3 track and the starve gates stay off
    the ring channel. Returns (terms [(net, x, y, side)], channel base y,
    block top y, block bottom y, x end, risers [(net, x0, y, layer)])."""
    import gdsfactory as gf

    ncols, pcols = [], []
    x = x0
    prev_stage = False
    for col in RING_COLS:
        if col[0] == "stage":
            k = col[1]
            w = max(dev_width(draw_fet, d) for d in
                    (f"MN_S{k}", f"MN_I{k}", f"MP_S{k}", f"MP_I{k}"))
            ncols.append((x, f"MN_S{k}", [f"MN_I{k}"]))
            pcols.append((x, f"MP_S{k}", [f"MP_I{k}"]))
            x += w + NEXT_X
            prev_stage = True
        else:
            _t, ng, pg = col
            x += NEXT_XR - NEXT_X if prev_stage else 0.0
            prev_stage = False
            for i in range(max(len(ng), len(pg))):
                w = max(dev_width(draw_fet, g[i]) for g in (ng, pg)
                        if i < len(g))
                if i < len(ng):
                    ncols.append((x, None, [ng[i]]))
                if i < len(pg):
                    pcols.append((x, None, [pg[i]]))
                x += w + NEXT_XR
    nh = gf.Component()
    nterms, ntop, nxe, nrail, nmini, nbot = ring_half(nh, draw_fet, "n",
                                                      ncols, used)
    ph = gf.Component()
    pterms, ptop, pxe, prail, pmini, pbot = ring_half(ph, draw_fet, "p",
                                                      pcols, used)
    top.add_ref(nh).move((0, r5(y0)))
    base = r5(y0 + ntop + RING_CH_IN)
    ct = base + max(ntracks - 1, 0) * RING_TRK_P + TRK_W / 2
    yp = r5(ct + RING_CH_IN + ptop)
    ref = top.add_ref(ph)
    ref.dmirror_y(0)
    ref.dmove((0, yp))
    terms = [(n, xx, y0 + yy, "below") for n, xx, yy in nterms]
    terms += [(n, xx, yp - yy, "above") for n, xx, yy in pterms]
    risers = [("vss", nrail[0], y0 + nrail[1], "m1"),
              ("vdd", prail[0], yp - prail[1], "m1"),
              ("vbn_vco", nmini[0], y0 + nmini[1], "m3"),
              ("vbp_vco", pmini[0], yp - pmini[1], "m3")]
    return (terms, base, yp - pbot, y0 + nbot, max(nxe, pxe), risers)


# ---------------------------------------------------------------- routing
def plan(chan_terms, x_rows, keepouts, ports):
    """Topological routing plan from each channel's terminals (x only).

    chan_terms: [[(net, x, y_local)], ...] bottom to top.
    keepouts: [(ch_lo, ch_hi, xa, xb)]: no metal4 column in xa..xb for a
      trunk whose channel span meets ch_lo..ch_hi (a cap lies there).
    ports: [(key, net, channel, x_pref)]: a cap plate's metal5 strap
      lands on a via4/via3 column on the net's own track in `channel`;
      the column goes as near x_pref as the keep-outs allow.
    Returns (trunks {net: (x, lo, hi)}, port_x {key: x}, tracks
    [{net: (i, x0, x1)}], ntracks [n], x_right)."""
    nch = len(chan_terms)
    where = {}
    for ci, terms in enumerate(chan_terms):
        for net, x, *_r in terms:
            where.setdefault(net, {}).setdefault(ci, []).append(x)
    occ = []                       # (column k, lo, hi)
    kmax = int((x_rows + 60.0) / TRK_P)

    def column(want, lo, hi, what):
        k0 = min(max(int(round((want - 0.4) / TRK_P)), 0), kmax)
        for dk in range(0, 2 * kmax + 2):
            k = k0 + (dk + 1) // 2 * (1 if dk % 2 else -1)
            if not 0 <= k <= kmax:
                continue
            x = 0.4 + k * TRK_P
            if any(xa <= x <= xb and lo <= chi and clo <= hi
                   for clo, chi, xa, xb in keepouts):
                continue
            if all(not (k == ok and lo <= ohi + 1 and olo <= hi + 1)
                   for ok, olo, ohi in occ):
                occ.append((k, lo, hi))
                return r5(x)
        raise layoutlib.LayoutError(f"no metal4 column for {what}")

    port_x = {}
    for key, net, ci, want, *lo in ports:
        if ci not in where.get(net, {}):
            raise layoutlib.LayoutError(
                f"port {key}: {net} has no track in channel {ci}")
        port_x[key] = column(want, lo[0] if lo else ci, ci, key)
    trunks = {}
    for net in sorted(where, key=lambda n: (-len(where[n]), n)):
        chans = sorted(where[net])
        if len(chans) < 2 and net not in BOTTOM_PINS:
            continue
        lo = -1 if net in BOTTOM_PINS else chans[0]
        hi = chans[-1]
        xs = sorted(x for c in chans for x in where[net][c])
        trunks[net] = (column(xs[len(xs) // 2], lo, hi, net), lo, hi)
    x_right = r5(max([x_rows] + [x for x, _l, _h in trunks.values()]
                     + list(port_x.values())) + 1.5)
    tracks, ntracks = [], []
    for ci in range(nch):
        iv = {}
        for net, xs in ((n, w[ci]) for n, w in where.items() if ci in w):
            pts = list(xs)
            if net in trunks:
                pts.append(trunks[net][0])
            pts += [port_x[k] for k, n, c, *_w in ports
                    if (n, c) == (net, ci)]
            if net in RIGHT_PINS and ci == min(where[net]):
                pts.append(x_right)
            iv[net] = [min(pts) - TRK_W / 2, max(pts) + TRK_W / 2]
        sides = [t for t in chan_terms[ci] if len(t) == 4]
        if sides:
            # a shared channel (stubs from below AND above): one track per
            # net, ordered so a stub from below never crosses a stub from
            # above of another net at the same x
            assign = vcg_tracks(sides, iv)
            tracks.append(assign)
            ntracks.append(max(t for t, _a, _b in assign.values()) + 1)
            continue
        last = []
        assign = {}
        for net in sorted(iv, key=lambda n: (iv[n][0], n)):
            a, b = iv[net]
            for t, end in enumerate(last):
                if a >= end + 0.4:
                    last[t] = b
                    break
            else:
                t = len(last)
                last.append(b)
            assign[net] = (t, r5(a), r5(b))
        tracks.append(assign)
        ntracks.append(len(last))
    return trunks, port_x, tracks, ntracks, x_right


def vcg_tracks(terms, iv):
    """Track per net for a two-sided channel: net a's track below net b's
    wherever a stub of a from below and a stub of b from above come
    within metal2 spacing in x. Raises on a cycle."""
    below = [(n, x) for n, x, _y, sd in terms if sd == "below"]
    above = [(n, x) for n, x, _y, sd in terms if sd == "above"]
    succ = {n: set() for n in iv}
    for a, xa in below:
        for b, xb in above:
            if a != b and abs(xa - xb) < TRK_W + 0.3:
                succ[a].add(b)
    indeg = {n: 0 for n in iv}
    for a in succ:
        for b in succ[a]:
            indeg[b] += 1
    # Kahn, ties broken by the net's mean stub side (below-only first)
    side = {n: 0.0 for n in iv}
    for n, _x, _y, sd in terms:
        side[n] += 1 if sd == "above" else -1
    order, ready = [], sorted((n for n in iv if indeg[n] == 0),
                              key=lambda n: (side[n], n))
    while ready:
        n = ready.pop(0)
        order.append(n)
        for b in succ[n]:
            indeg[b] -= 1
            if indeg[b] == 0:
                ready.append(b)
        ready.sort(key=lambda n: (side[n], n))
    if len(order) != len(iv):
        raise layoutlib.LayoutError(
            f"shared channel has a stub conflict cycle among "
            f"{sorted(set(iv) - set(order))}")
    # constrained left-edge: in that order, each net takes the lowest
    # track above all its predecessors' where its span is free, so the
    # channel - and every ring stub crossing it - stays short
    pred = {n: [a for a in succ if n in succ[a]] for n in iv}
    used = []                      # per track: [(x0, x1)]
    got = {}
    for n in order:
        t = max([got[a] + 1 for a in pred[n]] + [0])
        a, b = iv[n]
        while True:
            while t >= len(used):
                used.append([])
            if all(b + 0.4 <= u0 or a >= u1 + 0.4 for u0, u1 in used[t]):
                break
            t += 1
        used[t].append((a, b))
        got[n] = t
    return {n: (got[n], r5(iv[n][0]), r5(iv[n][1])) for n in iv}


# ---- caps over the rows ------------------------------------------------
# cap -> (first row under it, last row under it or None = up to the top,
#         x of its bottom plate's left edge, channel of the top plate's
#         port, channel of the bottom plate's port). The cap's metal4
#         bottom plate starts BOT_LIFT above that row's COMP bottom.
CAP_PLACE = {
    "C1": (3, None, 0.0, 3, 4),
    "C2": (1, 2, 0.0, 2, 1),
}
BOT_LIFT = 0.4
BOT_EXT = 2.0      # bottom plate widened right of the top plate, for via4
MIM_ENC = 0.6      # draw_cap_mim's own bottom-plate margin


def cap_box(name):
    """(x0, x1) of a cap's metal4 bottom plate, widened by BOT_EXT."""
    side = CAPS[name][2]
    x0 = CAP_PLACE[name][2]
    return x0, x0 + side + 2 * MIM_ENC + BOT_EXT


def generate():
    import gdsfactory as gf

    draw_fet, draw_res = layoutlib.gf180_cells()
    from cells.draw_cap_mim import draw_cap_mim

    # ---- pass 1: terminals (x) and row extents, in a scratch cell ----
    scratch = gf.Component()
    used = {}
    rows1 = []
    for name, kind, items in ROWS:
        if kind == "ring":
            rows1.append(None)
        elif kind == "r":
            rows1.append(build_res_row(scratch, draw_res, 0.0))
        else:
            rows1.append(build_row(scratch, draw_fet, name, kind, items,
                                   0.0, used))
    # the ring block goes flush right, under the vco_out pin's edge
    x_rows0 = max(r[3] for r in rows1 if r)
    rw = ring_block(gf.Component(), draw_fet, 0.0, 0.0, 0, {})[4]
    ring_x0 = r5(max(x_rows0 - rw, 0.0))
    for ri, (name, kind, items) in enumerate(ROWS):
        if kind == "ring":
            t, b, tp, bt, xe, rails = ring_block(scratch, draw_fet, 0.0,
                                                 ring_x0, 0, used)
            rows1[ri] = (t, b - CH_IN, bt, xe)
            ring_ri, ring_rails = ri, rails
    for dev, (_t, _d, _g, _s, _w, _l, nu) in DEV.items():
        if used.get(dev, 0) != nu:
            raise layoutlib.LayoutError(
                f"{dev}: {used.get(dev, 0)} units placed, {nu} wanted")
    nch = len(rows1)
    x_rows = max(r[3] for r in rows1)
    keepouts, ports = [], []
    for cname, (ra, rb, _x0, ct, cb) in CAP_PLACE.items():
        xa, xb = cap_box(cname)
        keepouts.append((ra, nch - 1 if rb is None else rb,
                         xa - M4_KEEP - TRK_W, xb + M4_KEEP + TRK_W))
        tnet, bnet, _side = CAPS[cname]
        ports.append((cname + ".top", tnet, ct, xb + M4_KEEP + TRK_W))
        ports.append((cname + ".bot", bnet, cb, xb + M4_KEEP + TRK_W))
    # ring rails: a metal4 riser from each rail's left end up to the
    # first channel above the block that carries the same supply
    for net, rx, _ry, _lay in ring_rails:
        ci = min(c for c in range(ring_ri + 1, nch)
                 if any(t[0] == net for t in rows1[c][0]))
        ports.append((f"ring.{net}", net, ci, rx, ring_ri))
    trunks, port_x, tracks, ntracks, x_right = plan(
        [r[0] for r in rows1], x_rows, keepouts, ports)

    if os.environ.get("PLL_LAYOUT_DEBUG"):
        for ci, tr in enumerate(tracks):
            print("channel", ci, ROWS[ci][0], ntracks[ci],
                  sorted((t, n, round(b - a, 1)) for n, (t, a, b)
                         in tr.items()), file=sys.stderr)
    # ---- pass 2: draw ----
    top = gf.Component()
    used = {}
    y0 = 0.0
    row_y0, chan_y, chan_top, bottoms, chan_p = [], [], [], [], []
    for ri in range(nch):
        row_y0.append(y0)
        name, kind, items = ROWS[ri]
        blk_top = None
        if kind == "ring":
            terms, base, blk_top, by, xe, rails2 = ring_block(
                top, draw_fet, y0, ring_x0, ntracks[ri], used)
        else:
            if kind == "r":
                terms, ty, by, xe = build_res_row(top, draw_res, y0)
            else:
                terms, ty, by, xe = build_row(top, draw_fet, name, kind,
                                              items, y0, used)
            base = r5(ty + CH_IN)
        bottoms.append(by)
        chan_y.append(base)
        pch = RING_TRK_P if kind == "ring" else TRK_P
        lw = RING_LW if kind == "ring" else TRK_W
        chan_p.append(pch)
        done = {}
        for net, x, y, *_sd in terms:
            yt = base + tracks[ri][net][0] * pch
            ya, yb = sorted((y, yt))
            rect(top, x - lw / 2, ya - TRK_W / 2 * (y > yt),
                 x + lw / 2, yb + TRK_W / 2 * (yt > y), M2)
            # an n stub and a p stub of one net meeting a few 10 nm apart
            # share the first one's via2 (two would merge into a bad cut)
            if any(abs(x - x2) < 0.7 for x2 in done.get(net, [])):
                continue
            done.setdefault(net, []).append(x)
            via(top, x, yt, "via2", M2, M3)
        for net, (t, a, b) in tracks[ri].items():
            yt = base + t * pch
            rect(top, a, yt - lw / 2, b, yt + lw / 2, M3)
        ch_top = base + max(ntracks[ri] - 1, 0) * pch + TRK_W / 2
        chan_top.append(ch_top)
        if blk_top is not None:
            ch_top = blk_top       # the ring block's PMOS half sits above
        if ri + 1 < nch:
            y0 = r5(ch_top + ROW_SP - rows1[ri + 1][2])
    y_bot = r5(min(bottoms) - 1.5)

    def track_y(net, ci):
        return chan_y[ci] + tracks[ci][net][0] * chan_p[ci]

    # ---- trunks ----
    labels = []
    for net, (x, lo, hi) in trunks.items():
        ys = [track_y(net, ci) for ci in range(max(lo, 0), hi + 1)
              if net in tracks[ci]]
        for y in ys:
            via(top, x, y, "via3", M3, M4)
        ya = y_bot if lo < 0 else min(ys)
        rect(top, x - TRK_W / 2, ya - TRK_W / 2, x + TRK_W / 2,
             max(ys) + TRK_W / 2, M4)
        if lo < 0:
            # pin: the trunk's foot, stacked down to a metal2 pad (the
            # abstract reads metal1/metal2 pin labels)
            via(top, x, y_bot, "via3", M3, M4)
            via(top, x, y_bot, "via2", M2, M3)
            labels.append((net, x, y_bot, LY["metal2_label"]))
    for net, rx, ry, lay in rails2:
        key = f"ring.{net}"
        if os.environ.get("PLL_LAYOUT_DEBUG"):
            print("riser", net, rx, ry, lay, port_x[key], file=sys.stderr)
        ci = [c for k, _n, c, *_r in ports if k == key][0]
        px = port_x[key]
        if lay == "m1":
            # a supply rail: metal1 out to the riser, via1/2/3 stack
            if px < rx:
                rect(top, px - TRK_W / 2, ry - 0.3, rx + 0.5, ry + 0.3, M1)
            via(top, px, ry, "via1", M1, M2)
            via(top, px, ry, "via2", M2, M3)
        else:
            # a starve-gate track: metal3 out to the riser
            rect(top, min(px, rx) - TRK_W / 2, ry - RING_LW / 2,
                 max(px, rx) + TRK_W / 2, ry + RING_LW / 2, M3)
        via(top, px, ry, "via3", M3, M4)
        yt = track_y(net, ci)
        rect(top, px - TRK_W / 2, ry - TRK_W / 2, px + TRK_W / 2,
             yt + TRK_W / 2, M4)
        via(top, px, yt, "via3", M3, M4)
    for net in RIGHT_PINS:
        ci = min(c for c in range(nch) if net in tracks[c])
        yt = track_y(net, ci)
        via(top, x_right - 0.2, yt, "via2", M2, M3)
        labels.append((net, x_right - 0.2, yt, LY["metal2_label"]))

    # ---- caps: MIM-B (metal4 bottom, metal5 top) over the rows. Both
    # plates come out in metal5: the top plate directly, the bottom plate
    # through a via4 column on its widened right edge (MIMTM.2/.5); each
    # strap runs right at its own net's track height to a via4+via3 stack
    # on that track, beside the cap.
    for cname, (ra, rb, x0, ct, cb) in CAP_PLACE.items():
        tnet, bnet, side = CAPS[cname]
        fx0 = r5(x0 + MIM_ENC)
        fy0 = r5(row_y0[ra] + BOT_LIFT + MIM_ENC)
        top.add_ref(draw_cap_mim(mim_option="MIM-B", metal_level="M5",
                                 lc=side, wc=side)).move((fx0, fy0))
        fx1, fy1 = fx0 + side, fy0 + side
        cap_top = fy1 + MIM_ENC
        if rb is not None and cap_top + M4_KEEP > chan_y[rb + 1] - TRK_W:
            raise layoutlib.LayoutError(
                f"{cname} runs into channel {rb + 1}'s metal4")
        rect(top, fx1, fy0 - MIM_ENC, fx1 + MIM_ENC + BOT_EXT,
             fy1 + MIM_ENC, M4)
        vx = fx1 + MIM_ENC + BOT_EXT / 2
        for plate, net, ci in (("top", tnet, ct), ("bot", bnet, cb)):
            yt = track_y(net, ci)
            lo, hi = ((fy0 + 1.0, fy1 - 1.0) if plate == "top"
                      else (fy0 + 0.4, fy1 - 0.4))
            if not lo <= yt <= hi:
                raise layoutlib.LayoutError(
                    f"{cname}.{plate}: track {net} y={yt:.2f} is off the "
                    f"plate ({lo:.2f}..{hi:.2f})")
            px = port_x[f"{cname}.{plate}"]
            x_from = fx1 - 1.0 if plate == "top" else vx
            if plate == "bot":
                via(top, vx, yt, "via4", M4, M5, land=1.0)
            rect(top, x_from - 0.5, yt - 0.5, px + 0.5, yt + 0.5, M5)
            # metal4 landing kept to a track's width: the next metal4
            # column is one TRK_P away (the metal5 strap is 1.0 tall)
            via(top, px, yt, "via4", M4, M5)
            via(top, px, yt, "via3", M3, M4)
    labels.sort(key=lambda lbl: PINS.index(lbl[0]))
    return layoutlib.finalize(top, CELL, labels)


if __name__ == "__main__":
    comp = generate()
    out_path = sys.argv[1] if len(sys.argv) > 1 else f"{CELL}.gds"
    comp.write_gds(out_path)
    print(f"wrote {out_path} bbox={comp.dbbox()}")

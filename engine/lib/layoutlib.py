"""layoutlib.py - shared analog-layout helpers for M9 (docs/design.md 1.5,
5, "### M9."; docs/spikes/glayout.md).

New at M9: nothing to port - the spike's own verdict is what this module
implements. gLayout doesn't run on the image's gdsfactory 9.51 (neither
backend is DRC-clean out of the box), so M9 takes the documented fallback:
"generator code written directly against gdsfactory ... with the GF180
PDK's primitive cells." Those primitive cells turn out to already exist,
gdsfactory-native, inside the PDK itself:
`$PDK/libs.tech/klayout/tech/pymacros/cells/{draw_fet,draw_res}.py` (vendor
code, `import gdsfactory as gf`, `@gf.cell`, returns `gf.Component`) - this
is what `gf180_cells()` imports. Verified empirically against the real
klayout GF180 signoff deck (the one docs/spikes/glayout.md says counts,
since magic re-snaps on load and hides off-grid shapes):
`draw_nfet()`/`draw_pfet()` at their own declared defaults are 1 violation
away from clean (DF.14: "max distance of a substrate tap from NCOMP is
20um" - an isolated device with no tap anywhere in range), and
`draw_npolyf_res(w_res>=0.564...)` is clean outright. Both generator
functions' own built-in `lbl=True`/`sd_lbl`/`g_lbl`/`sub_lbl` text-labeling
path is dead on this gdsfactory version (`Component.add_label()` silently
registers nothing `get_labels()` or a GDS re-read can find - the same class
of moved/changed-API break docs/spikes/glayout.md found in gLayout's own
adapter, just smaller) - `add_text_label()` below is the replacement,
writing a real GDS text record straight onto the (unlocked) assembling
Component's own klayout cell.

Every device this module places still needs the "context" a lone PCell
lacks: a P+ substrate tap within DF.14's 20um for any NMOS, wired to the
same ground net a source ties to. `psub_tap()` is that tap, hand-drawn
(no vendor generator for a lone tap was found) and verified clean at >=1.75um
edge clearance from a device.
"""
from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent  # engine/lib -> engine
REPO = ENGINE.parent
EDA_BIN = REPO / "bin" / "eda"

# GF180's own GDS layer numbers (layers_def.py in the pymacros tree) for the
# handful this module draws or labels directly.
GF180_LAYER = {
    "comp": (22, 0),
    "nwell": (21, 0),
    "poly2": (30, 0),
    "nplus": (32, 0),
    "pplus": (31, 0),
    "contact": (33, 0),
    "metal1": (34, 0),
    "via1": (35, 0),
    "metal2": (36, 0),
    "via2": (38, 0),
    "metal3": (42, 0),
    "metal1_label": (34, 10),
    "metal2_label": (36, 10),
    "res_mk": (110, 5),
    "metal1_res": (110, 11),
}

DRC_DECK_REL = "libs.tech/klayout/tech/drc/gf180mcu.drc"
# The PDK's own signoff selection (libs.tech/librelane/config.tcl:
# KLAYOUT_DRC_OPTIONS decks "all,-antenna,-density", variant $PDK): every
# FEOL, metal, via and guard-ring rule, less the decks in DRC_SKIPPED,
# which judge a whole chip's fill and gate-to-metal ratios, so a lone block
# with no floorplan around it cannot pass or fail them. The variant is the
# PDK's name (gf180mcuD: 5 metals, 11K top), never a fixed letter: variant
# A is a 3-metal stack, and its mslot deck dies on metal4_drawn. The antenna
# deck runs on its own (run_klayout_antenna), since its verdict depends on
# whether the net has a pin.
DRC_SKIPPED = {
    "density": "metal and poly density is a whole-chip figure; a lone "
               "block has no fill around it",
    "dummy": "the dummy-fill decks (DCF/DPF/DMF, e.g. DCF.1a: the space "
             "between COMP must be filled) check fill a lone block does not "
             "have yet; the chip's fill step adds it (msde's top_harden "
             "fills an analog macro, which the chip's fill skips), for the "
             "same reason density is off",
}
DRC_DECKS = ",".join(["all", "-antenna", *(f"-{d}" for d in DRC_SKIPPED)])
# (conductor layers bottom to top, each joined to the next; text layers on the
# metal they name) - klayout's connectivity for naming an antenna finding's net
_NET_STACK = [("poly2", (30, 0)), ("contact", (33, 0)), ("metal1", (34, 0)),
              ("via1", (35, 0)), ("metal2", (36, 0)), ("via2", (38, 0)),
              ("metal3", (42, 0)), ("via3", (40, 0)), ("metal4", (46, 0)),
              ("via4", (41, 0)), ("metal5", (81, 0))]
_NET_LABELS = {"metal1": (34, 10), "metal2": (36, 10), "metal3": (42, 10),
               "metal4": (46, 10), "metal5": (81, 10)}


class LayoutError(RuntimeError):
    """A layout tool step did not complete. Callers raise this up to
    CheckError - never a silent pass (docs/design.md: "a gate that did not
    run is a refusal, never a pass")."""


class LvsBlackBox(LayoutError):
    """netgen ran but compared a cell as an empty black box (no netlist
    defines it and the PDK setup has no class for it): an LVS verdict
    against the netlists, which a caller may record rather than refuse."""


_toolchain_root_cache: Path | None = None


def toolchain_root(timeout: float = 30.0) -> Path:
    global _toolchain_root_cache
    if _toolchain_root_cache is None:
        proc = subprocess.run(
            [str(EDA_BIN), "--print-toolchain-root"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise LayoutError(
                "could not resolve the eda toolchain root: "
                f"{(proc.stderr or proc.stdout).strip()}")
        _toolchain_root_cache = Path(proc.stdout.strip())
    return _toolchain_root_cache


def pdk_root() -> Path:
    return toolchain_root() / "foss" / "pdks" / "gf180mcuD"


def run_eda(args: list[str], cwd=None, timeout: float = 120.0,
           stdin_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [str(EDA_BIN), *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            input=stdin_text if stdin_text is not None else "")
    except subprocess.TimeoutExpired as exc:
        raise LayoutError(
            f"eda {' '.join(args)} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise LayoutError(f"eda {' '.join(args)} failed to launch: {exc}") from exc


_cells_path_ready = False


def gf180_cells():
    """Import and return (draw_fet, draw_res), the PDK's own gdsfactory-
    native primitive-cell modules. Raises LayoutError if the PDK tree is
    missing them (a stale/foreign toolchain) rather than importing nothing
    and looking like an empty-but-passing generator."""
    global _cells_path_ready
    pymacros = pdk_root() / "libs.tech" / "klayout" / "tech" / "pymacros"
    cells_dir = pymacros / "cells"
    if not (cells_dir / "draw_fet.py").is_file():
        raise LayoutError(f"no GF180 pymacros cells at {cells_dir}")
    if not _cells_path_ready:
        for p in (str(cells_dir), str(pymacros)):
            if p not in sys.path:
                sys.path.insert(0, p)
        _cells_path_ready = True
    import importlib
    draw_fet = importlib.import_module("cells.draw_fet")
    draw_res = importlib.import_module("cells.draw_res")
    return draw_fet, draw_res


def psub_tap(size: float = 1.0):
    """A P+ substrate tap: COMP + PPLUS + one 0.22x0.22 contact (CO.1's own
    fixed size - GF180 contacts are not a min/max range) + a metal1 pad.
    Hand-drawn (no vendor single-tap generator found under pymacros/cells);
    verified 0 klayout-DRC violations standalone and paired with a default
    draw_nfet() >=1.75um edge-to-edge away (docs/spikes/glayout.md's
    fallback note: M9's own generator code, not gLayout's)."""
    import gdsfactory as gf

    c = gf.Component()
    c.add_polygon([(0, 0), (size, 0), (size, size), (0, size)],
                 layer=GF180_LAYER["comp"])
    m = 0.2
    c.add_polygon([(-m, -m), (size + m, -m), (size + m, size + m),
                  (-m, size + m)], layer=GF180_LAYER["pplus"])
    cw = 0.22  # CO.1: contact must be exactly 0.22 x 0.22um, not a range
    co = (size - cw) / 2
    c.add_polygon([(co, co), (co + cw, co), (co + cw, co + cw),
                  (co, co + cw)], layer=GF180_LAYER["contact"])
    mm = 0.6
    mo = (size - mm) / 2
    c.add_polygon([(mo, mo), (mo + mm, mo), (mo + mm, mo + mm),
                  (mo, mo + mm)], layer=GF180_LAYER["metal1"])
    return c


def nwell_tap(size: float = 1.0):
    """An N+ well tap: psub_tap()'s own shapes with NPLUS for PPLUS. It
    ties an nwell to its supply (DF.13: one within 20um of every PCOMP in
    that well). No NWELL of its own: the caller draws the well around it
    and the PFETs it biases, since a lone tap's well would be one more
    shape to space against theirs."""
    import gdsfactory as gf

    c = gf.Component()
    c.add_polygon([(0, 0), (size, 0), (size, size), (0, size)],
                 layer=GF180_LAYER["comp"])
    m = 0.2
    c.add_polygon([(-m, -m), (size + m, -m), (size + m, size + m),
                  (-m, size + m)], layer=GF180_LAYER["nplus"])
    cw = 0.22
    co = (size - cw) / 2
    c.add_polygon([(co, co), (co + cw, co), (co + cw, co + cw),
                  (co, co + cw)], layer=GF180_LAYER["contact"])
    mm = 0.6
    mo = (size - mm) / 2
    c.add_polygon([(mo, mo), (mo + mm, mo), (mo + mm, mo + mm),
                  (mo, mo + mm)], layer=GF180_LAYER["metal1"])
    return c


def psub_ring(top, box, gap: float = 2.5, width: float = 0.5):
    """A P+ substrate guard ring around `box` (x0, y0, x1, y1 in um, a
    DNWELL's extent), `gap` um outside it: COMP + PPLUS (0.2 um over the
    COMP, as psub_tap) + a row of 0.22 um contacts at 0.5 um pitch along
    every side + a metal1 ring over the COMP. DN.3 wants every DNWELL
    directly inside a PCOMP ring; DF.18 wants PCOMP outside the wells
    2.5 um from it. The PDK pcells' own ring (`pcmpgr`) comes out of this
    gdsfactory as a filled square with no metal1, so it is drawn here.
    Returns the (x, y) of a point on the ring's metal1 (its left side's
    middle), for a label."""
    x0, y0, x1, y1 = (box[0] - gap - width, box[1] - gap - width,
                      box[2] + gap + width, box[3] + gap + width)
    sides = ((x0, y0, x1, y0 + width), (x0, y1 - width, x1, y1),
             (x0, y0 + width, x0 + width, y1 - width),
             (x1 - width, y0 + width, x1, y1 - width))
    m = 0.2
    for s in sides:
        rect(top, *s, GF180_LAYER["comp"])
        rect(top, *s, GF180_LAYER["metal1"])
    rect(top, x0 - m, y0 - m, x1 + m, y0 + width + m, GF180_LAYER["pplus"])
    rect(top, x0 - m, y1 - width - m, x1 + m, y1 + m, GF180_LAYER["pplus"])
    rect(top, x0 - m, y0 - m, x0 + width + m, y1 + m, GF180_LAYER["pplus"])
    rect(top, x1 - width - m, y0 - m, x1 + m, y1 + m, GF180_LAYER["pplus"])
    cw, pitch = 0.22, 0.5
    for sx0, sy0, sx1, sy1 in sides:
        horiz = (sx1 - sx0) >= (sy1 - sy0)
        # the side rows stop 0.2 um short of the corner rows (CO.2)
        lo, hi = (sx0, sx1) if horiz else (sy0 + 0.2, sy1 - 0.2)
        n = int((hi - lo - 0.3) // pitch) + 1
        start = lo + ((hi - lo) - (n - 1) * pitch - cw) / 2
        for i in range(n):
            a = round((start + i * pitch) / 0.005) * 0.005
            if horiz:
                b = sy0 + (width - cw) / 2
                rect(top, a, b, a + cw, b + cw, GF180_LAYER["contact"])
            else:
                b = sx0 + (width - cw) / 2
                rect(top, b, a, b + cw, a + cw, GF180_LAYER["contact"])
    return x0 + width / 2, (y0 + y1) / 2


def _device_cell(model: str, kind: str, size, name: str | None):
    import devicelib  # lazy: devicelib imports layoutlib

    e = devicelib.CATALOG.get(model)
    if e is None or e["kind"] != kind:
        raise LayoutError(f"{model} is not a catalogued gf180mcuD {kind}")
    why = devicelib.not_ready(model)
    if why:
        raise LayoutError(why)
    try:
        return devicelib.build(model, size, name=name)
    except devicelib.DeviceError as exc:
        raise LayoutError(str(exc)) from exc


def mos_cap(model: str, lc: float, wc: float, name: str | None = None):
    """A labelled gf180mcuD MOS capacitor (pins G, SD and its tap), drawn
    by the PDK pcell. Only a model whose devices.yaml row passed every
    step (pdk_devices.py, #137); anything else is a LayoutError."""
    return _device_cell(model, "mos_cap", (lc, wc), name)


def diode(model: str, la: float, wa: float, name: str | None = None):
    """A labelled gf180mcuD diode (pins A, K), as mos_cap()."""
    return _device_cell(model, "diode", (la, wa), name)


def bjt(model: str, name: str | None = None):
    """The PDK's fixed layout of a gf180mcuD BJT, labelled C B E [S], as
    mos_cap()."""
    return _device_cell(model, "bjt", (), name)


VIA1_SIZE = 0.26  # the PDK's own via1 cut (draw_fet.py's via_size)
VIA1_ENC = 0.07   # metal1 and metal2 enclosure of it (draw_fet.py's via_enc)


def via1(top, cx: float, cy: float) -> None:
    """One metal1-to-metal2 via centered on (cx, cy): the cut plus a
    0.40um square of metal1 and of metal2 around it. 0.40 squared is
    0.16um2, over both metals' minimum area. Give it a centre on the 5nm
    grid, since finalize() snaps but a half-grid cut would move."""
    h = VIA1_SIZE / 2
    rect(top, cx - h, cy - h, cx + h, cy + h, GF180_LAYER["via1"])
    e = h + VIA1_ENC
    for layer in (GF180_LAYER["metal1"], GF180_LAYER["metal2"]):
        rect(top, cx - e, cy - e, cx + e, cy + e, layer)


def pad_center(size: float = 1.0) -> tuple[float, float]:
    """psub_tap()'s own metal1 pad center, local frame."""
    return (size / 2, size / 2)


def add_text_label(top, text: str, x: float, y: float,
                   layer: tuple[int, int] | None = None) -> None:
    """Write a REAL GDS text record at (x, y) on `top`'s own cell.

    gdsfactory 9.51's Component.add_label()/get_labels() do not round-trip
    on this image (proved empirically: a vendor draw_npolyf_res(lbl=True)
    label is retrievable from neither get_labels() nor a written-and-reread
    GDS). `top` must be a component YOU created (gf.Component()) and
    add_ref()'d sub-cells into - a @gf.cell-decorated Component (what
    draw_nfet()/draw_npolyf_res() themselves return) is locked and raises
    on any direct shape insert."""
    import klayout.db as kdb

    if layer is None:
        layer = GF180_LAYER["metal1_label"]
    dbu = top.kcl.dbu
    li = top.kcl.layer(*layer)
    top.kdb_cell.shapes(li).insert(
        kdb.Text(text, kdb.Trans(int(round(x / dbu)), int(round(y / dbu)))))


def finalize(top, name: str, labels: list[tuple[str, float, float, tuple]]):
    """Flatten + snap `top` to the 5nm manufacturing grid, then add its
    labels. Needed because cells.via_generator.via_stack (unlike
    draw_fet.py/draw_res.py) does NOT snap itself - calling it directly, as
    gen_r2r_dac.py's metal2 jumper does, produced real *_OFFGRID findings
    until this was added.

    Not the PDK's own pcell_utilities.snap_to_grid: that rebuilds each
    polygon from its outline points and drops its holes, so the PDK
    filltie's NPLUS keyhole came back as a solid block over its P+ tap -
    96 of a real R-2R DAC's 98 DRC findings (DF.16_MV, DF.3b, NP.3d/e,
    PP.3d/e). This snaps each layer's shapes vertex by vertex with
    klayout's own Region.snapped, which keeps every contour, holes too.

    Only polygons are copied (a Region takes no texts), into a brand new
    Component - any text label added before calling this would be dropped.
    So every generator builds refs+wires only, then calls this ONCE at the
    end with its labels, in that order."""
    import gdsfactory as gf
    import klayout.db as kdb

    flat = top.copy()
    flat.flatten()
    snapped = gf.Component()
    snapped.name = name
    grid = int(round(0.005 / flat.kcl.dbu))
    for li in flat.kcl.layer_indexes():
        region = kdb.Region(flat.kdb_cell.begin_shapes_rec(li))
        if region.is_empty():
            continue
        # merged semantics off: snap the shapes as drawn, never a union of
        # them, so a finalized cell is the raw one moved onto the grid.
        region.merged_semantics = False
        info = flat.kcl.get_info(li)
        snapped.kdb_cell.shapes(snapped.kcl.layer(info.layer, info.datatype)
                                ).insert(region.snapped(grid, grid))
    for text, x, y, layer in labels:
        add_text_label(snapped, text, x, y, layer)
    return snapped


def layer_boxes(comp, layer: tuple[int, int]) -> list[tuple[float, ...]]:
    """The merged shapes of `comp` on `layer`, as (x0, y0, x1, y1) boxes in
    um, in comp's own frame. Generators read a primitive cell's pads off
    its geometry with this instead of copying coordinates in by hand, so a
    device drawn at another W/L still gets wired to its own pads."""
    import klayout.db as kdb

    li = comp.kcl.layer(*layer)
    dbu = comp.kcl.dbu
    region = kdb.Region(comp.kdb_cell.begin_shapes_rec(li)).merged()
    boxes = []
    for poly in region.each():
        b = poly.bbox()
        boxes.append((round(b.left * dbu, 4), round(b.bottom * dbu, 4),
                      round(b.right * dbu, 4), round(b.top * dbu, 4)))
    return sorted(boxes)


MATCH_PATTERNS = ("ABBA", "common_centroid", "interdigitate")


def _spread(counts: dict[str, int]) -> list[str]:
    """`counts`' units in one sequence, each device's spread evenly: slot t
    goes to the device furthest behind its share (ties to the earlier
    name in `counts`), so no device runs longer than ceil(max/min)."""
    total = sum(counts.values())
    placed = dict.fromkeys(counts, 0)
    seq = []
    for t in range(total):
        name = max(counts, key=lambda n: (counts[n] * (t + 1) / total - placed[n],
                                          -list(counts).index(n)))
        placed[name] += 1
        seq.append(name)
    return seq


def match_sequence(units: dict[str, int], pattern: str = "common_centroid",
                   dummies: int = 1) -> list[str | None]:
    """The order of a matched row's units, a dummy as None at both ends
    (`dummies` each). `units` maps each matched device to its unit count.

    - ABBA: two devices of equal, even counts, as ABBA ABBA ...
    - common_centroid: a row symmetric about its middle, so every device's
      centroid is the row's: each count even, except at most one device
      with an odd count, whose odd unit takes the middle.
    - interdigitate: every device spread evenly along the row (runs no
      longer than the ratio of counts). One unit per device keeps the
      order given.

    Raises LayoutError for a pattern the counts cannot make; the gate that
    reads the row is check_layout_match.py."""
    if pattern not in MATCH_PATTERNS:
        raise LayoutError(f"matched row pattern {pattern!r}: one of "
                          f"{', '.join(MATCH_PATTERNS)}")
    if len(units) < 2 or any(not isinstance(n, int) or isinstance(n, bool)
                             or n < 1 for n in units.values()):
        raise LayoutError(f"a matched row needs two or more devices of one "
                          f"or more units each, got {units}")
    if not isinstance(dummies, int) or dummies < 0:
        raise LayoutError(f"dummies must be a count >= 0, got {dummies!r}")
    names = list(units)
    if pattern == "ABBA":
        a, b = (names + [None])[:2] if len(names) == 2 else (None, None)
        if a is None or units[a] != units[b] or units[a] % 2:
            raise LayoutError(f"ABBA takes two devices of equal, even unit "
                              f"counts, got {units}: use common_centroid")
        seq = [a, b, b, a] * (units[a] // 2)
    elif pattern == "common_centroid":
        odd = [n for n in names if units[n] % 2]
        if len(odd) > 1:
            raise LayoutError(f"no one-row common centroid for {units}: "
                              f"{', '.join(odd)} all have odd unit counts "
                              "(at most one may); double the units or "
                              "draw a 2-D array")
        half = {n: units[n] // 2 for n in names if units[n] // 2}
        left = _spread(half) if half else []
        seq = left + odd + left[::-1]
    else:
        seq = _spread(units)
    return [None] * dummies + seq + [None] * dummies


def matched_row(top, cell, units: dict[str, int], *, pitch: float,
                pattern: str = "common_centroid", dummies: int = 1,
                axis: str = "x", origin: tuple[float, float] = (0.0, 0.0),
                pad_layer: tuple[int, int] = GF180_LAYER["metal1"]) -> dict:
    """Place a row of equal unit cells in `top`: match_sequence(units,
    pattern, dummies), slot i at `origin` + i * `pitch` along `axis` (a
    negative pitch runs the row down or left). Every slot is one `cell`
    (the unit device, so the units and the dummies are one size and one
    orientation by construction); the caller wires them.

    Returns {sequence: [device or None], refs: [ref per slot], units:
    {device: [slot origins]}, dummies: [slot origins], pads: [{device,
    slot, origin, boxes}]} with `boxes` the cell's `pad_layer` shapes
    (layer_boxes order) moved to the slot, in `top`'s frame: the pad map a
    caller routes each unit's terminals and ties each dummy's from."""
    if axis not in ("x", "y"):
        raise LayoutError(f"matched row axis {axis!r}: 'x' or 'y'")
    if not pitch:
        raise LayoutError("a matched row needs a non-zero pitch")
    seq = match_sequence(units, pattern, dummies)
    local = layer_boxes(cell, pad_layer)
    out = {"sequence": seq, "refs": [], "units": {n: [] for n in units},
           "dummies": [], "pads": []}
    for i, name in enumerate(seq):
        dx, dy = (i * pitch, 0.0) if axis == "x" else (0.0, i * pitch)
        at = (round(origin[0] + dx, 4), round(origin[1] + dy, 4))
        out["refs"].append(top.add_ref(cell).move(at))
        (out["units"][name] if name else out["dummies"]).append(at)
        out["pads"].append({"device": name, "slot": i, "origin": at,
                            "boxes": [(round(b[0] + at[0], 4), round(b[1] + at[1], 4),
                                       round(b[2] + at[0], 4), round(b[3] + at[1], 4))
                                      for b in local]})
    return out


def gds_bbox_um(gds_path, topcell: str) -> dict:
    """The bounding box of `topcell` in the GDS at `gds_path`, hierarchy
    included, as {x0, y0, x1, y1, width, height} in um. Raises LayoutError
    when the cell is missing or empty - a footprint that could not be
    measured is never read as one that fits."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise LayoutError(f"{gds_path} has no cell {topcell!r} to measure")
    b = cell.dbbox()
    if b.empty():
        raise LayoutError(f"cell {topcell!r} in {gds_path} is empty - "
                          "no footprint to measure")
    return {"x0": round(b.left, 4), "y0": round(b.bottom, 4),
            "x1": round(b.right, 4), "y1": round(b.top, 4),
            "width": round(b.width(), 4), "height": round(b.height(), 4)}


def fet_pads(fet) -> dict[str, tuple[float, ...]]:
    """The four metal1 pads of a single-finger draw_nfet()/draw_pfet():
    gate contacts above and below the channel, the two diffusion contacts
    left ("s") and right ("d") of it. Refuses any other shape, because a
    wrong guess here would wire a net to the wrong terminal."""
    boxes = layer_boxes(fet, GF180_LAYER["metal1"])
    if len(boxes) != 4:
        raise LayoutError(
            f"expected 4 metal1 pads on a one-finger FET, found {len(boxes)}")
    by_y = sorted(boxes, key=lambda b: (b[1] + b[3]) / 2)
    gate_bot, gate_top = by_y[0], by_y[-1]
    s, d = sorted(by_y[1:3], key=lambda b: b[0])
    return {"s": s, "d": d, "gate_top": gate_top, "gate_bot": gate_bot}


def rect(top, x0: float, y0: float, x1: float, y1: float,
        layer: tuple[int, int]) -> None:
    """Add one axis-aligned metal (or other) rectangle directly onto `top`'s
    own polygon set - the hand-routed wires connecting placed primitive-cell
    instances (docs/design.md 5: "routing them explicitly, because there is
    no analog autorouter")."""
    top.add_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer=layer)


# --------------------------------------------------------------------- DRC

def fresh(*paths) -> None:
    """Delete a tool's output before the tool runs, so a file left by an
    earlier run can never be read as this run's result. Every gate's work
    dir lives under the workspace's persistent log/."""
    for p in paths:
        Path(p).unlink(missing_ok=True)


def require_ok(proc, what: str) -> str:
    """stdout+stderr of a finished tool run, or LayoutError if it exited
    non-zero. A launch that failed outright never gets this far: run_eda
    raises on OSError and timeout."""
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise LayoutError(
            f"{what} exited {proc.returncode} - the run did not complete: "
            f"{out[-2000:]}")
    return out


def run_klayout_drc(gds_path, topcell: str, out_rdb, timeout: float = 240.0,
                    decks: str = DRC_DECKS) -> str:
    deck = pdk_root() / DRC_DECK_REL
    if not deck.is_file():
        raise LayoutError(f"no klayout GF180 DRC deck at {deck}")
    fresh(out_rdb)
    proc = run_eda(
        ["klayout", "-b", "-r", str(deck),
         "-rd", f"input={gds_path}", "-rd", f"topcell={topcell}",
         "-rd", f"report={out_rdb}", "-rd", "run_mode=flat",
         "-rd", "verbose=false", "-rd", f"variant={pdk_root().name}",
         "-rd", f"decks={decks}", "-rd", "threads=2", "-rd", "workers=1"],
        cwd=Path(out_rdb).parent, timeout=timeout)
    out = require_ok(proc, "klayout DRC")
    if "DRC RESULT" not in out:
        raise LayoutError(
            "klayout DRC did not complete - no 'DRC RESULT' line (no rules "
            f"loaded/ran is a refusal, never a pass): {out[-2000:]}")
    if not Path(out_rdb).is_file():
        raise LayoutError(f"klayout DRC produced no report at {out_rdb}")
    return out


def run_klayout_antenna(gds_path, topcell: str, out_rdb,
                        timeout: float = 240.0) -> str:
    return run_klayout_drc(gds_path, topcell, out_rdb, timeout, decks="antenna")


def nets_of_polygons(gds_path, topcell: str,
                     polygons: list[str]) -> list[list[str | None]]:
    """For each klayout polygon string `(x,y;x,y;...)` in um, the nets whose
    drawn shape it is or lies inside: exact matches if any, else containment.
    A net is its label (several labels: comma-joined), or None when unlabelled.
    An empty list means no conductor matched."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise LayoutError(f"{gds_path} has no cell named {topcell!r}")
    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, cell, []))
    regions = {}
    for name, (layer, datatype) in _NET_STACK:
        regions[name] = l2n.make_layer(layout.layer(layer, datatype), name)
        l2n.connect(regions[name])
    for (_a, _), (_b, _) in zip(_NET_STACK, _NET_STACK[1:]):
        l2n.connect(regions[_a], regions[_b])
    for metal, (layer, datatype) in _NET_LABELS.items():
        text = l2n.make_text_layer(layout.layer(layer, datatype), f"{metal}_t")
        l2n.connect(text, regions[metal])
    l2n.extract_netlist()
    circuit = next(l2n.netlist().each_circuit_top_down())
    shapes = []  # (net name, merged shapes of that net on one layer)
    for net in circuit.each_net():
        for region in regions.values():
            on_layer = l2n.shapes_of_net(net, region, True).merged()
            if not on_layer.is_empty():
                shapes.append((net.name or None, on_layer))
    out = []
    for text in polygons:
        try:
            poly = kdb.DPolygon.from_s(text).to_itype(layout.dbu)
        except RuntimeError:
            out.append([])
            continue
        probe = kdb.Region(poly)
        exact = [n for n, r in shapes if any(p == poly for p in r.each())]
        within = exact or [n for n, r in shapes if (probe - r).is_empty()]
        out.append(sorted(set(within), key=str))
    return out


_LAYER_PREFIXES = [
    ("OFFGRID", "offgrid"),
    ("ANT.", "antenna"),
    ("CO.", "contact"), ("DF.", "comp"), ("PL.", "poly2"),
    ("NP.", "nplus"), ("PP.", "pplus"), ("NW.", "nwell"),
    ("MET1", "metal1"), ("M1.", "metal1"),
    ("MET2", "metal2"), ("M2.", "metal2"),
    ("SAB", "sab"), ("RES", "resistor"),
]


def drc_layer_of(category: str) -> str:
    up = (category or "").upper()
    if "OFFGRID" in up:
        return "offgrid"
    for prefix, layer in _LAYER_PREFIXES:
        if up.startswith(prefix):
            return layer
    return "unknown"


def parse_drc_rdb(path) -> list[dict]:
    """Parse a klayout .lyrdb report database into
    [{category, description, cell, value, values}, ...] - one per violation
    instance. A report with zero <item> entries is a real clean pass, NOT
    treated as "no rules loaded" here (run_klayout_drc already refused that
    case by requiring the 'DRC RESULT' banner)."""
    tree = ET.parse(str(path))
    root = tree.getroot()
    cats: dict[str, str] = {}
    for c in root.iter("category"):
        name = (c.findtext("name") or "").strip("'")
        cats[name] = (c.findtext("description") or "").strip()
    items = []
    for item in root.iter("item"):
        cat = (item.findtext("category") or "").strip("'")
        cell = (item.findtext("cell") or "").strip("'")
        values = [v.text for v in item.iter("value") if v.text]
        items.append({
            "category": cat,
            "description": cats.get(cat, cat),
            "cell": cell or None,
            "value": values[0] if values else None,
            "values": values,
        })
    return items


# --------------------------------------------------------------------- LVS

# The PDK's magic tech reads GDS 110/11 (klayout's metal1_res, the marker
# draw_metal_res() puts on an rm1 body) as MET2RES - a typo beside the
# 110/12 line that really is MET2RES - so MET1RES is never set on input and
# magic sees an rm1 resistor as a plain metal1 short. Proved on a lone
# draw_metal_res(): stock tech, "Ports A and B are electrically shorted";
# with this one line fixed, "X0 A B rm1 r_width=2u r_length=10u".
_RM1_TYPO = " calma MET2RES 110 11\n"
_RM1_FIX = " calma MET1RES 110 11\n"


def magic_tech_source() -> Path:
    """The PDK's own magic tech file (magic_tech patches a copy of it)."""
    return pdk_root() / "libs.tech" / "magic" / f"{pdk_root().name}.tech"


# The PDK's pnp rules (#285) give the collector as the one terminal and the
# emitter's *pdiff as the substrate:
#   device msubcircuit pnp_10p00x10p00 pnp pwell,space/w *pdiff error a2>99.0 a2<101.0
# so the device has no second terminal, a2 is 0, no size range applies and
# the first pnp rule names every pnp (all four sizes came out
# pnp_10p00x00p42). The fix makes the emitter terminal 1 and the collector
# terminal 2, with no substrate, and selects on a1 (the emitter area, as the
# npn rules do over *ndiff):
#   device msubcircuit pnp_10p00x10p00 pnp *pdiff pwell,space/w a1>99.0 a1<101.0
# ext2spice writes an msubckt as terminal 2, gate, terminal 1, so the line
# stays `C B E pnp_...`, sm141064's pin order, and with no substrate magic
# writes no fourth pin (the .ext names it "None"). Proved on the PDK's four
# pnp cells: each extracts as itself, `X0 C B E pnp_<size>`.
_PNP_RULE = re.compile(
    r"^(\s*device\s+msubcircuit\s+pnp_\S+\s+pnp)\s+(.*)$", re.M)
_PNP_TYPO = re.compile(r"pwell,space/w \*pdiff error a2>(\S+) a2<(\S+)\s*")
_PNP_FIXED = re.compile(r"\*pdiff pwell,space/w a1>(\S+) a1<(\S+)\s*")


def _fix_pnp_rules(text: str, src) -> str:
    rules = list(_PNP_RULE.finditer(text))
    if not rules:
        raise LayoutError(
            f"{src} has no `device msubcircuit pnp_*` rule - check how this "
            "PDK extracts its pnp before trusting an extraction")

    def fix(m):
        body = m.group(2)
        if _PNP_FIXED.fullmatch(body):
            return m.group(0)
        t = _PNP_TYPO.fullmatch(body)
        if not t:
            raise LayoutError(
                f"{src}: pnp rule `{m.group(0).strip()}` is neither the known "
                "a2-on-substrate form nor its fix - check which size it "
                "names before trusting an extraction")
        return (f"{m.group(1)} *pdiff pwell,space/w "
                f"a1>{t.group(1)} a1<{t.group(2)}")
    return _PNP_RULE.sub(fix, text)


# The PDK's tech has no extract rule for its well diodes (#286 #287 #291):
# diode_pw2dw (lvpwell anode in deep nwell), diode_dw2ps (deep nwell over
# the substrate) and diode_nw2ps_03v3/_06v0 (nwell over the substrate),
# though its header lists them. Each pcell marks its well with WELL_DIODE
# (GDS 153/51), which cifinput does not read, so the well comes in as plain
# nwell / pwell / dnwell and magic has no device to name. The fix follows
# the tech's own nbase/pbase pattern: read 153/51, give each marked well a
# type of its own (nwdiode, mvnwdiode under DUALGATE, pwdiode, dnwdiode)
# that draws, connects and writes out exactly as the plain well, and add
# one `device` rule per diode. Proved on the PDK's cells: each extracts as
# itself, `D0 A K diode_<name> pj=8u area=4p` on a 2 x 2 um well.
_WELL_DIODE_RULES = (
    " device pdiode diode_pw2dw      *pwdiode dnwell,dnwdiode a=area p=pj\n"
    " device ndiode diode_dw2ps      dnwdiode space/w,pwell a=area p=pj\n"
    " device ndiode diode_nw2ps_03v3 nwdiode space/w,pwell a=area p=pj\n"
    " device ndiode diode_nw2ps_06v0 mvnwdiode space/w,pwell a=area p=pj\n")
_WELL_DIODE_NAMES = ("diode_pw2dw", "diode_dw2ps", "diode_nw2ps_03v3",
                     "diode_nw2ps_06v0")
_NWELL_CONNECT = "nwell,*nsd,*mvnsd,nbase,dnwell"
_PWELL_CONNECT = "pwell,*psd,*mvpsd,pbase,isosub"
_WELL_DIODE_EDITS = (
    ("  well nbase,pnp\n",
     "  well nbase,pnp\n  well nwdiode,nwd\n  well mvnwdiode,mvnwd\n"
     "  well pwdiode,pwd\n"),
    ("  dwell isosubstrate,isosub\n",
     "  dwell isosubstrate,isosub\n  dwell dnwdiode,dnwd\n"),
    ("  nbase\t    nwell ntransistor_stripes\n",
     "  nbase\t    nwell ntransistor_stripes\n  nwdiode   nwell\n"
     "  mvnwdiode nwell\n  pwdiode   pwell\n  dnwdiode  cwell\n"),
    ("  allnwell\t   nwell,rnwell,nbase",
     "  allnwell\t   nwell,rnwell,nbase,nwdiode,mvnwdiode"),
    ("  allpwell\t   pwell\n", "  allpwell\t   pwell,pwdiode\n"),
    (f"  {_NWELL_CONNECT} {_NWELL_CONNECT}\n",
     f"  {_NWELL_CONNECT},nwdiode,mvnwdiode,dnwdiode"
     f" {_NWELL_CONNECT},nwdiode,mvnwdiode,dnwdiode\n"),
    (f"  {_PWELL_CONNECT}  {_PWELL_CONNECT}\n",
     f"  {_PWELL_CONNECT},pwdiode  {_PWELL_CONNECT},pwdiode\n"),
    (" layer DNWELL\tdnwell\n", " layer DNWELL\tdnwell,dnwdiode\n"),
    (" layer pwell PWELL,PWELLTXT\n and-not BJTDEF,BJTDRC\n",
     " layer pwell PWELL,PWELLTXT\n and-not BJTDEF,BJTDRC\n"
     " and-not WELLDIODE\n"),
    (" layer nwell NWELL,NWELLTXT\n and-not BJTDEF,BJTDRC\n",
     " layer nwell NWELL,NWELLTXT\n and-not BJTDEF,BJTDRC\n"
     " and-not WELLDIODE\n"),
    (" layer dnwell DNWELL\n labels DNWELL\n",
     " templayer dnwdmk WELLDIODE\n and-not PWELL\n\n"
     " layer dnwell DNWELL\n and-not dnwdmk\n labels DNWELL\n\n"
     " layer dnwdiode DNWELL\n and dnwdmk\n\n"
     " layer nwdiode NWELL\n and WELLDIODE\n and-not BJTDEF,BJTDRC\n"
     " and-not DUALGATE\n\n"
     " layer mvnwdiode NWELL\n and WELLDIODE\n and-not BJTDEF,BJTDRC\n"
     " and DUALGATE\n\n"
     " layer pwdiode PWELL\n and WELLDIODE\n and-not BJTDEF,BJTDRC\n"),
    (" calma DNWELL 12 0\n", " calma DNWELL 12 0\n calma WELLDIODE 153 51\n"),
    (" device pdiode sc_diode", _WELL_DIODE_RULES + " device pdiode sc_diode"),
)
_DEVICE_NAME_RE = r"^\s*device\s+\S+\s+{}\s"


def _add_well_diodes(text: str, src) -> str:
    have = [n for n in _WELL_DIODE_NAMES
            if re.search(_DEVICE_NAME_RE.format(re.escape(n)), text, re.M)]
    if len(have) == len(_WELL_DIODE_NAMES):
        return text
    if have:
        raise LayoutError(
            f"{src} extracts some well diodes ({', '.join(have)}) but not "
            "all - check how this PDK extracts them before trusting an "
            "extraction")
    for old, new in _WELL_DIODE_EDITS:
        if text.count(old) != 1:
            raise LayoutError(
                f"{src} has no extract rule for its well diodes and does not "
                f"have the one line `{old.strip()}` the fix edits - check "
                "how this PDK reads WELL_DIODE (153/51) before trusting an "
                "extraction")
        text = text.replace(old, new)
    return text


# The Schottky diode (#289). Two faults in how cifinput reads it:
# - It makes nsd of any unmarked diffusion within 0.5 um of N+
#   (nsdexpand), and the PDK cell's anode sits 0.28 um from its cathode
#   tap, so the anode's edge nearest the cathode came in as nsd. That cut
#   the anode apart and magic wrote three sc_diodes, two with floating
#   anodes. The anode is SCHOTTKY, never an nsd tap: the nsd rule gains
#   `and-not SCHOTTKY`.
# - It closes the 0.28 um anode-cathode gap ("Grow Schottky anode so that
#   drawn device abuts the cathode") with anode, so a drawn 2 x 2 um anode
#   between two cathodes extracts as area=5.12p pj=9.12u. The junction is
#   the anode diffusion only (sm141064's sc_diode has cjp=0, an area
#   device; the PDK pcell and the reference netlist give w x l). The gap is
#   still closed so the anode abuts its cathode, but as cathode (nsd), and
#   the anode stays as drawn.
# With both, the PDK cell extracts as `D0 A K sc_diode pj=8u area=4p`.
_NSD_RULE = (" layer nsd DIFF\n and-not PPLUS\n and-not NPLUS\n"
             " and-not POLY\n and nwelldef\n and-not DUALGATE\n"
             " and nsdexpand\n")
_NSD_FIXED = _NSD_RULE + " and-not SCHOTTKY\n"
_SC_ANODE = (" layer schottky SCHOTTKY\n and DIFF\n and-not NPLUS\n"
             " and-not PPLUS\n or sccathode\n grow 140\n shrink 140\n"
             " and-not sccathode\n")
_SC_ANODE_FIXED = (_SC_ANODE + " and DIFF\n\n"
                   " # the closed anode-cathode gap is cathode\n"
                   " layer nsd SCHOTTKY\n and DIFF\n and-not NPLUS\n"
                   " and-not PPLUS\n or sccathode\n grow 140\n shrink 140\n"
                   " and-not DIFF\n")


def _fix_schottky(text: str, src) -> str:
    for old, new, what in ((_NSD_RULE, _NSD_FIXED, "nsd"),
                           (_SC_ANODE, _SC_ANODE_FIXED, "the Schottky anode")):
        if new in text:
            continue
        if text.count(old) != 1:
            raise LayoutError(
                f"{src} reads {what} neither through the one known rule "
                "that mis-extracts sc_diode nor its fix - check how this "
                "PDK extracts sc_diode before trusting an extraction")
        text = text.replace(old, new)
    return text

# The P-varactors cap_pmos_03v3_b and cap_pmos_06v0_b (P+ poly gate on
# P+ diffusion in the substrate, no well) extract no device. The tech reads
# their gate as pvaractor (POLY and DIFF and PPLUS and-not nwelldef) and
# names the device on its *ppdiff terminals, but it also counts the gate as
# a pfet area: it grows pfetexpand (mvpfetexpand) over it, so the source and
# drain beside it read as pfet diffusion (pdiff/pdc), not substrate
# diffusion (psd/psc), and it forces an nwell under it, which the gate then
# sits in. The fix keeps the varactor gate out of both, so its terminals
# read as ppdiff and the device extracts as
# `X0 G SD cap_pmos_03v3_b c_width=2u c_length=2u`. Only a MOSCAP-marked
# P+ gate outside any nwell counts as a varactor area, so a pfet, a pcap
# in its nwell, or a pfet drawn without one reads as before.
_PFET_EXPAND = (" templayer pfetexpand pfetarea\n grow 530\n\n"
                " # Always force nwell under pfet\n layer nwell pfetarea\n"
                " grow 310\n")
_PFET_EXPAND_FIXED = (" templayer pvararea pfetarea\n and MOSCAP\n"
                      " and PPLUS\n and-not nwelldef\n\n"
                      " templayer pfetexpand pfetarea\n and-not pvararea\n"
                      " grow 530\n\n"
                      " # Always force nwell under pfet\n layer nwell pfetarea\n"
                      " and-not pvararea\n grow 310\n")
_MVPFET_EXPAND = " templayer mvpfetexpand mvpfetarea\n grow 520\n"
_MVPFET_EXPAND_FIXED = (" templayer mvpvararea mvpfetarea\n"
                        " and MOSCAP\n and PPLUS\n and-not nwelldef\n\n"
                        " templayer mvpfetexpand mvpfetarea\n"
                        " and-not mvpvararea\n grow 520\n")


def _fix_pvaractors(text: str, src) -> str:
    for old, new, what in (
            (_PFET_EXPAND, _PFET_EXPAND_FIXED, "the 3.3 V pfet area"),
            (_MVPFET_EXPAND, _MVPFET_EXPAND_FIXED, "the 6 V pfet area")):
        if new in text:
            continue
        if text.count(old) != 1:
            raise LayoutError(
                f"{src} grows {what} neither through the one known rule "
                "that hides the P-varactor terminals nor its fix - check "
                "how this PDK extracts cap_pmos_*_b before trusting an "
                "extraction")
        text = text.replace(old, new)
    return text


def magic_tech(work_dir) -> Path | None:
    """A copy of the PDK's magic tech with its known extraction faults fixed
    (the rm1 input typo, the pnp size rules, the unextracted well diodes,
    the split Schottky diode, the hidden P-varactor terminals), written
    into work_dir, or None when the PDK already has them all right.
    Refuses a tech file that has neither the fault nor its fix, rather
    than guess."""
    src = magic_tech_source()
    if not src.is_file():
        raise LayoutError(f"no magic tech file at {src}")
    orig = src.read_text(encoding="utf-8", errors="replace")
    text = orig
    if _RM1_FIX not in text:
        if text.count(_RM1_TYPO) != 1:
            raise LayoutError(
                f"{src} maps GDS 110/11 neither to MET1RES nor through the "
                "one known MET2RES typo - check how this PDK reads rm1 "
                "before trusting an extraction")
        text = text.replace(_RM1_TYPO, _RM1_FIX)
    text = _fix_pnp_rules(text, src)
    text = _add_well_diodes(text, src)
    text = _fix_schottky(text, src)
    text = _fix_pvaractors(text, src)
    if text == orig:
        return None
    out = Path(work_dir) / src.name
    out.write_text(text, encoding="utf-8")
    return out


# ------------------------------------------- poly resistor body capacitance
#
# The tech's extract section removes every resistor type from parasitic C
# ("Resistor types should be removed from all parasitic capacitance
# calculations, or else they just create floating caps"), trusting the
# device model for it. The PDK's poly resistor models carry only part of
# it: `c1 1 3 fox_sub l='r_l/2' w=r_w` and `c2 2 3 ...`, cj 88.5 aF/um^2
# on the model's narrowed body (r_w = W - 2 r_dw, r_l = L - 2 r_dl), against
# the tech's own PDS_035_03 poly-to-substrate figures of 110.67 aF/um^2 of
# area and 50.72 aF/um of edge (nominal; the RC variants have their own).
# So magic's netlist of a poly resistor is short the difference on the
# body: (tech area x W L - cj x r_w r_l) + tech fringe x 2 L, the two long
# edges (the ends join the resistor's heads, which magic extracts as plain
# poly). poly_body_residuals() adds that, half to each terminal, to the
# device's third terminal - where the model puts its own cj.
#
# The diffusion resistors (pplus_u, nplus_u, ...) need nothing: their
# models put a junction diode on each half with area='r_w*r_l/2' and
# pj='r_w+2*r_l/2', so the two halves together carry the body's full area
# and perimeter at the junction's cj and cjsw, and the tech already relies
# on the models for diffusion C ("Rely on device models to capture *ndiff
# area cap"). nwell (rnw) gets the tech's own areacap, not a model C.

_TECH_POLY_DEVICE_RE = re.compile(
    r"^\s*device\s+rsubcircuit\s+(\S+)\s+\S+\s+\*poly\s")
_TECH_POLY_CAP_RE = re.compile(
    r"^\s*default(areacap|perimeter)\s+\*poly\s+active\s+([0-9.eE+-]+)\s*$")
_MODEL_NOMINAL_RE = re.compile(
    r"^'?([0-9.]+(?:[eE][-+]?\d+)?)(?:\*\(1\+[A-Za-z0-9_*/+()-]*\))?'?$")
# the model's body C, exactly as every poly resistor subckt writes it; any
# other form is a model this residual was not worked out for
_MODEL_BODY_C = ("c1 1 3 fox_sub dtemp=dtemp l='r_l/2' w=r_w",
                 "c2 2 3 fox_sub dtemp=dtemp l='r_l/2' w=r_w")
_MODEL_GEOMETRY = {"r_l": "'s*(r_length-2*r_dl)'",
                   "r_w": "'r_width-2*r_dw'"}
_PDS_NOTE_RE = re.compile(r"PDS_035_03")


def tech_poly_caps(tech_text: str, tech_name: str,
                   variant: str | None) -> dict:
    """The tech's poly-to-substrate C at `variant` (None: the nominal `()`
    one): {area_af_um2, perim_af_um, models: [poly resistor models],
    cites: {...}}. Refuses a tech that does not give exactly one of each
    for the variant, or names no PDS source for them."""
    want = f"({variant or ''})"
    in_extract = False
    active = None   # the variants the current block applies to
    found: dict[str, tuple[float, int]] = {}
    models: list[str] = []
    pds_line = None
    for n, line in enumerate(tech_text.splitlines(), 1):
        s = line.strip()
        if s == "extract":
            in_extract = True
            continue
        if not in_extract:
            continue
        if s == "end":
            break
        if pds_line is None and _PDS_NOTE_RE.search(s):
            pds_line = n
        if s.startswith("variants "):
            spec = s.split(None, 1)[1].replace(" ", "")
            active = None if spec == "*" else set(spec.split(","))
            continue
        m = _TECH_POLY_DEVICE_RE.match(line)
        if m:
            models.append(m.group(1))
            continue
        m = _TECH_POLY_CAP_RE.match(line)
        if m and (active is None or want in active):
            if m.group(1) in found:
                raise LayoutError(
                    f"{tech_name} gives default{m.group(1)} *poly active "
                    f"twice for variant {want} (lines {found[m.group(1)][1]}"
                    f" and {n}) - check which one magic uses")
            found[m.group(1)] = (float(m.group(2)), n)
    missing = {"areacap", "perimeter"} - set(found)
    if missing or not models or pds_line is None:
        raise LayoutError(
            f"{tech_name} does not give the poly-to-substrate C this "
            f"extraction adds to poly resistor bodies for variant {want} "
            f"(missing: {sorted(missing) or ''}"
            f"{' poly resistor devices' if not models else ''}"
            f"{' a PDS source note' if pds_line is None else ''})")
    return {"area_af_um2": found["areacap"][0],
            "perim_af_um": found["perimeter"][0],
            "models": models,
            "cites": {"area": f"{tech_name}:{found['areacap'][1]}",
                      "perim": f"{tech_name}:{found['perimeter'][1]}",
                      "source": f"{tech_name}:{pds_line} (PDS_035_03)"}}


def _model_nominal(expr: str, what: str, where: str) -> float:
    m = _MODEL_NOMINAL_RE.match(expr)
    if not m:
        raise LayoutError(
            f"{where} sets {what}={expr}, not a number (with at most an "
            "MC skew factor) - this residual was not worked out for it")
    return float(m.group(1))


def model_poly_body_c(model_text: str, model_name: str,
                      models: list[str]) -> dict:
    """{model: {cj_af_um2, dw_um, dl_um, cites}} for each poly resistor
    subckt in `models`, read from the PDK's ngspice model file. Refuses a
    subckt that is missing, defined twice, or carries its body C in any
    other form than the fox_sub c1/c2 pair this residual subtracts."""
    lines = model_text.splitlines()
    out = {}
    for model in models:
        heads = [i for i, line in enumerate(lines)
                 if line.split()[:2] == [".subckt", model]]
        if len(heads) != 1:
            raise LayoutError(
                f"{model_name} defines subckt {model} {len(heads)} times - "
                "the poly body residual needs exactly one model to read")
        start = heads[0]
        params: dict[str, tuple[str, int]] = {}
        for tok in lines[start].split()[5:]:
            k, _, v = tok.partition("=")
            params[k] = (v, start + 1)
        body: list[str] = []
        cj = None
        end = None
        for i in range(start + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith(".ends"):
                end = i
                break
            if s.startswith(".param "):
                k, _, v = s[len(".param "):].partition("=")
                params[k.strip()] = (v.strip(), i + 1)
            elif s.startswith(".model fox_sub c "):
                m = re.fullmatch(r"\.model fox_sub c cj=(\S+)", s)
                if not m or cj is not None:
                    raise LayoutError(
                        f"{model_name}:{i + 1}: fox_sub in {model} is not "
                        "a single plain cj - not the model C this residual "
                        "subtracts")
                cj = (float(m.group(1)), i + 1)
            elif s[:1].lower() == "c":
                body.append(s)
        where = f"{model_name}:{start + 1} ({model})"
        if (end is None or cj is None or sorted(body) != list(_MODEL_BODY_C)
                or any(params.get(k, ("",))[0] != v
                       for k, v in _MODEL_GEOMETRY.items())):
            raise LayoutError(
                f"{where} does not carry its body C as the fox_sub c1/c2 "
                f"pair on r_w x r_l/2 (found {body}) - the poly body "
                "residual would subtract the wrong model C")
        for k in ("r_dw", "r_dl"):
            if k not in params:
                raise LayoutError(f"{where} sets no {k}")
        out[model] = {
            # cj is in F/m^2 = 1e6 aF/um^2
            "cj_af_um2": cj[0] * 1e6,
            "dw_um": _model_nominal(params["r_dw"][0], "r_dw", where) * 1e6,
            "dl_um": _model_nominal(params["r_dl"][0], "r_dl", where) * 1e6,
            "cites": {"cj": f"{model_name}:{cj[1]}",
                      "dw": f"{model_name}:{params['r_dw'][1]}",
                      "dl": f"{model_name}:{params['r_dl'][1]}"}}
    return out


def poly_body_residual_ff(w_um: float, l_um: float, tech: dict,
                          model: dict) -> float:
    """The poly-to-substrate C (fF) one W x L poly resistor body has in the
    tech but not in its model: tech area x W L + tech fringe x 2 L (the two
    long edges) - model cj x (W - 2 dw)(L - 2 dl)."""
    tech_af = tech["area_af_um2"] * w_um * l_um + tech["perim_af_um"] * 2 * l_um
    model_af = (model["cj_af_um2"] * (w_um - 2 * model["dw_um"])
                * (l_um - 2 * model["dl_um"]))
    return (tech_af - model_af) / 1000.0


def poly_body_caps(variant: str | None = None) -> tuple[dict, dict]:
    """(tech poly C at `variant`, {model: model body C}) from the PDK's
    magic tech and ngspice model file."""
    import netlistlib
    root = pdk_root()
    tech_path = root / "libs.tech" / "magic" / f"{root.name}.tech"
    model_path = root / "libs.tech" / "ngspice" / netlistlib.PDK_NGSPICE_FILES[0]
    for p in (tech_path, model_path):
        if not p.is_file():
            raise LayoutError(f"no {p.name} in the PDK to read poly "
                              "resistor C from")
    tech = tech_poly_caps(
        tech_path.read_text(encoding="utf-8", errors="replace"),
        tech_path.name, variant)
    models = model_poly_body_c(
        model_path.read_text(encoding="utf-8", errors="replace"),
        model_path.name, tech["models"])
    return tech, models


def add_poly_body_residuals(pex_text: str, tech: dict,
                            models: dict) -> tuple[str, list[dict]]:
    """`pex_text` with each poly resistor body's residual C (see
    poly_body_residual_ff) added as two capacitors, half from each end to
    the device's third terminal, before its subckt's .ends; and one record
    per body {device, model, w_um, l_um, m, ends, body, residual_ff,
    model_c_ff}. A body whose residual is negative, or whose size or
    multiplier is not a plain number, is a refusal."""
    import netlistlib
    out: list[str] = []
    pending: list[str] = []
    bodies: list[dict] = []
    raw = pex_text.splitlines(keepends=True)
    i = 0
    while i < len(raw):
        line = raw[i]
        j = i + 1
        while j < len(raw) and raw[j].lstrip().startswith("+"):
            j += 1
        logical = " ".join([line.strip()] + [r.strip()[1:] for r in raw[i + 1:j]])
        parts = netlistlib.strip_comment(logical).split()
        low = parts[0].lower() if parts else ""
        if low.startswith(".ends"):
            out += pending
            pending = []
        elif low.startswith("x") and len(parts) >= 5:
            model_at = next((k for k, t in enumerate(parts[1:], 1)
                             if "=" in t), len(parts)) - 1
            model = parts[model_at]
            if model in models and model_at == 4:
                kv = dict(t.split("=", 1) for t in parts[5:] if "=" in t)
                w = netlistlib.spice_number(kv.get("r_width", ""))
                length = netlistlib.spice_number(kv.get("r_length", ""))
                mult = netlistlib.spice_number(kv.get("m", "1"))
                s = netlistlib.spice_number(kv.get("s", "1"))
                if None in (w, length, mult) or s != 1:
                    raise LayoutError(
                        f"poly resistor {parts[0]} ({model}) has no plain "
                        f"r_width/r_length/m, or s != 1: {logical.strip()}")
                w_um, l_um = w * 1e6, length * 1e6
                res = poly_body_residual_ff(w_um, l_um, tech,
                                            models[model]) * mult
                if res < 0:
                    raise LayoutError(
                        f"poly resistor {parts[0]} ({model}) has more C in "
                        f"its model than the tech gives it ({res:g} fF) - "
                        "check the tech and model values before adding "
                        "anything")
                mdl = models[model]
                mc = (mdl["cj_af_um2"] * (w_um - 2 * mdl["dw_um"])
                      * (l_um - 2 * mdl["dl_um"]) / 1000.0) * mult
                n1, n2, n3 = parts[1:4]
                tag = re.sub(r"[^A-Za-z0-9_]", "_", parts[0])
                for k, end in ((1, n1), (2, n2)):
                    pending.append(f"Crb{k}_{tag} {end} {n3} {res / 2:.6g}f\n")
                bodies.append({"device": parts[0], "model": model,
                               "w_um": w_um, "l_um": l_um, "m": mult,
                               "ends": [n1, n2], "body": n3,
                               "residual_ff": round(res, 5),
                               "model_c_ff": round(mc, 5)})
        out += raw[i:j]
        i = j
    if pending:
        raise LayoutError("poly resistor outside any .subckt ... .ends: "
                          "nowhere to put its body C")
    return "".join(out), bodies


# a node line of a magic .ext: node "name" R C x y type ... - C in aF,
# (x, y) the lower-left corner of one of the net's tiles, in internal units
_EXT_NODE_RE = re.compile(
    r'^node\s+"([^"]+)"\s+\S+\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)')
_EXT_CAP_RE = re.compile(r'^cap\s+"([^"]+)"\s+"([^"]+)"\s+(\S+)')
# extresist's sub-nodes of a net: name.t0 (a device terminal), name.n0
_SUBNODE_RE = re.compile(r"\.[tn]\d+$")


def _magic_head(tech, gds_path, topcell: str) -> list[str]:
    head = [f"tech load {tech}"] if tech else []
    return head + [f"gds read {gds_path}", f"load {topcell}",
                   "select top cell"]


def _ext_net(name: str) -> str:
    # magic marks a name it made up with '#' and a global one with '!';
    # ext2spice writes neither into a net's SPICE name
    return name.rstrip("#!")


def ext_nodes(ext_text: str) -> list[dict]:
    """The nets one magic .ext file defines, one per distinct node line:
    [{name, cap (aF, to substrate), x, y, type}]. magic can write the same
    node line twice (a port and its label); that is one net, not two."""
    seen, nodes = set(), []
    for line in ext_text.splitlines():
        m = _EXT_NODE_RE.match(line)
        if not m or m.groups() in seen:
            continue
        seen.add(m.groups())
        name, cap, x, y, typ = m.groups()
        nodes.append({"name": name, "cap": float(cap), "x": int(x),
                      "y": int(y), "type": typ})
    return nodes


def name_unlabelled_nets(ext_text: str) -> list[str]:
    """magic commands that put a text label on every net the .ext calls by
    a name magic made up (`a_38_n132#`), at one of that net's own tiles,
    naming it the same without the '#'. extresist drops every coupling and
    lumped C line of a net that has no label - only the C of its split
    sub-nodes stays - so an unlabelled net would reach the post-layout
    bench with a fraction of its real wire C. A label on a drawing layer is
    a name, not a port, so the pin set stays the same."""
    nodes = ext_nodes(ext_text)
    taken = {_ext_net(n["name"]) for n in nodes
             if not n["name"].endswith("#")}
    cmds, named = ["snap internal"], set()
    for n in nodes:
        if not n["name"].endswith("#") or n["name"] in named:
            continue
        named.add(n["name"])
        label = base = _ext_net(n["name"])
        i = 0
        while label in taken:
            i += 1
            label = f"{base}_{i}"
        taken.add(label)
        cmds += [f"box values {n['x']} {n['y']} {n['x'] + 1} {n['y'] + 1}",
                 f"label {label} c {n['type']}"]
    return cmds + ["snap lambda"] if named else []


def _pex_net(node: str) -> str:
    return _SUBNODE_RE.sub("", node)


def lost_couplings(ext_text: str, pex_text: str,
                   tolerance: float = 0.1) -> list[dict]:
    """The coupling capacitors magic's own .ext gives a pair of nets that a
    resist-extracted netlist does not carry: [{nets, ext_ff, pex_ff}].

    A pair's C in the SPICE is every C line between one of a net's nodes
    (the net or its extresist sub-nodes, name.tN/name.nN) and one of the
    other's. extresist spreads a net's C over its sub-nodes but keeps the
    total, and folds a device's own body node (an rm1's resistor body) into
    the device's terminals, so a pair's SPICE C is at least its .ext C
    unless lines were dropped; a pair under (1 - tolerance) of it was. A
    net the netlist never names (that body node) has no C of its own to
    keep and is not compared. Substrate C is not compared: magic hands a
    shielded plate's (a MIM's top plate) to its device, not the netlist."""
    import netlistlib

    want: dict[tuple[str, str], float] = {}
    for line in ext_text.splitlines():
        m = _EXT_CAP_RE.match(line)
        if m:
            a, b = sorted((_ext_net(m.group(1)), _ext_net(m.group(2))))
            if a != b:
                want[(a, b)] = want.get((a, b), 0.0) + float(m.group(3)) / 1e3
    named: set[str] = set()
    got: dict[tuple[str, str], float] = {}
    for line in netlistlib.join_continuations(pex_text):
        parts = netlistlib.strip_comment(line).split()
        if not parts or parts[0][:1].lower() not in "xrcmdq":
            continue
        named.update(_pex_net(tok) for tok in parts[1:] if "=" not in tok)
        if parts[0][:1].lower() != "c" or len(parts) < 4:
            continue
        value = netlistlib.spice_number(parts[3])
        a, b = sorted(_pex_net(node) for node in parts[1:3])
        if value is not None and a != b:
            got[(a, b)] = got.get((a, b), 0.0) + value * 1e15
    lost = []
    for pair, ff in sorted(want.items()):
        if not set(pair) <= named:
            continue
        have = got.get(pair, 0.0)
        if have < ff * (1.0 - tolerance) and ff - have > 1e-3:
            lost.append({"nets": list(pair), "ext_ff": round(ff, 5),
                         "pex_ff": round(have, 5)})
    return lost


_EXT_SUBSTRATE_RE = re.compile(r'^substrate\s+"([^"]+)"')
# a sub-node extresist wrote: rnode "name" R C x y ... - C in aF
_RNODE_RE = re.compile(r'^rnode\s+"([^"]+)"\s+\S+\s+(\S+)')


def uncount_rnode_caps(ext_text: str, res_ext_text: str,
                       pex_text: str) -> tuple[str, dict]:
    """pex_text with each resist-split net's C to the substrate net brought
    back to what magic's .ext gives it, and {net: [before_ff, after_ff]}.

    extresist gives each sub-node it writes (rnode, in .res.ext) a share of
    the net's coupling C - on every split net of the pll the rnode C sums
    to that net's coupling C in the .ext exactly - and ext2spice writes
    those shares as C to the substrate net while still writing the
    coupling capacitors themselves, so a split net carried its coupling C
    twice. A split net's C lines to the substrate net (its own node C plus
    the rnode shares) are scaled together to its .ext substrate C plus any
    .ext coupling to the substrate net: the coupling stays where it is
    real, between the nets, and the spread along the wire stays."""
    import netlistlib

    subs = [_ext_net(m.group(1)) for m in map(
        _EXT_SUBSTRATE_RE.match, ext_text.splitlines()) if m]
    shares: dict[str, float] = {}
    for line in res_ext_text.splitlines():
        m = _RNODE_RE.match(line)
        if m:
            net = _pex_net(_ext_net(m.group(1)))
            shares[net] = shares.get(net, 0.0) + float(m.group(2)) / 1e3
    split = {net for net, ff in shares.items() if ff > 0}
    if not split:
        return pex_text, {}
    if len(subs) != 1:
        raise LayoutError(
            f"the .ext names {len(subs)} substrate nets, not one - the "
            "rnode C extresist double-counts cannot be taken back off a "
            "net's C to the substrate")
    sub = subs[0]
    split.discard(sub)
    target: dict[str, float] = {}
    for n in ext_nodes(ext_text):
        net = _ext_net(n["name"])
        target[net] = target.get(net, 0.0) + n["cap"] / 1e3
    for line in ext_text.splitlines():
        m = _EXT_CAP_RE.match(line)
        if m:
            a, b = _ext_net(m.group(1)), _ext_net(m.group(2))
            if sub in (a, b) and a != b:
                net = b if a == sub else a
                target[net] = target.get(net, 0.0) + float(m.group(3)) / 1e3
    lines = pex_text.splitlines(keepends=True)
    to_sub: dict[str, list[tuple[int, float]]] = {}
    for i, line in enumerate(lines):
        parts = line.split()
        if len(parts) < 4 or parts[0][:1].lower() != "c":
            continue
        a, b = (_pex_net(node) for node in parts[1:3])
        value = netlistlib.spice_number(parts[3])
        if value is None or sub not in (a, b) or a == b:
            continue
        net = b if a == sub else a
        if net in split:
            to_sub.setdefault(net, []).append((i, value))
    changed = {}
    for net, found in sorted(to_sub.items()):
        have = sum(v for _i, v in found) * 1e15
        want = target.get(net, 0.0)
        if have <= want * (1.0 + 1e-6):
            continue
        scale = want / have
        for i, value in found:
            parts = lines[i].split()
            parts[3] = f"{value * scale * 1e15:.6g}f"
            lines[i] = " ".join(parts) + "\n"
        changed[net] = [round(have, 5), round(want, 5)]
    return "".join(lines), changed


# a style's own variants line in a magic tech: "variants (),(hrhc)" or "*"
_TECH_VARIANTS_RE = re.compile(r"^\s*variants\s+(\S+)\s*$")
_TECH_STYLE_RE = re.compile(r"^\s*style\s+(\S+)")
_EXT_STYLE_RE = re.compile(r"^style\s+([^\s(]+)\(([^)]*)\)\s*$", re.M)


def _tech_section(lines: list[str], name: str) -> list[str]:
    for i, line in enumerate(lines):
        if line.strip() == name:
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "end":
                    return lines[i + 1:j]
    raise LayoutError(f"the magic tech has no {name} section")


def _tech_types(token: str, aliases: dict[str, str], seen=()) -> set[str]:
    out = set()
    for t in token.split(","):
        t = t.split("/")[0].lstrip("*")
        if t in aliases and t not in seen:
            out |= _tech_types(aliases[t], aliases, (*seen, t))
        elif t:
            out.add(t)
    return out


def _has_mim(tech_text: str, pex_text: str) -> bool:
    """Whether the netlist has an instance of a cap the tech extracts as
    `device csubcircuit` (GF180's MIM)."""
    models = set(re.findall(r"^\s*device\s+csubcircuit\s+(\S+)",
                            tech_text, re.M))
    return any(tok in models for line in pex_text.splitlines()
               if line[:1] in "xX" for tok in line.split()[1:])


def mim_plate_areacaps(tech_text: str, style: str,
                       variant: str) -> dict[str, float]:
    """{model: aF/um^2} - for each capacitor magic extracts with its top
    plate as a type painted over the bottom plate's metal (`device
    csubcircuit MODEL TOP BOTTOM`, GF180's mimcap over m4), the C to the
    substrate per unit area the tech gives that top-plate type at this
    extract style and variant (its `defaultareacap`). magic files that C,
    the bottom plate's metal seen from below, on the top plate's net;
    move_mim_plate_caps puts it back on the bottom plate's. A model whose
    top type has no area cap at this style is left out."""
    lines = tech_text.splitlines()
    aliases = {}
    for line in _tech_section(lines, "aliases"):
        parts = line.split("#")[0].split()
        if len(parts) == 2:
            aliases[parts[0]] = parts[1]
    in_style, active, devices, caps = False, True, {}, {}
    for line in _tech_section(lines, "extract"):
        parts = line.split("#")[0].split()
        if not parts:
            continue
        m = _TECH_STYLE_RE.match(line)
        if m:
            in_style, active = m.group(1) == style, True
            continue
        if not in_style:
            continue
        m = _TECH_VARIANTS_RE.match(line)
        if m:
            got = m.group(1)
            active = got == "*" or f"({variant})" in got.split(",")
            continue
        if not active:
            continue
        if parts[:2] == ["device", "csubcircuit"] and len(parts) >= 5:
            devices[parts[2]] = _tech_types(parts[3], aliases)
        elif parts[0] == "defaultareacap" and len(parts) == 4:
            for t in _tech_types(parts[1], aliases):
                caps[t] = float(parts[3])
    out = {}
    for model, tops in devices.items():
        vals = {caps[t] for t in tops if t in caps}
        if len(vals) > 1:
            raise LayoutError(
                f"the magic tech gives {model}'s top-plate types "
                f"{sorted(tops)} {len(vals)} different area caps at style "
                f"{style}({variant}) - which one magic used cannot be told")
        if vals:
            out[model] = vals.pop()
    return out


def move_mim_plate_caps(ext_text: str, pex_text: str,
                        areacap: dict[str, float]) -> tuple[str, dict]:
    """pex_text with each MIM's bottom-plate C to the substrate moved from
    its top plate's net onto its bottom plate's, and {device: {top, bottom,
    ff}}.

    magic paints a MIM's top plate (mimcap) on the bottom plate's own
    plane (metal4), replacing the metal4 under it, so the plate's area C to
    the substrate - the bottom plate's metal4 seen from below, which shields
    the top plate - goes to the top plate's net: on the ota's 32x32 um MIM
    7.8 fF that kpex 2.5D (and the silicon) give the bottom plate. The
    device line names both plates and its size (`X.. top bottom MODEL
    c_width= c_length=`), so the area times the style's area cap
    (mim_plate_areacaps) comes off the top net's C lines to the substrate,
    scaled together, and goes onto the bottom plate's node as one C line.
    A top net that has less C to the substrate than that has something
    under the plate taking it (an overlap, not substrate C), which this
    cannot split, so it is a refusal. Couplings to a shape under the plate
    stay on the top net here; the kpex cross-check compares each pair."""
    import netlistlib

    if not areacap:
        return pex_text, {}
    subs = [_ext_net(m.group(1)) for m in map(
        _EXT_SUBSTRATE_RE.match, ext_text.splitlines()) if m]
    moves = []
    for line in netlistlib.join_continuations(pex_text):
        parts = netlistlib.strip_comment(line).split()
        if len(parts) < 4 or parts[0][:1].lower() != "x":
            continue
        model = next((p for p in parts[1:] if "=" not in p and
                      p in areacap), None)
        if model is None:
            continue
        nodes = parts[1:parts.index(model)]
        params = dict(p.split("=", 1) for p in parts if "=" in p)
        w = netlistlib.spice_number(params.get("c_width", ""))
        ln = netlistlib.spice_number(params.get("c_length", ""))
        if len(nodes) != 2 or w is None or ln is None:
            raise LayoutError(
                f"MIM {parts[0]} ({model}) is not 'top bottom MODEL "
                "c_width= c_length=' in magic's netlist, so its plate's C to "
                "the substrate cannot be moved to the bottom plate")
        moves.append((parts[0], nodes[0], nodes[1],
                      w * ln * 1e12 * areacap[model] * 1e-3))
    if not moves:
        return pex_text, {}
    if len(subs) != 1:
        raise LayoutError(
            f"the .ext names {len(subs)} substrate nets, not one - a MIM "
            "bottom plate's C to the substrate has nowhere to go")
    sub = subs[0]
    lines = pex_text.splitlines(keepends=True)
    moved, extra = {}, []
    for dev, top, bottom, ff in moves:
        net = _pex_net(top)
        if net == _pex_net(bottom) or ff <= 0:
            continue
        found = []
        for i, line in enumerate(lines):
            parts = line.split()
            if len(parts) < 4 or parts[0][:1].lower() != "c":
                continue
            a, b = (_pex_net(node) for node in parts[1:3])
            value = netlistlib.spice_number(parts[3])
            if value is not None and {a, b} == {net, sub} and net != sub:
                found.append((i, value))
        have = sum(v for _i, v in found) * 1e15
        if net != sub:
            if have < ff * (1.0 - 1e-6):
                raise LayoutError(
                    f"MIM {dev}'s top-plate net {net} has {have:.4g} fF to "
                    f"the substrate, less than the {ff:.4g} fF its plate "
                    "area gives it in magic - a shape under the MIM takes "
                    "that C, and magic files it on the top plate's net, "
                    "not the bottom plate's. Keep the area under a MIM "
                    "clear of every other layer in the layout generator")
            scale = (have - ff) / have
            for i, value in found:
                parts = lines[i].split()
                parts[3] = f"{value * scale * 1e15:.6g}f"
                lines[i] = " ".join(parts) + "\n"
        if _pex_net(bottom) != sub:
            extra.append(f"Cmimplate{len(extra)} {bottom} {sub} {ff:.6g}f\n")
        moved[dev] = {"top": net, "bottom": _pex_net(bottom),
                      "ff": round(ff, 5)}
    if extra:
        end = next((i for i in range(len(lines) - 1, -1, -1)
                    if lines[i].strip().lower().startswith(".ends")), None)
        if end is None:
            raise LayoutError("magic's netlist has no .ends to add a MIM "
                              "bottom plate's C to the substrate before")
        lines[end:end] = extra
    return "".join(lines), moved


def run_magic_extract(work_dir, gds_path, topcell: str, *, parasitics: bool,
                      timeout: float = 180.0,
                      variant: str | None = None,
                      geometry: bool = False) -> tuple[Path, str]:
    """Extract the GDS to SPICE with magic. Without parasitics this is the
    LVS netlist (`<topcell>.spice`, docs/spikes/glayout.md's "extract all;
    ext2spice"). With parasitics (`<topcell>.pex.spice`) it adds every
    coupling and substrate capacitor (cthresh 0) and splits each net that
    reaches a transistor into its real wire resistances (extresist).

    extresist keeps a net's coupling C only when the net has a label, so a
    parasitic extraction first extracts plainly and labels every net magic
    had to name itself (name_unlabelled_nets), then checks that each
    pair of top-cell nets came back with the coupling C the .ext gives it
    (lost_couplings); a pair that did not is a refusal, never an
    optimistic netlist. extresist also writes each split net's coupling C
    a second time, as C to the substrate (uncount_rnode_caps takes that
    back off), so the netlist written is magic's with those lines scaled.
    The tech also leaves resistor types out of parasitic C, trusting the
    device models, and the poly resistor models carry less than the tech's
    own poly-to-substrate C: the difference on each poly body is added
    (add_poly_body_residuals), half to each end, read from the tech's lines
    at `variant` and the model file's - so a poly resistor is not left
    lighter than the same poly wire would be.
    magic also files a MIM's bottom-plate C to the substrate on its top
    plate's net (move_mim_plate_caps puts it back on the bottom plate's).

    Order matters: `ext2spice lvs` resets cthresh and rthresh to infinity,
    so it must come before the thresholds, never after them.

    `variant` extracts at that RC variant of the tech's ngspice style (hrhc and
    its siblings), for both passes. magic only prints a complaint about a style
    its tech lacks and carries on at the nominal one, so the .ext's own `style`
    line is checked and anything else is a refusal.

    `geometry` (parasitics only) also has extresist write `<topcell>.fh`, its
    FastHenry view of every split net: each wire piece with its layer
    height, width and end points, which the em_ir gate reads current
    density off. The netlist itself is the same as without it."""
    if geometry and not parasitics:
        raise LayoutError("geometry comes from extresist: it needs parasitics")
    work_dir = Path(work_dir)
    base = work_dir / topcell
    out_path = work_dir / (f"{topcell}.pex.spice" if parasitics
                           else f"{topcell}.spice")
    ext_path = base.with_suffix(".ext")
    fresh(out_path, *(base.with_suffix(s) for s in
                      (".ext", ".res.ext", ".sim", ".nodes", ".fh")))
    tech = magic_tech(work_dir)
    lines = _magic_head(tech, gds_path, topcell)
    if variant:
        lines.append(f"extract style ngspice({variant})")
    naming_log = ""
    if parasitics:
        proc = run_eda(["magic", "-noconsole", "-dnull"], cwd=work_dir,
                       timeout=timeout, stdin_text="\n".join(
                           lines + ["extract all", "quit -noprompt"]) + "\n")
        naming_log = require_ok(proc, "magic extraction (net naming pass)")
        if not ext_path.is_file():
            raise LayoutError(
                f"magic's naming pass wrote no {ext_path.name} - the run did "
                f"not complete: {naming_log[-2000:]}")
        lines += name_unlabelled_nets(
            ext_path.read_text(encoding="utf-8", errors="replace"))
        fresh(ext_path)
        lines += ["extract do resistance", "extract all",
                  "ext2sim labels on", "ext2sim",
                  "extresist tolerance 0.01", "extresist simplify off",
                  *(["extresist geometry", "extresist fasthenry"]
                    if geometry else []),
                  "extresist all",
                  "ext2spice lvs", "ext2spice cthresh 0",
                  "ext2spice extresist on"]
    else:
        lines += ["extract all", "ext2spice lvs"]
    lines += [f"ext2spice -o {out_path.name}", "quit -noprompt"]
    proc = run_eda(["magic", "-noconsole", "-dnull"], cwd=work_dir,
                   timeout=timeout, stdin_text="\n".join(lines) + "\n")
    out = naming_log + require_ok(proc, "magic extraction")
    if not out_path.is_file():
        raise LayoutError(
            f"magic extraction produced no {out_path.name} - the run did "
            f"not complete: {out[-2000:]}")
    if parasitics:
        res_path = base.with_suffix(".res.ext")
        for need in (ext_path, res_path) + (
                (base.with_suffix(".fh"),) if geometry else ()):
            if not need.is_file():
                raise LayoutError(
                    f"magic extraction wrote no {need.name} to check "
                    f"{out_path.name}'s capacitance against: {out[-2000:]}")
        ext_text = ext_path.read_text(encoding="utf-8", errors="replace")
        if variant and not re.search(
                rf"^style\s+ngspice\({re.escape(variant)}\)\s*$", ext_text, re.M):
            raise LayoutError(
                f"magic extracted {ext_path.name} at another style than "
                f"ngspice({variant}), so a bench on it would read the wrong "
                "wires - check that the PDK's magic tech still has this "
                f"variant: {out[-2000:]}")
        pex_text, uncounted = uncount_rnode_caps(
            ext_text, res_path.read_text(encoding="utf-8", errors="replace"),
            out_path.read_text(encoding="utf-8", errors="replace"))
        if uncounted:
            out_path.write_text(pex_text, encoding="utf-8")
            out += (f"\nC to the substrate scaled back to the .ext's on "
                    f"{len(uncounted)} resist-split net(s) (fF, "
                    f"before -> after): " + ", ".join(
                        f"{net} {a:g} -> {b:g}"
                        for net, (a, b) in uncounted.items()) + "\n")
        tech_text = (tech or magic_tech_source()).read_text(
            encoding="utf-8", errors="replace")
        moved = {}
        if _has_mim(tech_text, pex_text):
            m = _EXT_STYLE_RE.search(ext_text)
            if m is None:
                raise LayoutError(
                    f"{ext_path.name} has no style line, so the area cap "
                    "magic gave its MIM plates is unknown")
            pex_text, moved = move_mim_plate_caps(
                ext_text, pex_text,
                mim_plate_areacaps(tech_text, m.group(1), m.group(2)))
        if moved:
            out_path.write_text(pex_text, encoding="utf-8")
            out += ("\nMIM bottom-plate C to the substrate moved off the top "
                    "plate's net (fF): " + ", ".join(
                        f"{d} {v['top']} -> {v['bottom']} {v['ff']:g}"
                        for d, v in moved.items()) + "\n")
        lost = lost_couplings(ext_text, pex_text)
        if lost:
            shown = ", ".join(f"{a}-{b} ({d['pex_ff']:g} of "
                              f"{d['ext_ff']:g} fF)"
                              for d in lost[:10] for a, b in [d["nets"]])
            raise LayoutError(
                f"{out_path.name} lost coupling C that magic's "
                f"{ext_path.name} gives {len(lost)} pair(s) of nets: "
                f"{shown} - extresist drops the coupling C of a net with "
                "no label, so a bench on this netlist would read an "
                "optimistic layout. Put a text label on a drawing layer "
                "(not a pin layer) on each of these nets in the layout "
                "generator")
        bodies = []
        if re.search(r"^\s*[xX]", pex_text, re.M):  # any device at all
            tech_c, model_c = poly_body_caps(variant)
            pex_text, bodies = add_poly_body_residuals(
                pex_text, tech_c, model_c)
        if bodies:
            out_path.write_text(pex_text, encoding="utf-8")
            used = sorted({b["model"] for b in bodies})
            out += (f"\nPoly resistor body C the model lacks added to "
                    f"{len(bodies)} bod(ies) ({', '.join(used)}): "
                    f"{sum(b['residual_ff'] for b in bodies):g} fF in all, "
                    f"tech {tech_c['area_af_um2']:g} aF/um^2 + "
                    f"{tech_c['perim_af_um']:g} aF/um "
                    f"({tech_c['cites']['area']}, {tech_c['cites']['perim']}, "
                    f"{tech_c['cites']['source']}) less model cj " + ", ".join(
                        f"{m} {model_c[m]['cj_af_um2']:g} aF/um^2 "
                        f"({model_c[m]['cites']['cj']}, dw "
                        f"{model_c[m]['cites']['dw']})" for m in used) + "\n")
    return out_path, out


def count_parasitics(spice_text: str) -> dict[str, int]:
    """How many wire resistors (R...) and capacitors (C...) an extracted
    netlist carries. Devices are X lines, so a netlist with neither is one
    nobody extracted parasitics into."""
    counts = {"r": 0, "c": 0}
    for line in spice_text.splitlines():
        head = line.lstrip()[:1].lower()
        if head in counts:
            counts[head] += 1
    return counts


_RC_VALUE_RE = re.compile(
    r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([A-Za-z]*)")


def scale_parasitics(spice_text: str, c: float, r: float) -> str:
    """`spice_text` with every capacitor's value times `c` and every resistor's
    times `r`, the unit suffix kept. Every R and C line count_parasitics counts
    is one `name node node value`: any other shape is refused, since leaving a
    line it cannot read at nominal would pass off a partly scaled netlist as a
    scaled one, as is a factor with no line of its kind to move. Pure."""
    factors = {"c": c, "r": r}
    moved = {"c": 0, "r": 0}
    out = []
    for line in spice_text.splitlines():
        head = line.lstrip()[:1].lower()
        if head in factors:
            tokens = line.split()
            match = (_RC_VALUE_RE.fullmatch(tokens[3])
                     if len(tokens) == 4 else None)
            if match is None:
                raise LayoutError(
                    f"cannot scale {line.strip()!r}: an extracted R or C line "
                    "must be 'name node node value'")
            moved[head] += 1
            if factors[head] != 1:
                value = float(match[1]) * factors[head]
                line = " ".join(tokens[:3] + [f"{value:.9g}{match[2]}"])
        out.append(line)
    for kind, factor in factors.items():
        if factor != 1 and not moved[kind]:
            raise LayoutError(f"cannot scale {kind.upper()} by {factor:g}: the "
                              f"netlist has no {kind.upper()} line")
    return "\n".join(out) + "\n"


def netgen_setup() -> Path:
    setup = (pdk_root() / "libs.tech" / "netgen" /
             f"{pdk_root().name}_setup.tcl")
    if not setup.is_file():
        raise LayoutError(
            f"no netgen setup at {setup} - without it netgen compares "
            "every extracted area/perimeter property as a mismatch and "
            "knows none of the PDK's device classes")
    return setup


_FINAL_RE = re.compile(r"^Final result:\s*(.*)$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"Call to undefined subcircuit (\S+)")
_SETUP_DEVICE_RE = re.compile(r"^\s*lappend\s+devices\s+(\S+)", re.MULTILINE)


def compared_devices(setup: Path) -> set[str]:
    """The device classes the PDK's netgen setup gives property rules
    (`lappend devices NAME` before each `property` loop): the resistors,
    FETs, caps, diodes and BJTs whose W/L, r_width/r_length, area... it
    compares. netgen reads each as a placeholder cell, since the extracted
    and the schematic netlist both call it as an undefined subckt, and
    still compares its properties (a ppolyf_u 150u against 100u is a
    property error)."""
    text = setup.read_text(encoding="utf-8", errors="replace")
    return {m.lower() for m in _SETUP_DEVICE_RE.findall(text)}


def setup_spelling(text: str, setup: Path) -> str:
    """text with every device model the PDK's netgen setup names respelt
    the way the setup spells it. SPICE is case-blind, but the setup finds
    a class with Tcl's case-exact lsearch, so a reference calling
    cap_mim_2f0ff never meets its "equate classes cap_mim_2f0_m4m5_noshield
    cap_mim_2f0fF" line nor its property rules, and a matching MIM cap
    reads as a device mismatch. Comment lines are left as written."""
    spelt = _SETUP_DEVICE_RE.findall(
        setup.read_text(encoding="utf-8", errors="replace"))
    if not spelt:
        return text
    word = re.compile(r"\b(" + "|".join(map(re.escape, spelt)) + r")\b",
                      re.IGNORECASE)
    canon = {name.lower(): name for name in spelt}
    return "".join(
        line if line.lstrip().startswith("*") else
        word.sub(lambda m: canon[m.group(1).lower()], line)
        for line in text.splitlines(keepends=True))


def std_cell_subckts(netlist_text: str) -> tuple[str, list[str]]:
    """(the .SUBCKT text, their names) of every PDK standard cell
    netlist_text calls but does not define, with the cells those call in
    turn. finalize() flattens a std cell into its transistors, so the
    extracted netlist has no buf_20, only its FETs. Handing netgen the
    cell's own transistor netlist lets it flatten the reference to the
    same level, and compare every std-cell device, not a black box."""
    import netlistlib

    library: dict[str, str] = {}
    for rel in netlistlib.PDK_STDCELL_FILES:
        p = pdk_root() / rel
        if not p.is_file():
            continue
        block: list[str] = []
        name = None
        for line in p.read_text(encoding="utf-8",
                                errors="replace").splitlines():
            m = netlistlib.SUBCKT_RE.match(line)
            if m:
                name, block = m.group(1).lower(), []
            if name is not None:
                block.append(line)
                if line.strip().lower().startswith(".ends"):
                    library.setdefault(name, "\n".join(block) + "\n")
                    name = None
    defined = set(netlistlib.subckt_pins(netlist_text))
    wanted = [d["model"] for d in netlistlib.parse_devices(netlist_text)]
    added: list[str] = []
    while wanted:
        cell = wanted.pop()
        if cell in defined or cell not in library:
            continue
        defined.add(cell)
        added.append(cell)
        wanted += [d["model"] for d in
                   netlistlib.parse_devices(library[cell])]
    return "".join(library[c] for c in added), added


def run_netgen_lvs(work_dir, extracted_spice, extracted_cell: str,
                   ref_spice, ref_cell: str, out_log,
                   timeout: float = 120.0,
                   setup: Path | None = None) -> tuple[bool, str]:
    """netgen LVS under the PDK's own setup (device classes, pin
    permutations, which properties to compare and which - ad/pd/as/ps, nf -
    to drop). A match is the top cell's last "Final result:" line reading
    "Circuits match uniquely." with no property error anywhere in the log.
    Refuses (LayoutError) when netgen had to black-box a cell whose
    properties it does not compare. `setup` swaps in another setup file
    (a test plants a misspelt device class in a copy); default the PDK's."""
    setup = Path(setup) if setup is not None else netgen_setup()
    # netgen writes out_log relative to its own cwd (work_dir) - resolve it
    # there regardless of the caller's own cwd.
    log_path = Path(work_dir) / out_log
    fresh(log_path)
    cmd = (f"lvs {{{extracted_spice} {extracted_cell}}} "
           f"{{{ref_spice} {ref_cell}}} {setup} {out_log}")
    proc = run_eda(["netgen", "-batch", cmd], cwd=work_dir, timeout=timeout)
    out = require_ok(proc, "netgen LVS")
    if not log_path.is_file():
        raise LayoutError(
            f"netgen LVS produced no log at {out_log} - the run did not "
            f"complete: {out[-2000:]}")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    finals = _FINAL_RE.findall(text)
    if not finals:
        raise LayoutError(
            "netgen LVS log has no 'Final result:' line - nothing was "
            f"compared: {text[-2000:]}")
    # A cell neither netlist defines is a placeholder netgen can only count
    # and wire. Only the PDK's own device classes carry property rules; any
    # other placeholder (a std cell the reference calls but never defines)
    # is compared as an empty box, so the gate refuses rather than pass it.
    blind = sorted({c.lower() for c in _PLACEHOLDER_RE.findall(out + text)}
                   - compared_devices(setup))
    if blind:
        raise LvsBlackBox(
            f"netgen compared {', '.join(blind)} as black boxes: neither "
            "netlist defines them and the PDK setup gives them no property "
            "rules, so LVS cannot see inside them. Give the reference their "
            ".subckt (std cells: layoutlib.std_cell_subckts) or flatten them "
            "out of the layout")
    matched = finals[-1].strip().startswith("Circuits match uniquely")
    failed = "property error" in text.lower() or "do not match" in text
    return (matched and not failed), text


# ------------------------------------------------------- klayout LVS (#117)

KLAYOUT_LVS_REL = "libs.tech/klayout/tech/lvs"
# extract_devices(<kind>('<name>', ...)) in the deck's rule_decks: the kind
# fixes the SPICE letter its schematic reader wants for that device
_KL_KIND_PREFIX = {"mos3": "M", "mos4": "M", "resistor": "R",
                   "resistor_with_bulk": "R", "capacitor": "C",
                   "diode": "D", "bjt3": "Q", "bjt4": "Q"}
_KL_EXTRACT_RE = re.compile(r"extract_devices\(\s*(\w+)\(\s*'(\w+)'")
# the PDK's own xschem-to-klayout converter
# (gf180_xschem_klayout_spice_convert.py) renames these per device letter
_KL_PARAM_RENAME = {"R": {"r_width": "W", "r_length": "L"},
                    "C": {"c_width": "W", "c_length": "L"},
                    "D": {"area": "A", "pj": "P"},
                    "Q": {"m": "NE"}}
# the ngspice/magic name of a MIM cap (cap_mim_2f0_m4m5_noshield: 2.0 fF/um2,
# metal4 to metal5) against the deck's one class per density (cap_mim_2f0fF):
# the deck takes the metal pair from its MIM_OPTION, the class says only fF
_KL_MIM_RE = re.compile(r"cap_mim_(\d)f(\d)_m\dm\d_noshield")
_SPICE_SCALE = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3,
                "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
_SPICE_NUM_RE = re.compile(
    r"([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[tgkmunpf])?[a-z]*")
_KL_PASS = "Congratulations! Netlists match."
_KL_FAIL = "Netlists don't match"


def klayout_lvs_dir() -> Path:
    deck = pdk_root() / KLAYOUT_LVS_REL
    if not (deck / "run_lvs.py").is_file() or not (deck / "gf180mcu.lvs").is_file():
        raise LayoutError(f"no klayout GF180 LVS deck (run_lvs.py, "
                          f"gf180mcu.lvs) under {deck}")
    return deck


def klayout_device_classes(deck: Path) -> dict[str, tuple[str, str]]:
    """{model lowercased: (SPICE letter, the deck's own spelling)} for every
    device the deck extracts."""
    classes: dict[str, tuple[str, str]] = {}
    for f in sorted((deck / "rule_decks").glob("*_extraction.lvs")):
        for kind, name in _KL_EXTRACT_RE.findall(
                f.read_text(encoding="utf-8", errors="replace")):
            if kind in _KL_KIND_PREFIX:
                classes[name.lower()] = (_KL_KIND_PREFIX[kind], name)
    if not classes:
        raise LayoutError(f"{deck}/rule_decks names no extract_devices "
                          "classes - the deck's layout changed")
    return classes


def _spice_value(expr: str, env: dict[str, float]) -> float:
    """A SPICE parameter value as a number: a literal with its scale suffix
    (2u, 1meg, 0.5), or +-*/** arithmetic over literals and names in env.
    Anything else is a LayoutError, never a guess."""
    import ast
    import operator as op

    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
           ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg,
           ast.UAdd: op.pos}
    text = expr.strip().strip("{}'\"")
    m = _SPICE_NUM_RE.fullmatch(text.lower())
    if m:
        return float(m.group(1)) * _SPICE_SCALE.get(m.group(2) or "", 1.0)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id.lower() in env:
            return env[node.id.lower()]
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        raise ValueError(text)

    try:
        return ev(ast.parse(text.lower(), mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError) as exc:
        raise LayoutError(
            f"cannot resolve the parameter value {expr!r} to a number for "
            "klayout LVS (a literal, or arithmetic over .param and subckt "
            "defaults)") from exc


_PARAM_RE = re.compile(r"(\w+)\s*=\s*(\S+)")


def klayout_reference(text: str, deck: Path) -> str:
    """The sized LVS reference (log/lvs/) as the klayout deck's schematic
    reader takes it. The reader drops X-prefixed device calls (so a
    reference written for netgen reads as an empty circuit), fails on a
    parameter given by name, and on a .subckt header with defaults; the
    PDK's own converter only handles an upper-case X and matches models by
    substring. Here: every .param and subckt default is resolved to a
    number, the header defaults and .param lines go, and each X call of a
    device the deck extracts becomes that device's letter (M/R/C/D/Q), its
    model spelt as the deck spells it (a MIM cap's ngspice name,
    cap_mim_2f0_m4m5_noshield, as the deck's class of its density,
    cap_mim_2f0fF), its parameters renamed as the PDK's
    converter renames them. Std-cell calls stay X: the deck flattens
    them."""
    classes = klayout_device_classes(deck)
    lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("+") and lines:
            lines[-1] += " " + line.lstrip()[1:]
        else:
            lines.append(line)
    env: dict[str, float] = {}
    local = env
    out: list[str] = []
    for line in lines:
        tok = line.split()
        if not tok or tok[0].startswith("*"):
            out.append(line)
            continue
        head = tok[0].lower()
        if head == ".param":
            for k, v in _PARAM_RE.findall(line[len(tok[0]):]):
                env[k.lower()] = _spice_value(v, env)
            continue
        if head == ".subckt":
            local = dict(env)
            for k, v in _PARAM_RE.findall(line):
                local[k.lower()] = _spice_value(v, local)
            out.append(" ".join(w for w in tok if "=" not in w))
            continue
        if head == ".ends":
            local = env
            out.append(line)
            continue
        if head[0] in "xmrcdq":
            pos = [i for i, w in enumerate(tok) if "=" not in w]
            letter = None
            model = tok[pos[-1]].lower()
            mim = _KL_MIM_RE.fullmatch(model)
            if mim:
                model = f"cap_mim_{mim.group(1)}f{mim.group(2)}ff"
            if head[0] == "x" and model in classes:
                letter, canon = classes[model]
                tok[pos[-1]] = canon
                tok[0] = letter + tok[0][1:]
            for i, w in enumerate(tok):
                if "=" in w:
                    k, v = w.split("=", 1)
                    k = _KL_PARAM_RENAME.get(letter or "", {}).get(k.lower(), k)
                    tok[i] = f"{k}={_spice_value(v, local):.12g}"
            out.append(" ".join(tok))
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def run_klayout_lvs(work_dir, gds_path, topcell: str, ref_spice,
                    timeout: float = 240.0) -> tuple[bool, str, Path]:
    """The PDK's klayout LVS deck (run_lvs.py, variant D, gf180mcu_gnd
    substrate) on gds_path against ref_spice, already in klayout_reference()
    form. Returns (matched, the run's log, the .lvsdb). A run with neither
    verdict line, or no .lvsdb, is a LayoutError. --combine merges parallel
    devices first, as netgen does: a device drawn as a row of equal units
    (a matched row, #118) is one schematic device of their summed W."""
    deck = klayout_lvs_dir()
    work_dir = Path(work_dir).resolve()
    if work_dir.exists():
        for p in work_dir.iterdir():
            if p.is_file():
                p.unlink()
    work_dir.mkdir(parents=True, exist_ok=True)
    gds = Path(gds_path).resolve()
    proc = run_eda(
        ["python3", str(deck / "run_lvs.py"), f"--layout={gds}",
         f"--netlist={Path(ref_spice).resolve()}", "--variant=D",
         f"--run_dir={work_dir}", f"--topcell={topcell}", "--run_mode=deep",
         "--combine"],
        cwd=work_dir, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    logs = sorted(work_dir.glob("*.log"))
    log = out + "".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in logs)
    lvsdb = work_dir / f"{gds.name.split('.')[0]}.lvsdb"
    if _KL_PASS in log:
        matched = True
    elif _KL_FAIL in log:
        matched = False
    else:
        raise LayoutError(
            f"klayout LVS gave no verdict (exit {proc.returncode}) - the "
            f"run did not complete: {log[-2000:]}")
    if not lvsdb.is_file():
        raise LayoutError(f"klayout LVS wrote no report at {lvsdb}")
    return matched, log, lvsdb


# drawn layers a net's extracted shapes are reported on, most telling
# first: the metals and cuts an open or a short is drawn in, then poly and
# diffusion
_KL_LOCATE_LAYERS = [*reversed(_NET_STACK[2:]), _NET_STACK[1], _NET_STACK[0],
                     ("comp", (22, 0))]


def _um_box(box, dbu: float) -> list[float]:
    return [round(box.left * dbu, 3), round(box.bottom * dbu, 3),
            round(box.right * dbu, 3), round(box.top * dbu, 3)]


def klayout_lvs_findings(lvsdb, gds_path, topcell: str,
                         max_shapes: int = 40) -> list[dict]:
    """Every pin, net and device the klayout cross-reference did not match,
    each with where it is in the layout: a net carries its drawn shapes
    as {layer, bbox (um)}, metals and cuts first, a device its class, both
    sides' parameters and its position. An open shows as the split net's
    pieces, a short as the joined net's shapes - the shorting shape among
    them. The lvsdb names its layers only by index, so each of its layers
    is put on the drawn GDS layer that wholly contains it."""
    import klayout.db as kdb

    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(lvsdb))
    xref = lvs.xref()
    if xref is None:
        raise LayoutError(f"{lvsdb} holds no cross-reference")
    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise LayoutError(f"{gds_path} has no cell named {topcell!r}")
    dbu = layout.dbu
    drawn = [(name, kdb.Region(cell.begin_shapes_rec(layout.layer(*ld))))
             for name, ld in _KL_LOCATE_LAYERS]
    on_layer: dict[str, list] = {}  # drawn layer name: the lvsdb layers on it
    for i in lvs.layer_indexes():
        region = lvs.layer_by_index(i)
        if region.is_empty():
            continue
        for name, shapes in drawn:
            if (region - shapes).is_empty():
                on_layer.setdefault(name, []).append(region)
                break
    ok = {kdb.NetlistCrossReference.Match,
          kdb.NetlistCrossReference.MatchWithWarning,
          kdb.NetlistCrossReference.Skipped}

    def where(net) -> list[dict]:
        if net is None:
            return []
        shapes = []
        for name, _shapes in drawn:
            mine = kdb.Region()
            for region in on_layer.get(name, []):
                mine += lvs.shapes_of_net(net, region, True)
            for poly in mine.merged().each():
                shapes.append({"layer": name,
                               "bbox": _um_box(poly.bbox(), dbu)})
        return shapes[:max_shapes]

    def name(obj):
        return None if obj is None else (obj.name or obj.expanded_name())

    findings: list[dict] = []
    for cp in xref.each_circuit_pair():
        if cp.status() in ok:
            continue
        circuit = name(cp.first()) or name(cp.second())
        for np_ in xref.each_net_pair(cp):
            if np_.status() in ok:
                continue
            findings.append({
                "what": "net", "circuit": circuit,
                "layout_net": name(np_.first()),
                "reference_net": name(np_.second()),
                "status": str(np_.status()).split(".")[-1],
                "shapes": where(np_.first())})
        for dp in xref.each_device_pair(cp):
            if dp.status() in ok:
                continue
            lay, ref = dp.first(), dp.second()
            entry = {"what": "device", "circuit": circuit,
                     "layout_device": name(lay), "reference_device": name(ref),
                     "status": str(dp.status()).split(".")[-1]}
            for side, dev in (("layout", lay), ("reference", ref)):
                if dev is not None:
                    entry[f"{side}_class"] = dev.device_class().name
                    entry[f"{side}_params"] = {
                        p.name: round(dev.parameter(p.id()), 6)
                        for p in dev.device_class().parameter_definitions()
                        if p.is_primary}
            # a paired device of the same class and sizes only mismatches
            # through its terminals' nets, which are reported themselves
            if (lay is not None and ref is not None
                    and entry["layout_class"] == entry["reference_class"]
                    and entry["layout_params"] == entry["reference_params"]):
                continue
            if lay is not None:
                t = lay.trans.disp
                entry["position"] = [round(t.x, 3), round(t.y, 3)]
            findings.append(entry)
        for pp in xref.each_pin_pair(cp):
            if pp.status() in ok:
                continue
            findings.append({
                "what": "pin", "circuit": circuit,
                "layout_pin": name(pp.first()),
                "reference_pin": name(pp.second()),
                "status": str(pp.status()).split(".")[-1]})
    return findings


# ---------------------------------------------------------------- ngspice

_MEASURE_RE = re.compile(
    r"^\s*([A-Za-z_@][\w.\[\]()@]*)\s*=\s*([-+0-9.eE]+)\s*$")


# A .control `meas` whose trigger or target never happened. This box's
# ngspice prints both lines, and the first one trips simlib's "^error"
# engine pattern:
#   Error: measure  tdelay  trig(TARG) : out of interval
#    meas tran tdelay trig v(clk) val=1.4 rise=1 targ v(outn) ... failed!
_MEAS_FAILED_RE = re.compile(
    r"^\s*\.?meas(?:ure)?\s+\S+\s+(\S+)\s.*failed!\s*$", re.I)
_MEAS_ERROR_RE = re.compile(r"^\s*error:\s*measure\s+(\S+)\s", re.I)


def split_failed_measures(out: str) -> tuple[str, set[str]]:
    """(out without its failed-measure lines, the failed measures' names,
    lowercased). An "Error: measure NAME" line goes only when NAME also has
    its own "... failed!" line, so any other error text stays in."""
    lines = out.splitlines()
    failed = {m.group(1).lower() for ln in lines
              for m in [_MEAS_FAILED_RE.match(ln)] if m}

    def is_failure(ln: str) -> bool:
        m = _MEAS_FAILED_RE.match(ln) or _MEAS_ERROR_RE.match(ln)
        return bool(m) and m.group(1).lower() in failed

    return "\n".join(ln for ln in lines if not is_failure(ln)), failed


def run_ngspice(cir_path, cwd=None, timeout: float = 120.0,
                failed_measures: set[str] | None = None) -> str:
    """ngspice -b on a bench. A non-zero exit is a refusal, and so is a
    zero exit whose text shows an engine failure (simlib's own list: a
    singular matrix, failed stepping, an unknown subckt ...), because batch
    ngspice exits 0 after some of those (engine/lib/simlib.py).

    A `meas` whose trigger or target never happened is the circuit's
    behaviour, not the engine's: pass a set as `failed_measures` and those
    names are added to it instead of refusing, and the caller scores them
    as findings (sim_run does the same through simlib.compare_bounds). Any
    other error text still refuses. Without the set, it refuses as before."""
    import simlib

    proc = run_eda(["ngspice", "-b", str(cir_path)], cwd=cwd, timeout=timeout)
    out = require_ok(proc, "ngspice")
    checked = out
    if failed_measures is not None:
        checked, failed = split_failed_measures(out)
        failed_measures.update(failed)
    kinds = simlib.detect_engine_errors(checked)
    if kinds:
        raise LayoutError(
            f"ngspice reported {', '.join(kinds)}: {out[-2000:]}")
    return out


def parse_ngspice_prints(output: str) -> dict[str, float]:
    """Parse simple `NAME = VALUE` lines ngspice's `.control ... print ...`
    (or `meas`) emits into a name->float map. The last occurrence of a
    name wins (a re-print after a further .control step is the final
    value)."""
    values: dict[str, float] = {}
    for line in output.splitlines():
        m = _MEASURE_RE.match(line)
        if m:
            try:
                values[m.group(1).lower()] = float(m.group(2))
            except ValueError:
                continue
    return values

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


def magic_tech(work_dir) -> Path | None:
    """A copy of the PDK's magic tech with the rm1 input typo fixed, written
    into work_dir, or None when the PDK already maps 110/11 to MET1RES.
    Refuses a tech file that has neither line, rather than guess."""
    src = pdk_root() / "libs.tech" / "magic" / f"{pdk_root().name}.tech"
    if not src.is_file():
        raise LayoutError(f"no magic tech file at {src}")
    text = src.read_text(encoding="utf-8", errors="replace")
    if _RM1_FIX in text:
        return None
    if text.count(_RM1_TYPO) != 1:
        raise LayoutError(
            f"{src} maps GDS 110/11 neither to MET1RES nor through the one "
            "known MET2RES typo - check how this PDK reads rm1 before "
            "trusting an extraction")
    out = Path(work_dir) / src.name
    out.write_text(text.replace(_RM1_TYPO, _RM1_FIX), encoding="utf-8")
    return out


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
                   timeout: float = 120.0) -> tuple[bool, str]:
    """netgen LVS under the PDK's own setup (device classes, pin
    permutations, which properties to compare and which - ad/pd/as/ps, nf -
    to drop). A match is the top cell's last "Final result:" line reading
    "Circuits match uniquely." with no property error anywhere in the log.
    Refuses (LayoutError) when netgen had to black-box a cell whose
    properties it does not compare."""
    setup = netgen_setup()
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
        raise LayoutError(
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
    model spelt as the deck spells it, its parameters renamed as the PDK's
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
    verdict line, or no .lvsdb, is a LayoutError."""
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
         f"--run_dir={work_dir}", f"--topcell={topcell}", "--run_mode=deep"],
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
    r"^\s*([A-Za-z_][\w.\[\]()]*)\s*=\s*([-+0-9.eE]+)\s*$")


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

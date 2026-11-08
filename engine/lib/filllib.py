"""The shuttle's metal fill on a scratch copy of a tile, and the extraction
view of it (check_pex_sim's `pex_fill`, issue 136).

The shuttle fills a tile after precheck with the PDK's own
libs.tech/klayout/tech/scripts/fill_all.rb: LibreLane's gf180 config sets
KLAYOUT_FILLER_SCRIPT to it, and Tiny Tapeout's precheck leaves density off
because the projects "rely on the metal fill that is added after precheck".
So the fill here is that script, run unchanged on a copy of the block's
GDS:

- fill_all.rb fills its top cell's bounding box less a 26 um ring it keeps
  for a die's scribe line. A block is not a die, so the copy gets a frame
  shape on FRAME_LAYER, SCRIBE_UM + HALO_UM outside the drawn extent: the
  scribe ring falls outside the tile and the fill runs up to and HALO_UM
  past its edge, as the shuttle's fill of the area around it would. The
  frame is taken out again after the fill.
- the fill goes on the dummy datatypes (COMP 22/4, Poly2 30/4, Metal1-5
  34/4 ... 81/4). magic's tech reads them as fillm1..fillm5 and fillpoly,
  which its extract section never names, so magic extracts no C for them.
  extraction_copy() moves the poly and metal fill onto the drawing layers,
  each island then a net of its own (magic names it, and layoutlib's naming
  pass labels it). COMP fill stays filldiff: it lies under the metal and
  couples nothing magic would model.
- fill_nets() finds those islands in the extracted netlist: a net with C
  lines and nothing else (no device terminal, no R). float_fill() folds
  them out into the C they leave between the real nets, and ground_fill()
  renames each to the ground net: fill floating and fill grounded, the
  two cases a fill-aware extraction runs."""
from __future__ import annotations

import re
from pathlib import Path

import layoutlib
import netlistlib

FILL_SCRIPT_REL = "libs.tech/klayout/tech/scripts/fill_all.rb"
SCRIBE_UM = 26.0   # fill_all.rb's scribe_line_ring, inside its frame
HALO_UM = 10.0     # the neighbour fill drawn past the tile's edge
FRAME_LAYER = (255, 255)  # no gf180mcu layer, deck or fill script uses it
MODES = ("floating", "grounded")
# the post-fill DRC: every deck the block's own DRC runs, plus the dummy-fill
# decks (DCF/DPF/DMF), which check the fill now there. Antenna is
# analog_drc's own run, and fill adds no gate connection to it. The density
# deck runs on its own (DENSITY_DECKS): its rules read the whole die
FILL_DRC_DECKS = "all,-antenna,-density"
DENSITY_DECKS = "density"
# the pin grounded fill is tied to, in this order of preference
GROUND_PINS = ("vss", "gnd", "avss", "vssa", "agnd", "dvss", "vgnd")
FILL_TIMEOUT_S = 600.0
# (drawing layer, its fill's datatype-4 twin), bottom up
FILL_LAYERS = {
    "comp": ((22, 0), (22, 4)),
    "poly2": ((30, 0), (30, 4)),
    "metal1": ((34, 0), (34, 4)),
    "metal2": ((36, 0), (36, 4)),
    "metal3": ((42, 0), (42, 4)),
    "metal4": ((46, 0), (46, 4)),
    "metal5": ((81, 0), (81, 4)),
}
# fill magic leaves out of the extraction (see the module docstring)
NOT_EXTRACTED = ("comp",)

# fill_all.rb's metal fill reads `-rd threads=N` as a string and hands it to
# TilingProcessor#threads=, which wants an Integer, so the script dies on
# any explicit thread count; this wrapper makes it one and loads the PDK's
# script unchanged
_WRAPPER = '$threads = ($threads || "1").to_i\nload $fill_script\n'


def modes_of(field) -> list[str]:
    """spec.yaml's `pex_fill` as the fill modes it asks for, in MODES'
    order: absent is none, "all" is both. ValueError on anything else."""
    if field is None:
        return []
    if field == "all":
        return list(MODES)
    if (isinstance(field, list) and field and len(set(field)) == len(field)
            and all(isinstance(m, str) and m in MODES for m in field)):
        return [m for m in MODES if m in field]
    raise ValueError(
        "spec.yaml 'pex_fill' must be \"all\" or a non-empty list of "
        f"distinct fill modes from {list(MODES)}; got {field!r}")


def ground_net(pins: list[str]) -> str:
    """The net grounded fill goes to: the first ground-named pin of the
    cell, else global ground 0."""
    low = {p.lower(): p for p in pins}
    return next((low[g] for g in GROUND_PINS if g in low), "0")


def _layout(gds):
    import klayout.db as db
    ly = db.Layout()
    ly.read(str(gds))
    return db, ly


def _top(ly, topcell: str):
    cell = ly.cell(topcell)
    if cell is None:
        raise layoutlib.LayoutError(f"the GDS has no cell named {topcell!r}")
    return cell


def _write(ly, path: Path) -> None:
    import klayout.db as db
    opts = db.SaveLayoutOptions()
    opts.write_context_info = False
    ly.write(str(path), opts)


def shuttle_fill(gds, topcell: str, work_dir: Path,
                 timeout: float = FILL_TIMEOUT_S) -> Path:
    """The PDK's fill_all.rb run on a framed copy of `gds` (module
    docstring), the frame taken back out: `<work_dir>/<topcell>.filled.gds`.
    The drawn shapes are the block's own; only fill is added."""
    script = layoutlib.pdk_root() / FILL_SCRIPT_REL
    if not script.is_file():
        raise layoutlib.LayoutError(f"no PDK fill script at {script}")
    work_dir = Path(work_dir)
    framed = work_dir / f"{topcell}.framed.gds"
    raw = work_dir / f"{topcell}.filled.raw.gds"
    out = work_dir / f"{topcell}.filled.gds"
    wrapper = work_dir / "fill_wrapper.rb"
    layoutlib.fresh(framed, raw, out)

    db, ly = _layout(gds)
    if len(list(ly.each_top_cell())) != 1:
        raise layoutlib.LayoutError(
            "fill_all.rb fills the layout's one top cell; this GDS has "
            f"{len(list(ly.each_top_cell()))}")
    top = _top(ly, topcell)
    if top.bbox().empty():
        raise layoutlib.LayoutError(f"{topcell} is empty: nothing to fill")
    grow = SCRIBE_UM + HALO_UM
    top.shapes(ly.layer(*FRAME_LAYER)).insert(top.dbbox().enlarged(grow, grow))
    _write(ly, framed)

    wrapper.write_text(_WRAPPER, encoding="utf-8")
    proc = layoutlib.run_eda(
        ["klayout", "-b", "-r", str(wrapper), "-rd", f"fill_script={script}",
         "-rd", f"input={framed}", "-rd", f"output={raw}",
         "-rd", "threads=1"], cwd=work_dir, timeout=timeout)
    log = layoutlib.require_ok(proc, "the PDK's fill_all.rb")
    if "Done!" not in log or not raw.is_file():
        raise layoutlib.LayoutError(
            f"fill_all.rb did not finish (no 'Done!' or no {raw.name}): "
            f"{log[-2000:]}")

    db, ly = _layout(raw)
    idx = ly.find_layer(*FRAME_LAYER)
    if idx is not None:
        ly.delete_layer(idx)
    _write(ly, out)
    raw.unlink()
    framed.unlink()
    return out


def fill_summary(filled_gds, drawn_gds, topcell: str) -> dict:
    """{layer: {fill_shapes, fill_um2, density_drawn, density_filled}} over
    the drawn tile's own extent (the fill past its edge is not counted): the
    per-tile density the deck's M1.4 / M2.4 / ... read over a whole die."""
    db, drawn_ly = _layout(drawn_gds)
    tile = _top(drawn_ly, topcell).bbox()
    area = tile.area() * drawn_ly.dbu ** 2
    db, ly = _layout(filled_gds)
    top = _top(ly, topcell)
    clip = db.Region(db.Box(tile))
    out = {}
    for name, (draw_spec, fill_spec) in FILL_LAYERS.items():
        def region(spec):
            i = ly.find_layer(*spec)
            return (db.Region(top.begin_shapes_rec(i)).merged()
                    if i is not None else db.Region())
        drawn, fill = region(draw_spec), region(fill_spec)
        both = (drawn + fill).merged() & clip
        out[name] = {
            "fill_shapes": fill.count(),
            "fill_um2": round(fill.area() * ly.dbu ** 2, 3),
            "density_drawn": round((drawn & clip).area() * ly.dbu ** 2 / area, 4),
            "density_filled": round(both.area() * ly.dbu ** 2 / area, 4),
        }
    return out


def extraction_copy(filled_gds, topcell: str, out: Path) -> int:
    """`out`: the filled GDS, flat, its poly and metal fill moved onto the
    drawing layers so magic extracts it as conductors. Returns how many
    fill islands moved."""
    db, ly = _layout(filled_gds)
    top = _top(ly, topcell)
    top.flatten(True)
    moved = 0
    for name, (draw_spec, fill_spec) in FILL_LAYERS.items():
        if name in NOT_EXTRACTED:
            continue
        i = ly.find_layer(*fill_spec)
        if i is None:
            continue
        fill = db.Region(top.shapes(i)).merged()
        top.shapes(i).clear()
        top.shapes(ly.layer(*draw_spec)).insert(fill)
        moved += fill.count()
    layoutlib.fresh(out)
    _write(ly, out)
    return moved


def fill_nets(pex_text: str, subckt: str) -> set[str]:
    """The nets of `subckt` (lowercased) that only capacitors touch: no
    device terminal, no resistor, no pin. Drawn metal always reaches a
    device or a pin, so these are the fill islands extraction_copy made."""
    pins = set()
    c_only: set[str] = set()
    other: set[str] = set()
    inside = False
    for line in netlistlib.join_continuations(pex_text):
        parts = netlistlib.strip_comment(line).split()
        if not parts:
            continue
        head = parts[0].lower()
        if head == ".subckt" and len(parts) > 1:
            inside = parts[1].lower() == subckt.lower()
            if inside:
                pins = {p.lower() for p in parts[2:] if "=" not in p}
            continue
        if head == ".ends":
            inside = False
            continue
        if not inside or head.startswith("."):
            continue
        nodes = [t.lower() for t in parts[1:] if "=" not in t]
        if head[0] == "c":
            c_only.update(nodes[:2])
        elif head[0] == "r":
            other.update(nodes[:2])
        elif head[0] in "xmdq":
            other.update(nodes[:-1] if head[0] == "x" else nodes[:4])
    return {n for n in c_only - other - pins if n != "0"}


_ENDS_RE = re.compile(r"^\.ends\b", re.I | re.M)


def _in_subckt(pex_text: str, subckt: str) -> tuple[int, int]:
    m = re.search(rf"^\.subckt\s+{re.escape(subckt)}\b", pex_text, re.I | re.M)
    if m is None:
        raise layoutlib.LayoutError(f"no .subckt {subckt} in the netlist")
    end = _ENDS_RE.search(pex_text, m.end())
    if end is None:
        raise layoutlib.LayoutError(f".subckt {subckt} has no .ends")
    return m.start(), end.start()


def float_fill(pex_text: str, subckt: str, nets: set[str]) -> str:
    """Fill floating, with the fill nets folded out: a floating island
    holds no charge, so its voltage is the capacitive divider of its
    neighbours, at DC and at every frequency. Eliminating those nodes
    (a Kron reduction of the capacitance graph) is exact and leaves C
    between the real nets alone, so ngspice solves the 200-odd nodes of
    the drawn circuit rather than thousands of islands - which, left in,
    took its AC sweep from a second to past ten minutes. A capacitor
    with a fill net on it is replaced by the reduced C between the
    real nets the fill couples (Cfill<i>); an island group that reaches
    no real net couples nothing and drops out."""
    import numpy as np

    start, end = _in_subckt(pex_text, subckt)
    keep_lines: list[str] = []
    edges: list[tuple[str, str, float]] = []
    for line in pex_text[start:end].splitlines(keepends=True):
        parts = line.split()
        if (parts and parts[0][:1].lower() == "c" and len(parts) >= 4
                and (parts[1].lower() in nets or parts[2].lower() in nets)):
            value = netlistlib.spice_number(parts[3])
            if value is None:
                raise layoutlib.LayoutError(
                    f"fill capacitor {parts[0]}: value {parts[3]!r} is not "
                    "a number")
            a, b = parts[1].lower(), parts[2].lower()
            if a != b and value != 0.0:
                edges.append((parts[1], parts[2], value))
            continue
        keep_lines.append(line)
    # the fill nets in groups joined by fill-to-fill C, each reduced alone
    parent = {n: n for n in nets}

    def find(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b, _v in edges:
        if a.lower() in nets and b.lower() in nets:
            parent[find(a.lower())] = find(b.lower())
    groups: dict[str, list[tuple[str, str, float]]] = {}
    for edge in edges:
        fill_end = edge[0] if edge[0].lower() in nets else edge[1]
        groups.setdefault(find(fill_end.lower()), []).append(edge)
    reduced: dict[tuple[str, str], float] = {}
    for group in groups.values():
        names: dict[str, int] = {}
        real: list[str] = []
        for a, b, _v in group:
            for n in (a, b):
                if n.lower() not in names:
                    names[n.lower()] = len(names)
                    if n.lower() not in nets:
                        real.append(n)
        if not real:
            continue
        lap = np.zeros((len(names), len(names)))
        for a, b, v in group:
            i, j = names[a.lower()], names[b.lower()]
            lap[i, i] += v
            lap[j, j] += v
            lap[i, j] -= v
            lap[j, i] -= v
        r_ix = [names[n.lower()] for n in real]
        f_ix = [i for n, i in names.items() if n not in {r.lower() for r in real}]
        l_rf = lap[np.ix_(r_ix, f_ix)]
        schur = (lap[np.ix_(r_ix, r_ix)]
                 - l_rf @ np.linalg.solve(lap[np.ix_(f_ix, f_ix)], l_rf.T))
        for x in range(len(real)):
            for y in range(x + 1, len(real)):
                c = -schur[x, y]
                if c > 0.0:
                    key = (real[x], real[y])
                    reduced[key] = reduced.get(key, 0.0) + c
    adds = "".join(f"Cfill{i} {a} {b} {c:.6g}\n"
                   for i, ((a, b), c) in enumerate(sorted(reduced.items())))
    return pex_text[:start] + "".join(keep_lines) + adds + pex_text[end:]


def ground_fill(pex_text: str, subckt: str, nets: set[str],
                ground: str) -> str:
    """Fill grounded: each fill net is the ground net `ground` (a pin of
    `subckt`), so its C to a signal is C to ground. A capacitor between two
    fill nets, or a fill net and ground, then has both ends on ground and
    is dropped."""
    start, end = _in_subckt(pex_text, subckt)
    out = []
    for line in pex_text[start:end].splitlines(keepends=True):
        parts = line.split()
        if parts and parts[0][:1].lower() in "cr" and len(parts) >= 4:
            a, b = (ground if parts[1].lower() in nets else parts[1],
                    ground if parts[2].lower() in nets else parts[2])
            if (a, b) != (parts[1], parts[2]):
                if a.lower() == b.lower():
                    continue
                line = " ".join([parts[0], a, b, *parts[3:]]) + "\n"
        out.append(line)
    return pex_text[:start] + "".join(out) + pex_text[end:]

"""kpexlib.py - klayout_pex (kpex) 2.5D as pex_sim's second extractor
(docs/design.md 1.5, "### M9."; issue #116).

pex_sim benches magic's extraction. This module extracts the same GDS with
kpex's 2.5D engine and compares the two on capacitance: each labelled net's
total C and the coupling between each pair of nets the bench probes. A net
past TOLERANCE is a `pex_extractors_disagree` finding. kpex's netlist is
never benched (#60): the wire R and the bench stay magic's.

What kpex is handed, and why:
  - prepare_gds: a flat copy of the GDS with every drawing-layer text copied
    onto its layer's label purpose (/10), where kpex reads net names, and
    with each metal resistor's body cut. kpex has no metal-resistor device:
    it reads a metal resistor (110/11-15 over the metal) as a plain wire,
    which shorts its two ends into one net. A 1 um cut in the middle of the
    body, across its width, splits it the way magic does; the body keeps its
    area and fringe C less the gap's. Any other resistor marking (res_mk
    110/5 off a metal resistor, the poly and diffusion resistor layers
    62/0 and 110/16) is a resistor kpex reads otherwise than magic, so it is
    a refusal (KpexError), not a comparison of two different circuits.
  - kpex_runner.py: kpex with gate poly as a conductor (its docstring).
Net names: kpex calls the substrate VSUBS; it is renamed to the name
magic's .ext gives the substrate net.

Not compared, each listed with its reason: a net with N-well under it, and
the substrate net when the block has any well. kpex models no well and no
junction - metal over an N-well couples to VSUBS in kpex and to the well's
net in magic - so on those nets the two extractors measure different
things. A net the reference has that neither extraction names is not
compared either (nothing to compare)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import layoutlib

TOLERANCE = 0.20  # |kpex - magic| / min(kpex, magic), on total C and coupling
# a coupling below this in both extractions (fF) is not compared: two 2.5D
# models of a far-off pair differ by more than 20% of almost nothing
COUPLING_FLOOR_FF = 0.05
RES_GAP_UM = 1.0
RUNNER = Path(__file__).resolve().with_name("kpex_runner.py")
KPEX_TIMEOUT = 300.0

# GF180: metal resistor markers 110/11..15 and the metal each one marks
METAL_RES = {11: (34, 0), 12: (36, 0), 13: (42, 0), 14: (46, 0), 15: (81, 0)}
RES_MK = (110, 5)
OTHER_RES = {(62, 0): "the resistor layer 62/0",
             (110, 16): "the diffusion/poly resistor marker 110/16"}
# drawing layers whose texts name a net: comp, poly2 and the five metals
TEXT_LAYERS = (22, 30, 34, 36, 42, 46, 81)
LABEL_PURPOSE = 10


class KpexError(RuntimeError):
    """kpex could not be run on this layout, or ran and wrote nothing: the
    cross-check did not run, so pex_sim refuses."""


def prepare_gds(src, dst, topcell: str, gap_um: float = RES_GAP_UM) -> dict:
    """Write kpex's copy of the GDS at `dst` (module docstring). Returns
    {"res_cuts": n}."""
    import klayout.db as kdb

    ly = kdb.Layout()
    ly.read(str(src))
    top = ly.cell(topcell)
    if top is None:
        raise KpexError(f"{Path(src).name} has no cell {topcell!r}")
    top.flatten(True)

    def region(layer):
        li = ly.find_layer(*layer)
        return (kdb.Region(top.shapes(li)).merged() if li is not None
                else kdb.Region())

    for layer, what in OTHER_RES.items():
        if not region(layer).is_empty():
            raise KpexError(f"the layout has {what}, a resistor kpex does not "
                            "extract as magic does")
    markers = kdb.Region()
    for dt in METAL_RES:
        markers += region((110, dt))
    stray = region(RES_MK) - markers
    if not stray.is_empty():
        b = stray.bbox()
        raise KpexError(
            "res_mk 110/5 marks a resistor that is not a metal resistor at "
            f"({b.left * ly.dbu:g}, {b.bottom * ly.dbu:g}): kpex does not "
            "extract that resistor as magic does")

    for layer in TEXT_LAYERS:
        li = ly.find_layer(layer, 0)
        if li is None:
            continue
        texts = [s.text for s in top.shapes(li).each() if s.is_text()]
        tgt = ly.layer(layer, LABEL_PURPOSE)
        for t in texts:
            top.shapes(tgt).insert(t)

    gap = int(round(gap_um / ly.dbu))
    cuts_made = 0
    for dt, metal_layer in METAL_RES.items():
        mk = ly.find_layer(110, dt)
        if mk is None:
            continue
        marker = kdb.Region(top.shapes(mk)).merged()
        if marker.is_empty():
            continue
        mi = ly.find_layer(*metal_layer)
        metal = (kdb.Region(top.shapes(mi)).merged() if mi is not None
                 else kdb.Region())
        cuts = kdb.Region()
        for poly in marker.each():
            b = poly.bbox()
            # the body runs along the axis whose two ends the metal leaves by
            left = metal.overlapping(kdb.Region(
                kdb.Box(b.left - 2, b.bottom + 1, b.left, b.top - 1)))
            right = metal.overlapping(kdb.Region(
                kdb.Box(b.right, b.bottom + 1, b.right + 2, b.top - 1)))
            if not left.is_empty() and not right.is_empty():
                if b.width() <= gap:
                    raise KpexError(f"a metal resistor 110/{dt} is shorter "
                                    f"than the {gap_um:g} um cut")
                cx = b.center().x
                cuts.insert(kdb.Box(cx - gap // 2, b.bottom, cx + gap - gap // 2,
                                    b.top))
            else:
                if b.height() <= gap:
                    raise KpexError(f"a metal resistor 110/{dt} is shorter "
                                    f"than the {gap_um:g} um cut")
                cy = b.center().y
                cuts.insert(kdb.Box(b.left, cy - gap // 2, b.right,
                                    cy + gap - gap // 2))
            cuts_made += 1
        texts = [s.text for s in top.shapes(mi).each() if s.is_text()]
        top.shapes(mi).clear()
        top.shapes(mi).insert(metal - cuts)
        for t in texts:
            top.shapes(mi).insert(t)
        top.shapes(mk).clear()
    rm = ly.find_layer(*RES_MK)
    if rm is not None:
        top.shapes(rm).clear()
    ly.write(str(dst))
    return {"res_cuts": cuts_made}


def run_kpex(work_dir, gds_path, topcell: str,
             timeout: float = KPEX_TIMEOUT) -> tuple[Path, Path]:
    """kpex 2.5D on the prepared copy of gds_path, under work_dir. Returns
    (its netlist, its log). Anything short of a netlist is a KpexError."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    gds = work_dir / f"{topcell}.kpex.gds"
    layoutlib.fresh(gds)
    prepare_gds(gds_path, gds, topcell)
    out_dir = work_dir / "out"
    shutil.rmtree(out_dir, ignore_errors=True)
    try:
        proc = layoutlib.run_eda(
            ["python", str(RUNNER), str(work_dir / "deck"), "--pdk",
             "gf180mcuD", "--gds", str(gds), "--cell", topcell, "--2.5D",
             "--out_dir", str(out_dir)], cwd=work_dir, timeout=timeout)
    except layoutlib.LayoutError as exc:
        raise KpexError(str(exc)) from exc
    log = work_dir / "kpex.log"
    log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    # kpex writes out/<the GDS name up to its first dot>__<cell>/
    found = sorted(out_dir.glob(f"*__{topcell}/{topcell}_k25d_pex_netlist.spice"))
    if proc.returncode != 0 or len(found) != 1:
        raise KpexError(
            f"kpex exited {proc.returncode} without writing "
            f"{topcell}_k25d_pex_netlist.spice: "
            f"{(proc.stderr or proc.stdout)[-2000:]}")
    netlist = found[0]
    return netlist, log


_SUBSTRATE_RE = re.compile(r'^substrate\s+"([^"]+)"', re.M)


def substrate_net(ext_text: str) -> str | None:
    m = _SUBSTRATE_RE.search(ext_text)
    return layoutlib._ext_net(m.group(1)).lower() if m else None


def well_nets(ext_text: str) -> set[str]:
    """The nets (lowercased) magic's .ext gives N-well area: on a node line
    the area/perimeter pairs follow the type, one per resist class, and the
    GF180 tech's class 0 is nwell (with dnwell)."""
    out = set()
    for line in ext_text.splitlines():
        if not line.startswith("node "):
            continue
        parts = line.split()
        try:
            area = float(parts[7])
        except (IndexError, ValueError):
            continue
        if area > 0:
            out.add(layoutlib._ext_net(parts[1].strip('"')).lower())
    return out


def rename_substrate(kpex_text: str, substrate: str) -> str:
    return re.sub(r"\bVSUBS\b", substrate, kpex_text)


def _off(a: float, b: float) -> float | None:
    """|a - b| over the smaller; None when only one of them is zero."""
    lo = min(a, b)
    if lo <= 0:
        return 0.0 if max(a, b) <= 0 else None
    return abs(a - b) / lo


def compare(magic: dict, kpex: dict, nets, probed, skip: dict[str, str],
            tolerance: float = TOLERANCE,
            floor_ff: float = COUPLING_FLOOR_FF) -> dict:
    """Compare per-net parasitics ({net: {c_ff, c_to}} from each extraction;
    a net missing from one is absent from its dict). `nets` are the labelled
    nets to compare on total C, `probed` the nets whose pairwise coupling is
    compared, `skip` {net: reason} the nets not compared. Returns {nets,
    couplings, not_compared, disagree}, `disagree` the rows past tolerance
    (a net only one extraction names is one too)."""
    rows, pairs, not_compared, bad = [], [], [], []
    for net in sorted(nets):
        if net in skip:
            not_compared.append({"net": net, "reason": skip[net]})
            continue
        m, k = magic.get(net), kpex.get(net)
        if m is None and k is None:
            not_compared.append({"net": net, "reason": "neither extraction "
                                 "names it (no label on the net)"})
            continue
        row = {"net": net, "magic_ff": m and m["c_ff"], "kpex_ff": k and k["c_ff"]}
        if m is None or k is None:
            row["off"] = None
            row["why"] = ("only kpex names it" if m is None
                          else "only magic names it")
            bad.append(row)
        else:
            off = _off(m["c_ff"], k["c_ff"])
            row["off"] = None if off is None else round(off, 4)
            if off is None or off > tolerance:
                bad.append(row)
        rows.append(row)
    probed = sorted(p for p in probed if p not in skip
                    and p in magic and p in kpex)
    for i, a in enumerate(probed):
        for b in probed[i + 1:]:
            mc = magic[a]["c_to"].get(b, 0.0)
            kc = kpex[a]["c_to"].get(b, 0.0)
            row = {"nets": [a, b], "magic_ff": round(mc, 5),
                   "kpex_ff": round(kc, 5)}
            if max(mc, kc) < floor_ff:
                row["off"] = None
                row["why"] = f"below the {floor_ff:g} fF floor in both"
            else:
                off = _off(mc, kc)
                row["off"] = None if off is None else round(off, 4)
                if off is None or off > tolerance:
                    bad.append(row)
            pairs.append(row)
    return {"tolerance": tolerance, "coupling_floor_ff": floor_ff,
            "nets": rows, "couplings": pairs, "not_compared": not_compared,
            "disagree": bad}

#!/usr/bin/env python
"""check_layout_match.py - the ade `layout_match` gate (#118).

    check_layout_match.py --workspace DIR [--block NAME] [--out FILE]

Matched devices (a mirror's reference and output, a pair's two halves) are
drawn as rows of equal unit devices in a pattern (ABBA, common centroid,
interdigitated) with dummies at the row ends. netgen merges parallel units
before it compares, so DRC, LVS and pex_sim all pass a row whose pattern is
wrong: AABB, a missing dummy and a rotated unit are each invisible to them.
This gate reads the row itself.

spec/spec.yaml declares the groups (speclib.matching):

    matching:
      - {devices: [xmref, xmout], style: common_centroid, tol_um: 0.05}

The GDS is regenerated (layout_gen.build, never a stale GDS) and extracted
with magic. Each extracted transistor is a unit. A unit belongs to the
schematic device in netlist/<block>.cir with the same model, gate net and
source/drain nets (an unnamed magic net matches any). A unit whose nets fit
only devices outside every group is a dummy or another device; one whose
nets fit devices of two groups is a refusal, since its group cannot be told
(label the nets). Each unit's centre and channel direction come from its
gate (poly2 over comp) in the GDS, its current direction from magic's
terminal order: the first diffusion magic lists is the left (or bottom) one,
so a unit whose first terminal is its schematic drain is drawn the other
way round.

Findings, each an error naming the devices:

- match_centroid_offset (common_centroid): two devices' unit centroids are
  more than tol_um (default 0.05) apart. The offset is reported in um with
  its dx and dy.
- match_unit_size: the units of a group do not all have one W and L.
- match_orientation: the devices' mixes of channel direction and current
  direction differ (one unit of one device rotated).
- match_end_no_dummy: a matched unit has no equal device (same model, W, L,
  channel direction) next to it on both sides along the row, within 1.5
  pitches. An end unit with no dummy etches differently from the rest.
- match_not_interdigitated (interdigitate): a run of one device's units is
  longer than the ratio of unit counts allows.
- match_no_units: a matched device has no extracted unit at all.

A spec with no `matching:` makes the gate not applicable (a report with
applicable=false, like check_mc): nothing is declared matched. Refusals
(exit 2): no layout generator, a malformed `matching:`, a group naming a
device the netlist does not have, a unit that fits two groups, a
hierarchical extraction the gate cannot place.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import layoutlib  # noqa: E402
import layout_gen  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_layout_match"
TOOL = "layout_match"
SPEC_REL = "spec/spec.yaml"
EPS = 0.002          # um: equal sizes and aligned rows
NEIGHBOUR_PITCHES = 1.5


def _params(tokens) -> dict:
    out = {}
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.lower()] = v
    return out


def parse_ext(ext_text: str) -> list[dict]:
    """magic .ext transistors in magic's own terminal order: {model, gate,
    terms: [first, second], w, l, at: (x, y)} in um (`scale R C L`: one
    unit is L/100 um). A dummy whose diffusions are one net has one term."""
    if re.search(r"^use\s", ext_text, re.M):
        raise CheckError("the extraction is hierarchical (the .ext has `use` "
                         "lines); layout_match reads a flat top cell - "
                         "flatten the generator's output")
    m = re.search(r"^scale\s+\S+\s+\S+\s+(\S+)", ext_text, re.M)
    unit = float(m.group(1)) / 100.0 if m else 0.005
    out = []
    for line in ext_text.splitlines():
        if not line.startswith("device "):
            continue
        t = line.split()
        if len(t) < 7 or t[1] != "msubckt":
            continue
        try:
            x0, y0 = int(t[3]), int(t[4])
        except ValueError:
            continue
        p = _params([tok for tok in t[7:] if "=" in tok and '"' not in tok])
        names = re.findall(r'"([^"]*)"', line)
        if len(names) < 3 or "w" not in p or "l" not in p:
            continue
        out.append({"model": t[2], "gate": names[1], "terms": names[2:4],
                    "w": round(float(p["w"]) * unit, 4),
                    "l": round(float(p["l"]) * unit, 4),
                    "at": (x0 * unit, y0 * unit)})
    return out


def gate_rects(gds_path, topcell: str) -> list[tuple[float, float, float, float]]:
    """Every transistor gate (poly2 AND comp) of `topcell`, flattened, as
    (x0, y0, x1, y1) in um."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise CheckError(f"{gds_path} has no cell {topcell!r}")

    def region(name):
        li = layout.find_layer(*layoutlib.GF180_LAYER[name])
        if li is None:
            return kdb.Region()
        return kdb.Region(cell.begin_shapes_rec(li)).merged()

    dbu = layout.dbu
    gates = region("poly2") & region("comp")
    return [(b.left * dbu, b.bottom * dbu, b.right * dbu, b.top * dbu)
            for b in (p.bbox() for p in gates.each())]


def place_units(devs: list[dict], rects) -> None:
    """Give each extracted unit its gate's centre and channel direction:
    axis 'x' when current runs along x (the gate is L wide in x)."""
    for d in devs:
        x, y = d["at"]
        hit = [r for r in rects
               if r[0] - EPS <= x <= r[2] + EPS and r[1] - EPS <= y <= r[3] + EPS]
        if not hit:
            raise CheckError(f"no gate shape under magic's {d['model']} at "
                             f"({x:.3f}, {y:.3f}) um: the GDS and the "
                             "extraction disagree")
        r = min(hit, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
        d["centre"] = ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
        width_x = r[2] - r[0]
        d["axis"] = "x" if abs(width_x - d["l"]) <= abs((r[3] - r[1]) - d["l"]) else "y"


def schematic_devices(cir_text: str) -> dict[str, dict]:
    """netlist/<block>.cir transistors: {name: {model, d, g, s}} (an
    `x... d g s b model ...` subckt call or an `m...` line)."""
    lines = []
    for raw in cir_text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    out = {}
    for line in lines:
        t = line.split("$")[0].split()
        if not t or t[0][0].lower() not in "xm" or t[0].startswith("*"):
            continue
        pos = [x for x in t[1:] if "=" not in x]
        if len(pos) < 5:
            continue
        out[t[0].lower()] = {"model": pos[4].lower(), "d": pos[0].lower(),
                             "g": pos[1].lower(), "s": pos[2].lower()}
    return out


def _net_fits(ext_net: str, net: str) -> bool:
    # magic names a net nobody labelled after a point on it, with a '#'
    n = ext_net.lower()
    return n == net or "#" in n


def _fits(unit: dict, dev: dict) -> bool:
    if unit["model"].lower() != dev["model"] or not _net_fits(unit["gate"], dev["g"]):
        return False
    terms = unit["terms"] if len(unit["terms"]) == 2 else unit["terms"] * 2
    a, b = terms
    return ((_net_fits(a, dev["s"]) and _net_fits(b, dev["d"]))
            or (_net_fits(a, dev["d"]) and _net_fits(b, dev["s"])))


def assign(units: list[dict], schem: dict, groups: list[dict]) -> None:
    """Set each unit's `dev` (the matched device it is a unit of) or None."""
    owner = {}
    for gi, g in enumerate(groups):
        for name in g["devices"]:
            if name not in schem:
                raise CheckError(f"spec.yaml matching names {name}, but the "
                                 "netlist has no transistor of that name")
            owner[name] = gi
    for u in units:
        cand = [n for n, dev in schem.items() if _fits(u, dev)]
        matched = sorted({n for n in cand if n in owner})
        u["dev"] = None
        if not matched:
            continue
        if len({owner[n] for n in matched}) > 1:
            raise CheckError(
                f"the {u['model']} at {_um(u['centre'])} fits {matched}, "
                "devices of two matching groups: label the nets so the "
                "gate can tell them apart")
        # within one group a unit that fits more than one device (equal
        # nets) is told apart by its drain
        if len(matched) > 1:
            by_drain = [n for n in matched
                        if schem[n]["d"] in [x.lower() for x in u["terms"]]]
            matched = by_drain if len(by_drain) == 1 else matched
        if len(matched) > 1:
            raise CheckError(f"the {u['model']} at {_um(u['centre'])} fits "
                             f"{matched} equally: label the nets")
        u["dev"] = matched[0]
        dev = schem[matched[0]]
        first = u["terms"][0].lower()
        u["drain_first"] = (first == dev["d"]) if dev["d"] != dev["s"] else None


def _um(p) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f}) um"


def _centroid(units):
    return (sum(u["centre"][0] for u in units) / len(units),
            sum(u["centre"][1] for u in units) / len(units))


def _orientation_mix(units) -> dict:
    mix: dict[str, float] = {}
    for u in units:
        key = u["axis"] + ("" if u["drain_first"] is None
                           else ("-" if u["drain_first"] else "+"))
        mix[key] = mix.get(key, 0) + 1 / len(units)
    return {k: round(v, 4) for k, v in sorted(mix.items())}


def _equal(a: dict, b: dict) -> bool:
    return (a["model"] == b["model"] and abs(a["w"] - b["w"]) <= EPS
            and abs(a["l"] - b["l"]) <= EPS and a["axis"] == b["axis"])


def _pitch(units, axis: int) -> float:
    xs = sorted({round(u["centre"][axis], 3) for u in units})
    gaps = sorted(b - a for a, b in zip(xs, xs[1:]) if b - a > EPS)
    return gaps[len(gaps) // 2] if gaps else 0.0


def check(units: list[dict], groups: list[dict], topcell: str) -> tuple[list, dict]:
    v = []

    def find(kind, refs, msg, **extra):
        v.append(checklib.violation(TOOL, "error", "layout", topcell, kind,
                                    refs, msg, "magic", **extra))

    facts = []
    for g in groups:
        names = g["devices"]
        per = {n: [u for u in units if u["dev"] == n] for n in names}
        empty = [n for n in names if not per[n]]
        for n in empty:
            find("match_no_units", [n], f"{n} is in a matching group but no "
                 "extracted transistor is a unit of it")
        if empty:
            continue
        mine = [u for n in names for u in per[n]]
        row = {"devices": names, "style": g["style"],
               "units": {n: len(per[n]) for n in names}}

        sizes = sorted({(u["w"], u["l"]) for u in mine})
        if len(sizes) > 1:
            find("match_unit_size", names,
                 f"the units of {'/'.join(names)} are not one size: "
                 + ", ".join(f"W={w:g} L={l:g}" for w, l in sizes),
                 sizes=[{"w_um": w, "l_um": l} for w, l in sizes])

        mixes = {n: _orientation_mix(per[n]) for n in names}
        if len({tuple(m.items()) for m in mixes.values()}) > 1:
            bad = [(n, u) for n in names for u in per[n]]
            find("match_orientation", names,
                 f"the units of {'/'.join(names)} are not drawn the same "
                 "way round (axis x/y, + source first, - drain first): "
                 + "; ".join(f"{n} {mixes[n]}" for n in names),
                 orientations=mixes,
                 units=[{"device": n, "at_um": [round(c, 3) for c in u["centre"]],
                         "axis": u["axis"], "drain_first": u["drain_first"]}
                        for n, u in bad])

        cents = {n: _centroid(per[n]) for n in names}
        row["centroids_um"] = {n: [round(c, 4) for c in cents[n]] for n in names}
        if g["style"] == "common_centroid":
            worst = 0.0
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    dx = cents[b][0] - cents[a][0]
                    dy = cents[b][1] - cents[a][1]
                    off = math.hypot(dx, dy)
                    worst = max(worst, off)
                    if off > g["tol_um"] + 1e-9:
                        find("match_centroid_offset", [a, b],
                             f"the centroids of {a} and {b} are {off:.3f} um "
                             f"apart (dx {dx:.3f}, dy {dy:.3f}); tol_um "
                             f"{g['tol_um']:g}",
                             offset_um=round(off, 4), dx_um=round(dx, 4),
                             dy_um=round(dy, 4), tol_um=g["tol_um"])
            row["centroid_offset_um"] = round(worst, 4)

        # neighbours along the row (and across it, for a 2-D array)
        for ax in (0, 1):
            pitch = _pitch(mine, ax)
            if pitch <= 0:
                continue
            reach = NEIGHBOUR_PITCHES * pitch
            for u in mine:
                same_line = [o for o in units if o is not u and _equal(o, u)
                             and abs(o["centre"][1 - ax] - u["centre"][1 - ax]) <= EPS * 10]
                d = [o["centre"][ax] - u["centre"][ax] for o in same_line]
                lo = any(-reach <= x < -EPS for x in d)
                hi = any(EPS < x <= reach for x in d)
                if not (lo and hi):
                    side = ("both sides" if not (lo or hi)
                            else ("the " + ("left" if ax == 0 else "bottom") if not lo
                                  else "the " + ("right" if ax == 0 else "top")))
                    find("match_end_no_dummy", [u["dev"]],
                         f"the {u['dev']} unit at {_um(u['centre'])} has no "
                         f"equal device on {side} within {reach:.2f} um: an "
                         "end unit needs a dummy",
                         at_um=[round(c, 3) for c in u["centre"]],
                         side=side, pitch_um=round(pitch, 4))

        if g["style"] == "interdigitate":
            axis = 0 if _pitch(mine, 0) > 0 else 1
            seq = [u["dev"] for u in sorted(mine, key=lambda u: (u["centre"][1 - axis], u["centre"][axis]))]
            counts = sorted(len(per[n]) for n in names)
            allowed = math.ceil(counts[-1] / counts[0])
            run, longest = 1, 1
            for a, b in zip(seq, seq[1:]):
                run = run + 1 if a == b else 1
                longest = max(longest, run)
            if longest > allowed:
                find("match_not_interdigitated", names,
                     f"{'/'.join(names)} run {longest} units of one device "
                     f"in a row ({''.join(chr(65 + names.index(s)) for s in seq)}); "
                     f"interdigitated allows {allowed}",
                     sequence=seq, longest_run=longest, allowed=allowed)
        facts.append(row)
    return v, {"groups": facts,
               "dummies": sum(1 for u in units if u["dev"] is None)}


def not_applicable_at_release(ws: Path) -> str | None:
    """attest's release-time question: the reason layout_match is not owed
    (spec.yaml declares no matching groups), or None when it is. A spec
    that is missing, unreadable or malformed owes the gate: doubt never
    counts as not applicable."""
    try:
        groups = speclib.matching(speclib.load_spec(Path(ws) / SPEC_REL))
    except CheckError:
        return None
    if groups is None or groups:
        return None
    return ("not applicable: spec.yaml declares no 'matching:' groups, "
            "re-read at this release")


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--block", help="override state.json's 'block'")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec_path = ws / SPEC_REL
    if not spec_path.is_file():
        raise CheckError(f"no {SPEC_REL}: layout_match reads its matching:")
    spec = speclib.load_spec(spec_path)
    groups = speclib.matching(spec)
    if groups is None:
        raise CheckError(f"{SPEC_REL} 'matching' is malformed - run spec_lint")
    if not groups:
        # Like check_mc's: a real answer but not a recordable pass (it is
        # stamped with spec.yaml alone, so state.record_gate refuses it);
        # release re-asks the spec (not_applicable_at_release).
        return checklib.report(
            SCRIPT, ws / "spec" / "spec.yaml", [], applicable=False,
            reason="spec.yaml declares no matching: groups"), args.out

    block = layout_gen.block_of(ws, args.block)
    cir = ws / "netlist" / f"{block}.cir"
    if not cir.is_file():
        raise CheckError(f"no netlist/{block}.cir: layout_match maps units "
                         "to the schematic's devices")
    schem = schematic_devices(cir.read_text(encoding="utf-8", errors="replace"))

    gds_path, topcell, _abs, _abstract = layout_gen.build(ws, args.block)
    work = ws / "log" / "layout_match"
    work.mkdir(parents=True, exist_ok=True)
    spice_path, _log = layoutlib.run_magic_extract(
        work, gds_path.resolve(), topcell, parasitics=False)
    ext_path = spice_path.with_suffix(".ext")
    if not ext_path.is_file():
        raise CheckError(f"magic wrote no {ext_path.name}")
    units = parse_ext(ext_path.read_text(encoding="utf-8", errors="replace"))
    place_units(units, gate_rects(gds_path, topcell))
    assign(units, schem, groups)
    violations, facts = check(units, groups, topcell)
    return checklib.report(SCRIPT, ws / "layout", violations, topcell=topcell,
                           gds=str(gds_path.relative_to(ws)),
                           extraction=str(ext_path.relative_to(ws)),
                           **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

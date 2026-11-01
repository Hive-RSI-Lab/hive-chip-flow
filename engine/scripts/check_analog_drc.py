#!/usr/bin/env python
"""check_analog_drc.py - the ade `drc` gate (docs/design.md 1.5, 5,
"### M9."; docs/spikes/glayout.md).

    check_analog_drc.py --workspace DIR [--block NAME] [--out FILE]

gates.yaml's ade.drc row names this tool `analog_drc` - a name distinct
from vde/msde's own `drc` (magic+klayout on a HARDENED digital GDS, M4) so
the two never collide on the same check_<tool>.py filename gate.py resolves
literally from `tool:`.

Regenerates the GDS fresh from `layout/gen_<block>.py` via layout_gen.build()
(never trusts a GDS already on disk - "layout is code", docs/design.md 5),
then runs ONLY klayout's real GF180 signoff deck, less the chip-level
density and dummy-fill decks (layoutlib.DRC_SKIPPED, whose reasons the
report carries as `decks_skipped`). magic DRC is not run here
at all: the spike (docs/spikes/glayout.md) found magic silently re-snaps a
GDS to the manufacturing grid on load, hiding an off-grid shape that
klayout's own deck reports (424/1004 *_OFFGRID findings on the very same
files magic called clean) - "klayout's GF180 deck is the DRC that counts."
Running magic here anyway would only recreate that false confidence.

Findings are clustered by (layer, cell) per docs/design.md 5 ("a DRC
finding is clustered by layer and cell") using layoutlib.drc_layer_of() to
read a layer name off the violated rule's own category name (CO./DF./PL./
NP./PP./NW./MET1/MET2, or *_OFFGRID) - cluster_violations.py's own (file,
module, kind) key already groups by kind (=category here) and module (=cell
here); `file` carries the layer name so the SAME kind on two different
layers still separates.

A run with zero <item> entries in the .lyrdb IS a real pass (klayout logs
"DRC RESULT: SUCCESS (0 violations)"); layoutlib.run_klayout_drc() refuses
(CheckError, never a pass) if that banner line - proof the deck actually
loaded and ran to completion - is missing.

Footprint: after DRC the top cell's bounding box is measured
(layoutlib.gds_bbox_um) and reported as `bbox_um`, next to `footprint`,
spec.yaml's optional `footprint_um: {width, height}` limit in um. A bbox
wider or taller than that limit is a `footprint_exceeded` finding (exit 1).
With no footprint_um (or no spec.yaml) nothing is compared and `footprint`
is null, so the missing limit is visible; a malformed footprint_um refuses
(exit 2) - spec_lint's `bad_footprint` is the fix.

Antenna: the PDK's antenna deck runs on its own. A net with no pin is fully
drawn in the generator, so its ratio is final: an `error`, one per (rule, net),
with the net's name and ratio. A pin net takes more metal from the chip's
routing, so the block cannot judge it: an `info` with the ratio, for whoever
integrates it. Pins are the ports of the block's .subckt when
netlist/<block>.{cir,spice} declares one, else every label (`antenna.pins_from`
says which); a label on an internal net is not a pin. A finding that does not
resolve to pin nets alone stays an `error`: never downgraded on a guess.
"""
from __future__ import annotations

import argparse
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
import netlistlib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_analog_drc"
SPEC_REL = "spec/spec.yaml"


def footprint_check(ws: Path, gds_path: Path, topcell: str):
    """(facts, violations): the top cell's bbox against spec.yaml's
    footprint_um. facts is {bbox_um, footprint} for the payload; footprint
    is None when the spec sets no limit."""
    bbox = layoutlib.gds_bbox_um(gds_path, topcell)
    spec_path = ws / SPEC_REL
    spec = speclib.load_spec(spec_path) if spec_path.is_file() else {}
    fp = speclib.footprint_um(spec)
    if fp is None and "footprint_um" in spec:
        raise CheckError(f"{SPEC_REL} 'footprint_um' is not {{width, height}} "
                         "positive numbers - run spec_lint and fix it")
    violations = []
    if fp is not None:
        over = [k for k in ("width", "height") if bbox[k] > fp[k]]
        if over:
            violations.append(checklib.violation(
                "analog_drc", "error", SPEC_REL, topcell, "footprint_exceeded",
                over, f"{topcell} is {bbox['width']} x {bbox['height']} um, "
                f"over the spec's footprint_um {fp['width']} x "
                f"{fp['height']} um ({', '.join(over)})", "klayout",
                value={"bbox_um": bbox, "footprint": fp}))
    return {"bbox_um": bbox, "footprint": fp}, violations


def block_pins(ws: Path, topcell: str) -> tuple[set[str] | None, str]:
    """(lower-case port names, source), or (None, "labels") when no netlist
    declares the top cell, so every label counts as a pin."""
    for ext in (".cir", ".spice"):
        for path in sorted((ws / "netlist").glob(f"*{ext}")):
            pins = netlistlib.subckt_pins(
                path.read_text(encoding="utf-8", errors="replace"))
            if topcell.lower() in pins:
                return {p.lower() for p in pins[topcell.lower()]}, "netlist"
    return None, "labels"


def _is_pin_net(net: str | None, pins: set[str] | None) -> bool:
    if not net:
        return False
    return pins is None or any(n.lower() in pins for n in net.split(","))


def antenna_check(ws: Path, gds_path: Path, topcell: str):
    """(facts, violations) from the antenna deck, one per (rule, net)."""
    rdb_path = ws / "log" / "analog_drc_antenna.lyrdb"
    layoutlib.run_klayout_antenna(gds_path, topcell, rdb_path)
    items = layoutlib.parse_drc_rdb(rdb_path)
    pins, pins_from = block_pins(ws, topcell)
    nets = layoutlib.nets_of_polygons(
        gds_path, topcell,
        [re.sub(r"^polygon:\s*", "", it["value"] or "") for it in items]
    ) if items else []
    findings: dict[tuple, dict] = {}
    for it, candidates in zip(items, nets):
        ratio = next((float(m.group(1)) for m in
                      (re.match(r"\[#RATIO\] float: (\S+)", v)
                       for v in it["values"]) if m), None)
        is_pin = bool(candidates) and all(
            _is_pin_net(n, pins) for n in candidates)
        names = tuple(n or "<unlabelled>" for n in candidates) or ("<unresolved>",)
        entry = findings.setdefault((it["category"], names), {
            "item": it, "is_pin": is_pin, "ratio": ratio})
        entry["is_pin"] = entry["is_pin"] and is_pin

    violations = []
    for (category, names), entry in findings.items():
        where = "pin net" if entry["is_pin"] else "internal net"
        ratio = entry["ratio"]
        violations.append(checklib.violation(
            "analog_drc", "info" if entry["is_pin"] else "error", "antenna",
            entry["item"].get("cell") or topcell, category, list(names),
            f"{where} {', '.join(names)}: {entry['item']['description']}"
            + (f" (ratio {ratio:.1f})" if ratio is not None else ""),
            "klayout", net=", ".join(names), ratio=ratio,
            pin_net=entry["is_pin"]))
    facts = {"report": str(rdb_path.relative_to(ws)), "pins_from": pins_from,
             "internal": sum(v["severity"] == "error" for v in violations),
             "pin_nets": sum(v["severity"] == "info" for v in violations)}
    return facts, violations


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--block", help="override state.json's 'block'")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()  # tools run with cwd=log/
    gds_path, topcell, _abs_path, _abstract = layout_gen.build(ws, args.block)

    rdb_path = (ws / "log" / "analog_drc.lyrdb")
    rdb_path.parent.mkdir(parents=True, exist_ok=True)
    layoutlib.run_klayout_drc(gds_path, topcell, rdb_path)
    items = layoutlib.parse_drc_rdb(rdb_path)

    violations = []
    for it in items:
        layer = layoutlib.drc_layer_of(it["category"])
        violations.append(checklib.violation(
            "analog_drc", "error", layer, it.get("cell") or topcell,
            it["category"], [], it["description"], "klayout",
            value=it.get("value")))
    footprint_facts, footprint_violations = footprint_check(ws, gds_path, topcell)
    violations += footprint_violations
    antenna_facts, antenna_violations = antenna_check(ws, gds_path, topcell)
    violations += antenna_violations

    payload = checklib.report(SCRIPT, ws / "layout", violations,
                              topcell=topcell, gds=str(gds_path.relative_to(ws)),
                              report=str(rdb_path.relative_to(ws)),
                              decks_skipped=layoutlib.DRC_SKIPPED,
                              antenna=antenna_facts)
    payload.update(footprint_facts)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

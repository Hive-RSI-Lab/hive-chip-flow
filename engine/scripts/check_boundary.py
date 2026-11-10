#!/usr/bin/env python
"""check_boundary.py - msde's boundary gate (engine/lib/boundary.py, #110).

    check_boundary.py --workspace MSDE_WS [--out FILE]

Measures the analog side's crossing pins on the sized schematic at the three
STA corners and compares them with the signal's level in
engine/reference/levels.yaml: every a2d pin's swing and its 10-90% edge into
the load interface.yaml declares, every d2a pin's switching point.

Exit 0: every pin inside its level. 1: findings (boundary_swing,
boundary_transition, boundary_threshold), or no `boundary:` section on a macro
with crossing pins (boundary_undeclared). 2: refused, with a remediation: an
incomplete declaration, an untyped interface, a load the liberty cannot
resolve.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

import boundary  # noqa: E402
import charlib  # noqa: E402
import check_macro_lib  # noqa: E402
import checklib  # noqa: E402
import layoutlib  # noqa: E402
import macrolib  # noqa: E402

SCRIPT = "boundary"
SPEC_REL = "analog/spec/spec.yaml"


def declared(ws: Path) -> dict | None:
    """The analog spec's boundary section, or None. Unreadable raises: doubt
    is never "not declared"."""
    import yaml
    path = Path(ws) / "analog" / "spec" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("boundary") or None


def crossing_signals(ws: Path) -> tuple[dict, dict[str, dict]]:
    """(the interface's data, {pin: its signal's entry})."""
    import yaml
    import check_top_harden
    data = yaml.safe_load((Path(ws) / "interface.yaml").read_text(encoding="utf-8")) or {}
    signals = {s["name"]: s for s in data.get("signals") or []}
    lowered = {name.lower(): sig for name, sig in signals.items()}
    pins = check_top_harden.signal_pins(
        {n: s["direction"] for n, s in lowered.items()},
        {n: int(s.get("width", 1)) for n, s in lowered.items()})
    return data, {pin: lowered[sig] for pin, sig in pins.items()}


def not_applicable_at_release(ws: Path) -> str | None:
    """Owed by every macro with a crossing pin."""
    try:
        if not crossing_signals(ws)[1]:
            return ("not applicable: the analog macro has no crossing pin, so "
                    "there is no boundary to measure")
    except Exception:  # noqa: BLE001  unreadable: the gate is owed
        return None
    return None


def typed_signals(path: Path, data: dict, pins: dict[str, dict], levels: dict) -> None:
    """Refuse an interface the gate cannot read as numbers."""
    if boundary.interface_version(data) != boundary.TYPED_VERSION:
        raise boundary.Refused(
            "interface_untyped",
            f"{path} is version {data.get('version')!r}: the boundary gate "
            f"reads version {boundary.TYPED_VERSION}'s typed level and load")
    for pin, sig in sorted(pins.items()):
        if sig.get("level") not in levels:
            raise boundary.Refused(
                "interface_untyped", f"{pin}: level {sig.get('level')!r} is not "
                f"in levels.yaml ({sorted(levels)})")
        problem = boundary.load_problem(sig.get("load"))
        if problem:
            raise boundary.Refused("interface_untyped", f"{pin}: {problem}")


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="msde block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    interface_path = ws / "interface.yaml"
    data, pins = crossing_signals(ws)
    section = declared(ws)
    if section is None:
        msg = (f"no boundary section, and the analog macro has crossing pins "
               f"{sorted(pins)}: nothing says how to measure them" if pins else
               "no boundary section, and the analog macro has no crossing pin")
        return checklib.report(SCRIPT, interface_path, [checklib.violation(
            SCRIPT, "error" if pins else "warning", SPEC_REL, None,
            "boundary_undeclared", [], msg, SCRIPT)], declared=False), args.out

    levels = boundary.load_levels()
    typed_signals(interface_path, data, pins, levels)
    # the declaration first: incomplete is refused before anything is simulated
    decl = boundary.parse_section(section, {p: s["direction"] for p, s in pins.items()})
    cell, schematic = check_macro_lib.schematic_of(ws / "analog")
    work = ws / "log" / "boundary"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    netlist = work / f"{cell}.schematic.sp"
    netlist.write_text(check_macro_lib.bench_schematic(schematic, cell), encoding="utf-8")
    netlist_text = netlist.read_text(encoding="utf-8")
    ports = charlib.ports_of(netlist_text, cell)
    lowered = [p.lower() for p in ports]
    missing = sorted(set(pins) - set(lowered))
    if missing:
        raise boundary.Refused(
            "boundary_incomplete",
            f"{cell}'s ports {ports} do not include crossing pins {missing}")
    import check_top_harden
    vdd = [p for p in ports if check_top_harden.VDD_RE.match(p)]
    vss = [p for p in ports if check_top_harden.VSS_RE.match(p)]
    if len(vdd) != 1 or len(vss) != 1:
        raise boundary.Refused(
            "boundary_incomplete",
            f"{cell}'s ports {ports}: need exactly one supply and one ground")
    pdk = layoutlib.pdk_root()
    includes = macrolib.stdcell_includes(netlist, pdk)

    violations = []
    results: dict[str, dict] = {}

    def bad(kind: str, pin: str, corner: str, msg: str) -> None:
        violations.append(checklib.violation(
            SCRIPT, "error", SPEC_REL, None, kind, [pin, corner], msg, SCRIPT))

    for corner_name in macrolib.STA_CORNERS:
        library = boundary.library_text(pdk, corner_name)
        corner = macrolib.corner_of(corner_name)
        bench_for = {}

        def bench(level_name: str):
            if level_name not in bench_for:
                bench_for[level_name] = macrolib.Bench(
                    cell, netlist, ports, vdd[0], vss[0], pdk, corner,
                    levels[level_name]["corners"][corner_name]["vdd"],
                    work / corner_name, None, includes)
            return bench_for[level_name]

        results[corner_name] = {}
        for pin, out in sorted(decl.outputs.items()):
            level = levels[pins[pin]["level"]]
            load = boundary.resolve_load(pins[pin]["load"], library)
            measured = boundary.measure_output(bench(pins[pin]["level"]), decl,
                                               out, level, load)
            limit = boundary.transition_limit(level, corner_name,
                                              load["max_transition_ns"])
            results[corner_name][pin] = {**measured, "max_transition_ns": limit}
            for kind, msg in boundary.judge_output(pin, corner_name, level,
                                                   measured, limit):
                bad(kind, pin, corner_name, msg)
        for pin, inp in sorted(decl.inputs.items()):
            level = levels[pins[pin]["level"]]
            measured = boundary.measure_input(bench(pins[pin]["level"]), decl, inp,
                                              level, netlist_text)
            results[corner_name][pin] = measured
            for kind, msg in boundary.judge_input(pin, corner_name, level,
                                                  measured["threshold_v"]):
                bad(kind, pin, corner_name, msg)

    return checklib.report(SCRIPT, interface_path, violations, declared=True,
                           cell=cell, corners=list(macrolib.STA_CORNERS),
                           pins=results), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

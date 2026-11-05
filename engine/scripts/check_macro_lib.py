#!/usr/bin/env python
"""check_macro_lib.py - msde's macro_lib gate (engine/lib/macrolib.py).

    check_macro_lib.py --workspace MSDE_WS [--out FILE]

Reads `macro_timing` from analog/spec/spec.yaml and writes, under
<ws>/macro_lib/, one <cell>__<corner>.lib per STA corner, <cell>.clocks.json
with the macro-sourced clocks' ceilings from the analog side's record, and
<cell>.report.json. top_harden copies the libs into top/macros and gives them to
LibreLane as the macro's `lib` view.

Exit 0: written, no finding. 1: an error, no macro_timing on a macro with
crossing pins (the gate is owed for every such macro, #177), which fails it; or
warnings only, which the gate's fail_severities pass: no macro_timing on a macro
with no crossing pin (release does not owe the gate), or limits that disagree at
max_capacitance. 2: refused, with a remediation: a limit, arc or static level the spec does not state, an arc
whose output never moved, no passing record for a clock.

The netlist is macro_timing.extracted when that file exists, else the sized
schematic top_harden itself delivers.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

import charlib  # noqa: E402
import checklib  # noqa: E402
import layoutlib  # noqa: E402
import macrolib  # noqa: E402

SCRIPT = "macro_lib"


def declared(ws: Path) -> dict | None:
    """The analog spec's macro_timing, or None. Unreadable raises: doubt is
    never "not declared"."""
    import yaml
    path = Path(ws) / "analog" / "spec" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("macro_timing") or None


def not_applicable_at_release(ws: Path) -> str | None:
    """Owed for every macro with a crossing pin, declared or not (#177)."""
    try:
        if declared(ws) is None and not crossing_pins(ws):
            return ("not applicable: the analog macro has no crossing pin, so "
                    "there is no lib view to characterise")
    except Exception:  # noqa: BLE001  unreadable: the gate is owed
        return None
    return None


def crossing_pins(ws: Path) -> dict[str, str]:
    import yaml
    import check_top_harden
    iface = yaml.safe_load((Path(ws) / "interface.yaml").read_text(encoding="utf-8")) or {}
    signals = {s["name"].lower(): s["direction"] for s in iface.get("signals") or []}
    widths = {s["name"].lower(): int(s.get("width", 1))
              for s in iface.get("signals") or []}
    return {pin: signals[sig]
            for pin, sig in check_top_harden.signal_pins(signals, widths).items()}


def schematic_of(analog: Path) -> tuple[str, str]:
    """(cell, the sized .subckt text), as top_harden delivers it."""
    import check_analog_lvs
    import layout_gen
    import sim_run
    block = layout_gen.block_of(analog, None)
    ref_path = check_analog_lvs.find_reference_netlist(analog, block)
    text = ref_path.read_text(encoding="utf-8", errors="replace")
    cell = check_analog_lvs.reference_cell(analog, block, text)
    sizing = sim_run.load_sizing(analog)
    if sizing:
        text, _applied = check_analog_lvs.sized_reference(text, cell, sizing)
    return cell, text


def extracted_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="msde block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    analog = ws / "analog"
    rtl = ws / "digital" / "rtl"
    section = declared(ws)
    if section is None:
        pins = sorted(crossing_pins(ws))
        msg = (f"no macro_timing, and the analog macro has crossing pins "
               f"{pins}: top_harden needs their lib view" if pins else
               "no macro_timing, and the analog macro has no crossing pin")
        return checklib.report(SCRIPT, ws / "interface.yaml", [checklib.violation(
            SCRIPT, "error" if pins else "warning", "analog/spec/spec.yaml",
            None, "macro_timing_undeclared", [], msg, SCRIPT)],
            declared=False), args.out

    # the declaration first: incomplete is refused before anything is read or run
    mt = macrolib.parse_spec(section, "", crossing_pins(ws))
    cell, schematic = schematic_of(analog)
    mt.cell = cell
    work = ws / "log" / "macro_lib"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    sch = work / f"{cell}.schematic.sp"
    sch.write_text(charlib.prepare_netlist(schematic, cell), encoding="utf-8")
    named = analog / mt.extracted if mt.extracted else None
    path, kind = charlib.choose_netlist(named, sch)
    text = path.read_text(encoding="utf-8", errors="replace")
    if kind == "extracted":
        netlist = work / f"{cell}.extracted.sp"
        netlist.write_text(charlib.prepare_netlist(text, cell), encoding="utf-8")
    else:
        netlist = sch
    ports = charlib.ports_of(netlist.read_text(encoding="utf-8"), cell)
    import check_top_harden
    vdd = [p for p in ports if check_top_harden.VDD_RE.match(p)]
    vss = [p for p in ports if check_top_harden.VSS_RE.match(p)]
    if len(vdd) != 1 or len(vss) != 1:
        raise macrolib.Refused(
            "macro_timing_incomplete",
            f"{cell}'s ports {ports}: need exactly one supply and one ground")
    out_dir = ws / macrolib.OUT_DIR
    ceilings = macrolib.clock_ceilings(mt, analog)
    includes = macrolib.resolve_includes(mt, analog)
    extra = {"includes_sha256": macrolib.includes_digest(includes)}
    if kind == "extracted":
        extra["extracted_sha256"] = extracted_digest(named)
    else:  # top_harden's macrolib.check_current compares it
        extra["schematic_sha256"] = macrolib.text_digest(schematic)
    report = macrolib.characterize(
        mt, netlist=netlist, netlist_kind=kind, ports=ports, power=vdd[0],
        ground=vss[0], pdk=layoutlib.pdk_root(), out_dir=out_dir, work_dir=work,
        extra_report=extra, includes=includes)
    macrolib.write_clocks(ceilings, out_dir, cell)
    violations = [checklib.violation(
        SCRIPT, "warning", "analog/spec/spec.yaml", w["pin"],
        "macro_limit_inconsistent", [w["corner"], w["edge"]],
        f"{w['pin']} at {w['corner']}: {w['edge']} {w['transition_ns']} ns at its "
        f"max_capacitance {w['max_capacitance_pf']} pF is over its "
        f"max_transition {w['max_transition_ns']} ns", SCRIPT)
        for w in report["warnings"]]
    # interface.yaml: the gate's first input, which state.py holds input_digest to
    return checklib.report(SCRIPT, ws / "interface.yaml", violations, declared=True, cell=cell,
                           netlist_kind=kind, corners=report["corners"],
                           slews_ns=mt.slews, loads_pf=mt.loads,
                           clocks=ceilings), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

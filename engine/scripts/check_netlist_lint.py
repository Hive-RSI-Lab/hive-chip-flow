#!/usr/bin/env python
"""check_netlist_lint.py - the netlist_lint gate (docs/design.md 1.5,
"### M8."). New at M8: no /hwde precedent.

    check_netlist_lint.py --workspace DIR [--out FILE]

Pass criteria (gates.yaml): "Every device uses a gf180mcu_fd_pr model, no
floating node, subcircuit pins match the spec." Two layers:

STATIC (no ngspice call): netlistlib.parse_devices() over netlist/*.cir -
every device's model must be a real subckt this box's own PDK ngspice model
files declare (netlistlib.known_models(), read live off the toolchain, never
a hardcoded snapshot - the fault this catches: "a model not in the PDK");
every refdes spec.yaml's own `devices` list names must actually appear as a
device in the netlist (the "subcircuit pins match the spec" half - a block's
declared device inventory is the spec-side contract check_bench_strength.py
also mutates against); and a floating-node scan (netlistlib.floating_nodes(),
excluding this netlist's own `.subckt` header pins and ground/supply rails).

DYNAMIC ("ngspice dry run"): netlist/*.cir alone is a `.subckt` LIBRARY, not
a runnable deck (nothing instantiates it, so ngspice would never even solve
it) - the same rtl/-vs-tb/ split /vde already uses. The actual dry run reuses
the SAME tb/*.cir bench(es) sim_tt/sim_pvt will run, at the single 'tt'
corner, and scores ONLY for a known ngspice failure signature
(simlib.detect_engine_errors - never bounds; that is sim_tt's job, not
this gate's). This is what a static parse alone cannot catch: this box's own
gf180mcuD models drove a REAL floating gate node through "singular matrix"
-> failed gmin/source stepping -> a "successful", exit-0 `.op` anyway
(simlib.py's own docstring has the transcript) - a purely textual scan
cannot see that ngspice's own DC solve never actually converged cleanly.

DEVICES (#137): every MOS cap, diode, BJT and MIM the netlist uses is held
to its row in engine/reference/devices.yaml (engine/scripts/pdk_devices.py):
- not lvs_ok: `device_not_lvs_ok`, with the round trip's reason - an error
  when the workspace has a layout generator (layout/gen_*.py: analog_lvs
  would fail on it), a warning when it has none yet;
- not tt_legal, in a TT tile: `device_not_tt_legal` (an error). A workspace
  is in a TT tile when spec.yaml says `tt_tile: true`, or, with no
  `tt_tile`, when it is an msde side (its parent has an interface.yaml).
A model with no row (the FETs, the resistors) is not judged here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import devicelib  # noqa: E402
import netlistlib  # noqa: E402
import sim_run  # noqa: E402
import speclib  # noqa: E402

SCRIPT = "check_netlist_lint"
DRY_RUN_TIMEOUT = 30.0


def static_violations(ws: Path, spec: dict, netlist_path: Path,
                      pdk_root: Path) -> list[dict]:
    text = netlist_path.read_text(encoding="utf-8")
    rel = str(netlist_path.relative_to(ws))
    devices = netlistlib.parse_devices(text)
    violations = []

    known = netlistlib.known_models(pdk_root)
    for dev in devices:
        if dev["model"] not in known:
            violations.append(checklib.violation(
                "netlist_lint", "error", rel, None, "model_not_in_pdk",
                [dev["ref"]],
                f"{dev['ref']} (line {dev['line']}) uses model "
                f"{dev['model']!r}, not one of the PDK's own ngspice "
                "subcircuits", "netlistlib"))

    declared = spec.get("devices") or []
    # every element counts, not only `x` lines: a declared behavioural
    # source (`b...`) is a real device bench_strength mutates
    device_refs = {e["ref"] for e in netlistlib.parse_elements(text)}
    for ref in declared:
        if ref not in device_refs:
            violations.append(checklib.violation(
                "netlist_lint", "error", rel, None, "declared_device_missing",
                [ref],
                f"spec.yaml declares device {ref!r} but no such instance "
                "exists in the netlist", "netlistlib"))

    declared_pins: set[str] = set()
    for pins in netlistlib.subckt_pins(text).values():
        declared_pins.update(p.lower() for p in pins)
    for node in netlistlib.floating_nodes(devices, declared_pins):
        violations.append(checklib.violation(
            "netlist_lint", "error", rel, None, "floating_node", [node],
            f"node {node!r} is touched by fewer than two device terminals "
            "in the netlist - likely floating", "netlistlib"))

    violations += device_violations(ws, spec, rel, text)
    return violations


# a diode element: d<name> anode cathode model [params]
_DIODE_RE = re.compile(r"^\s*(d\S*)\s+(\S+)\s+(\S+)\s+(\S+)",
                                  re.I)


def in_tt_tile(ws: Path, spec: dict) -> bool:
    """spec.yaml's `tt_tile`, else whether the workspace is an msde side."""
    if "tt_tile" in spec:
        return spec["tt_tile"] is True
    return (ws.resolve().parent / "interface.yaml").is_file()


def device_violations(ws: Path, spec: dict, rel: str, text: str,
                      table: dict | None = None) -> list[dict]:
    """devices.yaml's verdict on each device the netlist uses (#137)."""
    table = devicelib.load() if table is None else table
    if "tt_tile" in spec and not isinstance(spec["tt_tile"], bool):
        return [checklib.violation(
            "netlist_lint", "error", "spec/spec.yaml", None, "bad_tt_tile",
            [], f"spec.yaml's tt_tile is {spec['tt_tile']!r}, not true or "
            "false", "devicelib")]
    uses = [(d["ref"], d["model"], d["line"])
            for d in netlistlib.parse_devices(text)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        m = _DIODE_RE.match(netlistlib.strip_comment(raw))
        if m and "=" not in m.group(4):
            uses.append((m.group(1), m.group(4).lower(), lineno))
    tt = in_tt_tile(ws, spec)
    # a generator, not the bare layout/ dir: state.py and faults.py
    # scaffold an empty layout/ before any block has one, and analog_lvs
    # runs only layout/gen_<block>.py (layout_gen.load_generator)
    has_layout = any((ws / "layout").glob("gen_*.py"))
    out = []
    for ref, model, line in uses:
        why = devicelib.lvs_problem(model, table)
        if why:
            out.append(checklib.violation(
                "netlist_lint", "error" if has_layout else "warning", rel,
                None, "device_not_lvs_ok", [ref],
                f"{ref} (line {line}): {why}", "devicelib"))
        why = devicelib.tt_problem(model, table) if tt else None
        if why:
            out.append(checklib.violation(
                "netlist_lint", "error", rel, None, "device_not_tt_legal",
                [ref], f"{ref} (line {line}) in a TT tile: {why}",
                "devicelib"))
    return out


def dry_run_violations(ws: Path, timeout: float = DRY_RUN_TIMEOUT) -> list[dict]:
    """Reuse sim_run's own bench materialization at the single 'tt' corner,
    scoring ONLY simlib.detect_engine_errors - bounds are out of scope for
    this gate (sim_tt's own job). skip_unscored=False: with bounds out of
    scope, every bench is dry-run, whatever corners its bounds name."""
    result = sim_run.run_workspace_benches(
        ws, corner_names=["tt"], timeout=timeout, check="netlist_lint",
        skip_unscored=False)
    violations = []
    for r in result["results"]:
        if r["engine_errors"]:
            tail = r["violations"]
            violations.extend(v for v in tail
                              if v["kind"].startswith("sim_engine_error"))
    return violations


def run(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--timeout", type=float, default=None,
                    help="ngspice limit per run, s (default: the spec's "
                    f"sim_timeout_s.netlist_lint, else {DRY_RUN_TIMEOUT:g} s)")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    timeout = speclib.sim_timeout(spec, "netlist_lint", args.timeout,
                                  DRY_RUN_TIMEOUT)
    netlist_path = sim_run.find_netlist(ws)
    t_root = sim_run.toolchain_root()
    pdk_root = sim_run.pdk_root(t_root)

    violations = static_violations(ws, spec, netlist_path, pdk_root)
    violations += dry_run_violations(ws, timeout=timeout)

    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this gate ("netlist" for ade's netlist_lint).
    payload = checklib.report(SCRIPT, ws / "netlist", violations,
                              top=spec.get("top"),
                              netlist=str(netlist_path.relative_to(ws)))
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""check_analog_em_ir.py - the ade `em_ir` gate: electromigration and static
IR drop on the extracted layout (issue #120).

    check_analog_em_ir.py --workspace DIR [--block NAME] [--timeout SEC] [--out FILE]

Regenerates the GDS fresh (layout_gen.build(), as pex_sim does) and extracts
it with magic, parasitics on and extresist's FastHenry geometry written too
(layoutlib.run_magic_extract(geometry=True)): `<top>.pex.spice` has every
wire as resistors between `<net>.tN`/`<net>.nN` nodes, `<top>.fh` the same
wires as segments with a width and a layer thickness.

Then it runs the block's operating-point bench, `tb/<block>_emir_tb.cir` or
`layout_ref/<block>_emir_tb.cir`, on that netlist at every corner of the
spec's sweep (sim_run.spec_sweep, the corners sim_pvt runs). The bench sets
the block at its heaviest DC state - the code, the bias, the load that draws
the most supply current - and instantiates it as `xdut`; it has no .control
block, the gate adds `op` and `print all`. It takes the same tokens as the
pex_sim bench ({pdk}, {extracted}, {corner}, {res_corner}, {mim_corner},
{temp_c}, {vdd}).

From each operating point (emirlib.py has the detail):
  EM, wires and cuts, against engine/reference/em_limits.yaml (the GF180MCU
    DRM's DC limits at 125 C; never edited per block). Each net is first
    held to a bound that needs no position from magic's FastHenry file: the
    current into the net over its narrowest metal width, and over one cut.
    A net inside that passes outright. A net over it is solved segment by
    segment (each metal segment's |I|/width) and its cut arrays are counted
    on the GDS (each array charged with every current that can cross it,
    shared by its cuts); either over its limit is a finding.
  IR, each supply rail's worst |V(node) - V(pin)| over its extracted nodes,
    against spec.yaml's `ir_drop_mv` (default 2 % of supply.vdd). The rails
    are spec.yaml's `ir_rails`, else the block's pins named like a key of
    `supply` or vss/gnd. A block with no supply pin (a passive ladder) sets
    `ir_rails: []`: EM alone is checked and the report's `ir_checked` is
    false; with no rail found and no such line, the gate refuses.

Findings: em_wire_over, em_cut_over, ir_drop_over. A bench that is missing,
has a .control block, or has no `xdut`, a rail the block does not have, a
current magic's geometry cannot place, or a cut magic puts where the GDS
draws none: each is a refusal (exit 2), never a pass.
"""
from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import emirlib  # noqa: E402
import layoutlib  # noqa: E402
import layout_gen  # noqa: E402
import sim_run  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_analog_em_ir"
CHECK = "em_ir"
DEFAULT_TIMEOUT = 120.0
IR_DEFAULT_PCT = 2.0
INST = "xdut"


def find_bench(ws: Path, block: str) -> Path:
    for base in (ws / "tb", ws / "layout_ref"):
        cir = base / f"{block}_emir_tb.cir"
        if cir.is_file():
            return cir
    raise CheckError(
        f"no em_ir bench for {block!r}: looked for tb/{block}_emir_tb.cir "
        f"and layout_ref/{block}_emir_tb.cir. Write one (the bench-writer): "
        "the block at its heaviest DC state, instantiated as xdut, no "
        ".control block - a gate with nothing to run is a refusal")


def dut_line(bench_text: str) -> tuple[list[str], str]:
    """(outer nodes, subckt) of the bench's `xdut` instance."""
    for line in emirlib._logical_lines(bench_text):
        tok = line.split()
        if tok and tok[0].lower() == INST:
            nodes = [t for t in tok[1:] if "=" not in t]
            if len(nodes) < 2:
                break
            return nodes[:-1], nodes[-1]
    raise CheckError(f"the em_ir bench has no `{INST} <nodes> <subckt>` line: "
                     "the gate reads the block's currents through that "
                     "instance")


def with_op(bench_text: str) -> str:
    if re.search(r"^\s*\.control\b", bench_text, re.M | re.I):
        raise CheckError("the em_ir bench has a .control block: leave it out, "
                         "the gate adds the `op` and `print all` it reads")
    m = list(re.finditer(r"^\s*\.end\s*$", bench_text, re.M | re.I))
    tail = ".control\nset noaskquit\nop\nprint all\n.endc\n"
    if not m:
        return bench_text.rstrip("\n") + "\n" + tail + ".end\n"
    at = m[-1].start()
    return bench_text[:at] + tail + bench_text[at:]


def ir_limit_v(spec: dict | None) -> tuple[float, str]:
    if spec is not None and spec.get("ir_drop_mv") is not None:
        return float(spec["ir_drop_mv"]) * 1e-3, "spec.yaml ir_drop_mv"
    vdd = ((spec or {}).get("supply") or {}).get("vdd")
    if isinstance(vdd, bool) or not isinstance(vdd, (int, float)):
        raise CheckError("spec.yaml has neither ir_drop_mv nor a numeric "
                         "supply.vdd to take the default IR bound from - run "
                         "spec_lint first")
    return vdd * IR_DEFAULT_PCT / 100.0, f"{IR_DEFAULT_PCT:g} % of supply.vdd"


def rails_of(spec: dict | None, pins: list[str]) -> list[str]:
    low = {p.lower(): p for p in pins}
    if spec is not None and spec.get("ir_rails") is not None:
        rails = [str(r) for r in spec["ir_rails"]]
        missing = [r for r in rails if r.lower() not in low]
        if missing:
            raise CheckError(f"spec.yaml ir_rails names {missing}, which the "
                             f"block's pins {pins} do not have")
        return [low[r.lower()] for r in rails]
    names = {k.lower() for k in ((spec or {}).get("supply") or {})}
    names |= {"vss", "gnd"}
    rails = [low[n] for n in low if n in names]
    if not rails:
        raise CheckError(
            f"no supply rail among the block's pins {pins}: name them in "
            "spec.yaml's ir_rails, or set `ir_rails: []` if the block has "
            "no supply pin at all (a passive ladder): EM is then checked "
            "alone and the report says IR was not")
    return rails


def analyse(corner_name, volts, fh, rnodes, resistors, clusters0, limits,
            rails, ir_v, rel_gds, topcell):
    """(violations, facts) of one operating point."""
    inj = emirlib.injections(resistors, volts)
    nets = sorted({emirlib.base_net(n) for n in inj})
    bounds, violations, orphans, worst = [], [], [], {"wire": None, "cut": None}
    clusters = copy.deepcopy(clusters0)
    for name in nets:
        b = emirlib.net_bound(name, inj, fh.get(name), limits)
        if b["i_total"] < emirlib.I_FLOOR_A:
            continue
        bounds.append(b)
        if b["wire_ratio"] <= 1.0 and b["cut_ratio"] <= 1.0:
            continue
        net = fh[name]
        placed, _folded = emirlib.place(net, inj, rnodes, resistors, volts)
        segs, leftover = emirlib.solve_net(net, placed, limits)
        if leftover > max(emirlib.I_FLOOR_A, 1e-3 * b["i_total"]):
            raise CheckError(
                f"at {corner_name}: {leftover:.3g} A of net {name} lands on "
                "a piece of it magic's geometry does not join to its port")
        b["tier"] = 2
        if b["wire_ratio"] > 1.0:
            ww = emirlib.worst_wire(segs, limits)
            if ww and (worst["wire"] is None
                       or ww[1] / ww[2] > worst["wire"][1] / worst["wire"][2]):
                worst["wire"] = ww
            for s, dens, lim in emirlib.wire_over(segs, limits):
                x = (s.xyz_a[0] + s.xyz_b[0]) / 2
                y = (s.xyz_a[1] + s.xyz_b[1]) / 2
                violations.append(checklib.violation(
                    CHECK, "error", rel_gds, topcell, "em_wire_over",
                    [name, corner_name],
                    f"{name} @ {corner_name}: a {s.width:g} um "
                    f"{'/'.join(s.layers)} wire near ({x:.2f}, {y:.2f}) um "
                    f"carries {abs(s.amps) * 1e3:.3g} mA = "
                    f"{dens * 1e-3:.3g} mA/um, over the {lim * 1e-3:.3g} "
                    "mA/um DC limit: widen it (or add a parallel strap)",
                    "emirlib", x_um=round(x, 3), y_um=round(y, 3),
                    width_um=s.width, amps=abs(s.amps)))
        if b["cut_ratio"] > 1.0:
            orphans += emirlib.assign_cuts(net, segs, placed, clusters,
                                           limits, cap=b["i_total"])
    for c, per, lim in emirlib.cuts_over(clusters, limits):
        name = ",".join(sorted(c.nets))
        violations.append(checklib.violation(
            CHECK, "error", rel_gds, topcell, "em_cut_over",
            [name, corner_name, f"{c.gds[0]}/{c.gds[1]}"],
            f"{name} @ {corner_name}: the {c.cuts}-cut array on GDS layer "
            f"{c.gds[0]}/{c.gds[1]} at ({c.box[0]:.2f}, {c.box[1]:.2f})-"
            f"({c.box[2]:.2f}, {c.box[3]:.2f}) um carries up to "
            f"{c.amps * 1e3:.3g} mA, {per * 1e3:.3g} mA a cut, over the "
            f"{lim * 1e3:.3g} mA DC limit: draw more cuts there",
            "emirlib", box_um=[round(v, 3) for v in c.box], cuts=c.cuts,
            amps=c.amps))
    wc = emirlib.worst_cut(clusters, limits)
    if wc:
        worst["cut"] = wc
    ir = emirlib.ir_drops(volts, rails)
    for rail in rails:
        drop, node = ir.get(rail, (0.0, rail))
        if drop > ir_v:
            violations.append(checklib.violation(
                CHECK, "error", rel_gds, topcell, "ir_drop_over",
                [rail, corner_name],
                f"{rail} @ {corner_name}: {drop * 1e3:.3g} mV from the pin "
                f"to {node}, over the {ir_v * 1e3:.3g} mV bound: widen the "
                "rail or feed it from more sides", "emirlib",
                drop_mv=round(drop * 1e3, 4), node=node))
    top = sorted(bounds, key=lambda b: -max(b["wire_ratio"], b["cut_ratio"]))
    facts = {
        "nets": [{k: (round(v, 4) if isinstance(v, float) and k.endswith("ratio")
                      else v) for k, v in b.items()} for b in top[:8]],
        "ir_mv": {r: round(ir.get(r, (0.0, r))[0] * 1e3, 4) for r in rails},
        "ir_node": {r: ir.get(r, (0.0, r))[1] for r in rails},
        "worst_wire": None if worst["wire"] is None else {
            "net": worst["wire"][0].net, "mA_per_um": worst["wire"][1] * 1e-3,
            "limit": worst["wire"][2] * 1e-3},
        "worst_cut": None if worst["cut"] is None else {
            "gds": list(worst["cut"][0].gds), "cuts": worst["cut"][0].cuts,
            "mA_per_cut": worst["cut"][1] * 1e3,
            "limit": worst["cut"][2] * 1e3},
        "cuts_located_off_magic": orphans,
    }
    return violations, facts


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--block", help="override state.json's 'block'")
    ap.add_argument("--timeout", type=float, default=None,
                    help="ngspice limit per corner, s (default: the spec's "
                    f"sim_timeout_s.em_ir, else {DEFAULT_TIMEOUT:g} s)")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec_path = ws / "spec" / "spec.yaml"
    if not spec_path.is_file() and (ws / "spec.yaml").is_file():
        spec_path = ws / "spec.yaml"
    spec = speclib.load_spec(spec_path) if spec_path.is_file() else None
    timeout = speclib.sim_timeout(spec, "em_ir", args.timeout, DEFAULT_TIMEOUT)
    block = layout_gen.block_of(ws, args.block)
    bench_tpl = find_bench(ws, block)
    limits = emirlib.load_limits()
    ir_v, ir_source = ir_limit_v(spec)

    import check_analog_lvs
    import check_pex_sim
    ref_path = check_analog_lvs.find_reference_netlist(ws, block)
    ref_text = ref_path.read_text(encoding="utf-8", errors="replace")
    ref_cell = check_analog_lvs.reference_cell(ws, block, ref_text)

    gds_path, topcell, _abs, _abstract = layout_gen.build(ws, block)
    rel_gds = str(gds_path.relative_to(ws))
    work_dir = ws / "log" / "em_ir"
    work_dir.mkdir(parents=True, exist_ok=True)
    raw, _log = layoutlib.run_magic_extract(
        work_dir, gds_path.resolve(), topcell, parasitics=True, geometry=True)
    raw_text = raw.read_text(encoding="utf-8", errors="replace")
    resistors = emirlib.subckt_resistors(raw_text, topcell)
    if not resistors:
        raise CheckError(f"{raw.name} has no wire resistor: magic split no "
                         "net, so no current density can be read off it")
    ref_pins = check_pex_sim.pins_of(ref_text, ref_cell)
    extracted = work_dir / f"{topcell}.pex.ordered.spice"
    layoutlib.fresh(extracted)
    extracted.write_text(check_pex_sim.reorder_pins(raw_text, topcell, ref_pins),
                         encoding="utf-8")
    fh = emirlib.parse_fh((work_dir / f"{topcell}.fh").read_text(
        encoding="utf-8", errors="replace"))
    rnodes = emirlib.parse_rnodes((work_dir / f"{topcell}.res.ext").read_text(
        encoding="utf-8", errors="replace"))
    try:
        clusters = emirlib.cut_clusters(gds_path, topcell, limits)
    except emirlib.EmirError as exc:
        raise CheckError(str(exc)) from exc
    rails = rails_of(spec, ref_pins)

    if spec is None:
        sweep = [dict(check_pex_sim.BASE, name="tt")]
        nominal_vdd = None
    else:
        sweep, _scoped = sim_run.spec_sweep(ws)
        nominal_vdd = (spec.get("supply") or {}).get("vdd")
    template = bench_tpl.read_text(encoding="utf-8")
    pdk = layoutlib.pdk_root()
    rel_bench = str(bench_tpl.relative_to(ws))

    violations, by_corner = [], {}
    for corner in sweep:
        cname = corner["name"]
        text = with_op(template.format(
            pdk=pdk, extracted=extracted.resolve(),
            **check_pex_sim.corner_tokens(corner, nominal_vdd)))
        outer, cell = dut_line(text)
        if cell.lower() != topcell.lower() and cell.lower() != ref_cell.lower():
            raise CheckError(f"{rel_bench}'s {INST} is a {cell}, not the "
                             f"extracted {topcell}")
        if len(outer) != len(ref_pins):
            raise CheckError(f"{rel_bench}'s {INST} has {len(outer)} nodes; "
                             f"{topcell} has pins {ref_pins}")
        bench = work_dir / f"{block}_emir_tb__{cname}.cir"
        layoutlib.fresh(bench)
        bench.write_text(text, encoding="utf-8")
        try:
            out = layoutlib.run_ngspice(bench, cwd=work_dir, timeout=timeout)
        except layoutlib.LayoutError as exc:
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise CheckError(f"at corner {cname}: " + speclib.timeout_hint(
                    "em_ir", timeout)) from exc
            raise CheckError(f"at corner {cname}: {exc}") from exc
        op = layoutlib.parse_ngspice_prints(out)
        try:
            volts = emirlib.dut_voltages(op, INST, ref_pins, outer)
            v, facts = analyse(cname, volts, fh, rnodes, resistors, clusters,
                               limits, rails, ir_v, rel_gds, topcell)
        except emirlib.EmirError as exc:
            raise CheckError(f"at corner {cname}: {exc}") from exc
        violations += v
        by_corner[cname] = facts

    worst_ir = {r: max(f["ir_mv"][r] for f in by_corner.values())
                for r in rails}
    payload = checklib.report(
        SCRIPT, ws / "layout", violations, topcell=topcell, gds=rel_gds,
        bench=rel_bench, extracted=str(extracted.relative_to(ws)),
        limits=str(emirlib.REFERENCE.relative_to(ENGINE.parent)),
        temperature_c=limits["temperature_c"], rails=rails,
        ir_checked=bool(rails),
        ir_bound_mv=round(ir_v * 1e3, 4), ir_bound_source=ir_source,
        ir_worst_mv=worst_ir, corners=list(by_corner), by_corner=by_corner)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

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

  Resistors (issue #268), each PDK resistor instance: its body current
    (`@b.xdut.<inst>.brb[i]`) over its drawn width against em_limits.yaml's
    `resistors:` body limit (PRES.8/LRES.8/HRES.11/PL.10, 1 mA/um; null
    for the diffusion and nwell ones, reported unrated), and the current
    through each head's contact row (found on the GDS) per contact.

A block whose current is not DC also has `<block>_emir_tran_tb.cir` (same
tokens and xdut, a `.tran` line, no .control): run through whole periods
of its heaviest steady activity (tstart past the settling), max step at
most the fastest edge / 25. Every wire, cut, body and head row is then held
over the window to em_limits.yaml's `pulsed:` rules: mean |I|, I_rms and
the duty-scaled I_peak, each against the DC limit (the derating is stated
there). A resistor's bulk terminal (its well or substrate pin), and both
heads of a dummy resistor (both on one net), carry only the body's
displacement current or a strap's own drop, and magic writes them at the
device: they are handed on along the extracted resistors, never refused
for distance (emirlib.handed_on_nodes). So is a MOS's bulk terminal, and
the deviceless well or substrate pieces magic splits off between such
terminals, far from any metal of the net (emirlib.body_nodes).

Findings: em_wire_over, em_cut_over, ir_drop_over, em_res_body_over,
em_res_contact_over, and in the transient em_wire_over / em_cut_over /
em_res_body_over / em_res_contact_over (average) with their _rms and _peak
forms (em_wire_rms_over, em_cut_peak_over, em_res_body_rms_over, ...). A
bench that is missing, has a .control block, or has no `xdut`, a rail the
block does not have, a current magic's geometry cannot place, a resistor
ngspice gives no body current for, or a cut magic puts where the GDS draws
none: each is a refusal (exit 2), never a pass.
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


def _with_control(bench_text: str, tail: str, what: str) -> str:
    if re.search(r"^\s*\.control\b", bench_text, re.M | re.I):
        raise CheckError(f"the em_ir {what} bench has a .control block: leave "
                         "it out, the gate adds the analysis and the output "
                         "it reads")
    m = list(re.finditer(r"^\s*\.end\s*$", bench_text, re.M | re.I))
    if not m:
        return bench_text.rstrip("\n") + "\n" + tail + ".end\n"
    at = m[-1].start()
    return bench_text[:at] + tail + bench_text[at:]


# significant digits of the op print (with_op)
NUMDGT = 12


def with_op(bench_text: str, probes=()) -> str:
    """The op bench with `op`, `print all` (every node voltage) and a print
    of each resistor body current probe, at NUMDGT significant digits.
    ngspice's default 6 leaves a node voltage good to about 1 uV, and
    injections() turns that into the current of each extracted resistor:
    over a few ohms of well or strap it is tens of nA of rounding, enough
    to give a node with no device on it a current above I_FLOOR_A and so a
    position to defend. At 12 digits the rounding is a few pA."""
    prints = "".join(f"print {p}\n" for p in probes)
    return _with_control(
        bench_text, f".control\nset noaskquit\nset numdgt={NUMDGT}\nop\n"
        "print all\n" + prints
        + ".endc\n", "op")


def find_tran_bench(ws: Path, block: str):
    """The optional transient bench (issue #268), or None."""
    for base in (ws / "tb", ws / "layout_ref"):
        cir = base / f"{block}_emir_tran_tb.cir"
        if cir.is_file():
            return cir
    return None


def with_tran(bench_text: str, raw_name: str, probes=()) -> str:
    """The transient bench with every vector (and each body probe) saved
    and written as an ascii raw file. The bench's own `.tran` line sets the
    window: it must cover whole periods of the pulsed currents."""
    if not re.search(r"^\s*\.tran\b", bench_text, re.M | re.I):
        raise CheckError("the em_ir transient bench has no .tran line: it "
                         "sets the window the pulsed currents are averaged "
                         "over")
    save = " ".join(["all", *probes])
    return _with_control(
        bench_text, ".control\nset noaskquit\nset filetype=ascii\n"
        f"save {save}\nrun\nwrite {raw_name}\n.endc\n", "transient")


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
            rails, ir_v, rel_gds, topcell, bulk=frozenset()):
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
        placed, _folded = emirlib.place(net, inj, rnodes, resistors, volts,
                                        bulk=bulk)
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


RULE_WORDS = {"": "DC/average", "_rms": "RMS", "_peak": "duty-scaled peak"}


def _stats(series, weights):
    """(mean |I|, rms, peak |I|) of a scalar (an op: all three |I|) or of a
    series over a transient's timepoints."""
    if weights is None:
        a = abs(float(series))
        return a, a, a
    m, r, p = emirlib.wave_stats(series, weights)
    return float(m), float(r), float(p)


def _hits(stats, limit, limits, tran):
    """[(kind suffix, ratio)] of the rules `stats` breaks against `limit`;
    an op has one rule, the DC one."""
    if not tran:
        return ([("", stats[0] / limit)] if stats[0] > limit else [])
    return emirlib.pulsed_hits(*stats, limit, limits)[0]


def _amount(stats, suffix, tran):
    """The current a finding names: |I| (op), the mean, rms or peak."""
    if not tran:
        return "carries", stats[0]
    return {"": ("carries on average", stats[0]),
            "_rms": ("carries an RMS", stats[1]),
            "_peak": ("peaks at", stats[2])}[suffix]


def check_resistors(corner_name, cur, insts, heads, limits, rel_gds,
                    topcell, weights=None):
    """(violations, facts) of the PDK resistors at one corner: each body's
    |I|/W against its em_limits.yaml body limit, and each head row's
    current per contact (issue #268). `cur` is {instance: body current},
    a number (op) or a series (transient, with its trapezoid `weights`)."""
    import numpy as np

    tran = weights is not None
    violations, bodies, unrated = [], [], []
    for r in insts:
        if r.name not in cur:
            raise CheckError(f"at {corner_name}: ngspice gave no body current "
                             f"for resistor {r.name} ({r.model})")
        if r.rule["body"] is None:
            unrated.append(r.name)
            continue
        st = _stats(cur[r.name], weights)
        limit = r.rule["body"] * r.width_um * 1e-6
        bodies.append({"inst": r.name, "model": r.model,
                       "width_um": round(r.width_um, 4),
                       "ratio": round(max(st[0], emirlib.pulsed_stat(
                           *st, limits) if tran else 0.0) / limit, 4)})
        for suffix, ratio in _hits(st, limit, limits, tran):
            word, amps = _amount(st, suffix, tran)
            violations.append(checklib.violation(
                CHECK, "error", rel_gds, topcell, f"em_res_body{suffix}_over",
                [r.name, corner_name],
                f"{r.name} @ {corner_name}: the {r.width_um:g} um wide "
                f"{r.model} body {word} {amps * 1e3:.3g} mA = "
                f"{amps / (r.width_um * 1e-6) * 1e-3:.3g} mA/um, over its "
                f"{RULE_WORDS[suffix]} limit ({r.rule['rule']}: "
                f"{r.rule['body'] * 1e-3:.3g} mA/um DC"
                f"{', pulsed: em_limits.yaml' if suffix else ''}): widen "
                "the resistor, lengthening it in step to keep its value",
                "emirlib", inst=r.name, model=r.model,
                width_um=r.width_um, amps=amps, ratio=round(ratio, 4)))
    by_key = {}
    for r in insts:
        if r.rule["head"] is None:
            continue
        a = np.abs(cur[r.name]) if tran else abs(float(cur[r.name]))
        by_key[r.key] = a if r.key not in by_key else np.maximum(by_key[r.key], a)
    worst_head = None
    for h in heads:
        total = emirlib.head_current(h, by_key)
        st = _stats(total, weights)
        if st[2] < emirlib.I_FLOOR_A:
            continue
        if not h["cuts"]:
            raise CheckError(
                f"at {corner_name}: the {h['model']} shape at "
                f"({h['box'][0]:.2f}, {h['box'][1]:.2f}) um carries "
                f"{st[2] * 1e3:.3g} mA and the GDS draws no contact in it: "
                "its heads cannot be checked")
        n = min(h["cuts"])
        per = tuple(v / n for v in st)
        ratio = max(per[0], emirlib.pulsed_stat(*per, limits)
                    if tran else 0.0) / h["limit"]
        if worst_head is None or ratio > worst_head["ratio"]:
            worst_head = {"model": h["model"], "box_um": h["box"],
                          "cuts": n, "mA_per_cut": round(per[0] * 1e3, 6),
                          "limit_mA": h["limit"] * 1e3,
                          "ratio": round(ratio, 4)}
        for suffix, rr in _hits(per, h["limit"], limits, tran):
            word, amps = _amount(per, suffix, tran)
            b = h["box"]
            violations.append(checklib.violation(
                CHECK, "error", rel_gds, topcell,
                f"em_res_contact{suffix}_over",
                [h["model"], corner_name, f"{b[0]:.2f},{b[1]:.2f}"],
                f"{h['model']} @ {corner_name}: the {n}-contact head row of "
                f"the resistor at ({b[0]:.2f}, {b[1]:.2f})-({b[2]:.2f}, "
                f"{b[3]:.2f}) um {word} {amps * 1e3:.3g} mA a contact, over "
                f"the {RULE_WORDS[suffix]} limit of {h['cut']} "
                f"({h['limit'] * 1e3:.3g} mA DC): draw more contacts in "
                "the head (widen it)", "emirlib", box_um=b, cuts=n,
                amps=amps * n, ratio=round(rr, 4)))
    bodies.sort(key=lambda b: -b["ratio"])
    return violations, {"bodies": bodies[:6], "worst_head": worst_head,
                        "unrated": unrated}


def _series_of(names, arr):
    """({lowercased vector: series}, time) of a raw file: `v(x)` as `x`
    (what `print all` names it), `i(@b...)` as the probe `@b...`. A source's
    branch current keeps its `i(x)` name: stripped, `i(vdd)` would overwrite
    the voltage of node vdd."""
    out, t = {}, None
    for j, n in enumerate(names):
        if n == "time":
            t = arr[:, j]
        elif n.startswith("v(") and n.endswith(")"):
            out[n[2:-1]] = arr[:, j]
        elif n.startswith("i(@") and n.endswith(")"):
            out[n[2:-1]] = arr[:, j]
        else:
            out[n] = arr[:, j]
    if t is None:
        raise CheckError("the transient raw file has no time vector")
    return out, t


def analyse_tran(corner_name, t, volts, fh, rnodes, resistors, clusters0,
                 limits, rel_gds, topcell, bulk=frozenset()):
    """(violations, facts) of one transient: each net's and cut array's
    mean, RMS and duty-scaled peak current against em_limits.yaml's pulsed
    rules (issue #268). Tier one holds each net's pulsed_stat of its
    i_total(t) to the DC bound; a net over it is solved at every timepoint
    (one factorised system per net), its cuts charged per timepoint."""
    import numpy as np

    w = emirlib.trapezoid_weights(t)
    T = len(t)
    inj = emirlib.injections(resistors, volts)
    nets = sorted({emirlib.base_net(n) for n in inj})
    bounds, violations, orphans = [], [], {}
    charge = {}                       # id(cluster) -> [cluster, series, nets]
    clusters = copy.deepcopy(clusters0)
    worst = None
    for name in nets:
        mine = {n: np.broadcast_to(np.asarray(a, dtype=float), (T,))
                for n, a in inj.items() if emirlib.base_net(n) == name}
        i_t = sum(np.maximum(a, 0.0) for a in mine.values())
        st = _stats(i_t, w)
        if st[2] < emirlib.I_FLOOR_A:
            continue
        b = emirlib.net_bound(name, inj, fh.get(name), limits,
                              i_total=emirlib.pulsed_stat(*st, limits))
        b.update(i_mean=st[0], i_rms=st[1], i_peak=st[2])
        bounds.append(b)
        if b["wire_ratio"] <= 1.0 and b["cut_ratio"] <= 1.0:
            continue
        b["tier"] = 2
        net = fh[name]
        system = emirlib.NetSystem(net, limits)
        vnet = {n: np.broadcast_to(np.asarray(v, dtype=float), (T,))
                for n, v in volts.items() if emirlib.base_net(n) == name}
        cache, placements = {}, []
        for k in range(T):
            pk, _f = emirlib.place(
                net, {n: float(a[k]) for n, a in mine.items()}, rnodes,
                resistors, {n: float(v[k]) for n, v in vnet.items()}, cache,
                bulk)
            placements.append(pk)
        amps, leftover = system.solve_many(placements)
        bad = leftover > np.maximum(emirlib.I_FLOOR_A, 1e-3 * i_t)
        if bad.any():
            k = int(np.argmax(bad))
            raise CheckError(
                f"at {corner_name}, t = {t[k]:.4g} s: {leftover[k]:.3g} A of "
                f"net {name} lands on a piece of it magic's geometry does "
                "not join to its port")
        if b["wire_ratio"] > 1.0:
            mean, rms, peak = emirlib.wave_stats(amps, w)
            for s, i in zip(system.segs(np.ones(len(system.edges))),
                            range(len(system.edges))):
                hit = emirlib._wire_density(s, limits)
                if hit is None:
                    continue
                per_a, lim = hit
                sst = (mean[i] * per_a, rms[i] * per_a, peak[i] * per_a)
                ratio = emirlib.pulsed_stat(*sst, limits) / lim
                if worst is None or ratio > worst["ratio"]:
                    worst = {"net": name, "width_um": s.width,
                             "mA_per_um_mean": sst[0] * 1e-3,
                             "mA_per_um_rms": sst[1] * 1e-3,
                             "mA_per_um_peak": sst[2] * 1e-3,
                             "limit": lim * 1e-3, "ratio": round(ratio, 4)}
                for suffix, rr in _hits(sst, lim, limits, True):
                    word, dens = _amount(sst, suffix, True)
                    x = (s.xyz_a[0] + s.xyz_b[0]) / 2
                    y = (s.xyz_a[1] + s.xyz_b[1]) / 2
                    violations.append(checklib.violation(
                        CHECK, "error", rel_gds, topcell,
                        "em_wire_over" if not suffix
                        else f"em_wire{suffix}_over",
                        [name, corner_name, f"{x:.2f},{y:.2f}"],
                        f"{name} @ {corner_name} (transient): a {s.width:g} "
                        f"um {'/'.join(s.layers)} wire near ({x:.2f}, "
                        f"{y:.2f}) um {word} {dens * 1e-3:.3g} mA/um, over "
                        f"its {RULE_WORDS[suffix]} limit ({lim * 1e-3:.3g} "
                        "mA/um DC, derated as em_limits.yaml's pulsed: "
                        "says): widen it (or add a parallel strap)",
                        "emirlib", x_um=round(x, 3), y_um=round(y, 3),
                        width_um=s.width, density_mA_per_um=dens * 1e-3,
                        ratio=round(rr, 4)))
        if b["cut_ratio"] > 1.0:
            ctx = {}
            for k in range(T):
                if i_t[k] < emirlib.I_FLOOR_A:
                    continue
                ch, orph = emirlib.cut_charges(
                    net, system.segs(amps[:, k]), placements[k], clusters,
                    limits, ctx)
                for o in orph:
                    orphans.setdefault((o["net"], tuple(o["at"]), o["what"]),
                                       o)
                for cid, (c, a) in ch.items():
                    row = charge.setdefault(cid, [c, np.zeros(T), set()])
                    row[1][k] += min(a, i_t[k])
                    row[2].add(name)
    worst_cut = None
    for c, series, cnets in charge.values():
        lim = limits["cuts"][c.gds]["limit"]
        per = _stats(series / max(c.cuts, 1), w)
        ratio = emirlib.pulsed_stat(*per, limits) / lim
        if worst_cut is None or ratio > worst_cut["ratio"]:
            worst_cut = {"gds": list(c.gds), "cuts": c.cuts,
                         "mA_per_cut_mean": per[0] * 1e3,
                         "mA_per_cut_rms": per[1] * 1e3,
                         "mA_per_cut_peak": per[2] * 1e3,
                         "limit": lim * 1e3, "ratio": round(ratio, 4)}
        nm = ",".join(sorted(cnets))
        for suffix, rr in _hits(per, lim, limits, True):
            word, amps = _amount(per, suffix, True)
            violations.append(checklib.violation(
                CHECK, "error", rel_gds, topcell,
                "em_cut_over" if not suffix else f"em_cut{suffix}_over",
                [nm, corner_name, f"{c.gds[0]}/{c.gds[1]}",
                 f"{c.box[0]:.2f},{c.box[1]:.2f}"],
                f"{nm} @ {corner_name} (transient): the {c.cuts}-cut array "
                f"on GDS layer {c.gds[0]}/{c.gds[1]} at ({c.box[0]:.2f}, "
                f"{c.box[1]:.2f})-({c.box[2]:.2f}, {c.box[3]:.2f}) um {word} "
                f"{amps * 1e3:.3g} mA a cut, over its {RULE_WORDS[suffix]} "
                f"limit ({lim * 1e3:.3g} mA DC, derated as em_limits.yaml's "
                "pulsed: says): draw more cuts there", "emirlib",
                box_um=[round(v, 3) for v in c.box], cuts=c.cuts,
                amps=amps * c.cuts, ratio=round(rr, 4)))
    top = sorted(bounds, key=lambda b: -max(b["wire_ratio"], b["cut_ratio"]))
    facts = {
        "window_s": [float(t[0]), float(t[-1])], "timepoints": T,
        "nets": [{k: (round(v, 4) if isinstance(v, float) and k.endswith("ratio")
                      else v) for k, v in b.items()} for b in top[:8]],
        "worst_wire": worst, "worst_cut": worst_cut,
        "cuts_located_off_magic": list(orphans.values()),
    }
    return violations, facts


def check_dut(text, rel_bench, topcell, ref_cell, ref_pins) -> list:
    """The bench's xdut outer nodes, after checking it is the block."""
    outer, cell = dut_line(text)
    if cell.lower() != topcell.lower() and cell.lower() != ref_cell.lower():
        raise CheckError(f"{rel_bench}'s {INST} is a {cell}, not the "
                         f"extracted {topcell}")
    if len(outer) != len(ref_pins):
        raise CheckError(f"{rel_bench}'s {INST} has {len(outer)} nodes; "
                         f"{topcell} has pins {ref_pins}")
    return outer


def simulate(work_dir, name, text, cname, timeout) -> str:
    bench = work_dir / name
    layoutlib.fresh(bench)
    bench.write_text(text, encoding="utf-8")
    try:
        return layoutlib.run_ngspice(bench, cwd=work_dir, timeout=timeout)
    except layoutlib.LayoutError as exc:
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            raise CheckError(f"at corner {cname}: " + speclib.timeout_hint(
                "em_ir", timeout)) from exc
        raise CheckError(f"at corner {cname}: {exc}") from exc


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
    try:
        insts = emirlib.resistor_instances(raw_text, topcell, limits)
        for sub in re.findall(r"^\s*\.subckt\s+(\S+)", raw_text, re.M | re.I):
            if sub.lower() != topcell.lower() and emirlib.resistor_instances(
                    raw_text, sub, limits):
                raise CheckError(
                    f"{raw.name} has PDK resistors inside subcell {sub}: "
                    "the gate reads body currents of the top cell's own "
                    "resistors only; flatten the extraction")
        ext_path = work_dir / f"{topcell}.ext"
        heads = emirlib.resistor_heads(
            gds_path, topcell, insts,
            emirlib.ext_resistors(ext_path.read_text(
                encoding="utf-8", errors="replace")) if insts else [],
            limits)
    except emirlib.EmirError as exc:
        raise CheckError(str(exc)) from exc
    probes = [r.probe(INST) for r in insts]
    _terms, mos_bulks = emirlib.device_terminals(raw_text, topcell)
    bulk = emirlib.body_nodes(emirlib.handed_on_nodes(insts, mos_bulks),
                              _terms, resistors, fh, rnodes)
    tran_tpl = find_tran_bench(ws, block)

    if spec is None:
        sweep = [dict(check_pex_sim.BASE, name="tt")]
        nominal_vdd = None
    else:
        sweep, _scoped = sim_run.spec_sweep(ws)
        nominal_vdd = (spec.get("supply") or {}).get("vdd")
    template = bench_tpl.read_text(encoding="utf-8")
    pdk = layoutlib.pdk_root()
    rel_bench = str(bench_tpl.relative_to(ws))
    rel_tran = None if tran_tpl is None else str(tran_tpl.relative_to(ws))

    violations, by_corner = [], {}
    for corner in sweep:
        cname = corner["name"]
        tokens = check_pex_sim.corner_tokens(corner, nominal_vdd)
        text = with_op(template.format(
            pdk=pdk, extracted=extracted.resolve(), **tokens), probes)
        outer = check_dut(text, rel_bench, topcell, ref_cell, ref_pins)
        out = simulate(work_dir, f"{block}_emir_tb__{cname}.cir", text,
                       cname, timeout)
        op = layoutlib.parse_ngspice_prints(out)
        try:
            volts = emirlib.dut_voltages(op, INST, ref_pins, outer)
            v, facts = analyse(cname, volts, fh, rnodes, resistors, clusters,
                               limits, rails, ir_v, rel_gds, topcell, bulk)
            missing = [p for p in probes if p not in op]
            if missing:
                raise CheckError(f"at corner {cname}: ngspice printed no "
                                 f"body current for {missing[:4]}")
            rv, rfacts = check_resistors(
                cname, {r.name: op[r.probe(INST)] for r in insts}, insts,
                heads, limits, rel_gds, topcell)
            violations += v + rv
            facts["resistors"] = rfacts
            if tran_tpl is not None:
                raw_name = f"{block}_emir_tran__{cname}.raw"
                ttext = with_tran(tran_tpl.read_text(encoding="utf-8").format(
                    pdk=pdk, extracted=extracted.resolve(), **tokens),
                    raw_name, probes)
                touter = check_dut(ttext, rel_tran, topcell, ref_cell,
                                   ref_pins)
                raw_path = work_dir / raw_name
                layoutlib.fresh(raw_path)
                simulate(work_dir, f"{block}_emir_tran_tb__{cname}.cir",
                         ttext, cname, timeout)
                if not raw_path.is_file():
                    raise CheckError(f"at corner {cname}: the transient "
                                     f"bench wrote no {raw_name}")
                series, t = _series_of(*emirlib.parse_ascii_raw(
                    raw_path.read_text(encoding="utf-8", errors="replace")))
                tvolts = emirlib.dut_voltages(series, INST, ref_pins, touter)
                tv, tfacts = analyse_tran(cname, t, tvolts, fh, rnodes,
                                          resistors, clusters, limits,
                                          rel_gds, topcell, bulk)
                missing = [p for p in probes if p not in series]
                if missing:
                    raise CheckError(f"at corner {cname}: the transient "
                                     f"saved no body current for "
                                     f"{missing[:4]}")
                trv, trf = check_resistors(
                    cname, {r.name: series[r.probe(INST)] for r in insts},
                    insts, heads, limits, rel_gds, topcell,
                    emirlib.trapezoid_weights(t))
                violations += tv + trv
                tfacts["resistors"] = trf
                facts["tran"] = tfacts
        except emirlib.EmirError as exc:
            raise CheckError(f"at corner {cname}: {exc}") from exc
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
        ir_worst_mv=worst_ir, resistors=len(insts),
        resistors_unrated=sorted({r.model for r in insts
                                  if r.rule["body"] is None}),
        resistor_heads=len(heads), tran_bench=rel_tran,
        pulsed_checked=tran_tpl is not None,
        pulsed=limits["pulsed"] if tran_tpl is not None else None,
        corners=list(by_corner), by_corner=by_corner)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

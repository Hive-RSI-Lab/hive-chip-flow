#!/usr/bin/env python
"""check_pex_sim.py - the ade `pex_sim` gate (docs/design.md 1.5, 5,
"### M9.").

    check_pex_sim.py --workspace DIR [--block NAME] [--timeout SEC] [--out FILE]

Regenerates the GDS fresh (layout_gen.build(), same discipline as
check_analog_drc.py/check_analog_lvs.py), extracts it with magic including
parasitics - every capacitor down to 0 fF, and the wire resistance of
every net that reaches a transistor (layoutlib.run_magic_extract) - and
refuses a netlist that came back with no R and no C line at all, since a
"post-layout" run on that is the schematic again. extresist drops every
coupling C of a net with no label, so run_magic_extract labels each net
magic would name itself (a_x_y#) before extresist runs, and refuses a
netlist in which a pair of nets still lost the coupling C magic's .ext
gives it: an optimistic netlist is never benched. klayout_pex as the second
extractor is not wired up at M9.

Then it runs the block's post-layout bench, `tb/<block>_pex_tb.cir` or
`layout_ref/<block>_pex_tb.cir`, against the extracted netlist at each
corner of its corner set (below), and checks every value the bench prints
(`print` or `meas` in its .control block, "name = value") against the
same-stem `.bounds.json`'s `measures` map ({name: {min, max}}, or a list of
those for a measure with a different bound per corner). A bench here should
measure what parasitics move - a pole, a delay, a settling time - not only
a DC point that a wire's capacitance cannot touch.

Corner set. Extraction runs once; only the bench repeats, so the cost is
one ngspice run per corner. The corners come from spec.yaml's `corners`
through sim_run.spec_sweep, the expansion sim_pvt uses, and `pex_corners`
picks among them:
  - absent: typical, the sweep's slow/hot/low-supply corner, and for each
    bounded measure sim_pvt also measured, the corner where sim_pvt's value
    came closest to (or past) this bound. Slow/hot is always in because
    sim_pvt ran the schematic: it never saw the wire R and C, so a measure
    it found safest at slow can still be the one a long route breaks there.
    With no fresh sim_pvt record to read, the whole sweep.
  - a list of corner names (any name corners.names_of accepts): those, and
    typical, which is never dropped.
  - "all": the whole sweep.
A bound may carry a `corners` list, as a sim sidecar's does (simlib.
scored_at); such a bound is scored only there, and the corners it names join
the set, so no bound goes unscored because of the selection. A bound naming
a corner outside the sweep is `sim_bound_not_swept`. A corner no bound is
scored at is skipped and listed in `not_scored`.

Every finding names its corner. The report keeps `measured` (the typical
corner's values, or the first corner run when no bound is scored at typical),
and adds `corners`, `corner_source`, `measured_by_corner` and `not_scored`.

RC knobs. At the corners above the wire R and C are the same; magic extracts
at its nominal style. spec.yaml's `pex_rc_knobs` ("all" or a list of names,
absent means none, since each knob is one more ngspice run) adds runs on a
copy of the extracted netlist with every C and R line scaled by the factors
pinned in engine/reference/pex_rc_knobs.yaml. Each knob pairs with one corner:
a `pair: ss` knob with the sweep's slow, hot, low-supply corner, a `pair: ff`
knob with its fast, cold, high-supply corner, whether or not the corner set
above holds that corner. The scaling is an approximation and not a foundry RC
corner, as the table says: one factor for all layers, and vias move as little
as wires. (The PDK's magic tech does hold RC variants, `ngspice(hrhc)` and its
siblings; this gate does not extract at them.) Findings name the knob too, in refs and in the message, and the report
adds `rc_knobs`, each knob's corner, factors and values. A knob with no bound
scored at its corner is a refusal: the run it asked for would measure nothing.

The bench template carries substitution tokens, filled by str.format: `{pdk}`
(the resolved toolchain's PDK root) and `{extracted}` (the parasitic netlist
this run just produced), so it runs unchanged on any host, and per corner
`{corner}` (the transistor .lib section), `{res_corner}` and `{mim_corner}`
(the passive .lib sections, res_<p> and mimcap_<p>), `{temp_c}` and `{vdd}`.
A bench that hard-codes any of these while the corner set moves it would run
every corner as typical and pass them unseen, so it is a
`pex_bench_not_corner_aware` finding and nothing is run. `pex_corners:
[<typical>]` is the one way to keep a typical-only bench. The bench instantiates
the extracted cell positionally in the pin order of M8's netlist, and magic
numbers ports in an order of its own, so the extracted `.subckt` line is
rewritten into the reference's pin order first; a pin set that differs is
a refusal.

Ground is checked before the bench runs. A reference subckt that reaches
ngspice's global ground (a node `0`, or its alias `gnd`) without a port for
it needs the extracted netlist to carry a node of that name too - magic
names a net `0` when the layout puts a non-pin text label "0" on it (a
drawing-layer label such as metal1 34/0; a pin-layer label would make it a port and change the pin set).
Without one, the layout's ground is a local net of magic's own naming and
the bench runs with it floating. The gate never guesses which extracted net
is ground - a substrate net and a ground wire both carry magic-made names,
and tying the wrong one would pass a layout that is wired wrong - so a
missing ground label is a `ground_unlabelled` finding for the layout
generator, and no bench is run on that netlist.

Probes are checked the same way, before the bench runs. A bench that reads
a net inside the extracted cell (`v(xdut.s1)` in its .control block, where
`xdut` instantiates the top cell) needs that net to keep its name through
extraction, and magic keeps a name only where the layout puts a text label
on the net. An unlabelled net comes back as `a_..#`, and ngspice then
either warns that the vector is not available or refuses the whole run
with an error that names no net. So a probed net that the reference
netlist has and the extracted netlist does not is a `probe_unlabelled`
finding for the layout generator, one per net, and no bench is run. A
probe of a net the reference does not have either is a bench bug, not a
layout one, and is left to ngspice to refuse.

The bench run has a time limit: --timeout when given, else the spec's
`sim_timeout_s: {pex_sim: N}` (speclib.sim_timeout; a large RC netlist can
take minutes), else 120 s, per corner. A run past it is a refusal whose
remediation names that knob and the corner, never a pass.
"""
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import layoutlib  # noqa: E402
import layout_gen  # noqa: E402
import netlistlib  # noqa: E402
import sim_run  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
import statelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_pex_sim"
DEFAULT_TIMEOUT = 120.0


def find_bench(ws: Path, block: str) -> tuple[Path, Path]:
    for base in (ws / "tb", ws / "layout_ref"):
        cir = base / f"{block}_pex_tb.cir"
        bounds = base / f"{block}_pex_tb.bounds.json"
        if cir.is_file() and bounds.is_file():
            return cir, bounds
    raise CheckError(
        f"no pex_sim bench for {block!r}: looked for "
        f"tb/{block}_pex_tb.cir and layout_ref/{block}_pex_tb.cir, each "
        "with a matching .bounds.json - a gate with nothing to run is a "
        "refusal, never a pass")


def pins_of(netlist_text: str, cell: str) -> list[str] | None:
    pins = netlistlib.subckt_pins(netlist_text).get(cell.lower())
    if pins is None:
        return None
    return [p for p in pins if "=" not in p]


def reorder_pins(extracted_text: str, cell: str, want: list[str]) -> str:
    """The extracted netlist with `cell`'s .subckt pins put in `want`'s
    order. Only the header moves: a pin is a node name inside the subckt,
    so its position there changes nothing but how an instance binds."""
    have = pins_of(extracted_text, cell)
    if have is None:
        raise CheckError(f"the extracted netlist declares no .subckt {cell}")
    if sorted(p.lower() for p in have) != sorted(p.lower() for p in want):
        raise CheckError(
            f"the extracted {cell} has pins {have}, the reference has "
            f"{want} - a bench bound to the reference's pins would wire "
            "the layout wrong")
    by_lower = {p.lower(): p for p in have}
    head = re.compile(rf"^(\s*\.subckt\s+{re.escape(cell)})\s.*(?:\n[ \t]*\+.*)*",
                      re.IGNORECASE | re.MULTILINE)
    return head.sub(lambda m: m.group(1) + " " + " ".join(
        by_lower[p.lower()] for p in want), extracted_text, count=1)


# ngspice's global ground: node 0, and `gnd`, which it accepts as an alias
GROUND = {"0", "gnd"}

# node count per element letter; `x` is every token before the subckt name
NODE_COUNT = {"r": 2, "c": 2, "l": 2, "d": 2, "v": 2, "i": 2, "b": 2,
              "f": 2, "h": 2, "w": 2, "e": 4, "g": 4, "m": 4, "s": 4,
              "t": 4, "o": 4, "q": 3, "j": 3, "z": 3, "u": 3}


def element_nodes(netlist_text: str) -> set[str]:
    """Every node name (lowercased) an element line of netlist_text touches.
    Dot-cards and a .control block are not elements; a letter NODE_COUNT
    does not know contributes nothing rather than a guess."""
    nodes: set[str] = set()
    in_control = False
    for line in netlistlib.join_continuations(netlist_text):
        tokens = line.split()
        low = tokens[0].lower()
        if low.startswith(".control"):
            in_control = True
            continue
        if low.startswith(".endc"):
            in_control = False
            continue
        if in_control or low[0] in ".$;":
            continue
        rest = [t for t in tokens[1:] if "=" not in t]
        if low[0] == "x":
            nodes.update(t.lower() for t in rest[:-1])
        else:
            nodes.update(t.lower() for t in rest[:NODE_COUNT.get(low[0], 0)])
    return nodes


def ground_finding(ref_text: str, ref_pins: list[str], extracted_text: str,
                   topcell: str, rel_gds: str) -> dict | None:
    """A `ground_unlabelled` finding when the reference reaches global
    ground without a port for it and the extracted netlist has no node of
    that name; None when there is nothing to tie or the layout names it."""
    pins = {p.lower() for p in ref_pins}
    used = (element_nodes(ref_text) & GROUND) - pins
    if not used or element_nodes(extracted_text) & GROUND:
        return None
    return checklib.violation(
        "pex_sim", "error", rel_gds, topcell, "ground_unlabelled",
        sorted(used),
        f"the reference {topcell} ties devices to global ground "
        f"({', '.join(sorted(used))}) with no port for it, and the "
        "extracted netlist has no node 0 - the layout's ground is a local "
        "net of magic's own naming, so the bench would run with it "
        "floating. Fix in layout/gen_<block>.py (the layout-fixer): put a "
        "non-pin text label \"0\" on the ground net's drawing layer (e.g. "
        "metal1 34/0), "
        "not on a pin/label layer, which would add a port and change the "
        "pin set", "magic")


# a voltage probe in a .control block: v(), vdb(), vm(), vp(), vr(), vi()
PROBE_RE = re.compile(r"\bv(?:db|m|p|r|i)?\(([^()]*)\)", re.IGNORECASE)


def probed_nets(bench_text: str, topcell: str) -> set[str]:
    """Every net (lowercased) the bench's .control block probes one level
    inside an instance of `topcell` - `net` for `v(xdut.net)`. A top-level
    node or a deeper path is not the extracted cell's own net name."""
    lines = netlistlib.join_continuations(bench_text)
    insts = set()
    for line in lines:
        tokens = [t for t in line.split() if "=" not in t]
        if (len(tokens) > 1 and tokens[0][0] in "xX"
                and tokens[-1].lower() == topcell.lower()):
            insts.add(tokens[0].lower())
    nets: set[str] = set()
    in_control = False
    for line in lines:
        low = line.split()[0].lower()
        if low.startswith(".control"):
            in_control = True
        elif low.startswith(".endc"):
            in_control = False
        elif in_control:
            for m in PROBE_RE.finditer(line):
                for arg in m.group(1).split(","):
                    parts = arg.strip().lower().split(".")
                    if len(parts) == 2 and parts[0] in insts and parts[1]:
                        nets.add(parts[1])
    return nets


def probe_findings(bench_text: str, ref_text: str, extracted_text: str,
                   topcell: str, rel_gds: str) -> list[dict]:
    """A `probe_unlabelled` finding for each net the bench probes inside
    the top cell that the reference has and the extraction does not."""
    missing = ((probed_nets(bench_text, topcell) & element_nodes(ref_text))
               - element_nodes(extracted_text))
    return [checklib.violation(
        "pex_sim", "error", rel_gds, topcell, "probe_unlabelled", [net],
        f"the pex bench probes {topcell}'s net {net!r}, which the reference "
        f"netlist has, but the extracted netlist has no node {net!r} - magic "
        "keeps a net's name only where the layout labels it, so the bench "
        "would read a vector that does not exist. Fix in "
        "layout/gen_<block>.py (the layout-fixer): put a non-pin text label "
        f"\"{net}\" on that net's drawing layer (e.g. metal1 34/0), not on "
        "a pin/label layer, which would add a port and change the pin set",
        "magic") for net in sorted(missing)]


# ---- the corner set ---------------------------------------------------------
def bound_list(measures: dict, where: str) -> list[dict]:
    """The sidecar's `measures` map as simlib-shaped bounds, [{measure, min,
    max, corners?}, ...]. A measure holds one bound or a list of them."""
    out = []
    for name, entry in measures.items():
        for b in entry if isinstance(entry, list) else [entry]:
            if not isinstance(b, dict):
                raise CheckError(f"{where}: measure {name!r} must be a "
                                 "{min, max, corners} mapping or a list of them")
            out.append({**b, "measure": name})
    return out


def _named(sweep: list[dict], name: str) -> dict | None:
    return next((c for c in sweep if name in corners_mod.names_of(c)), None)


def typical_corner(sweep: list[dict]) -> dict:
    """The sweep's nearest thing to tt at 27 C and nominal supply."""
    tts = [c for c in sweep if c["process"] == "typical"
           and corners_mod.passive_of(c) == "typical"] or sweep
    return min(tts, key=lambda c: (abs(c["supply_pct"]), abs(c["temp_c"] - 27)))


def slow_corner(sweep: list[dict]) -> dict | None:
    """The sweep's slow, hot, low-supply corner, passives moving with the
    transistors (ss, not ss_pff, on a tie). None with no ss corner."""
    slows = [c for c in sweep if c["process"] == "ss"]
    return min(slows, key=lambda c: (-c["temp_c"], c["supply_pct"],
                                     corners_mod.passive_of(c) != "ss"),
               default=None)


def fast_corner(sweep: list[dict]) -> dict | None:
    """slow_corner's mirror: the fast, cold, high-supply corner, passives moving
    with the transistors. None with no ff corner."""
    fasts = [c for c in sweep if c["process"] == "ff"]
    return min(fasts, key=lambda c: (c["temp_c"], -c["supply_pct"],
                                     corners_mod.passive_of(c) != "ff"),
               default=None)


def _margin(value: float, bound: dict) -> float:
    """Distance from `value` to the nearest side of `bound`; negative past it."""
    sides = []
    if bound.get("min") is not None:
        sides.append(value - bound["min"])
    if bound.get("max") is not None:
        sides.append(bound["max"] - value)
    return min(sides) if sides else math.inf


def worst_corners(sweep: list[dict], bounds: list[dict],
                  results: list[dict]) -> list[str]:
    """For each bound, the sweep corner where sim_pvt's value of that measure
    (`results`, its report's) has the least margin to this bound. A measure
    sim_pvt did not print contributes nothing. Pure."""
    worst = []
    for b in bounds:
        key = b["measure"].lower()
        best = None
        for c in sweep:
            if not simlib.scored_at(b, corners_mod.names_of(c)):
                continue
            for r in results:
                if r.get("corner") != c["name"]:
                    continue
                for name, v in (r.get("measures") or {}).items():
                    if name.lower() == key and isinstance(v, (int, float)):
                        m = _margin(v, b)
                        if best is None or m < best[0]:
                            best = (m, c["name"])
        if best is not None:
            worst.append(best[1])
    return worst


def fresh_sim_pvt_results(ws: Path) -> list[dict] | None:
    """The recorded sim_pvt gate's per-corner results, or None when there is
    no record or its inputs have moved since: a stale record says nothing
    about which corner is worst now."""
    rec_path = ws / "reports" / "recorded" / "gate-sim_pvt.json"
    if not rec_path.is_file():
        return None
    rec = checklib.load_json(rec_path, "the recorded sim_pvt gate")
    recorded = rec.get("inputs_at_start")
    results = (rec.get("facts") or {}).get("results")
    if not recorded or not results:
        return None
    imap = statelib.load_map()
    current = statelib.gate_input_hashes(ws, "ade", "sim_pvt", imap)
    # a record older than the pdk_* inputs does not carry them: judged on the
    # inputs it holds, since the design's own inputs are what move the margins
    if not current or any(k in recorded and recorded[k] != v
                          for k, v in current.items()):
        return None
    return results


def select_corners(field, sweep: list[dict], bounds: list[dict],
                   results: list[dict] | None
                   ) -> tuple[list[dict], str, list[dict]]:
    """(corners to run in sweep order, where the choice came from, bounds
    scoped only to corners outside the sweep). `field` is the spec's
    `pex_corners` (None when absent); a name outside the sweep refuses."""
    if field == "all" or (field is None and results is None):
        picked = list(sweep)
        source = ("pex_corners: all" if field == "all" else
                  "the whole sweep: no fresh sim_pvt record to pick from")
    elif field is None:
        slow = slow_corner(sweep)
        picked = [typical_corner(sweep)] + ([slow] if slow else []) + [
            _named(sweep, n) for n in worst_corners(sweep, bounds, results)]
        source = "typical, slow/hot, and sim_pvt's worst per measure"
    else:
        problem = pex_corners_problem(field, sweep)
        if problem:
            raise CheckError(problem)
        picked = [typical_corner(sweep)] + [_named(sweep, n) for n in field]
        source = "spec.yaml pex_corners"
    unswept = []
    for b in bounds:
        scope = b.get("corners")
        if scope in (None, "all"):
            continue
        scope = scope if isinstance(scope, list) else [scope]
        hits = [_named(sweep, n) for n in scope if _named(sweep, n)]
        if hits:
            picked += hits
        else:
            unswept.append(b)
    names = {c["name"] for c in picked}
    return [c for c in sweep if c["name"] in names], source, unswept


def pex_corners_problem(field, sweep: list[dict]) -> str | None:
    """What is wrong with a spec's `pex_corners`, or None. spec_lint and the
    gate both ask here."""
    if field == "all":
        return None
    if not (isinstance(field, list) and field
            and all(isinstance(n, str) for n in field)):
        return ("spec.yaml 'pex_corners' must be \"all\" or a non-empty list "
                "of corner names")
    known = set().union(*(corners_mod.names_of(c) for c in sweep))
    bad = [n for n in field if n not in known]
    if bad:
        return (f"spec.yaml 'pex_corners' names {bad}, which the spec's "
                f"corners do not sweep; swept: {sorted(known)}")
    return None


def rc_knob_runs(field, sweep: list[dict]) -> list[dict]:
    """The knob runs a spec's `pex_rc_knobs` asks for, each {knob, corner, c,
    r}, in table order for "all" and in the spec's order otherwise. Raises
    CheckError on a field spec_lint would refuse."""
    if field is None:
        return []
    knobs = corners_mod.rc_knobs()
    problem = corners_mod.rc_knobs_problem(field, sweep, knobs)
    if problem:
        raise CheckError(problem)
    pair = {"ss": slow_corner(sweep), "ff": fast_corner(sweep)}
    return [{"knob": name, "corner": pair[knobs[name]["pair"]],
             "c": knobs[name]["c"], "r": knobs[name]["r"]}
            for name in (list(knobs) if field == "all" else field)]


def unaware_tokens(template: str, corners: list[dict],
                   passives: list[str]) -> list[str]:
    """The tokens the bench must use for `corners` and does not. Pure."""
    def moves(fn) -> bool:
        return len({fn(c) for c in corners}) > 1 or fn(corners[0]) != fn(BASE)
    need = []
    if moves(lambda c: c["process"]):
        need.append("corner")
    if moves(lambda c: c["temp_c"]):
        need.append("temp_c")
    if moves(lambda c: c["supply_pct"]):
        need.append("vdd")
    for device, token in (("resistor", "res_corner"), ("mim_cap", "mim_corner")):
        if device in passives and moves(
                lambda c, d=device: corners_mod.passive_of(c, d)):
            need.append(token)
    return [t for t in need if "{" + t + "}" not in template]


# what a bench hard-coding everything is correct at
BASE = {"process": "typical", "temp_c": 27, "supply_pct": 0}


def corner_tokens(corner: dict, nominal_vdd) -> dict:
    vdd = (f"{corners_mod.resolve_vdd(corner, nominal_vdd):.6g}"
           if nominal_vdd is not None else "")
    return {"corner": corner["process"],
            "res_corner": f"res_{corners_mod.passive_of(corner, 'resistor')}",
            "mim_corner": f"mimcap_{corners_mod.passive_of(corner, 'mim_cap')}",
            "temp_c": corner["temp_c"], "vdd": vdd}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--block", help="override state.json's 'block'")
    ap.add_argument("--timeout", type=float, default=None,
                    help="ngspice limit, s (default: the spec's "
                    f"sim_timeout_s.pex_sim, else {DEFAULT_TIMEOUT:g} s)")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()  # tools run with cwd=log/
    spec_path = ws / "spec" / "spec.yaml"
    spec = speclib.load_spec(spec_path) if spec_path.is_file() else None
    timeout = speclib.sim_timeout(spec, "pex_sim", args.timeout,
                                  DEFAULT_TIMEOUT)
    block = layout_gen.block_of(ws, args.block)
    bench_tpl, bounds_path = find_bench(ws, block)
    bounds = checklib.load_json(bounds_path, "pex_sim bounds")
    measures = bounds.get("measures")
    if not isinstance(measures, dict) or not measures:
        raise CheckError(f"{bounds_path} has no non-empty 'measures' map")
    bound_rows = bound_list(measures, str(bounds_path))

    import check_analog_lvs
    ref_path = check_analog_lvs.find_reference_netlist(ws, block)
    ref_text = ref_path.read_text(encoding="utf-8", errors="replace")
    ref_cell = check_analog_lvs.reference_cell(ws, block, ref_text)

    gds_path, topcell, _abs_path, _abstract = layout_gen.build(ws, block)

    work_dir = ws / "log" / "pex_sim"
    work_dir.mkdir(parents=True, exist_ok=True)
    raw, _log = layoutlib.run_magic_extract(
        work_dir, gds_path.resolve(), topcell, parasitics=True)
    raw_text = raw.read_text(encoding="utf-8", errors="replace")
    parasitics = layoutlib.count_parasitics(raw_text)
    if not parasitics["r"] and not parasitics["c"]:
        raise CheckError(
            f"{raw.name} carries no R and no C line - magic extracted no "
            "parasitics, so a bench on it is the schematic again")
    extracted = work_dir / f"{topcell}.pex.ordered.spice"
    layoutlib.fresh(extracted)
    ref_pins = pins_of(ref_text, ref_cell)
    extracted.write_text(reorder_pins(raw_text, topcell, ref_pins),
                         encoding="utf-8")

    if spec is None:
        # spec_lint refuses a block with no spec long before this gate; a
        # bare workspace has no sweep to read, so typical is all there is
        sweep, field, nominal_vdd = [dict(BASE, name="tt")], None, None
        corner_list, source, unswept = sweep, "no spec.yaml: typical only", []
        knob_runs = []
    else:
        sweep, _scoped = sim_run.spec_sweep(ws)
        field, nominal_vdd = spec.get("pex_corners"), (spec.get("supply")
                                                       or {}).get("vdd")
        corner_list, source, unswept = select_corners(
            field, sweep, bound_rows,
            None if field is not None else fresh_sim_pvt_results(ws))
        knob_runs = rc_knob_runs(spec.get("pex_rc_knobs"), sweep)

    template = bench_tpl.read_text(encoding="utf-8")
    if "{vdd}" in template and (isinstance(nominal_vdd, bool) or not isinstance(
            nominal_vdd, (int, float))):
        raise CheckError(f"{bench_tpl.name} uses {{vdd}} and spec.yaml has no "
                         "numeric 'supply.vdd' - run spec_lint first")
    pdk = layoutlib.pdk_root()

    def deck(corner: dict, netlist: Path = extracted) -> str:
        return template.format(pdk=pdk, extracted=netlist.resolve(),
                               **corner_tokens(corner, nominal_vdd))
    bench_text = deck(corner_list[0])

    # a floating ground or an unlabelled probe is not a bench worth running:
    # the findings alone
    rel_gds = str(gds_path.relative_to(ws))
    ground = ground_finding(ref_text, ref_pins, raw_text, topcell, rel_gds)
    unrun = ([ground] if ground is not None else []) + probe_findings(
        bench_text, ref_text, raw_text, topcell, rel_gds)
    if unrun:
        payload = checklib.report(
            SCRIPT, ws / "layout", unrun, topcell=topcell, gds=rel_gds,
            extracted=str(extracted.relative_to(ws)),
            parasitics=parasitics, measured={})
        return payload, args.out

    corner_names = [c["name"] for c in corner_list]
    rel_bounds = str(bounds_path.relative_to(ws))
    rel_bench = str(bench_tpl.relative_to(ws))
    passives = corners_mod.passive_devices(ref_text)
    refused = [checklib.violation(
        "pex_sim", "error", rel_bench, topcell, "pex_bench_not_corner_aware",
        [token], f"{bench_tpl.name} never uses {{{token}}}, and this run's "
        f"corners ({', '.join(corner_names)}) move it: every corner would "
        "simulate as typical and pass unseen. Use the token in the bench (the "
        f"bench-writer), or keep it typical-only with `pex_corners: "
        f"[{typical_corner(sweep)['name']}]` in spec.yaml", "check_pex_sim")
        for token in unaware_tokens(
            template, corner_list + [k["corner"] for k in knob_runs], passives)]
    if knob_runs and "{extracted}" not in template:
        refused.append(checklib.violation(
            "pex_sim", "error", rel_bench, topcell, "pex_bench_not_corner_aware",
            ["extracted"], f"{bench_tpl.name} never uses {{extracted}}, and "
            "spec.yaml's pex_rc_knobs needs it to reach the scaled netlist: "
            "every knob would simulate the nominal wires and pass unseen. Use "
            "the token in the bench (the bench-writer)", "check_pex_sim"))
    refused += [checklib.violation(
        "pex_sim", "error", rel_bounds, topcell, "sim_bound_not_swept",
        [b["measure"]], f"{b['measure']}: its bound is scoped to "
        f"{b.get('corners')}, which the spec's corners do not sweep, so it "
        "would never be scored", "check_pex_sim") for b in unswept]
    if refused:
        payload = checklib.report(
            SCRIPT, ws / "layout", refused, topcell=topcell, gds=rel_gds,
            extracted=str(extracted.relative_to(ws)), parasitics=parasitics,
            measured={}, corners=corner_names, corner_source=source)
        return payload, args.out

    violations = []

    def run_bench(corner: dict, scored: list[dict], stem: str, where: str,
                  refs: list[str], netlist: Path = extracted) -> dict:
        """One ngspice run at `corner`: the values of `scored`, and their
        findings appended to `violations`, each naming `where`."""
        bench_path = work_dir / f"{block}_pex_tb__{stem}.cir"
        layoutlib.fresh(bench_path)
        bench_path.write_text(deck(corner, netlist), encoding="utf-8")

        # a meas whose trigger/target never happened (a latch that decided
        # the wrong way never lets outn fall) comes back by name and is a
        # finding below; any other ngspice error still refuses the gate
        never_met: set[str] = set()
        try:
            sim_out = layoutlib.run_ngspice(bench_path, cwd=work_dir,
                                            timeout=timeout,
                                            failed_measures=never_met)
        except layoutlib.LayoutError as exc:
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise CheckError(f"at corner {where}: " + speclib.timeout_hint(
                    "pex_sim", timeout)) from exc
            raise CheckError(f"at corner {where}: {exc}") from exc
        values = layoutlib.parse_ngspice_prints(sim_out)
        if not values:
            raise CheckError(
                f"at corner {where}: ngspice produced no parseable 'name = "
                "value' print lines - the bench did not measure anything: "
                f"{sim_out[-2000:]}")

        measured = {}
        for bound in scored:
            name = bound["measure"]
            key = name.lower()
            if key in never_met and key not in values:
                violations.append(checklib.violation(
                    "pex_sim", "error", rel_bounds, topcell, "measure_missing",
                    [name, *refs], f"{name} @ {where}: the bench's trigger/"
                    "target condition was never met (ngspice: 'failed!') - "
                    "the circuit never made the transition this measure times",
                    "ngspice"))
                continue
            if key not in values:
                violations.append(checklib.violation(
                    "pex_sim", "error", rel_bounds, topcell, "measure_missing",
                    [name, *refs], f"{name} @ {where}: bench never printed a "
                    f"value for it (printed: {sorted(values)})", "ngspice"))
                continue
            v = values[key]
            measured[name] = v
            lo, hi = bound.get("min"), bound.get("max")
            if (not math.isfinite(v) or (lo is not None and v < lo)
                    or (hi is not None and v > hi)):
                violations.append(checklib.violation(
                    "pex_sim", "error", rel_bounds, topcell,
                    "measure_out_of_bounds", [name, *refs],
                    f"{name} @ {where} = {v:.6g}, outside bound [{lo}, {hi}]",
                    "ngspice"))
        return measured

    def scored_at(corner: dict) -> list[dict]:
        names = corners_mod.names_of(corner)
        return [b for b in bound_rows if simlib.scored_at(b, names)]

    # a knob that would measure nothing, or a netlist it cannot scale, stops
    # the gate before the first bench rather than after a long sweep
    scaled = {}
    for run_ in knob_runs:
        knob, cname = run_["knob"], run_["corner"]["name"]
        if not scored_at(run_["corner"]):
            raise CheckError(
                f"pex_rc_knobs asks for {knob}, which runs at {cname}, and no "
                f"bound in {bounds_path.name} is scored there: a run with "
                "nothing to score is a refusal")
        try:
            text = layoutlib.scale_parasitics(
                extracted.read_text(encoding="utf-8"), run_["c"], run_["r"])
        except layoutlib.LayoutError as exc:
            raise CheckError(f"pex_rc_knobs {knob}: {exc}") from exc
        scaled[knob] = work_dir / f"{topcell}.pex.ordered.{knob}.spice"
        layoutlib.fresh(scaled[knob])
        scaled[knob].write_text(text, encoding="utf-8")

    by_corner: dict[str, dict] = {}
    not_scored = []
    for corner in corner_list:
        cname = corner["name"]
        scored = scored_at(corner)
        if not scored:
            not_scored.append({"corner": cname,
                               "reason": "no bound is scored at this corner"})
            continue
        by_corner[cname] = run_bench(corner, scored, cname, cname, [cname])

    rc_runs = []
    for run_ in knob_runs:
        corner, knob = run_["corner"], run_["knob"]
        cname = corner["name"]
        where = f"{cname} / {knob} (C x{run_['c']:g}, R x{run_['r']:g})"
        rc_runs.append({"knob": knob, "corner": cname, "c": run_["c"],
                        "r": run_["r"], "measured": run_bench(
                            corner, scored_at(corner), f"{cname}__{knob}", where,
                            [cname, knob], scaled[knob])})

    if not by_corner:
        raise CheckError(
            f"no bound in {bounds_path.name} is scored at any of "
            f"{corner_names}: a gate with nothing to run is a refusal")
    typical = typical_corner(corner_list)["name"]
    measured = by_corner.get(typical) or next(iter(by_corner.values()))

    payload = checklib.report(
        SCRIPT, ws / "layout", violations, topcell=topcell,
        gds=str(gds_path.relative_to(ws)),
        extracted=str(extracted.relative_to(ws)),
        parasitics=parasitics, measured=measured, corners=corner_names,
        corner_source=source, measured_by_corner=by_corner,
        not_scored=not_scored, **({"rc_knobs": rc_runs} if rc_runs else {}))
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

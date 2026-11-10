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
gives it: an optimistic netlist is never benched.

A second extractor checks magic's capacitance (#116, kpexlib): klayout_pex
2.5D extracts the same GDS, and each labelled net's total C and the
coupling C of each pair of nets the bench probes must agree within
kpexlib.TOLERANCE (20% of the smaller; the corpus blocks agree within 13%,
a dropped label on the comparator's tail net puts its inputs 28% apart).
A net past it, or a net only one extractor names, is a
`pex_extractors_disagree` finding with both values; the bench still runs.
kpex's netlist is never benched (#60): the wire R and the bench stay
magic's. A poly resistor's body is C kpex extracts in full, as a wire, but
magic only in part: the device model carries cj x r_w x r_l of it (fox_sub
c1/c2, half at each end) and run_magic_extract adds the rest
(layoutlib.add_poly_body_residuals). So each body's model C is taken off
kpex's C of the nets at its ends and of its third terminal before the
comparison (subtract_model_c, listed per net in `extractor_crosscheck`'s
`model_c_subtracted`) - that subtraction is what lets the two agree on a
resistor ladder within the same 20%, with no net excluded for it. A net over an N-well, and the substrate of a block with a well,
are not compared (kpex models no well); the report lists them, and every
row, in `extractor_crosscheck`. kpex failing to run is a refusal.

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
as wires: a quick envelope. Findings name the knob too, in refs and in the
message, and the report adds `rc_knobs`, each knob's corner, factors and values.
A knob with no bound scored at its corner is a refusal: the run it asked for
would measure nothing.

RC corners. spec.yaml's `pex_rc_corners` ("all" or a list of names, absent means
none, since each is one more extraction and ngspice run) asks for the real thing:
magic extracts the layout again at that variant of the PDK's tech, `ngspice(hrhc)`
and its siblings, where each layer and each via has its own R and C. Each variant
runs once, with the one process engine/reference/pex_rc_variants.yaml pairs it
with: hrhc with the sweep's slow corner, lrlc with its fast corner, hrlc with sf
and lrhc with fs, whether or not the corner set above holds that corner. A variant
whose process the sweep lacks is refused. A finding names the variant in refs and
in the message. The netlist is log/pex_sim/<top>.pex.ordered.<variant>.spice, with
the magic files under log/pex_sim/rc_<variant>/; it gets the same ground, probe and
no-parasitics checks as the nominal one. The report adds `rc_variants`, each one's
corner, pair, netlist, hash, parasitic counts and values, and gate.py records each
netlist's hash and pair in the gate's state entry as a view of its own
(statelib.views_of): editing the file, or re-pairing the variant in the table,
stales the gate. A variant with no bound scored at its corner is a refusal, before
any extraction.

Fill (#136). The shuttle fills the tile after precheck with the PDK's
fill_all.rb, and the block's own extraction never sees that metal. spec.yaml's
`pex_fill` ("all", or a list of `floating` and `grounded`; absent means none)
asks for the view after fill (engine/lib/filllib.py):
- a copy of the GDS is filled the way the shuttle fills it;
- DRC runs on the copy with every deck but antenna and density, and a
  finding is `fill_drc`, refs [rule, "fill"];
- the density deck runs too, but is only reported (`fill.density_deck`):
  its rules read a whole die's coverage, not a tile's;
- magic extracts the copy with each fill island a net of its own, and the
  ground, probe and symmetry checks run on that netlist too;
- the bench then runs at every scored corner, once per mode.

`floating` folds the islands out exactly (filllib.float_fill), and
`grounded` ties them to the cell's ground pin. A finding names the mode in
refs ("fill_floating") and in the message. The report adds `fill`: the
density per layer before and after, the island count, each reference net's
total C before and after, and each mode's netlist, hash and measures.
gate.py records each netlist as a view of its own (statelib.views_of).

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

Symmetric nets are checked before the bench too, from the extraction
alone. spec.yaml's `symmetric_nets` (speclib.symmetric_nets) lists pairs
that must route alike - a differential output, a charge pump's UP/DN -
each `{nets: [a, b], max_dc_pct, max_dr_pct, min_dc_ff, min_dr_ohm,
aggressors}`. For each net, net_parasitics reads its total C (every C line
from one of its nodes to another net's: ground, substrate and coupling), its
R from the node named for it (where its label, the pin, is) to the worst of
the nodes a device touches, solved over extresist's R network, and its C to
each other net. A pair whose C, R or C to a named aggressor differs by more
than the bound's percentage of the pair's mean, and by more than the floor
(0 unless the spec sets one), is a `symmetry_violated` finding with both
values, for the layout generator. A declared net the reference netlist
lacks is a refusal; one the extraction never names is
`symmetry_net_unlabelled` and no bench is run, as for a probe. The report
adds `symmetry`, each pair's values and the mismatch of each quantity
checked. The bench still runs after a violation, so its findings join it.

The bench run has a time limit: --timeout when given, else the spec's
`sim_timeout_s: {pex_sim: N}` (speclib.sim_timeout; a large RC netlist can
take minutes), else 120 s, per corner. A run past it is a refusal whose
remediation names that knob and the corner, never a pass.

Device stress. A block with layout_ref/<block>_stress.yaml also runs that
file's transient benches on the extracted netlist and scores every MOS
device's own terminal voltages over each whole run, at the corners the
sidecar names (stresslib; limits and their citations in
engine/reference/stress_limits.yaml). Gate-oxide pairs (gate to source,
drain or bulk) are held to the 3.63 V DC limit of DRM 14.1.4.2 with two
allowances for true transients, which are this flow's own numbers: a
repeating excess inside a declared window, and a one-off edge. Past either
it is a `device_overstress` finding. Junction pairs (drain or source to
bulk, drain to source) have their own limit per model and are a
`junction_over_breakdown` finding past it. The repeating share counts only
time more than a small band over the limit (share_band_v, a provisional
project number). The report adds `stress`: every run, every device that
passes only through an allowance with its peak and time, the ones that pass
only because of the share band with their peak and both shares, and the
junction pairs above the 3.63 V reference. A block without
the file is not stress-checked, and its report has no `stress` key.
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
import filllib  # noqa: E402
import kpexlib  # noqa: E402
import layoutlib  # noqa: E402
import layout_gen  # noqa: E402
import netlistlib  # noqa: E402
import padlib  # noqa: E402
import sim_run  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
import statelib  # noqa: E402
import stresslib  # noqa: E402
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


def probed_dut_nets(bench_text: str, topcell: str,
                    ref_pins: list[str] | None) -> set[str]:
    """The top cell's nets (lowercased) the bench reads: each net it probes
    inside an instance of `topcell` (probed_nets), and each pin of the cell
    whose top-level node the bench probes - `v(out)` where `xdut ... out
    ... cell` binds `out` to a pin, by position in the reference's pin
    order."""
    nets = set(probed_nets(bench_text, topcell))
    lines = netlistlib.join_continuations(bench_text)
    bound: dict[str, set[str]] = {}
    for line in lines:
        tokens = [t for t in line.split() if "=" not in t]
        if (ref_pins and len(tokens) > 1 and tokens[0][0] in "xX"
                and tokens[-1].lower() == topcell.lower()):
            for node, pin in zip(tokens[1:-1], ref_pins):
                bound.setdefault(node.lower(), set()).add(pin.lower())
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
                    nets |= bound.get(arg.strip().lower(), set())
    return nets


def _auto_name(net: str) -> bool:
    # a name an extractor made up: magic's a_x_y (labelled by
    # name_unlabelled_nets) or kpex's $N
    return net.startswith(("a_", "$", "\\$"))


def subtract_model_c(pk: dict, bodies: list[dict]) -> dict:
    """Take each poly resistor body's model C (cj x r_w x r_l, half from
    each end to its third terminal; layoutlib.add_poly_body_residuals'
    `model_c_ff`) off kpex's per-net C `pk`, in place, and return
    {net: fF taken off}. kpex extracts a poly body as a wire with the
    tech's full C to the substrate; magic's netlist has only the part of it
    the device model lacks, the model the rest, so without this kpex reads
    every poly resistor net heavier by the model's share."""
    taken: dict[str, float] = {}
    for b in bodies:
        half = b["model_c_ff"] / 2
        body = _net_of(b["body"])
        for end in (_net_of(n) for n in b["ends"]):
            if end == body:
                continue
            for x, y in ((end, body), (body, end)):
                if x in pk:
                    pk[x]["c_ff"] -= half
                    pk[x]["c_to"][y] = pk[x]["c_to"].get(y, 0.0) - half
                    taken[x] = taken.get(x, 0.0) + half
    for x in pk:
        if x in taken:
            pk[x]["c_ff"] = round(pk[x]["c_ff"], 5)
    return {x: round(v, 5) for x, v in sorted(taken.items())}


def extractor_crosscheck(ws: Path, gds_path: Path, topcell: str,
                         rel_gds: str, ref_text: str, ref_pins, bench_text: str,
                         magic_text: str, ext_path: Path
                         ) -> tuple[list[dict], dict]:
    """magic against kpex 2.5D (kpexlib) on the same GDS: each labelled
    net's total C and the coupling of each pair of probed nets. Returns the
    `pex_extractors_disagree` findings and the report's
    `extractor_crosscheck`. kpex failing is a refusal. `ext_path` is
    magic's .ext of the same extraction (its substrate and well nets)."""
    ext_text = Path(ext_path).read_text(encoding="utf-8", errors="replace")
    kdir = ws / "log" / "pex_sim" / "kpex"
    try:
        netlist, log = kpexlib.run_kpex(kdir, gds_path.resolve(), topcell)
    except kpexlib.KpexError as exc:
        raise CheckError(
            f"the second extractor (kpex 2.5D) did not run on {rel_gds}: "
            f"{exc} - pex_sim cross-checks magic's capacitance against it, "
            "so the gate cannot pass without it") from exc
    substrate = kpexlib.substrate_net(ext_text)
    if substrate is None:
        raise CheckError("magic's .ext names no substrate net, so kpex's "
                         "VSUBS has nothing to compare against")
    kpex_text = kpexlib.rename_substrate(
        netlist.read_text(encoding="utf-8", errors="replace"), substrate)

    def names(text):
        return {_net_of(n) for n in element_nodes(text)}
    magic_names, kpex_names = names(magic_text), names(kpex_text)
    labelled = {n for n in element_nodes(ref_text) if not _auto_name(n)}
    labelled.discard(topcell.lower())
    wells = kpexlib.well_nets(ext_text)
    skip = {n: "N-well under it: kpex models no well, so its metal-over-well "
            "C goes to VSUBS, not to this net" for n in wells}
    if wells:
        skip[substrate] = ("the substrate of a block with an N-well: kpex "
                           "gives it the metal-over-well C magic gives the "
                           "well's net")
    probed = probed_dut_nets(bench_text, topcell, ref_pins)
    want = labelled | probed
    pm = net_parasitics(magic_text, want & magic_names)
    pk = net_parasitics(kpex_text, want & kpex_names)
    tech_c, model_c = layoutlib.poly_body_caps(None)
    _, bodies = layoutlib.add_poly_body_residuals(magic_text, tech_c, model_c)
    subtracted = subtract_model_c(pk, bodies)
    result = kpexlib.compare(pm, pk, labelled, probed, skip)
    result["model_c_subtracted"] = subtracted
    tol = result["tolerance"]
    findings = []
    for row in result.pop("disagree"):
        if "net" in row:
            what, refs = f"net {row['net']}'s total C", [row["net"]]
        else:
            what, refs = (f"the coupling C between {row['nets'][0]} and "
                          f"{row['nets'][1]}"), list(row["nets"])
        m, k = row["magic_ff"], row["kpex_ff"]
        diff = (row["why"] if row.get("why") else
                "one of them is zero" if row["off"] is None else
                f"{row['off'] * 100:.0f}% apart")
        findings.append(checklib.violation(
            "pex_sim", "error", rel_gds, topcell, "pex_extractors_disagree",
            refs, f"{what}: magic {m} fF, kpex 2.5D {k} fF ({diff}; the "
            f"tolerance is {tol * 100:.0f}% of the smaller) - one extractor "
            "reads this layout wrong, so magic's netlist cannot be trusted "
            "for the bench", "kpex", magic_ff=m, kpex_ff=k))
    result.update(
        netlist=str(netlist.relative_to(ws)), log=str(log.relative_to(ws)),
        substrate=substrate, probed=sorted(probed))
    return findings, result


# ---- symmetric nets ----------------------------------------------------------
def _net_of(node: str) -> str:
    return layoutlib._pex_net(node).lower()


def net_parasitics(pex_text: str, nets) -> dict[str, dict]:
    """Each net's parasitics in an extracted netlist, {net: {c_ff, r_ohm,
    c_to: {other: ff}, terminals}}, nets lowercased. A net's nodes are its
    own name and extresist's sub-nodes of it (name.tN, name.nN).

    c_ff is every C line between one of the net's nodes and a node of
    another net: ground, substrate and coupling alike. r_ohm is the worst
    effective resistance, through the net's own R lines, from the node named
    for the net (where its label is) to a node of it a device touches: zero
    for a net extresist never split. A device node the R network does not
    reach from the label is a refusal, not a zero."""
    import numpy as np

    want = {n.lower() for n in nets}
    c_to = {n: {} for n in want}
    edges = {n: [] for n in want}
    dev_nodes = {n: set() for n in want}
    for line in netlistlib.join_continuations(pex_text):
        parts = netlistlib.strip_comment(line).split()
        if not parts or parts[0][:1].lower() not in "xrcmdq":
            continue
        letter = parts[0][:1].lower()
        pos = [t for t in parts[1:] if "=" not in t]
        if letter in "rc":
            if len(pos) < 3:
                continue
            a, b = pos[0].lower(), pos[1].lower()
            value = netlistlib.spice_number(pos[2])
            na, nb = _net_of(a), _net_of(b)
            if value is None:
                continue
            if letter == "c" and na != nb:
                for x, y in ((na, nb), (nb, na)):
                    if x in want:
                        c_to[x][y] = c_to[x].get(y, 0.0) + value * 1e15
            elif letter == "r" and na == nb and na in want and a != b:
                edges[na].append((a, b, value))
            continue
        terms = pos[:-1] if letter == "x" else pos[:NODE_COUNT.get(letter, 0)]
        for t in terms:
            if _net_of(t) in want:
                dev_nodes[_net_of(t)].add(t.lower())

    out = {}
    for net in sorted(want):
        r_ohm = 0.0
        if edges[net]:
            nodes = sorted({x for a, b, _ in edges[net] for x in (a, b)} | {net})
            idx = {n: i for i, n in enumerate(nodes)}
            lap = np.zeros((len(nodes), len(nodes)))
            for a, b, r in edges[net]:
                if r <= 0:
                    raise CheckError(f"the extracted netlist gives net {net} "
                                     f"a resistor of {r} ohm between {a} and {b}")
                i, j = idx[a], idx[b]
                lap[i, i] += 1 / r
                lap[j, j] += 1 / r
                lap[i, j] -= 1 / r
                lap[j, i] -= 1 / r
            keep = [i for i in range(len(nodes)) if i != idx[net]]
            reached = {net}
            grow = True
            while grow:  # the nodes the R network joins to the label's node
                grow = False
                for a, b, _ in edges[net]:
                    if (a in reached) != (b in reached):
                        reached |= {a, b}
                        grow = True
            far = sorted(n for n in dev_nodes[net] if n in idx and n not in reached)
            if far:
                raise CheckError(
                    f"net {net}: device node(s) {far} are on R lines the "
                    f"network never joins to the node {net} itself - the "
                    "pin-to-device R cannot be read")
            if keep:
                inv = np.linalg.pinv(lap[np.ix_(keep, keep)])
                pos_of = {nodes[i]: k for k, i in enumerate(keep)}
                r_ohm = max((float(inv[pos_of[n], pos_of[n]])
                             for n in dev_nodes[net] if n in pos_of), default=0.0)
        out[net] = {"c_ff": round(sum(c_to[net].values()), 5),
                    "r_ohm": round(r_ohm, 4),
                    "c_to": {k: round(v, 5) for k, v in sorted(c_to[net].items())},
                    "terminals": len(dev_nodes[net])}
    return out


def _mismatch(a: float, b: float) -> float:
    """|a - b| as a percentage of the pair's mean (0 for two zeros)."""
    mean = (a + b) / 2
    return 0.0 if mean == 0 else abs(a - b) / mean * 100.0


def symmetry_findings(rows: list[dict], ref_text: str, extracted_text: str,
                      topcell: str, rel_gds: str, rel_spec: str
                      ) -> tuple[list[dict], list[dict]]:
    """The findings of spec.yaml's symmetric_nets on the extracted netlist,
    and one report row per pair. A pair's quantity fails when it differs by
    more than its max_*_pct of the pair's mean and by more than its floor.
    A declared net the reference lacks is a refusal (a spec error); one the
    reference has and the extraction does not name is `symmetry_net_unlabelled`
    for the layout generator, as an unlabelled probe is."""
    if not rows:
        return [], []
    ref_nodes, ext_nodes = element_nodes(ref_text), element_nodes(extracted_text)
    ext_nets = {_net_of(n) for n in ext_nodes}
    findings, report = [], []
    named = sorted({n.lower() for r in rows for n in r["nets"] + r["aggressors"]})
    unknown = [n for n in named if n not in ref_nodes]
    if unknown:
        raise CheckError(
            f"{rel_spec} symmetric_nets names {unknown}, which the reference "
            f"{topcell} netlist does not have - a pair the gate cannot find is "
            "a refusal. Fix the net names in spec.yaml (the spec-writer)")
    unlabelled = [n for n in named if n not in ext_nets]
    for net in unlabelled:
        findings.append(checklib.violation(
            "pex_sim", "error", rel_gds, topcell, "symmetry_net_unlabelled",
            [net], f"spec.yaml's symmetric_nets names {topcell}'s net {net!r}, "
            "which the reference has, but the extracted netlist has no node "
            f"{net!r} - magic keeps a net's name only where the layout labels "
            "it. Fix in layout/gen_<block>.py (the layout-fixer): put a "
            f"non-pin text label \"{net}\" on that net's drawing layer (e.g. "
            "metal1 34/0)", "magic"))
    if unlabelled:
        return findings, report
    par = net_parasitics(extracted_text, named)
    for row in rows:
        a, b = (n.lower() for n in row["nets"])
        pa, pb = par[a], par[b]
        checks = [("C", "c_ff", "fF", row["max_dc_pct"], row["min_dc_ff"],
                   pa["c_ff"], pb["c_ff"], [])]
        checks.append(("R", "r_ohm", "ohm", row["max_dr_pct"], row["min_dr_ohm"],
                       pa["r_ohm"], pb["r_ohm"], []))
        for g in (x.lower() for x in row["aggressors"]):
            checks.append((f"C to {g}", "c_ff", "fF", row["max_dc_pct"],
                           row["min_dc_ff"], pa["c_to"].get(g, 0.0),
                           pb["c_to"].get(g, 0.0), [g]))
        entry = {"nets": [a, b], a: {k: pa[k] for k in ("c_ff", "r_ohm")},
                 b: {k: pb[k] for k in ("c_ff", "r_ohm")}, "checked": []}
        for what, _key, unit, pct, floor, va, vb, extra in checks:
            if pct is None:
                continue
            diff, off = abs(va - vb), _mismatch(va, vb)
            entry["checked"].append({"quantity": what, a: va, b: vb,
                                     "mismatch_pct": round(off, 3)})
            if off > pct and diff > floor:
                findings.append(checklib.violation(
                    "pex_sim", "error", rel_gds, topcell, "symmetry_violated",
                    [a, b, *extra],
                    f"{a} and {b} must route alike (spec.yaml symmetric_nets), "
                    f"and the extracted netlist gives {a} {va:.4g} {unit} and "
                    f"{b} {vb:.4g} {unit} of {what}: {off:.1f}% apart, over "
                    f"the {pct:g}% bound"
                    + (f" and {diff:.4g} {unit} apart, over the {floor:g} {unit} "
                       "floor" if floor else "")
                    + ". Fix in layout/gen_<block>.py (the layout-fixer): give "
                    "the two nets mirrored routes of the same length, layers "
                    "and vias", "magic"))
        report.append(entry)
    return findings, report


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
            if b.get("pad", "both") not in padlib.PAD_SCOPES:
                raise CheckError(
                    f"{where}: measure {name!r}: pad must be one of "
                    f"{', '.join(padlib.PAD_SCOPES)}, got {b.get('pad')!r}")
            out.append({**b, "measure": name})
    return out


def _named(sweep: list[dict], name: str) -> dict | None:
    return next((c for c in sweep if name in corners_mod.names_of(c)), None)


def typical_corner(sweep: list[dict]) -> dict:
    """The sweep's nearest thing to tt at 27 C and nominal supply."""
    tts = [c for c in sweep if c["process"] == "typical"
           and corners_mod.passive_of(c) == "typical"] or sweep
    return min(tts, key=lambda c: (abs(c["supply_pct"]), abs(c["temp_c"] - 27)))


def slow_corner(sweep: list[dict], process: str = "ss") -> dict | None:
    """The sweep's slow, hot, low-supply corner, passives moving with the
    transistors (ss, not ss_pff, on a tie). None with no such corner. sf takes
    this same temperature and supply (corners.yaml)."""
    slows = [c for c in sweep if c["process"] == process]
    return min(slows, key=lambda c: (-c["temp_c"], c["supply_pct"],
                                     corners_mod.passive_of(c) != process),
               default=None)


def fast_corner(sweep: list[dict], process: str = "ff") -> dict | None:
    """slow_corner's mirror: the fast, cold, high-supply corner, passives moving
    with the transistors. None with no such corner. fs takes this same
    temperature and supply (corners.yaml)."""
    fasts = [c for c in sweep if c["process"] == process]
    return min(fasts, key=lambda c: (c["temp_c"], -c["supply_pct"],
                                     corners_mod.passive_of(c) != process),
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


def rc_variant_runs(field, sweep: list[dict]) -> list[dict]:
    """The extractions a spec's `pex_rc_corners` asks for, each {variant, pair,
    corner}, in table order for "all" and in the spec's order otherwise. Raises
    CheckError on a field spec_lint would refuse."""
    if field is None:
        return []
    table = corners_mod.rc_variants()
    problem = corners_mod.rc_variants_problem(field, sweep, table)
    if problem:
        raise CheckError(problem)
    corner_of = {"ss": slow_corner, "sf": slow_corner,
                 "ff": fast_corner, "fs": fast_corner}
    return [{"variant": name, "pair": table[name]["pair"],
             "corner": corner_of[table[name]["pair"]](sweep, table[name]["pair"])}
            for name in (list(table) if field == "all" else field)]


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


def stress_stage(ws: Path, side: Path, block: str, work_dir: Path,
                 extracted: Path, ref_text: str, ref_cell: str, topcell: str,
                 rel_gds: str, pdk: str, nominal_vdd, timeout,
                 violations: list[dict]) -> dict:
    """The opt-in per-device stage (stresslib): each deck of `side` at each
    of its corners on the extracted netlist, findings onto `violations`,
    and the report's `stress` facts."""
    limits = stresslib.load_limits()
    decks = stresslib.load_sidecar(side)
    raw_text = extracted.read_text(encoding="utf-8", errors="replace")
    devices, others = stresslib.parse_devices(raw_text, topcell)
    stresslib.check_models(devices, limits, f"{extracted.name} cell {topcell}")
    stresslib.check_flat(raw_text, topcell, others)
    stresslib.label_devices(devices, ref_text, ref_cell)
    pins = stresslib.subckt_pins(raw_text, topcell)
    sweep, _ = sim_run.spec_sweep(ws)
    out = work_dir / "stress"
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for row in decks:
        template = row["bench"].read_text(encoding="utf-8")
        outer, cell = stresslib.dut_of(template, row["bench"].name)
        if cell.lower() != topcell.lower() or len(outer) != len(pins):
            raise CheckError(
                f"{row['bench'].name} instantiates {cell} with {len(outer)} "
                f"nodes; the extraction is {topcell} with {len(pins)} pins")
        stresslib.vectors_of(devices, pins, outer)
        vectors = sorted({v for d in devices for v in d["vectors"] if v})
        for name in row["corners"]:
            corner = _named(sweep, name)
            if corner is None:
                raise CheckError(f"{side.name}: deck {row['name']} names "
                                 f"corner {name}, which the sweep lacks")
            stem = f"{row['name']}__{name}"
            bench_path, raw = out / f"{stem}.cir", out / f"{stem}.raw"
            layoutlib.fresh(bench_path)
            bench_path.write_text(stresslib.with_control(
                template.format(pdk=pdk, extracted=extracted.resolve(),
                                **corner_tokens(corner, nominal_vdd)),
                raw.name, vectors, row["bench"].name), encoding="utf-8")
            limit = row["timeout"] if row["timeout"] is not None else timeout
            try:
                layoutlib.run_ngspice(bench_path, cwd=out, timeout=limit)
            except layoutlib.LayoutError as exc:
                if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise CheckError(f"stress {stem}: " + speclib.timeout_hint(
                        "pex_sim", limit)) from exc
                raise CheckError(f"stress {stem}: {exc}") from exc
            if not raw.is_file():
                raise CheckError(f"stress {stem}: ngspice wrote no {raw.name}")
            t, store = stresslib.read_binary_raw(raw)
            found = stresslib.score_run(t, store, devices, row["windows"], limits)
            rows += [{**r, "corner": name, "deck": row["name"]} for r in found]
            del store
            raw.unlink()    # every terminal of a macro: gigabytes per run

    for (_dev, pair), bad in sorted(stresslib.failures_by_pair(rows).items()):
        first = bad[0]
        kind = "device_overstress" if first["class"] == "gate" \
            else "junction_over_breakdown"
        detail = ("; ".join(first["reasons"]) if first["class"] == "gate"
                  else f"peak {first['peak_v']:.3f} V at {first['t_peak_s']:.4g} s, "
                       f"breakdown {first['breakdown_v']:g} V")
        runs = sorted({f"{r['corner']} / {r['deck']}" for r in bad})
        violations.append(checklib.violation(
            "pex_sim", "error", rel_gds, topcell, kind,
            [first["label"], pair, first["corner"], first["deck"]],
            f"{first['label']} {pair} at {first['corner']} / {first['deck']}: "
            f"{detail} (fails in {len(runs)} run(s): {', '.join(runs[:8])})",
            "check_pex_sim"))
    return stresslib.summarise(rows, sum(len(d["corners"]) for d in decks),
                               len(devices), limits, others)


def fill_view(ws: Path, gds_path: Path, topcell: str, rel_gds: str,
              work_dir: Path, ref_text: str, ref_pins: list[str],
              raw_text: str, bench_text: str, sym_rows: list[dict],
              modes: list[str]) -> dict:
    """spec.yaml's `pex_fill` (module docstring): the block filled the way
    the shuttle fills it, on a copy under log/pex_sim/fill/, its DRC with
    the dummy-fill decks, and one extracted netlist per fill mode, written
    and hashed for the benches. {violations, unrun, facts}: `unrun` are
    findings that make the benches pointless (a filled extraction with no
    ground or no probed net); `facts` is the report's `fill`."""
    fdir = work_dir / "fill"
    fdir.mkdir(exist_ok=True)
    rdb = fdir / f"{topcell}.filled.lyrdb"
    drdb = fdir / f"{topcell}.filled.density.lyrdb"
    xgds = fdir / f"{topcell}.filled.extract.gds"
    try:
        filled = filllib.shuttle_fill(gds_path.resolve(), topcell, fdir)
        density = filllib.fill_summary(filled, gds_path, topcell)
        layoutlib.run_klayout_drc(filled, topcell, rdb, timeout=600,
                                  decks=filllib.FILL_DRC_DECKS)
        drc_items = layoutlib.parse_drc_rdb(rdb)
        layoutlib.run_klayout_drc(filled, topcell, drdb, timeout=600,
                                  decks=filllib.DENSITY_DECKS)
        density_items = layoutlib.parse_drc_rdb(drdb)
        islands = filllib.extraction_copy(filled, topcell, xgds)
        fraw, _log = layoutlib.run_magic_extract(
            fdir, xgds.resolve(), topcell, parasitics=True, timeout=600)
    except layoutlib.LayoutError as exc:
        raise CheckError(f"pex_fill: {exc}") from exc
    ftext = fraw.read_text(encoding="utf-8", errors="replace")
    counts = layoutlib.count_parasitics(ftext)
    nets = filllib.fill_nets(ftext, topcell)
    if islands and not nets:
        raise CheckError(
            f"pex_fill: {islands} fill islands went into the extraction and "
            f"{fraw.name} has no net only capacitors touch - magic extracted "
            "no fill, so a bench on it is the unfilled layout again")

    by_rule: dict[str, list[dict]] = {}
    for item in drc_items:
        by_rule.setdefault(item["category"], []).append(item)
    violations = [checklib.violation(
        "pex_sim", "error", rel_gds, topcell, "fill_drc", [rule, "fill"],
        f"after the shuttle's fill (PDK fill_all.rb): {rule} x{len(items)} - "
        f"{items[0]['description']}. The drawn layout leaves fill a gap it "
        "cannot fill legally; fix in layout/gen_<block>.py (the "
        "layout-fixer), see log/pex_sim/fill/" + rdb.name, "klayout")
        for rule, items in sorted(by_rule.items())]

    unrun = []
    ground = ground_finding(ref_text, ref_pins, ftext, topcell, rel_gds)
    for v in ([ground] if ground is not None else []) + probe_findings(
            bench_text, ref_text, ftext, topcell, rel_gds):
        v["refs"] = [*v["refs"], "fill"]
        v["msg"] = f"after fill: {v['msg']}"
        unrun.append(v)

    if sym_rows:
        sym, _rows = symmetry_findings(sym_rows, ref_text, ftext, topcell,
                                       rel_gds, "spec/spec.yaml")
        for v in sym:
            v["refs"] = [*v["refs"], "fill"]
            v["msg"] = f"after fill (floating): {v['msg']}"
        violations += sym

    named = sorted(element_nodes(ref_text) - {"0"})
    base = net_parasitics(raw_text, named)
    after = net_parasitics(ftext, named)
    coupling = {n: {"c_ff": round(base[n]["c_ff"], 3),
                    "c_ff_filled": round(after[n]["c_ff"], 3)}
                for n in named if n in base and n in after}

    ground_pin = filllib.ground_net(ref_pins)
    views = []
    for mode in modes:
        text = (filllib.float_fill(ftext, topcell, nets) if mode == "floating"
                else filllib.ground_fill(ftext, topcell, nets, ground_pin))
        view = work_dir / f"{topcell}.pex.ordered.fill_{mode}.spice"
        layoutlib.fresh(view)
        view.write_text(reorder_pins(text, topcell, ref_pins), encoding="utf-8")
        views.append({"mode": mode, "netlist": str(view.relative_to(ws)),
                      "sha256": statelib.hash_artifact(view, "text_eol"),
                      "parasitics": layoutlib.count_parasitics(text),
                      **({"ground": ground_pin} if mode == "grounded" else {})})
    facts = {
        "script": filllib.FILL_SCRIPT_REL,
        "filled_gds": str(filled.relative_to(ws)),
        "islands": islands, "fill_nets": len(nets), "parasitics": counts,
        "drc": {"decks": filllib.FILL_DRC_DECKS, "findings": len(drc_items),
                "report": str(rdb.relative_to(ws))},
        # the density deck's rules are whole-die figures (">30% of the
        # die"): on a tile they say how much the tile adds, not whether the
        # die passes, so they are reported here with the tile's own
        # numbers, never a finding
        "density": density,
        "density_deck": sorted({f"{i['category']}: {i['description']}"
                                for i in density_items}),
        "coupling": coupling, "views": views}
    return {"violations": violations, "unrun": unrun, "facts": facts}


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
        knob_runs, variant_runs, fill_modes = [], [], []
        ua_pins, board = [], {}
    else:
        sweep, _scoped = sim_run.spec_sweep(ws)
        field, nominal_vdd = spec.get("pex_corners"), (spec.get("supply")
                                                       or {}).get("vdd")
        corner_list, source, unswept = select_corners(
            field, sweep, bound_rows,
            None if field is not None else fresh_sim_pvt_results(ws))
        knob_runs = rc_knob_runs(spec.get("pex_rc_knobs"), sweep)
        variant_runs = rc_variant_runs(spec.get("pex_rc_corners"), sweep)
        try:
            fill_modes = filllib.modes_of(spec.get("pex_fill"))
            ua_pins, board = padlib.ua_pins_of(spec), padlib.board_of(spec)
        except ValueError as exc:
            raise CheckError(f"{exc} - run spec_lint first") from exc

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

    # a spec with ua_pins runs every bench bare and again with TT's pad on
    # each ua pin of every instance of the extracted cell (#112). A bench
    # the pad cannot be put in is a refusal: its "pad" runs would only be
    # bare runs again
    conditions = padlib.conditions_of(spec) if spec is not None else [None]

    def pad_deck(text: str, corner: dict) -> str:
        vdd = corner_tokens(corner, nominal_vdd)["vdd"]
        return padlib.insert_pads(
            text, topcell, ref_pins or [], ua_pins, padlib.pad_subckt(
                pdk, corner["process"], float(vdd) if vdd else 0.0,
                board))[0]
    if ua_pins:
        try:
            if not nominal_vdd:
                raise padlib.PadRefused(
                    "ua_pad_not_inserted", "spec.yaml has no numeric "
                    "'supply.vdd', and the pad's ESD diodes sit on DVDD")
            pad_deck(bench_text, corner_list[0])
        except padlib.PadRefused as exc:
            payload = checklib.report(
                SCRIPT, ws / "layout", [checklib.violation(
                    "pex_sim", "error", str(bench_tpl.relative_to(ws)), topcell,
                    exc.kind, ["pad"], f"{bench_tpl.name}: {exc} - every "
                    "bench of a spec with ua_pins runs through the pad model "
                    "too (engine/lib/padlib.py), so put an instance of "
                    f"{topcell} with every port on it in the bench",
                    "check_pex_sim")], topcell=topcell,
                gds=str(gds_path.relative_to(ws)),
                extracted=str(extracted.relative_to(ws)),
                parasitics=parasitics, measured={})
            return payload, args.out

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

    rows = speclib.symmetric_nets(spec) if spec is not None else []
    if rows is None:
        raise CheckError("spec.yaml 'symmetric_nets' is malformed - run "
                         "spec_lint first")
    violations, symmetry = symmetry_findings(
        rows, ref_text, raw_text, topcell, rel_gds, "spec/spec.yaml")
    if any(v["kind"] == "symmetry_net_unlabelled" for v in violations):
        payload = checklib.report(
            SCRIPT, ws / "layout", violations, topcell=topcell, gds=rel_gds,
            extracted=str(extracted.relative_to(ws)),
            parasitics=parasitics, measured={}, symmetry=symmetry)
        return payload, args.out

    crossed, crosscheck = extractor_crosscheck(
        ws, gds_path, topcell, rel_gds, ref_text, ref_pins, bench_text,
        raw_text, work_dir / f"{topcell}.ext")
    violations += crossed

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
            template, corner_list + [k["corner"] for k in knob_runs + variant_runs],
            passives)]
    if (knob_runs or variant_runs or fill_modes) and "{extracted}" not in template:
        refused.append(checklib.violation(
            "pex_sim", "error", rel_bench, topcell, "pex_bench_not_corner_aware",
            ["extracted"], f"{bench_tpl.name} never uses {{extracted}}, and "
            "spec.yaml's pex_rc_knobs, pex_rc_corners and pex_fill need it to "
            "reach their "
            "netlists: every run would simulate the nominal wires and pass "
            "unseen. Use the token in the bench (the bench-writer)",
            "check_pex_sim"))
    refused += [checklib.violation(
        "pex_sim", "error", rel_bounds, topcell, "sim_bound_not_swept",
        [b["measure"]], f"{b['measure']}: its bound is scoped to "
        f"{b.get('corners')}, which the spec's corners do not sweep, so it "
        "would never be scored", "check_pex_sim") for b in unswept]
    if refused:
        payload = checklib.report(
            SCRIPT, ws / "layout", violations + refused, topcell=topcell,
            gds=rel_gds,
            extracted=str(extracted.relative_to(ws)), parasitics=parasitics,
            measured={}, corners=corner_names, corner_source=source,
            extractor_crosscheck=crosscheck)
        return payload, args.out

    pad_runs: list[dict] = []

    def run_bench(corner: dict, scored: list[dict], stem: str, where: str,
                  refs: list[str], netlist: Path = extracted) -> dict:
        """run_once in each of the spec's pad conditions: the bare run's
        values (the pad runs' go to pad_runs), each condition scoring only
        the bounds padlib.scored_in gives it."""
        measured: dict = {}
        for cond in conditions:
            mine = [b for b in scored if padlib.scored_in(b, cond)]
            if not mine:
                continue
            if cond != "pad":
                measured = run_once(corner, mine, stem, where, refs, netlist)
                continue
            pad_runs.append({"run": where, "corner": corner["name"],
                             "measured": run_once(
                                 corner, mine, f"{stem}__pad", f"{where} / pad",
                                 [*refs, "pad"], netlist, pad=True)})
        return measured

    def run_once(corner: dict, scored: list[dict], stem: str, where: str,
                 refs: list[str], netlist: Path = extracted,
                 pad: bool = False) -> dict:
        """One ngspice run at `corner`, through the ua pad model when `pad`:
        the values of `scored`, and their findings appended to
        `violations`, each naming `where`."""
        bench_path = work_dir / f"{block}_pex_tb__{stem}.cir"
        layoutlib.fresh(bench_path)
        text = deck(corner, netlist)
        bench_path.write_text(pad_deck(text, corner) if pad else text,
                              encoding="utf-8")

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

    # each RC corner is an extraction of its own. A variant that would measure
    # nothing stops the gate before the first one
    for run_ in variant_runs:
        if not scored_at(run_["corner"]):
            raise CheckError(
                f"pex_rc_corners asks for {run_['variant']}, which runs at "
                f"{run_['corner']['name']}, and no bound in {bounds_path.name} "
                "is scored there: a run with nothing to score is a refusal")
    views: dict[str, dict] = {}
    for run_ in variant_runs:
        variant = run_["variant"]
        vdir = work_dir / f"rc_{variant}"
        vdir.mkdir(exist_ok=True)
        vraw, _log = layoutlib.run_magic_extract(
            vdir, gds_path.resolve(), topcell, parasitics=True, variant=variant)
        vtext = vraw.read_text(encoding="utf-8", errors="replace")
        vcounts = layoutlib.count_parasitics(vtext)
        if not vcounts["r"] and not vcounts["c"]:
            raise CheckError(
                f"{variant}: {vraw.name} carries no R and no C line - magic "
                "extracted no parasitics at this variant")
        ground = ground_finding(ref_text, ref_pins, vtext, topcell, rel_gds)
        unrun = ([ground] if ground is not None else []) + probe_findings(
            bench_text, ref_text, vtext, topcell, rel_gds)
        if unrun:
            for v in unrun:
                v["refs"] = [*v["refs"], variant]
                v["msg"] = f"at RC variant {variant}: {v['msg']}"
            payload = checklib.report(
                SCRIPT, ws / "layout", unrun, topcell=topcell, gds=rel_gds,
                extracted=str(extracted.relative_to(ws)),
                parasitics=parasitics, measured={})
            return payload, args.out
        view = work_dir / f"{topcell}.pex.ordered.{variant}.spice"
        layoutlib.fresh(view)
        view.write_text(reorder_pins(vtext, topcell, ref_pins), encoding="utf-8")
        views[variant] = {"path": view, "parasitics": vcounts}

    fill = None
    if fill_modes:
        fill = fill_view(ws, gds_path, topcell, rel_gds, work_dir, ref_text,
                         ref_pins, raw_text, bench_text, rows, fill_modes)
        if fill["unrun"]:
            payload = checklib.report(
                SCRIPT, ws / "layout", fill["unrun"], topcell=topcell,
                gds=rel_gds, extracted=str(extracted.relative_to(ws)),
                parasitics=parasitics, measured={}, fill=fill["facts"])
            return payload, args.out
        violations += fill["violations"]

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

    variant_rows = []
    for run_ in variant_runs:
        corner, variant = run_["corner"], run_["variant"]
        cname, view = corner["name"], views[variant]
        variant_rows.append({
            "variant": variant, "corner": cname, "pair": run_["pair"],
            "netlist": str(view["path"].relative_to(ws)),
            "sha256": statelib.hash_artifact(view["path"], "text_eol"),
            "parasitics": view["parasitics"],
            "measured": run_bench(corner, scored_at(corner), f"{cname}__{variant}",
                                  f"{cname} / {variant}", [cname, variant],
                                  view["path"])})

    if fill is not None:
        for row in fill["facts"]["views"]:
            mode, path = row["mode"], ws / row["netlist"]
            row["measured_by_corner"] = {
                c["name"]: run_bench(
                    c, scored_at(c), f"{c['name']}__fill_{mode}",
                    f"{c['name']} / fill {mode}", [c["name"], f"fill_{mode}"],
                    path)
                for c in corner_list if scored_at(c)}

    if not by_corner:
        raise CheckError(
            f"no bound in {bounds_path.name} is scored at any of "
            f"{corner_names}: a gate with nothing to run is a refusal")
    typical = typical_corner(corner_list)["name"]
    measured = by_corner.get(typical) or next(iter(by_corner.values()))

    side = stresslib.find_sidecar(ws, block)
    stress = None if side is None else stress_stage(
        ws, side, block, work_dir, extracted, ref_text, ref_cell, topcell,
        str(gds_path.relative_to(ws)), pdk, nominal_vdd, timeout, violations)

    payload = checklib.report(
        SCRIPT, ws / "layout", violations, topcell=topcell,
        gds=str(gds_path.relative_to(ws)),
        extracted=str(extracted.relative_to(ws)),
        parasitics=parasitics, measured=measured, corners=corner_names,
        corner_source=source, measured_by_corner=by_corner,
        not_scored=not_scored, extractor_crosscheck=crosscheck,
        **({"symmetry": symmetry} if symmetry else {}),
        **({"rc_knobs": rc_runs} if rc_runs else {}),
        **({"rc_variants": variant_rows} if variant_rows else {}),
        **({"fill": fill["facts"]} if fill is not None else {}),
        **({"stress": stress} if stress is not None else {}),
        **({"pad_runs": pad_runs} if ua_pins else {}))
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

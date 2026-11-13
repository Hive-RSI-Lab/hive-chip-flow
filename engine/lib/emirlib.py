"""emirlib.py - electromigration and IR drop on an extracted analog layout
(issue #120), for engine/scripts/check_analog_em_ir.py and its tests.

    load_limits(path)                      -> engine/reference/em_limits.yaml, checked
    parse_fh(text)                         -> {net: FhNet}, magic's FastHenry view
    parse_rnodes(res_ext_text)             -> {node: (x, y)} in um
    subckt_resistors(pex_text, cell)       -> [(a, b, ohm)] of the cell's wire R
    dut_voltages(op, inst, pins, outer)    -> the block's own node voltages
    injections(resistors, volts)           -> {node: A} each node's device current
    net_bound(name, inj, net, limits)      -> tier one: the bound needing no position
    place(net, inj, rnodes, res, volts)    -> device currents on FastHenry nodes
    solve_net(net, placed, limits)         -> [SegCurrent], current in every piece
    cut_clusters(gds, top, limits)         -> {cut layer: [Cluster]} counted on the GDS
    assign_cuts(net, segs, placed, cl, ..) -> each array charged with what can cross it
    wire_over / cuts_over / ir_drops       -> what the gate reports (worst_* the margin)
    resistor_instances(pex, cell, limits)  -> [ResInst] the PDK resistors (#268)
    resistor_heads(gds, top, insts, ..)    -> each head shape and its contact count
    trapezoid_weights / wave_stats         -> a transient's mean |I|, I_rms, I_peak
    pulsed_stat / pulsed_hits              -> those against em_limits.yaml `pulsed:`

Where the current comes from. The gate runs an operating point on the
parasitic netlist (`<top>.pex.spice`, every wire split into resistors between
nodes `<net>.tN`/`<net>.nN`) and prints every node voltage. Each resistor
then carries (Va - Vb)/R, and a node's sum of those is the current its
devices push into the wire there (KCL). That is exact: it is ngspice's own
answer on the extracted circuit.

Tier one (net_bound). With no loop current in a resistive net, no piece of
it carries more than the current going in, so that current over the net's
narrowest metal width, and over one cut, bounds every wire and cut of the
net. A net inside the bound passes with nothing taken from the geometry.

Tier two, a net over the bound. The netlist has no widths, so the same
extraction also writes `<top>.fh` (`extresist geometry` + `extresist
fasthenry`): each split net as nodes at x/y/z and segments with a width, a
thickness and two end nodes. The device currents go on the FastHenry node at
their `rnode` position (place), the net's port node is held at 0 V, and a
nodal solve on the segments' own resistance (sheet x length/width, a cut's
per-cut value) gives each segment's current. On a tree that is the only
answer KCL allows; where pieces run in parallel the split follows their
resistance, as ngspice's does. A metal segment fails when |I|/width is over
its layer's mA/um.

Cuts. The FastHenry file merges a contact into the node it sits in and
writes an array as one segment, so cuts are counted on the GDS: the cut
layer's shapes within CUT_GAP_UM of each other form one array. Which current
crosses which array is not written either, so the charge is a bound: every
array within CUT_REACH_UM of a node carries that node's whole throughput
(its net under-metal1 current, for a contact), but only arrays whose pad
metal a segment of the net overlaps (an array on another net's metal, such
as a gate contact beside a source rail, carries none of it); when none in
reach passes that test, all of them are charged. What one net puts on an
array is capped at the net's tier-one total, and the array's current is
shared by its cuts: equal sharing is the optimistic side of a real array, so
the limit is the per-cut limit times its cuts, no more.

magic's quirks it reads through: node ids restart per network; a cut array's
`<n>_i_j` subnodes may be used without being listed (_alias_subnode); a
crossing placed away from every cut is charged to the smallest array within
FOLD_REACH_UM (_charge_orphan); the substrate joined to metal with no drawn
tap may carry leakage only.

Resistors (#268). A PDK resistor is a subckt in the extraction; its body
current is ngspice's own (`@b.<inst>.brb[i]`), held per um of r_width to
its body limit, and its two heads are the contact arrays inside the
`head` layer shape it lies on (on the GDS): the shape is charged with the
sum over the devices on it of each one's largest |I| (head_current), over
its fewest-cut head row. Its bulk terminal (the third, its well or
substrate) carries the body's displacement current in a transient, and
magic puts that node at one point however far the resistor is: place()
hands such a node's current on along the extracted resistors rather than
refusing it for distance.

Pulsed (#268). A transient is a series per resistor; every current the
gate derives (a node's injection, a segment's, a cut array's) is linear
in it, so tier one bounds a net by pulsed_stat (max(mean, rms / rms_factor,
duty_floor x peak)) of its i_total(t), and tier two solves the net once
per timepoint on the same factorised system. Means and RMS use trapezoid
weights over the bench's window, which must be whole periods. A
transient's well and body capacitance puts real current on the substrate,
which magic joins to its net at a spot with no drawn tap (leaving out the
taps a PDK cell draws itself): past leakage, that current is charged, whole,
to every drawn substrate tap (Cluster.tap, em_limits.yaml substrate_tap).

IR. A supply net's drop is the worst |V(node) - V(pin)| over its extracted
nodes, straight from the operating point.

Refuses rather than guesses: a thickness with no layer, a device current
with no FastHenry node near it, a current with no geometry, a layer
change with no cut within reach, or substrate current in a cell with no
substrate tap is an EmirError, never a skipped wire.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[1] / "reference" / "em_limits.yaml"

# a device current further than this from every FastHenry node of its net
# is a mapping failure (magic writes the two from the same tiles: the
# measured worst on the corpus is under 0.5 um)
PLACE_TOL_UM = 1.5
# a node further than this is not a stub left out of the geometry
FOLD_REACH_UM = 10.0
# currents below this are noise from the solver's tolerance, not current
I_FLOOR_A = 1e-9
# cut shapes this close belong to one array
CUT_GAP_UM = 0.5
# a vertical segment's array must lie within this of its end points
CUT_REACH_UM = 1.0
# a substrate join magic makes with no drawn tap may carry at most this
# share of one contact's limit (junction leakage) uncharged; more is charged
# to the cell's drawn substrate taps, and is a refusal when it has none
SUBSTRATE_MAX_FRAC = 0.1
# z values are written with two decimals


class EmirError(RuntimeError):
    """The geometry or the operating point cannot be read as stated."""


def load_limits(path=REFERENCE) -> dict:
    """{layers: {name: {thick, sheet, limit (A/m) or None, metal}},
    cuts: {gds layer: {names, limit (A per cut), ohm, under, pads}},
    cut_names: {name: {gds, limit}}, resistors: {model: {body (A/m) or None,
    rule, head (gds) or None, head_cut, head_gds, head_limit (A per cut)}},
    pulsed: {rms_factor, duty_floor}, substrate_tap: {gds, diff, implant,
    outside} or None, temperature_c}. A cut layer drawn for several layer pairs (gf180's one
    contact layer serves diffusion, wells and poly) takes the lowest of
    their limits."""
    import yaml

    data = yaml.safe_load(Path(path).read_text())
    try:
        layers = {}
        for name, row in data["wires"].items():
            layers[name] = {"thick": float(row["thick"]),
                            "sheet": float(row["sheet_ohm"]),
                            "limit": float(row["limit_ma_per_um"]) * 1e-3 / 1e-6,
                            "metal": True}
        for name, row in data["under_metal1"].items():
            layers[name] = {"thick": float(row["thick"]),
                            "sheet": float(row["sheet_ohm"]),
                            "limit": None, "metal": False}
        cuts, cut_names = {}, {}
        for name, row in data["cuts"].items():
            lo, hi = row["between"]
            if lo not in layers or hi not in layers:
                raise EmirError(f"cut {name} joins an unknown layer: {lo}, {hi}")
            gds = tuple(row["gds"])
            c = cuts.setdefault(gds, {"names": [], "limit": math.inf,
                                      "ohm": 0.0, "under": False,
                                      "pads": set()})
            c["names"].append(name)
            c["pads"] |= {tuple(p) for p in row["pads"]}
            c["limit"] = min(c["limit"], float(row["limit_ma"]) * 1e-3)
            c["ohm"] = max(c["ohm"], float(row["cut_ohm"]))
            c["under"] |= not (layers[lo]["metal"] and layers[hi]["metal"])
            cut_names[name] = {"gds": gds,
                               "limit": float(row["limit_ma"]) * 1e-3}
        resistors = {}
        for model, row in (data.get("resistors") or {}).items():
            if "body_layer" in row:
                lay = row["body_layer"]
                if lay not in layers or not layers[lay]["metal"]:
                    raise EmirError(f"resistor {model}'s body_layer {lay} "
                                    "is not a wire layer")
                resistors[model] = {"body": layers[lay]["limit"],
                                    "rule": f"{lay} wire", "head": None,
                                    "head_cut": None, "head_limit": None}
                continue
            cut = row["head_cut"]
            if cut not in cut_names:
                raise EmirError(f"resistor {model}'s head_cut {cut} is not "
                                "a cut")
            body = row["body_ma_per_um"]
            resistors[model] = {
                "body": None if body is None else float(body) * 1e-3 / 1e-6,
                "rule": row.get("rule"), "head": tuple(row["head"]),
                "head_cut": cut, "head_gds": cut_names[cut]["gds"],
                "head_limit": cut_names[cut]["limit"]}
        pulsed = data.get("pulsed") or {}
        tap = data.get("substrate_tap")
        if tap is not None:
            if tap["cut"] not in cut_names:
                raise EmirError(f"substrate_tap's cut {tap['cut']} is not "
                                "a cut")
            tap = {"gds": cut_names[tap["cut"]]["gds"],
                   "diff": tuple(tap["diff"]),
                   "implant": tuple(tap["implant"]),
                   "outside": [tuple(o) for o in tap.get("outside") or []]}
        pulsed = {"rms_factor": float(pulsed.get("rms_factor", 1.0)),
                  "duty_floor": float(pulsed.get("duty_floor", 1.0))}
        if not (0 < pulsed["duty_floor"] <= 1 and pulsed["rms_factor"] > 0):
            raise EmirError(f"{path}: pulsed needs 0 < duty_floor <= 1 and "
                            "rms_factor > 0")
    except (KeyError, TypeError, ValueError) as e:
        raise EmirError(f"{path} is malformed: {e}") from e
    return {"layers": layers, "cuts": cuts, "cut_names": cut_names,
            "resistors": resistors, "pulsed": pulsed,
            "substrate_tap": tap,
            "temperature_c": data.get("temperature_c")}


def layers_of(w: float, h: float, limits: dict) -> tuple:
    """The layers a FastHenry segment may lie on, from its thickness h
    (magic writes a wire's layer thickness there, and a cut's edge as both
    w and h). Returns () for a cut."""
    if abs(w - h) < 0.005 and not any(abs(r["thick"] - h) < 0.005
                                      for r in limits["layers"].values()):
        return ()
    found = tuple(n for n, r in limits["layers"].items()
                  if abs(r["thick"] - h) < 0.005)
    if not found:
        raise EmirError(
            f"a FastHenry segment is h={h} um thick, which no layer in "
            "em_limits.yaml is: the gate cannot tell what it is (a tech file "
            "with other thicknesses needs its own table)")
    return found


# --- magic's FastHenry file -------------------------------------------------

@dataclass
class FhNet:
    name: str
    port: str
    nodes: dict = field(default_factory=dict)     # name -> (x, y, z)
    equiv: dict = field(default_factory=dict)     # name -> representative
    segs: list = field(default_factory=list)      # (a, b, w, h)

    def rep(self, n: str) -> str:
        while n in self.equiv and self.equiv[n] != n:
            n = self.equiv[n]
        return n


_NODE = re.compile(r"^N(\S+)\s+x=(\S+)\s+y=(\S+)\s+z=(\S+)", re.M)
_SEG = re.compile(r"^E\S+\s+N(\S+)\s+N(\S+)\s+w=(\S+)\s+h=(\S+)", re.M)
_EQUIV = re.compile(r"^\.equiv\s+(.+)$", re.M | re.I)
_PORT = re.compile(r"^\.external\s+N(\S+)", re.M)


_SUBNODE = re.compile(r"^(.+)_\d+_\d+$")


def _alias_subnode(net, node: str) -> None:
    """magic's writer splits a node a cut array lands on into `<n>_i_j`
    subnodes, one per cut, and names them in an `.equiv <n> <n>_0_0 ...`
    line: one electrical node. For some arrays it writes the segments to
    `<n>_i_j` and leaves out both the subnodes and the .equiv; that is the
    same node, so an unlisted `<n>_i_j` whose `<n>` is listed joins it."""
    if node in net.nodes:
        return
    m = _SUBNODE.match(node)
    if m and m.group(1) in net.nodes:
        net.nodes[node] = net.nodes[m.group(1)]
        net.equiv[node] = m.group(1)


def parse_fh(text: str) -> dict:
    """{net: FhNet}. Node ids are local to a network block (every block
    starts again at N1), so each is read on its own; the port is the block's
    `.external N<net>` node. The substrate plane (Gsub/Nsub) is not a net."""
    nets = {}
    for block in re.split(r"^\* List of nodes in network\s*$", text,
                          flags=re.M)[1:]:
        block = block.split("\n.freq", 1)[0]
        port = _PORT.search(block)
        if not port:
            raise EmirError("a FastHenry network has no .external port")
        net = FhNet(name=port.group(1), port=port.group(1))
        for m in _NODE.finditer(block):
            net.nodes[m.group(1)] = (float(m.group(2)), float(m.group(3)),
                                     float(m.group(4)))
        for m in _EQUIV.finditer(block):
            names = [t[1:] for t in m.group(1).split()]
            for n in names[1:]:
                net.equiv[n] = names[0]
        for m in _SEG.finditer(block):
            a, b = m.group(1), m.group(2)
            for n in (a, b):
                _alias_subnode(net, n)
            if a not in net.nodes or b not in net.nodes:
                raise EmirError(f"net {net.name}: segment {m.group(0)!r} "
                                "names a node the network does not list")
            net.segs.append((a, b, float(m.group(3)), float(m.group(4))))
        if net.port not in net.nodes:
            raise EmirError(f"net {net.name}: its port node is not listed")
        if net.name in nets:
            raise EmirError(f"net {net.name} has two FastHenry networks")
        nets[net.name] = net
    return nets


def parse_rnodes(res_ext_text: str) -> dict:
    """{node: (x, y)} in um from the .res.ext's `rnode` lines; the file's
    `scale` line gives the unit (centimicrons times its third number)."""
    sc = re.search(r"^scale\s+\S+\s+\S+\s+(\S+)", res_ext_text, re.M)
    unit = float(sc.group(1)) * 0.01 if sc else 0.01
    return {m.group(1).lower(): (int(m.group(2)) * unit, int(m.group(3)) * unit)
            for m in re.finditer(
                r'^rnode\s+"([^"]+)"\s+\S+\s+\S+\s+(-?\d+)\s+(-?\d+)',
                res_ext_text, re.M)}


# --- the extracted netlist and its operating point --------------------------

def _logical_lines(text: str):
    cur = None
    for raw in text.splitlines():
        if raw.startswith("+") and cur is not None:
            cur += " " + raw[1:]
            continue
        if cur is not None:
            yield cur
        cur = raw
    if cur is not None:
        yield cur


def subckt_resistors(pex_text: str, cell: str) -> list:
    """[(a, b, ohm)] of every plain R element inside `.subckt cell` (the wire
    resistance magic writes; a PDK resistor is an X instance, not this)."""
    out, inside = [], False
    for line in _logical_lines(pex_text):
        tok = line.split()
        if not tok:
            continue
        head = tok[0].lower()
        if head == ".subckt":
            inside = len(tok) > 1 and tok[1].lower() == cell.lower()
        elif head == ".ends":
            inside = False
        elif inside and head.startswith("r") and len(tok) >= 4:
            try:
                ohm = float(tok[3])
            except ValueError:
                raise EmirError(f"wire resistor {tok[0]} has value {tok[3]!r}")
            out.append((tok[1].lower(), tok[2].lower(), ohm))
    return out


def base_net(node: str) -> str:
    """`vss.t21` and `vss.n3` are pieces of net `vss`."""
    m = re.match(r"^(.*)\.[tn]\d+$", node)
    return m.group(1) if m else node


def dut_voltages(op: dict, inst: str, pins: list, outer: list) -> dict:
    """The DUT's own node voltages from ngspice's `print all` (lowercased
    names): `<inst>.<node>` inside, and each pin from the node the instance
    line ties it to (node 0 and gnd are 0 V)."""
    pre = inst.lower() + "."
    volts = {k[len(pre):]: v for k, v in op.items() if k.startswith(pre)}
    for pin, node in zip(pins, outer):
        node = node.lower()
        if node in ("0", "gnd"):
            volts[pin.lower()] = 0.0
        elif node in op:
            volts[pin.lower()] = op[node]
        else:
            raise EmirError(f"the operating point has no voltage for {node}, "
                            f"the bench node on pin {pin}")
    return volts


def injections(resistors: list, volts: dict) -> dict:
    """{node: A}: the current each extracted node hands its wire, from the
    operating point. A node every resistor of the net reaches with no
    voltage is an error: its current would be a guess."""
    inj = {}
    for a, b, ohm in resistors:
        if a not in volts or b not in volts:
            miss = a if a not in volts else b
            raise EmirError(f"the operating point printed no voltage for "
                            f"extracted node {miss}")
        if ohm <= 0:
            continue
        i = (volts[a] - volts[b]) / ohm
        inj[a] = inj.get(a, 0.0) + i
        inj[b] = inj.get(b, 0.0) - i
    return inj


# --- the bound ---------------------------------------------------------------

def net_bound(name: str, inj: dict, net: FhNet | None,
              limits: dict, i_total: float | None = None) -> dict:
    """Tier one: an upper bound on a net's EM from the operating point and
    the wire widths alone, using no position from magic's FastHenry file.

    A resistive net fed by device currents (and its pin) carries no current
    round a loop, so by superposition no piece of it carries more than
    `i_total`, the current going in (KCL makes that the current coming out).
    No metal wire of it is narrower than its narrowest metal segment, and a
    cut array has at least one cut. So `wire_ratio` (i_total over the
    narrowest width, against the tightest wire limit) and `cut_ratio`
    (i_total against the tightest per-cut limit) at or under 1 pass the net
    with nothing guessed; over 1 the gate looks closer (tier two).

    `i_total`, given, stands for the sum (a transient's pulsed_stat of it:
    one current that each pulsed rule holds to the DC limit)."""
    name = name.lower()
    if i_total is None:
        i_total = sum(a for n, a in inj.items()
                      if base_net(n) == name and a > 0)
    wire_ratio, w_min = 0.0, None
    if net is not None:
        for _a, _b, w, h in net.segs:
            lay = layers_of(w, h, limits)
            lims = [limits["layers"][n]["limit"] for n in lay
                    if limits["layers"][n]["metal"]]
            if not lims:
                continue
            w_min = w if w_min is None else min(w_min, w)
            wire_ratio = max(wire_ratio, i_total / (w * 1e-6) / min(lims))
    elif i_total >= I_FLOOR_A:
        raise EmirError(f"net {name} carries {i_total:.3g} A but magic's "
                        "FastHenry file has no geometry for it")
    cut_limit = min(c["limit"] for c in limits["cuts"].values())
    return {"net": name, "i_total": i_total, "w_min_um": w_min,
            "wire_ratio": wire_ratio, "cut_ratio": i_total / cut_limit}


@dataclass
class SegCurrent:
    net: str
    a: str            # end nodes (FastHenry names) and their (x, y, z)
    b: str
    xyz_a: tuple
    xyz_b: tuple
    width: float
    layers: tuple     # the layers it may lie on; () for a cut
    amps: float       # from a to b

    @property
    def is_cut(self) -> bool:
        return not self.layers


def _seg_ohm(seg_layers, a, b, w, limits) -> float:
    if not seg_layers:
        return max(c["ohm"] for c in limits["cuts"].values())
    sheet = min(limits["layers"][n]["sheet"] for n in seg_layers)
    run = math.hypot(a[0] - b[0], a[1] - b[1])
    return max(sheet * run / max(w, 1e-3), 1e-6)


class NetSystem:
    """One net's nodal system (solve_net's), built once so that many
    placements of its device currents, one per timepoint of a transient,
    are solved against the same conductances in one call."""

    def __init__(self, net: FhNet, limits: dict):
        import numpy as np

        self.net = net
        reps = sorted({net.rep(n) for n in net.nodes})
        self.idx = idx = {n: i for i, n in enumerate(reps)}
        n = self.n = len(reps)
        self.edges = []
        for a, b, w, h in net.segs:
            lay = layers_of(w, h, limits)
            ra, rb = idx[net.rep(a)], idx[net.rep(b)]
            g = None if ra == rb else 1.0 / _seg_ohm(lay, net.nodes[a],
                                                     net.nodes[b], w, limits)
            self.edges.append((a, b, w, lay, ra, rb, g))
        G = np.zeros((n, n))
        for *_x, ra, rb, g in self.edges:
            if g is None:
                continue
            G[ra, ra] += g
            G[rb, rb] += g
            G[ra, rb] -= g
            G[rb, ra] -= g
        # ground the port, and one node of each piece the port does not reach
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for *_x, ra, rb, _g in self.edges:
            parent[find(ra)] = find(rb)
        port = idx[net.rep(net.port)]
        grounds = {find(port): port}
        for i in range(n):
            grounds.setdefault(find(i), i)
        self.comp = np.array([find(i) for i in range(n)])
        self.floating = [r for r, g in grounds.items() if g != port]
        gset = set(grounds.values())
        self.keep = [i for i in range(n) if i not in gset]
        self.sub = G[np.ix_(self.keep, self.keep)] if self.keep else None
        self.ra = np.array([e[4] for e in self.edges], dtype=int)
        self.rb = np.array([e[5] for e in self.edges], dtype=int)
        self.g = np.array([0.0 if e[6] is None else e[6] for e in self.edges])

    def solve_many(self, placements: list) -> tuple:
        """(amps [edge, k] from a to b, leftover [k]) for each placement
        [(fh node, A)] in `placements`: G v = inj with the grounds at 0 V;
        leftover is what lands on pieces the port does not reach."""
        import numpy as np

        k = len(placements)
        inj = np.zeros((self.n, k))
        for j, placed in enumerate(placements):
            for node, amps in placed:
                inj[self.idx[self.net.rep(node)], j] += amps
        leftover = np.zeros(k)
        for root in self.floating:
            leftover += np.abs(inj[self.comp == root].sum(axis=0))
        v = np.zeros((self.n, k))
        if self.keep:
            # inj is current INTO the wire at a node; G v = inj
            v[self.keep] = np.linalg.solve(self.sub, inj[self.keep])
        amps = (v[self.ra] - v[self.rb]) * self.g[:, None]
        return amps, leftover

    def segs(self, amps) -> list:
        net = self.net
        return [SegCurrent(net.name, a, b, net.nodes[a], net.nodes[b], w,
                           lay, float(i))
                for (a, b, w, lay, *_x), i in zip(self.edges, amps)]


def solve_net(net: FhNet, placed: list, limits: dict) -> tuple:
    """Each segment's current from the device currents `placed` ([(node, A)]
    on this net's FastHenry nodes), with the port at 0 V. Returns
    ([SegCurrent], leftover): leftover is the current of any piece of the
    net the port does not reach (it must be ~0, or a device current landed
    on the wrong piece)."""
    system = NetSystem(net, limits)
    amps, leftover = system.solve_many([placed])
    return system.segs(amps[:, 0]), float(leftover[0])


def place(net: FhNet, inj: dict, rnodes: dict, resistors: list,
          volts: dict, cache: dict | None = None,
          bulk: frozenset = frozenset()) -> tuple:
    """([(fh node, A)], folded): each of the net's extracted node currents
    put on the FastHenry node nearest its rnode position (the lowest one
    there: a device terminal sits on diffusion or poly, under the metal over
    it). The port's own current is left out: the port is the solve's
    reference.

    A node with no FastHenry node within PLACE_TOL_UM lies on a piece magic
    leaves out of the geometry (a poly resistor's head, between the
    resistor's terminal and its first contact). Its current is handed on,
    exactly, through its extracted resistors to their other ends (each gets
    the (Va - Vb)/R the resistor carried), until it lands on a node the
    geometry has. Those pieces are under metal1, where the gate checks no
    wire, and `folded` counts them. A node more than FOLD_REACH_UM from every
    FastHenry node is refused: that is not a stub but a wire the geometry
    lost, except for a node in `bulk` (handed_on_nodes): a PDK resistor's
    well or substrate terminal, which magic puts at one point of a body
    that is no wire at all, or a dummy resistor's head; what flows there
    (in a transient, the body capacitance's current) is handed on, still
    exactly, through the extracted resistors to the taps however far they
    are. `cache` (a dict kept between calls on one net) keeps the
    nearest-node search of a node from one timepoint to the next."""
    name = net.name.lower()
    port = net.port.lower()
    pts = list(net.nodes.items())
    cache = {} if cache is None else cache

    def nearest(node):
        if node not in cache:
            cache[node] = _nearest(node)
        return cache[node]

    def _nearest(node):
        x, y = rnodes[node]
        best = min(pts, key=lambda p: (round(math.hypot(p[1][0] - x,
                                                        p[1][1] - y), 2),
                                       p[1][2]))
        return best[0], math.hypot(best[1][0] - x, best[1][1] - y)

    cur = {n: a for n, a in inj.items() if base_net(n) == name}
    for node in cur:
        if node not in rnodes and node != port:
            if abs(cur[node]) >= I_FLOOR_A:
                raise EmirError(f"extracted node {node} carries "
                                f"{cur[node]:.3g} A and the .res.ext gives "
                                "no position for it")
    near = {n: nearest(n) for n in cur if n in rnodes and n != port}
    far = [n for n, (_fh, d) in near.items() if d > PLACE_TOL_UM]
    for n in far:
        if (near[n][1] > FOLD_REACH_UM and n not in bulk
                and abs(cur[n]) >= I_FLOOR_A):
            raise EmirError(
                f"extracted node {n} carries {cur[n]:.3g} A, and the nearest "
                f"FastHenry node of net {net.name} is {near[n][1]:.2f} um "
                "away: the geometry and the netlist disagree")
    far_set = set(far)
    edges = [(a, b, ohm) for a, b, ohm in resistors
             if base_net(a) == name and ohm > 0
             and (a in far_set or b in far_set)]
    folded = 0
    for n in far:
        for i, (a, b, ohm) in enumerate(edges):
            if ohm is None or n not in (a, b):
                continue
            other = b if a == n else a
            amps = (volts[n] - volts[other]) / ohm
            cur[other] = cur.get(other, 0.0) + amps
            cur[n] -= amps
            edges[i] = (a, b, None)
        folded += 1
        if abs(cur[n]) > max(I_FLOOR_A, 1e-6 * abs(inj.get(n, 0.0))):
            raise EmirError(f"extracted node {n}'s current could not be "
                            "handed on through its resistors")
    for n in far:
        if abs(cur[n]) >= I_FLOOR_A:
            raise EmirError(f"extracted node {n}'s current was handed to it "
                            "from another node off the geometry and stops "
                            "there")
    out = []
    for node, amps in cur.items():
        if node == port or node in far_set or abs(amps) < I_FLOOR_A:
            continue
        if node not in near:          # handed on to it from a far node
            if node not in rnodes or nearest(node)[1] > PLACE_TOL_UM:
                raise EmirError(f"extracted node {node}'s current was handed "
                                "to it from a node off the geometry, and it "
                                "is off the geometry too")
            near[node] = nearest(node)
        out.append((near[node][0], amps))
    return out, folded


# --- cuts on the GDS --------------------------------------------------------

@dataclass
class Cluster:
    gds: tuple        # the cut layer
    box: tuple        # x0, y0, x1, y1 in um
    cuts: int
    amps: float = 0.0
    nets: set = field(default_factory=set)
    pad: object = None   # klayout Region: the drawn metal the array lands on
    dbu: float = 0.001
    tap: bool = False    # a substrate tap (em_limits.yaml substrate_tap)


def cut_clusters(gds_path, topcell: str, limits: dict) -> dict:
    """{gds cut layer: [Cluster]} for every cut layer the table names, the
    layer's shapes grouped into arrays (within CUT_GAP_UM), each with the
    merged shapes of its pad layers that touch it (Cluster.pad). Each pad
    layer is picked on its own: merged together, a metal1 shape and a
    metal2 one that overlap anywhere would join, and the pad would chain
    across the layers over the whole cell. An array with a cut on a
    substrate tap (em_limits.yaml substrate_tap) is marked Cluster.tap."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise EmirError(f"{gds_path} has no cell {topcell!r}")
    dbu = layout.dbu
    grow = int(round(CUT_GAP_UM / 2 / dbu))

    def region(lay):
        i = layout.find_layer(*lay)
        return (kdb.Region() if i is None else
                kdb.Region(cell.begin_shapes_rec(i)).merged())
    tap = limits.get("substrate_tap")
    tap_area = None
    if tap is not None:
        tap_area = region(tap["diff"]) & region(tap["implant"])
        for o in tap["outside"]:
            tap_area -= region(o)
    out = {}
    for gds in limits["cuts"]:
        li = layout.find_layer(*gds)
        clusters = []
        metals = []
        for pad in limits["cuts"][gds]["pads"]:
            pli = layout.find_layer(*pad)
            if pli is not None:
                metals.append(kdb.Region(cell.begin_shapes_rec(pli)).merged())
        if li is not None:
            shapes = kdb.Region(cell.begin_shapes_rec(li))
            shapes.merged_semantics = False
            merged = kdb.Region(cell.begin_shapes_rec(li)).sized(grow).merged()
            for poly in merged.each():
                b = poly.bbox()
                core = shapes.interacting(kdb.Region(poly))
                n = core.count()
                pad = kdb.Region()
                for m in metals:
                    pad += m.interacting(core)
                on_tap = (tap_area is not None and gds == tap["gds"]
                          and core.inside(tap_area).count() > 0)
                clusters.append(Cluster(gds, (b.left * dbu + CUT_GAP_UM / 2,
                                              b.bottom * dbu + CUT_GAP_UM / 2,
                                              b.right * dbu - CUT_GAP_UM / 2,
                                              b.top * dbu - CUT_GAP_UM / 2), n,
                                        pad=pad, dbu=dbu, tap=on_tap))
        out[gds] = clusters
    return out


def _box_dist(box, x, y) -> float:
    dx = max(box[0] - x, 0.0, x - box[2])
    dy = max(box[1] - y, 0.0, y - box[3])
    return math.hypot(dx, dy)


def net_footprint(net: FhNet, dbu: float, limits: dict):
    """The net's metal and cut segments as one klayout Region, each segment
    the rectangle its end points and width span: where in x/y the net has
    metal, whatever metal layer. Wires under metal1 (diffusion, poly, the
    substrate) are left out: a net reaches a cut's pad only through metal
    or a cut, and its substrate plane would cover every pad in the cell.
    A cut segment adds only its end points, not the square of its width:
    magic writes a whole merged array (and a join it draws no cut for) as
    one square cut that can reach past the array onto a neighbour's pad,
    while the node it sits at lies on the array's own pad."""
    import klayout.db as kdb

    out = kdb.Region()
    for a, b, w, h in net.segs:
        if a not in net.nodes or b not in net.nodes:
            continue
        lay = layers_of(w, h, limits)
        if lay and not any(limits["layers"][n]["metal"] for n in lay):
            continue
        (xa, ya, _), (xb, yb, _) = net.nodes[a], net.nodes[b]
        g = w / 2 if lay else dbu
        out.insert(kdb.Box(int(round((min(xa, xb) - g) / dbu)),
                           int(round((min(ya, yb) - g) / dbu)),
                           int(round((max(xa, xb) + g) / dbu)),
                           int(round((max(ya, yb) + g) / dbu))))
    return out


def _on_net(c: "Cluster", footprint) -> bool:
    """Whether the net can reach array c: a segment of it overlaps the
    metal the array lands on. Every layer counts (the segments' thickness
    cannot tell metal1-4 apart), so a strap of the net passing over another
    net's pad counts too: the test can only keep an array, never drop one
    the net reaches."""
    if c.pad is None:
        return True
    return not (c.pad & footprint).is_empty()


def node_flows(net: FhNet, segs: list, placed: list, limits: dict) -> dict:
    """{representative node: {through, under, metal, cut, xy}} per node of
    one net: `through` is the most current the node can pass between its
    layers, max(sum in, sum out) over its segments and device current;
    `below` is what the wires under metal1 and the devices there put in or
    take out, net, which is what must cross a contact at the node (KCL);
    under/metal/cut say which kinds of segment meet there; xy lists every
    position the node (and the contact points magic equivs to it) has."""
    out = {}

    def entry(node):
        r = net.rep(node)
        if r not in out:
            out[r] = {"in": 0.0, "out": 0.0, "under": False, "metal": False,
                      "cut": False, "xy": set(), "below": 0.0,
                      "only_well": True}
        return out[r]
    for node, xyz in net.nodes.items():
        entry(node)["xy"].add((xyz[0], xyz[1]))
    for s in segs:
        if net.rep(s.a) == net.rep(s.b):
            continue
        for node, sign in ((s.a, -1.0), (s.b, 1.0)):
            e = entry(node)
            flow = sign * s.amps          # + is into the node
            e["in" if flow > 0 else "out"] += abs(flow)
            if s.is_cut:
                e["cut"] = True
            elif any(limits["layers"][n]["metal"] for n in s.layers):
                e["metal"] = True
            else:
                e["under"] = True
                e["below"] += flow
                e["only_well"] &= s.layers == ("well",)
    for node, amps in placed:
        e = entry(node)
        e["in" if amps > 0 else "out"] += abs(amps)
        if e["under"]:
            e["below"] += amps            # a terminal on the wire under metal1
        elif not (e["metal"] or e["cut"]):
            e["under"] = True             # a device terminal: diffusion or poly
            e["below"] += amps
            e["only_well"] = False
        # else magic puts the current straight onto metal (a pin, or a
        # terminal it extracts on metal): nothing crosses a contact there
    for e in out.values():
        e["through"] = max(e.pop("in"), e.pop("out"))
        e["below"] = abs(e["below"])
    return out


def _charge_orphan(net, node_reps, xy, amps, layers, clusters, what,
                   charge, on_net=None):
    """Charge `amps` that crosses a cut magic put nowhere near one to the
    array with the fewest cuts near it: of the arrays within FOLD_REACH_UM
    of the node or of the far end of any segment at it, those no more than
    CUT_REACH_UM farther than the nearest one. magic's node spot can sit
    off the drawn shapes, so the nearest array need not be the one; of those
    about as near, the smallest is the one the current could load most.
    Only arrays the net reaches (`on_net`, the _on_net test) are candidates
    while any is in reach, so a single-cut array on another net's pad is
    not charged with this net's current; when none is, every array in reach
    is, as if the test were not there. The test cannot tell metal layers
    apart, so another net's array a strap of this net only crosses passes
    it; the nearness band keeps such an array from drawing current
    across the cell from a cut that has its own array beside it. Returns
    the orphan record; refuses when no array is in reach."""
    pts = set(xy)
    for a, b, _w, _h in net.segs:
        ra, rb = net.rep(a), net.rep(b)
        for mine, other in ((ra, b), (rb, a)):
            if mine in node_reps and other in net.nodes:
                pts.add(net.nodes[other][:2])
    dist = {}
    for gds in layers:
        for c in clusters.get(gds, []):
            d = min(_box_dist(c.box, x, y) for x, y in pts)
            if d <= FOLD_REACH_UM:
                dist[id(c)] = (c, d)
    reach = [c for c, _ in dist.values()]
    if on_net is not None:
        reach = [c for c in reach if on_net(c)] or reach
    if reach:
        d0 = min(dist[id(c)][1] for c in reach)
        reach = [c for c in reach if dist[id(c)][1] <= d0 + CUT_REACH_UM]
    best = None
    for c in reach:
        if best is None or c.cuts < best.cuts:
            best = c
    if best is None:
        x, y = sorted(xy)[0]
        raise EmirError(f"net {net.name}: {amps:.3g} A changes layer at "
                        f"({x:.2f}, {y:.2f}) ({what}) but the GDS draws no "
                        f"cut within {FOLD_REACH_UM} um of it")
    charge[id(best)] = (best, charge.get(id(best), (best, 0.0))[1] + amps)
    return {"net": net.name, "at": [round(v, 2) for v in sorted(xy)[0]],
            "amps": amps, "what": what, "charged": best.gds,
            "box": [round(v, 2) for v in best.box], "cuts": best.cuts}


def assign_cuts(net: FhNet, segs: list, placed: list, clusters: dict,
                limits: dict, cap: float = math.inf) -> list:
    """cut_charges, each array's charge capped at `cap` and added to it.
    Returns the orphan records (_charge_orphan)."""
    charge, orphans = cut_charges(net, segs, placed, clusters, limits)
    for c, amps in charge.values():
        c.amps += min(amps, cap)
        c.nets.add(net.name)
    return orphans


def cut_charges(net: FhNet, segs: list, placed: list, clusters: dict,
                limits: dict, ctx: dict | None = None) -> tuple:
    """Charge every cut array with the current that can cross it. Returns
    the crossings magic placed away from any drawn cut, each charged to the
    smallest array near it (_charge_orphan), as facts for the report.

    magic's FastHenry file does not say which segment crosses which cut (it
    merges a contact into the node it sits in, and gives a node the height
    of whichever of its wires it met last), so the charge is a bound: every
    array within CUT_REACH_UM of a node carries all of that node's
    throughput, once per node, of the arrays whose pad metal a segment of
    the net overlaps (_on_net); when none in reach passes that test, every
    one in reach is charged, as if the test were not there. Contact arrays
    are charged, by nodes where a wire under metal1 or a device terminal
    meets metal or a cut segment, with what those under-metal pieces carry
    in or out net; via arrays by nodes with a metal or cut segment, with the
    node's whole throughput. A node can only ever over-charge an array, never hide current
    from it. What one net puts on an array is then capped (assign_cuts) at
    the net's i_total (net_bound): no piece of the net carries more.

    Returns ({id(cluster): (cluster, A)}, orphans), nothing added to the
    clusters. `ctx`, a dict kept between calls on one net (a transient's
    timepoints), keeps the geometry searches, which do not change."""
    orphans = []
    charge = {}                           # id(cluster) -> (cluster, A)
    contact = [g for g, r in limits["cuts"].items() if r["under"]]
    every = list(limits["cuts"])
    flows = node_flows(net, segs, placed, limits)
    ctx = {} if ctx is None else ctx
    footprints = ctx.setdefault("footprints", {})
    near_of = ctx.setdefault("near", {})

    def on_net(c):
        if c.dbu not in footprints:
            footprints[c.dbu] = net_footprint(net, c.dbu, limits)
        return _on_net(c, footprints[c.dbu])

    for node, e in flows.items():
        if e["through"] < I_FLOOR_A:
            continue
        changes = (e["under"] and (e["metal"] or e["cut"])
                   and e["below"] >= I_FLOOR_A)
        hit_contact = False
        for gds, row in limits["cuts"].items():
            if row["under"] and not changes:
                continue
            if not row["under"] and not (e["metal"] or e["cut"]):
                continue
            amps = e["below"] if row["under"] else e["through"]
            if (node, gds) not in near_of:
                near = [c for c in clusters.get(gds, [])
                        if min(_box_dist(c.box, x, y)
                               for x, y in e["xy"]) <= CUT_REACH_UM]
                near_of[node, gds] = [c for c in near if on_net(c)] or near
            for c in near_of[node, gds]:
                charge[id(c)] = (c, charge.get(id(c), (c, 0.0))[1] + amps)
                hit_contact |= row["under"]
        if changes and not hit_contact and e["only_well"]:
            # magic joins the substrate to the net it is labelled with
            # without a drawn tap; the taps it does draw are charged above.
            # Leakage through it is noise. A real current there (a
            # transient's well and body capacitance) crosses the drawn
            # substrate taps magic left out: each is charged with all of it
            # (the bound); with no tap drawn anywhere, it is a refusal
            x, y = sorted(e["xy"])[0]
            rec = {"net": net.name, "at": [round(x, 2), round(y, 2)],
                   "amps": e["below"], "charged": None,
                   "what": "substrate joins metal with no drawn tap"}
            if e["below"] > SUBSTRATE_MAX_FRAC * min(
                    limits["cuts"][g]["limit"] for g in contact):
                taps = [c for cl in clusters.values() for c in cl if c.tap]
                if not taps:
                    raise EmirError(
                        f"net {net.name}: {e['below']:.3g} A goes between "
                        f"the substrate and metal at ({x:.2f}, {y:.2f}) and "
                        "the GDS draws no substrate tap: draw the substrate "
                        "tap it needs")
                for c in taps:
                    charge[id(c)] = (c, charge.get(id(c), (c, 0.0))[1]
                                     + e["below"])
                rec["charged"] = "substrate taps"
                rec["taps"] = len(taps)
                rec["cuts"] = min(c.cuts for c in taps)
            orphans.append(rec)
        elif changes and not hit_contact:
            orphans.append(_charge_orphan(
                net, {node}, e["xy"], e["below"], contact, clusters,
                "a wire under metal1 meets metal", charge, on_net))
    for s in segs:
        if not s.is_cut or abs(s.amps) < I_FLOOR_A:
            continue
        x = (s.xyz_a[0] + s.xyz_b[0]) / 2
        y = (s.xyz_a[1] + s.xyz_b[1]) / 2
        key = ("seg", x, y)
        if key not in near_of:
            near_of[key] = any(_box_dist(c.box, x, y) <= CUT_REACH_UM
                               for cl in clusters.values() for c in cl)
        if not near_of[key]:
            orphans.append(_charge_orphan(
                net, {net.rep(s.a), net.rep(s.b)}, {(x, y)}, abs(s.amps),
                every, clusters, "a cut segment", charge, on_net))
    return charge, orphans


def _wire_density(s, limits):
    """(density A/m, limit A/m) of a metal wire segment, against the
    tightest limit of the layers its thickness allows; None otherwise."""
    lims = [limits["layers"][n]["limit"] for n in s.layers
            if limits["layers"][n]["metal"]]
    if not lims or len(lims) != len(s.layers):
        if lims:
            raise EmirError(f"segment {s.a}-{s.b} of {s.net} may be metal or "
                            "not: em_limits.yaml gives two kinds of layer "
                            "one thickness")
        return None
    return abs(s.amps) / (s.width * 1e-6), min(lims)


def wire_over(segs: list, limits: dict) -> list:
    """[(seg, density A/m, limit A/m)] for every metal segment over its
    layer's limit (density in A per m of width = mA/um)."""
    out = []
    for s in segs:
        hit = _wire_density(s, limits)
        if hit and hit[0] > hit[1]:
            out.append((s, *hit))
    return out


def worst_wire(segs: list, limits: dict):
    """(seg, density, limit) of the metal segment nearest its limit."""
    best = None
    for s in segs:
        hit = _wire_density(s, limits)
        if hit is None:
            continue
        dens, lim = hit
        if best is None or dens / lim > best[1] / best[2]:
            best = (s, dens, lim)
    return best


def cuts_over(clusters: dict, limits: dict) -> list:
    """[(cluster, amps per cut, limit per cut)] over the limit."""
    out = []
    for gds, cl in clusters.items():
        lim = limits["cuts"][gds]["limit"]
        for c in cl:
            if c.amps and c.amps / max(c.cuts, 1) > lim:
                out.append((c, c.amps / max(c.cuts, 1), lim))
    return out


def worst_cut(clusters: dict, limits: dict):
    """(cluster, amps per cut, limit per cut) of the array nearest its limit."""
    best = None
    for gds, cl in clusters.items():
        lim = limits["cuts"][gds]["limit"]
        for c in cl:
            per = c.amps / max(c.cuts, 1)
            if c.amps and (best is None or per / lim > best[1] / best[2]):
                best = (c, per, lim)
    return best


# --- PDK resistors (issue #268) ---------------------------------------------

@dataclass
class ResInst:
    name: str          # the X instance in the extracted cell, lowercased
    model: str
    a: str             # its two terminal nodes
    b: str
    width_um: float    # drawn width: the body limit is per um of it
    length_um: float
    rule: dict         # limits["resistors"][model]
    bulk: str = ""     # its well/substrate terminal, if it has one

    @property
    def key(self) -> tuple:
        """What the .ext writes of it too: the model, the two nets, the
        drawn length and width (to 0.01 um)."""
        return (self.model, frozenset((base_net(self.a), base_net(self.b))),
                round(self.length_um, 2), round(self.width_um, 2))

    def probe(self, inst: str) -> str:
        """The ngspice vector of its body current: every PDK resistor
        subckt writes the body as `rb`, a B source once its value has an
        expression, so `@b.<inst>.<name>.brb[i]`."""
        return f"@b.{inst}.{self.name}.brb[i]".lower()


def resistor_instances(pex_text: str, cell: str, limits: dict) -> list:
    """[ResInst] of every PDK resistor (a model em_limits.yaml's resistors
    table names) instantiated in `.subckt cell`. One with `m` or `par`
    other than 1 is refused: what its body probe returns per copy is not
    the gate's to guess."""
    from netlistlib import spice_number

    out, inside = [], False
    models = limits["resistors"]
    for line in _logical_lines(pex_text):
        tok = line.split()
        if not tok:
            continue
        head = tok[0].lower()
        if head == ".subckt":
            inside = len(tok) > 1 and tok[1].lower() == cell.lower()
            continue
        if head == ".ends":
            inside = False
        if not inside or not head.startswith("x"):
            continue
        pos = [t for t in tok[1:] if "=" not in t]
        if not pos or pos[-1].lower() not in models:
            continue
        model = pos[-1].lower()
        params = {}
        for t in tok[1:]:
            if "=" in t:
                k, v = t.split("=", 1)
                params[k.lower()] = v
        for k in ("m", "par"):
            if k in params and spice_number(params[k]) != 1:
                raise EmirError(f"resistor {tok[0]} ({model}) has {k}="
                                f"{params[k]}: the gate reads one body's "
                                "current and cannot split it per copy")
        w = next((spice_number(params[k]) for k in ("r_width", "wr", "w")
                  if k in params), None)
        ln = next((spice_number(params[k]) for k in ("r_length", "lr", "l")
                   if k in params), None)
        if not w or ln is None or len(pos) < 3:
            raise EmirError(f"resistor {tok[0]} ({model}): no numeric width "
                            "and length, or not two terminals")
        out.append(ResInst(head, model, pos[0].lower(), pos[1].lower(),
                           w * 1e6, ln * 1e6, models[model],
                           pos[2].lower() if len(pos) >= 4 else ""))
    return out


_EXT_RES = re.compile(r"^device\s+rsubckt\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+"
                      r"-?\d+\s+-?\d+\s+(.*)$", re.M)


def ext_resistors(ext_text: str) -> list:
    """[{key, xy}] of every resistor device of a magic .ext: its key as
    ResInst.key and a point of its body in um (the device line's location
    box, in the file's `scale` units). The terminals are the last two
    quoted names; the first is the substrate, the next the body itself."""
    sc = re.search(r"^scale\s+\S+\s+\S+\s+(\S+)", ext_text, re.M)
    unit = float(sc.group(1)) * 0.01 if sc else 0.01
    out = []
    for m in _EXT_RES.finditer(ext_text):
        rest = m.group(4)
        lm = re.search(r"\bl=(\d+)", rest)
        wm = re.search(r"\bw=(\d+)", rest)
        names = re.findall(r'"([^"]*)"', rest)
        if not (lm and wm) or len(names) < 3:
            raise EmirError(f"an .ext resistor line has no l=, w= or two "
                            f"terminals: {m.group(0)[:120]!r}")
        a, b = names[-2].lower(), names[-1].lower()
        out.append({"key": (m.group(1).lower(), frozenset((a, b)),
                            round(int(lm.group(1)) * unit, 2),
                            round(int(wm.group(1)) * unit, 2)),
                    "xy": (int(m.group(2)) * unit, int(m.group(3)) * unit)})
    return out


def resistor_heads(gds_path, topcell: str, insts: list, ext_devs: list,
                   limits: dict) -> list:
    """[{model, layer, box, cuts: [cuts per array], keys: [ResInst.key],
    limit, cut}] per merged shape of a head layer that resistors lie on.

    The netlist gives a resistor no position; the .ext's device line does,
    and an instance is matched to it by ResInst.key. Instances with one key
    (twins: same model, nets and size) cannot be told apart, so each device
    of the key is charged the largest current among them (head_current);
    a different count on the two sides is a refusal. The resistor's head
    layer shape (poly, COMP or Nwell, from em_limits.yaml) at that point
    holds the body and its heads; the contact arrays inside it (cuts within
    CUT_GAP_UM of each other) are its head rows. Every device on the shape
    can send its current through any of them, so each array is charged
    with the sum (the bound the gate checks). Metal resistors have no head
    and are left out."""
    import klayout.db as kdb

    by_key = {}
    for r in insts:
        if r.rule["head"] is not None:
            by_key.setdefault(r.key, []).append(r)
    ext_by_key = {}
    for d in ext_devs:
        if d["key"] in by_key:
            ext_by_key.setdefault(d["key"], []).append(d)
    for k, rs in by_key.items():
        n = len(ext_by_key.get(k, []))
        if n != len(rs):
            model, nets, ln, w = k
            raise EmirError(
                f"{len(rs)} {model} resistor(s) {sorted(r.name for r in rs)} "
                f"on {sorted(nets)} ({w} x {ln} um) in the netlist, {n} in "
                "the .ext: the gate cannot find their heads on the GDS")
    if not by_key:
        return []
    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise EmirError(f"{gds_path} has no cell {topcell!r}")
    dbu = layout.dbu
    shapes, cutl, heads = {}, {}, {}
    for k, devs in ext_by_key.items():
        rule = by_key[k][0].rule
        lay, cut = rule["head"], rule["head_gds"]
        if lay not in shapes:
            li = layout.find_layer(*lay)
            shapes[lay] = (kdb.Region() if li is None else
                           kdb.Region(cell.begin_shapes_rec(li)).merged())
        if cut not in cutl:
            ci = layout.find_layer(*cut)
            reg = (kdb.Region() if ci is None else
                   kdb.Region(cell.begin_shapes_rec(ci)))
            reg.merged_semantics = False
            cutl[cut] = reg
        for d in devs:
            x, y = (int(round(v / dbu)) for v in d["xy"])
            spot = kdb.Region(kdb.Box(x, y, x + 1, y + 1))
            body = shapes[lay].interacting(spot)
            if body.count() != 1:
                raise EmirError(
                    f"resistor {k[0]} at ({d['xy'][0]:.2f}, {d['xy'][1]:.2f}) "
                    f"um lies on {body.count()} shapes of GDS layer "
                    f"{lay[0]}/{lay[1]}, not one: its heads cannot be found")
            poly = next(body.each())
            hk = (lay, poly.bbox().left, poly.bbox().bottom,
                  poly.bbox().right, poly.bbox().top, str(poly))
            if hk not in heads:
                inner = cutl[cut].inside(body)
                grow = int(round(CUT_GAP_UM / 2 / dbu))
                arrays = [inner.interacting(kdb.Region(p)).count()
                          for p in inner.sized(grow).merged().each()]
                b = poly.bbox()
                heads[hk] = {"model": k[0], "layer": list(lay),
                             "box": [round(v * dbu, 3) for v in
                                     (b.left, b.bottom, b.right, b.top)],
                             "cuts": arrays, "keys": [],
                             "cut": rule["head_cut"],
                             "limit": rule["head_limit"]}
            heads[hk]["keys"].append(k)
    return list(heads.values())


def handed_on_nodes(insts: list, mos_bulks=()) -> frozenset:
    """The extracted nodes place() hands on along the wire R instead of
    refusing for distance: each resistor's and each MOS's (`mos_bulks`,
    device_terminals) bulk terminal, and both heads of a dummy (a resistor
    with both heads on one net). All carry only the current a body's
    capacitance or junction, or a strap's own drop, pushes through it, and
    magic writes their rnode at the device, off the net's metal."""
    out = {r.bulk for r in insts if r.bulk} | set(mos_bulks)
    for r in insts:
        if base_net(r.a) == base_net(r.b):
            out |= {r.a, r.b}
    return frozenset(out)


# a MOS model: gf180mcu's nfet_03v3 / pfet_06v0, sky130's sky130_fd_pr__nfet_01v8
MOS_RE = re.compile(r"(?:^|__)[np]fet_", re.I)


def device_terminals(pex_text: str, cell: str) -> tuple:
    """(terminals, mos_bulks): every node an X or M device instance of
    `.subckt cell` touches, and the bulk (fourth) node of each MOS one
    (a model MOS_RE matches)."""
    terms, bulks, inside = set(), set(), False
    for line in _logical_lines(pex_text):
        tok = line.split()
        if not tok:
            continue
        head = tok[0].lower()
        if head == ".subckt":
            inside = len(tok) > 1 and tok[1].lower() == cell.lower()
            continue
        if head == ".ends":
            inside = False
        if not inside or head[0] not in "xm":
            continue
        pos = [t.lower() for t in tok[1:] if "=" not in t]
        if len(pos) < 2:
            continue
        nodes, model = pos[:-1], pos[-1]
        terms |= set(nodes)
        if MOS_RE.search(model) and len(nodes) >= 4:
            bulks.add(nodes[3])
    return terms, bulks


def body_nodes(bulk, terminals, resistors: list, fh: dict,
               rnodes: dict) -> frozenset:
    """`bulk` (handed_on_nodes) and the well or substrate around it: each
    extracted node more than FOLD_REACH_UM from every FastHenry node of its
    net that no device touches (`terminals`) and that the extracted
    resistors join to a bulk node through such nodes only. Magic splits an
    n-well under a row of PFETs into pieces between their bulk terminals,
    with no metal over them; in a transient each carries only the well's
    own capacitance current. A node of a wire stays out: either a device
    is on it or the walk reaches it through a node the geometry has."""
    def far(n):
        net = fh.get(base_net(n))
        if net is None or n not in rnodes:
            return False
        x, y = rnodes[n]
        return min(math.hypot(p[0] - x, p[1] - y)
                   for p in net.nodes.values()) > FOLD_REACH_UM

    adj = {}
    for a, b, ohm in resistors:
        if ohm > 0:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
    out, todo = set(bulk), list(bulk)
    while todo:
        n = todo.pop()
        for m in adj.get(n, ()):
            if m not in out and m not in terminals and far(m):
                out.add(m)
                todo.append(m)
    return frozenset(out)


def head_current(head: dict, cur_by_key: dict):
    """The current a head shape's arrays are charged with: the sum over its
    devices of the largest |I| among the instances of each one's key
    (`cur_by_key`, a number or a series per key)."""
    return sum(cur_by_key[k] for k in head["keys"])


def body_density(r: ResInst, amps) -> float:
    """|I| over the drawn width, A/m (= mA/um x 1e3)."""
    return abs(amps) / (r.width_um * 1e-6)


# --- transient benches (pulsed nets, issue #268) ------------------------------

def parse_ascii_raw(text: str) -> tuple:
    """(names, values [point, variable]) of an ngspice ascii raw file (`set
    filetype=ascii`, `write`), names lowercased as ngspice writes them
    (`time`, `v(<node>)`, `i(<vector>)`). One plot only."""
    import numpy as np

    nv = re.search(r"^No\. Variables:\s*(\d+)", text, re.M)
    npt = re.search(r"^No\. Points:\s*(\d+)", text, re.M)
    vs = re.search(r"^Variables:\s*$", text, re.M)
    val = re.search(r"^Values:\s*$", text, re.M)
    if not (nv and npt and vs and val):
        raise EmirError("the transient raw file is not an ngspice ascii raw "
                        "(no Variables/Points/Values header)")
    n, p = int(nv.group(1)), int(npt.group(1))
    names = []
    for line in text[vs.end():val.start()].strip().splitlines()[:n]:
        parts = line.split()
        if len(parts) < 2:
            raise EmirError(f"raw file variable line {line!r}")
        names.append(parts[1].lower())
    tok = text[val.end():].split()
    if len(tok) < p * (n + 1):
        raise EmirError(f"the raw file holds {len(tok)} values, not the "
                        f"{p} x {n + 1} its header states")
    arr = np.array(tok[:p * (n + 1)], dtype=float).reshape(p, n + 1)
    return names, arr[:, 1:]


def trapezoid_weights(t):
    """Each timepoint's share of the window: the trapezoid rule's weights,
    divided by the window. ngspice's timepoints are not evenly spaced, so
    a plain mean of the samples would weigh the fast edges too much."""
    import numpy as np

    t = np.asarray(t, dtype=float)
    span = t[-1] - t[0] if len(t) > 1 else 0.0
    if span <= 0:
        raise EmirError("the transient window is empty: the raw file has "
                        "fewer than two timepoints")
    w = np.zeros(len(t))
    dt = np.diff(t)
    w[:-1] += dt / 2
    w[1:] += dt / 2
    return w / span


def wave_stats(x, weights) -> tuple:
    """(mean |x|, rms, peak |x|) along the last axis."""
    import numpy as np

    ax = np.abs(np.asarray(x, dtype=float))
    return (ax @ weights, np.sqrt((ax * ax) @ weights), ax.max(axis=-1))


def pulsed_ratios(mean, rms, peak, limit, limits) -> dict:
    """{avg, rms, peak, duty}: each pulsed rule's use of the DC `limit`
    (em_limits.yaml's `pulsed:`): mean |I| over L, I_rms over rms_factor
    x L, and I_peak over L / max(duty, duty_floor), which is
    max(mean, duty_floor x peak) / L. Over 1 is over."""
    p = limits["pulsed"]
    duty = mean / peak if peak > 0 else 1.0
    return {"avg": mean / limit, "rms": rms / (p["rms_factor"] * limit),
            "peak": peak * max(duty, p["duty_floor"]) / limit,
            "duty": duty}


def pulsed_stat(mean, rms, peak, limits):
    """The one current that stands for all three rules against a single
    limit L: each rule is `stat <= L` for this stat (and every rule rises
    with mean, rms and peak, so a bound on them bounds it)."""
    p = limits["pulsed"]
    return max(mean, rms / p["rms_factor"], p["duty_floor"] * peak)


def pulsed_hits(mean, rms, peak, limit, limits) -> tuple:
    """([(kind suffix, ratio)], ratios) of the pulsed rules a current
    breaks: '' the average, '_rms', '_peak'. The peak rule is reported only
    where the duty is under duty_floor: above it the rule is the average
    one again (L / duty against the peak is L against the mean)."""
    r = pulsed_ratios(mean, rms, peak, limit, limits)
    hits = []
    if r["avg"] > 1.0:
        hits.append(("", r["avg"]))
    if r["rms"] > 1.0:
        hits.append(("_rms", r["rms"]))
    if r["peak"] > 1.0 and r["duty"] < limits["pulsed"]["duty_floor"]:
        hits.append(("_peak", r["peak"]))
    return hits, r


def ir_drops(volts: dict, rails: list) -> dict:
    """{rail: (worst |V(node) - V(pin)| in V, node)} over each rail net's
    extracted nodes."""
    out = {}
    for rail in rails:
        r = rail.lower()
        if r not in volts:
            continue
        ref = volts[r]
        worst = (0.0, r)
        for node, v in volts.items():
            if base_net(node) == r and abs(v - ref) > worst[0]:
                worst = (abs(v - ref), node)
        out[rail] = worst
    return out

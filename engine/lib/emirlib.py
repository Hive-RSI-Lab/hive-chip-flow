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

IR. A supply net's drop is the worst |V(node) - V(pin)| over its extracted
nodes, straight from the operating point.

Refuses rather than guesses: a thickness with no layer, a device current
with no FastHenry node near it, a current with no geometry, or a layer
change with no cut within reach is an EmirError, never a skipped wire.
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
# share of one contact's limit (junction leakage); more is a refusal
SUBSTRATE_MAX_FRAC = 0.1
# z values are written with two decimals


class EmirError(RuntimeError):
    """The geometry or the operating point cannot be read as stated."""


def load_limits(path=REFERENCE) -> dict:
    """{layers: {name: {thick, sheet, limit (A/m) or None, metal}},
    cuts: {gds layer: {names, limit (A per cut), ohm, under, pads}},
    temperature_c}. A cut layer drawn for several layer pairs (gf180's one
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
        cuts = {}
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
    except (KeyError, TypeError, ValueError) as e:
        raise EmirError(f"{path} is malformed: {e}") from e
    return {"layers": layers, "cuts": cuts,
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
              limits: dict) -> dict:
    """Tier one: an upper bound on a net's EM from the operating point and
    the wire widths alone, using no position from magic's FastHenry file.

    A resistive net fed by device currents (and its pin) carries no current
    round a loop, so by superposition no piece of it carries more than
    `i_total`, the current going in (KCL makes that the current coming out).
    No metal wire of it is narrower than its narrowest metal segment, and a
    cut array has at least one cut. So `wire_ratio` (i_total over the
    narrowest width, against the tightest wire limit) and `cut_ratio`
    (i_total against the tightest per-cut limit) at or under 1 pass the net
    with nothing guessed; over 1 the gate looks closer (tier two)."""
    name = name.lower()
    i_total = sum(a for n, a in inj.items() if base_net(n) == name and a > 0)
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


def solve_net(net: FhNet, placed: list, limits: dict) -> tuple:
    """Each segment's current from the device currents `placed` ([(node, A)]
    on this net's FastHenry nodes), with the port at 0 V. Returns
    ([SegCurrent], leftover): leftover is the current of any piece of the
    net the port does not reach (it must be ~0, or a device current landed
    on the wrong piece)."""
    import numpy as np

    reps = sorted({net.rep(n) for n in net.nodes})
    idx = {n: i for i, n in enumerate(reps)}
    n = len(reps)
    edges = []
    for a, b, w, h in net.segs:
        lay = layers_of(w, h, limits)
        ra, rb = idx[net.rep(a)], idx[net.rep(b)]
        g = None if ra == rb else 1.0 / _seg_ohm(lay, net.nodes[a],
                                                 net.nodes[b], w, limits)
        edges.append((a, b, w, lay, ra, rb, g))
    G = np.zeros((n, n))
    for *_x, ra, rb, g in edges:
        if g is None:
            continue
        G[ra, ra] += g
        G[rb, rb] += g
        G[ra, rb] -= g
        G[rb, ra] -= g
    inj = np.zeros(n)
    for node, amps in placed:
        inj[idx[net.rep(node)]] += amps
    # ground the port, and one node of each piece the port does not reach
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for *_x, ra, rb, _g in edges:
        parent[find(ra)] = find(rb)
    port = idx[net.rep(net.port)]
    grounds = {find(port): port}
    for i in range(n):
        grounds.setdefault(find(i), i)
    leftover = 0.0
    for root, g in grounds.items():
        if g != port:
            leftover += abs(sum(inj[i] for i in range(n) if find(i) == root))
    keep = [i for i in range(n) if i not in set(grounds.values())]
    v = np.zeros(n)
    if keep:
        sub = G[np.ix_(keep, keep)]
        # inj is current INTO the wire at a node; G v = inj
        v[keep] = np.linalg.solve(sub, inj[keep])
    out = []
    for a, b, w, lay, ra, rb, g in edges:
        amps = 0.0 if g is None else (v[ra] - v[rb]) * g
        out.append(SegCurrent(net.name, a, b, net.nodes[a], net.nodes[b], w,
                              lay, float(amps)))
    return out, leftover


def place(net: FhNet, inj: dict, rnodes: dict, resistors: list,
          volts: dict) -> tuple:
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
    lost."""
    name = net.name.lower()
    port = net.port.lower()
    pts = list(net.nodes.items())

    def nearest(node):
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
        if near[n][1] > FOLD_REACH_UM and abs(cur[n]) >= I_FLOOR_A:
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


def cut_clusters(gds_path, topcell: str, limits: dict) -> dict:
    """{gds cut layer: [Cluster]} for every cut layer the table names, the
    layer's shapes grouped into arrays (within CUT_GAP_UM), each with the
    merged shapes of its pad layers that touch it (Cluster.pad). Each pad
    layer is picked on its own: merged together, a metal1 shape and a
    metal2 one that overlap anywhere would join, and the pad would chain
    across the layers over the whole cell."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(topcell)
    if cell is None:
        raise EmirError(f"{gds_path} has no cell {topcell!r}")
    dbu = layout.dbu
    grow = int(round(CUT_GAP_UM / 2 / dbu))
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
                clusters.append(Cluster(gds, (b.left * dbu + CUT_GAP_UM / 2,
                                              b.bottom * dbu + CUT_GAP_UM / 2,
                                              b.right * dbu - CUT_GAP_UM / 2,
                                              b.top * dbu - CUT_GAP_UM / 2), n,
                                        pad=pad, dbu=dbu))
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
    from it. What one net puts on an array is then capped at `cap`, the
    net's i_total (net_bound): no piece of the net carries more."""
    orphans = []
    charge = {}                           # id(cluster) -> (cluster, A)
    contact = [g for g, r in limits["cuts"].items() if r["under"]]
    every = list(limits["cuts"])
    flows = node_flows(net, segs, placed, limits)
    footprints = {}

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
            near = [c for c in clusters.get(gds, [])
                    if min(_box_dist(c.box, x, y)
                           for x, y in e["xy"]) <= CUT_REACH_UM]
            near = [c for c in near if on_net(c)] or near
            for c in near:
                charge[id(c)] = (c, charge.get(id(c), (c, 0.0))[1] + amps)
                hit_contact |= row["under"]
        if changes and not hit_contact and e["only_well"]:
            # magic joins the substrate to the net it is labelled with
            # without a drawn tap; the taps it does draw are charged above.
            # Leakage through it is noise; a real current there is not
            x, y = sorted(e["xy"])[0]
            if e["below"] > SUBSTRATE_MAX_FRAC * min(
                    limits["cuts"][g]["limit"] for g in contact):
                raise EmirError(
                    f"net {net.name}: {e['below']:.3g} A goes between the "
                    f"substrate and metal at ({x:.2f}, {y:.2f}) where the "
                    "GDS draws no tap: draw the substrate tap it needs")
            orphans.append({"net": net.name, "at": [round(x, 2), round(y, 2)],
                            "amps": e["below"], "charged": None,
                            "what": "substrate joins metal with no drawn tap"})
        elif changes and not hit_contact:
            orphans.append(_charge_orphan(
                net, {node}, e["xy"], e["below"], contact, clusters,
                "a wire under metal1 meets metal", charge, on_net))
    for s in segs:
        if not s.is_cut or abs(s.amps) < I_FLOOR_A:
            continue
        x = (s.xyz_a[0] + s.xyz_b[0]) / 2
        y = (s.xyz_a[1] + s.xyz_b[1]) / 2
        near = any(_box_dist(c.box, x, y) <= CUT_REACH_UM
                   for cl in clusters.values() for c in cl)
        if not near:
            orphans.append(_charge_orphan(
                net, {net.rep(s.a), net.rep(s.b)}, {(x, y)}, abs(s.amps),
                every, clusters, "a cut segment", charge, on_net))
    for c, amps in charge.values():
        c.amps += min(amps, cap)
        c.nets.add(net.name)
    return orphans


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

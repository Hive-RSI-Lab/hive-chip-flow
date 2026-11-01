"""cdclib.py - clock domain crossing analysis over a yosys JSON netlist.

Pure: no tools, no files. check_cdc.py runs yosys (`proc; flatten; techmap`,
so before abc scrambles anything) and hands the flattened top module here.

Model. Every flop's clock pin is walked back to its clock ROOT: an input
port, or a hard macro's output. A divided clock (a flop's Q on a clock pin)
and a gated or muxed clock keep their source's root, so a DOMAIN is a set of
roots: clk_pre and clk_fb, both derived from vco_div2, are one domain, and STA
times their paths; only paths between domains are crossings.

Input ports. A data input belongs to spec's clock.primary (the flow's SDC
times it there), or to the only domain there is, unless spec/cdc.yaml's
`ports` says otherwise (`async`, or a domain's name).

Crossing. A flop whose D cone reads another domain's flop or port. It is
SYNCHRONISED only when the D pin reads that one source through no logic and
its Q feeds one thing, the D of a flop on the same clock and edge. The same
Q pattern with logic ahead of it is comb_before_sync; anything else is
unsynchronized. A flop's async set/reset reading another domain's FLOP is
reported too, unless that flop is a reset synchroniser's stage, whose whole
job is to take an async assert from anywhere. A reset PORT is the next check.

Reset release. Every flop's async set/reset pin is walked back to its sources
(reset_report). A release is clean in a domain only through a RESET SYNCHRONISER
clocked in that domain: at least 2 flops on one clock and edge, all async by one
net, the first with D tied to the value opposite its reset, each stage's D
straight from the stage before, and each stage but the last feeding only the
next. Against that:
  unsynced  a raw source (port, macro, ICG) reaches a flop's async pin. A port
            spec/cdc.yaml places in that domain (`ports`) is taken as already
            synchronous there; the default clock.primary is NOT assumed here,
            since a reset that needs no synchroniser is a claim someone makes.
  comb      logic on the reset net, per net and domain. Its raw sources are
            still walked, so waiving the logic doesn't hide the release.
  nonreset  a flop with no async reset of its own drives an async pin: nothing
            defines that net until the flop's first clock.
A flop in another domain on an async pin is the crossing above, not reported
twice. A same-domain flop with a reset (a soft-reset register) is timed by STA,
and is judged by what resets it. A stage's own raw reset is the point of it.

Clock gates. A net on a clock path driven by logic rather than a buffer. An
AND gate is glitch-free only if each enable comes straight from a flop or
latch that changes while that clock is low (the opposite edge of the same
net), an OR gate while it is high, and an ICG cell always is. Anything else,
every clock mux included, is reported: no structure here can prove a mux
safe. Clock nets leaving through an output port count as well.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

CONST = {"0", "1", "x", "z"}
UNKNOWN_ROOT = "<no driver>"

YOSYS_FF = re.compile(r"^\$_(?:S?DFFC?E?|ALDFFE?|DFFSRE?)_([PN])[PN01]*_$")
YOSYS_LATCH = re.compile(r"^\$_DLATCH(?:SR)?_([PN])[PN01]*_$")
LIB_FF = re.compile(r"__s?dff(n?)(?:r?s?n)?q_\d+$")
LIB_LATCH = re.compile(r"__lat(?:r?s?n)?q_\d+$")
LIB_ICG = re.compile(r"__icgt[pn]_\d+$")
LIB_BUF = re.compile(r"__(?:buf|clkbuf|dly[a-d]|hold)_\d+$")
LIB_INV = re.compile(r"__(?:inv|clkinv)_\d+$")
LIB_AND = re.compile(r"__n?and\d_\d+$")
LIB_OR = re.compile(r"__n?or\d_\d+$")
LIB_MUX = re.compile(r"__mux\d_\d+$")
LIB_CELL = re.compile(r"^gf180mcu_fd_sc_")
YOSYS_RST_FF = re.compile(r"^\$_DFF_[PN][PN]([01])_$")
LIB_RST_FF = re.compile(r"__dffn?([rs])nq_\d+$")
RESET_PINS = ("R", "S", "RN", "SETN", "L")
ASYNC_PINS = RESET_PINS + ("AD",)
SCAN_PINS = ("SI", "SE")


@dataclass
class Flop:
    name: str
    q: int
    clk: int | None
    negedge: bool
    data: list = field(default_factory=list)
    resets: list = field(default_factory=list)
    latch: bool = False
    reset_pins: list = field(default_factory=list)  # ASYNC_PINS less load data
    reset_value: str | None = None   # a plain one-reset flop's reset state


@dataclass
class Comb:
    kind: str                # buf | inv | and | or | mux | logic
    inputs: list             # bits this output reads
    select: list = field(default_factory=list)  # mux select bits


class Netlist:
    """The flattened top, by bit: who drives it and who reads it."""

    def __init__(self, module: dict):
        self.names: dict[int, list[str]] = {}
        for name, net in (module.get("netnames") or {}).items():
            if net.get("hide_name"):
                continue
            bits = net["bits"]
            for i, b in enumerate(bits):
                if b not in CONST:
                    label = f"{name}[{i}]" if len(bits) > 1 else name
                    self.names.setdefault(b, []).append(label)
        self.in_ports: dict[int, str] = {}
        self.out_bits: dict[int, str] = {}
        for name, port in (module.get("ports") or {}).items():
            bits = port["bits"]
            for i, b in enumerate(bits):
                label = f"{name}[{i}]" if len(bits) > 1 else name
                if b in CONST:
                    continue
                if port["direction"] == "input":
                    self.in_ports[b] = label
                elif port["direction"] == "output":
                    self.out_bits[b] = label
        self.flops: dict[int, Flop] = {}
        self.combs: dict[int, Comb] = {}
        self.icgs: dict[int, int] = {}       # Q bit -> CLK bit
        self.macros: dict[int, str] = {}     # output bit -> "inst.pin"
        self.loads: dict[int, list] = {}     # bit -> [(kind, owner)]
        for name, cell in (module.get("cells") or {}).items():
            self._add_cell(name.lstrip("\\"), cell)

    def _add_cell(self, name: str, cell: dict) -> None:
        typ = cell["type"].lstrip("\\")
        conn = cell.get("connections") or {}
        dirs = cell.get("port_directions") or {}

        def bit(pin):
            bits = conn.get(pin) or []
            return bits[0] if bits else None

        def pins(names):
            return [bit(p) for p in names if bit(p) is not None]

        m = YOSYS_FF.match(typ) or YOSYS_LATCH.match(typ)
        if m or LIB_FF.search(typ) or LIB_LATCH.search(typ):
            latch = bool(YOSYS_LATCH.match(typ) or LIB_LATCH.search(typ))
            if m:
                negedge = m.group(1) == "N"
            else:
                lm = LIB_FF.search(typ)
                negedge = bool(lm and lm.group(1)) or "CLKN" in conn
            clk = bit("E") if latch else (bit("C") if m else
                                          bit("CLK") or bit("CLKN"))
            data = pins(("D",) + SCAN_PINS + (() if latch else ("E",)))
            ff = Flop(name, bit("Q"), clk, negedge, data, pins(ASYNC_PINS),
                      latch, pins(RESET_PINS), self._reset_value(typ))
            if ff.q is not None and ff.q not in CONST:
                self.flops[ff.q] = ff
            self._read(ff.clk, ("clock", ff))
            for b in data:
                self._read(b, ("data", ff))
            for b in ff.resets:
                self._read(b, ("reset", ff))
            return
        if LIB_ICG.search(typ):
            q = bit("Q")
            self.icgs[q] = bit("CLK")
            self._read(bit("CLK"), ("clock", None))
            self._read(bit("E"), ("logic", None))
            return
        kind = self._comb_kind(typ)
        if kind is None:
            for pin, bits in conn.items():
                if dirs.get(pin) == "output":
                    for i, b in enumerate(bits):
                        if b not in CONST:
                            self.macros[b] = (
                                f"{name}.{pin}[{i}]" if len(bits) > 1
                                else f"{name}.{pin}")
                else:
                    for b in bits:
                        self._read(b, ("macro", name))
            return
        outs = [b for pin, bits in conn.items() if dirs.get(pin) == "output"
                for b in bits]
        select = [b for pin, bits in conn.items()
                  if kind == "mux" and dirs.get(pin) == "input"
                  and pin.startswith("S") for b in bits]
        ins = [b for pin, bits in conn.items() if dirs.get(pin) == "input"
               for b in bits if b not in select]
        for b in ins + select:
            self._read(b, ("logic" if kind not in ("buf", "inv") else kind,
                           outs))
        for out in outs:
            if out not in CONST:
                self.combs[out] = Comb(kind, ins, select)

    @staticmethod
    def _reset_value(typ: str) -> str | None:
        m = YOSYS_RST_FF.match(typ)
        if m:
            return m.group(1)
        m = LIB_RST_FF.search(typ)
        if m:
            return "0" if m.group(1) == "r" else "1"
        return None

    @staticmethod
    def _comb_kind(typ: str) -> str | None:
        table = {"$_BUF_": "buf", "$_NOT_": "inv", "$_AND_": "and",
                 "$_OR_": "or", "$_MUX_": "mux"}
        if typ in table:
            return table[typ]
        if typ.startswith("$_"):
            return "logic"
        for rx, kind in ((LIB_BUF, "buf"), (LIB_INV, "inv"), (LIB_AND, "and"),
                         (LIB_OR, "or"), (LIB_MUX, "mux")):
            if rx.search(typ):
                return kind
        return "logic" if LIB_CELL.match(typ) else None

    def _read(self, b, load) -> None:
        if b is not None and b not in CONST:
            self.loads.setdefault(b, []).append(load)

    def canon(self, b) -> tuple:
        """(base bit, inverted) through buffers and inverters only."""
        inv = False
        seen = set()
        while b in self.combs and b not in seen:
            seen.add(b)
            c = self.combs[b]
            if c.kind not in ("buf", "inv"):
                break
            inv ^= c.kind == "inv"
            b = c.inputs[0]
        return b, inv

    def label(self, b) -> str:
        names = self.names.get(b)
        if not names:
            return f"net{b}"
        # the deepest alias is the register or wire's own name
        return sorted(names, key=lambda n: (-n.count("."), n))[0]

    def aliases(self, b) -> list[str]:
        return sorted(self.names.get(b) or [f"net{b}"])

    def cone(self, b) -> tuple[set, bool]:
        """(leaf bits, any logic) for a combinational cone: leaves are flop
        and ICG outputs, ports, macro outputs and undriven nets."""
        leaves, logic, seen, stack = set(), False, set(), [b]
        while stack:
            cur = stack.pop()
            if cur in CONST or cur is None or cur in seen:
                continue
            seen.add(cur)
            c = self.combs.get(cur)
            if c is None:
                leaves.add(cur)
                continue
            if c.kind not in ("buf", "inv"):
                logic = True
            stack.extend(c.inputs + c.select)
        return leaves, logic


def base_name(label: str) -> str:
    return re.sub(r"\[\d+\]$", "", label)


class Analysis:
    def __init__(self, module: dict, primary: str | None = None,
                 port_domains: dict | None = None,
                 clock_ports: list | None = None):
        self.nl = Netlist(module)
        self.primary = primary
        self.port_domains = port_domains or {}
        self.clock_ports = set(clock_ports or [])
        self.gates: dict[int, dict] = {}
        self._roots: dict[int, frozenset] = {}
        self.clock_like = self._find_clock_like()
        self.domains = self._flop_domains()
        self._walk_output_clocks()
        self.all_roots = sorted({r for d in self.domains.values() for r in d})
        self.sync_stages_of = self._find_reset_syncs()

    # ---- clocks ----------------------------------------------------------

    def _find_clock_like(self) -> set:
        nl = self.nl
        out = {b for b, name in nl.in_ports.items()
               if base_name(name) in self.clock_ports}
        for ff in nl.flops.values():
            if ff.clk is None or ff.clk in CONST:
                continue
            base, _ = nl.canon(ff.clk)
            if base not in nl.combs:
                out.add(base)
        return out

    def _flop_domains(self) -> dict:
        return {q: self.roots(ff.clk) for q, ff in self.nl.flops.items()}

    def roots(self, b) -> frozenset:
        nl = self.nl
        if b is None or b in CONST:
            return frozenset({UNKNOWN_ROOT})
        base, _ = nl.canon(b)
        if base in self._roots:
            return self._roots[base]
        self._roots[base] = frozenset()      # a ring ends here
        if base in nl.in_ports:
            out = frozenset({base_name(nl.in_ports[base])})
        elif base in nl.macros:
            out = frozenset({nl.macros[base]})
        elif base in nl.flops:
            out = self.roots(nl.flops[base].clk)
        elif base in nl.icgs:
            self.gates[base] = {"net": base, "kind": "icg", "safe": True,
                                "why": "integrated clock gate cell",
                                "clocks": [nl.icgs[base]], "enables": []}
            out = self.roots(nl.icgs[base])
        elif base in nl.combs:
            out = self._gate_roots(base)
        else:
            out = frozenset({UNKNOWN_ROOT})
        # empty only when a ring of flops clocks itself: no source at all
        out = out or frozenset({UNKNOWN_ROOT})
        self._roots[base] = out
        return out

    def _gate_roots(self, base: int) -> frozenset:
        nl = self.nl
        leaves, _ = nl.cone(base)
        mux_data = self._mux_data_leaves(base)
        clocks = {b for b in leaves if b in self.clock_like} | mux_data
        if not clocks:
            clocks = self._guess_clock_leaves(leaves)
        enables = leaves - clocks
        self.gates[base] = self._classify_gate(base, clocks, enables)
        out = frozenset()
        for b in clocks:
            out |= self.roots(b)
        return out or frozenset({UNKNOWN_ROOT})

    def _mux_data_leaves(self, base: int) -> set:
        """Leaves reached through a mux's data pins, never its select."""
        nl = self.nl
        out, seen, stack = set(), set(), [(base, False)]
        while stack:
            cur, via_mux = stack.pop()
            if (cur, via_mux) in seen or cur in CONST or cur is None:
                continue
            seen.add((cur, via_mux))
            c = nl.combs.get(cur)
            if c is None:
                if via_mux:
                    out.add(cur)
                continue
            stack.extend((b, via_mux or c.kind == "mux") for b in c.inputs)
        return out

    def _guess_clock_leaves(self, leaves: set) -> set:
        """A gate with no known clock among its inputs: a port wins, then a
        flop that no other leaf is clocked from, else all of them."""
        nl = self.nl
        ports = {b for b in leaves if b in nl.in_ports}
        if ports:
            return ports
        ffs = {b for b in leaves if b in nl.flops}
        derived = {b for b in ffs
                   if nl.canon(nl.flops[b].clk)[0] in leaves - {b}}
        return (ffs - derived) or set(leaves)

    def _classify_gate(self, base: int, clocks: set, enables: set) -> dict:
        nl = self.nl
        c = nl.combs[base]
        gate = {"net": base, "clocks": sorted(clocks),
                "enables": sorted(enables), "safe": False, "kind": c.kind}
        has_mux = self._cone_has_mux(base)
        if has_mux:
            gate["kind"] = "mux"
            gate["why"] = ("a clock mux: nothing in the netlist shows the "
                           "select changes only while both clocks are safe")
            return gate
        if c.kind not in ("and", "or"):
            gate["kind"] = "logic"
            gate["why"] = f"clock path through {c.kind} logic"
            return gate
        inputs = [nl.canon(b) for b in c.inputs]
        clk_in = [(b, inv) for b, inv in inputs if b in clocks]
        en_in = [(b, inv) for b, inv in inputs if b not in clocks]
        if len(clk_in) != 1 or not en_in:
            gate["why"] = "more than one clock, or no enable, at the gate"
            return gate
        clk_base, clk_inv = clk_in[0]
        for en, _ in en_in:
            ff = nl.flops.get(en)
            if ff is None:
                gate["why"] = (f"enable {nl.label(en)} is not directly a "
                               "flop's output")
                return gate
            en_base, en_inv = nl.canon(ff.clk)
            if en_base != clk_base:
                gate["why"] = (f"enable {nl.label(en)} is not clocked by the "
                               f"net it gates ({nl.label(clk_base)})")
                return gate
            # launches on the falling edge of the base net?
            falling = ff.negedge ^ en_inv ^ clk_inv
            if falling != (c.kind == "and"):
                gate["why"] = (f"enable {nl.label(en)} changes while "
                               f"{nl.label(clk_base)} is "
                               f"{'high' if c.kind == 'and' else 'low'}")
                return gate
        gate["safe"] = True
        gate["why"] = (f"{c.kind.upper()} gate with its enable on the opposite "
                       "edge of the same clock, no logic between")
        return gate

    def _cone_has_mux(self, base: int) -> bool:
        seen, stack = set(), [base]
        while stack:
            cur = stack.pop()
            c = self.nl.combs.get(cur)
            if c is None or cur in seen:
                continue
            seen.add(cur)
            if c.kind == "mux":
                return True
            stack.extend(c.inputs)
        return False

    def _walk_output_clocks(self) -> None:
        """A clock leaving through logic to an output port is a clock net
        too, though no flop here reads it."""
        nl = self.nl
        for b in nl.out_bits:
            base, _ = nl.canon(b)
            if base not in nl.combs or base in self.gates:
                continue
            leaves, _ = nl.cone(base)
            if leaves & self.clock_like:
                self.roots(base)

    # ---- crossings -------------------------------------------------------

    def source_domain(self, leaf) -> frozenset | None:
        """The leaf's domain; None for an input port with no domain known."""
        nl = self.nl
        if leaf in nl.flops:
            return self.domains[leaf]
        if leaf in nl.in_ports:
            name = base_name(nl.in_ports[leaf])
            declared = self.port_domains.get(name)
            if declared == "async":
                return frozenset({f"async:{name}"})
            if declared:
                return frozenset({declared})
            if name in self.all_roots:
                return frozenset({name})
            if self.primary:
                return frozenset({self.primary})
            if len(self.all_roots) == 1:
                return frozenset(self.all_roots)
            return None
        if leaf in nl.icgs:
            return self.roots(leaf)
        if leaf in nl.macros:
            return frozenset({nl.macros[leaf]})
        return frozenset()   # undriven: synth's no_driver reports it

    def sync_stages(self, ff: Flop) -> int:
        """How many further flops this one's Q feeds as a clean chain: its
        Q's one load is the D of a flop on the same clock net and edge."""
        stages, cur, seen = 0, ff, set()
        while cur.q not in seen:
            seen.add(cur.q)
            nxt = self._only_flop_load(cur)
            if nxt is None:
                break
            stages += 1
            cur = nxt
        return stages

    def _only_flop_load(self, ff: Flop) -> Flop | None:
        nl = self.nl
        loads, stack, seen = [], [ff.q], set()
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            if b in nl.out_bits:
                return None
            for kind, owner in nl.loads.get(b, []):
                if kind in ("buf", "inv"):
                    if kind == "inv":
                        return None
                    stack.extend(o for o in owner if o not in CONST)
                else:
                    loads.append((kind, owner))
        if len(loads) != 1 or loads[0][0] != "data":
            return None
        nxt = loads[0][1]
        same = (nl.canon(nxt.clk) == nl.canon(ff.clk)
                and nxt.negedge == ff.negedge)
        return nxt if same else None

    def crossings(self) -> tuple[list, list]:
        """(crossings, input ports with no domain). One crossing per source
        bus, destination bus and status."""
        nl = self.nl
        grouped: dict[tuple, dict] = {}
        no_domain = set()

        def add(status, leaf, ff, dst_dom, src_dom, stages=0):
            src = nl.label(leaf)
            dst = nl.label(ff.q)
            key = (status, base_name(src), base_name(dst))
            g = grouped.setdefault(key, {
                "status": status, "from": base_name(src), "to": base_name(dst),
                "from_aliases": set(), "to_aliases": set(),
                "from_domain": sorted(src_dom), "to_domain": sorted(dst_dom),
                "bits": 0, "stages": 0})
            g["from_aliases"].update(base_name(a) for a in nl.aliases(leaf))
            g["to_aliases"].update(base_name(a) for a in nl.aliases(ff.q))
            g["bits"] += 1
            g["stages"] = max(g["stages"], stages)

        for ff in nl.flops.values():
            dst_dom = self.domains[ff.q]
            for pin_bit in ff.data:
                leaves, logic = nl.cone(pin_bit)
                crossing = []
                for leaf in sorted(leaves):
                    dom = self.source_domain(leaf)
                    if dom is None:
                        no_domain.add(base_name(nl.in_ports[leaf]))
                    elif dom and dom != dst_dom:
                        crossing.append((leaf, dom))
                if not crossing:
                    continue
                stages = self.sync_stages(ff)
                direct = not logic and len(leaves) == 1
                status = ("synchronized" if stages and direct else
                          "comb_before_sync" if stages else "unsynchronized")
                for leaf, dom in crossing:
                    add(status, leaf, ff, dst_dom, dom, stages + 1)
            for pin_bit in ff.resets:
                leaves, _ = nl.cone(pin_bit)
                if ff.q in self.sync_stages_of:
                    break     # a reset synchroniser re-times this release
                for leaf in sorted(leaves):
                    if leaf in nl.flops and self.domains[leaf] != dst_dom:
                        add("async_reset", leaf, ff, dst_dom,
                            self.domains[leaf])
        out = []
        for g in grouped.values():
            g["from_aliases"] = sorted(g["from_aliases"])
            g["to_aliases"] = sorted(g["to_aliases"])
            out.append(g)
        out.sort(key=lambda g: (g["from"], g["to"], g["status"]))
        return out, sorted(no_domain)

    # ---- reset release ---------------------------------------------------

    def _sync_chain(self, last: Flop) -> list | None:
        """The stages of the reset synchroniser ending at this flop, first
        stage first; None if it isn't the end of one."""
        nl = self.nl
        chain, cur, seen = [], last, set()
        while True:
            if (cur.q in seen or cur.latch or len(cur.data) != 1
                    or len(cur.reset_pins) != 1 or cur.reset_value is None
                    or cur.reset_pins[0] in CONST):
                return None
            seen.add(cur.q)
            chain.insert(0, cur)
            d = cur.data[0]
            if d in CONST:
                break
            base, inv = nl.canon(d)
            prev = nl.flops.get(base)
            if inv or prev is None or self._only_flop_load(prev) is not cur:
                return None
            cur = prev
        first = chain[0]
        if len(chain) < 2 or first.data[0] not in ("0", "1") \
                or first.data[0] == first.reset_value:
            return None
        if len({nl.canon(f.reset_pins[0]) for f in chain}) != 1 \
                or len({f.reset_value for f in chain}) != 1:
            return None
        return chain

    def _find_reset_syncs(self) -> dict:
        """{Q bit of every stage: the longest chain it belongs to}."""
        out: dict[int, list] = {}
        for ff in self.nl.flops.values():
            chain = self._sync_chain(ff)
            for stage in chain or []:
                if len(chain) > len(out.get(stage.q, [])):
                    out[stage.q] = chain
        return out

    def reset_syncs(self) -> list[dict]:
        nl = self.nl
        rows = {}
        for chain in self.sync_stages_of.values():
            last = chain[-1]
            rows[last.q] = {
                "net": nl.label(last.q), "stages": len(chain),
                "stage_nets": [nl.label(f.q) for f in chain],
                "reset": nl.label(nl.canon(last.reset_pins[0])[0]),
                "clock": nl.label(nl.canon(last.clk)[0]),
                "domain": sorted(self.domains[last.q])}
        return sorted(rows.values(), key=lambda r: r["net"])

    def _release_status(self, leaf, ff: Flop) -> str:
        """How a source's release reaches this flop's async pin."""
        nl = self.nl
        dom = self.domains[ff.q]
        stage = ff.q in self.sync_stages_of
        if leaf in nl.flops:
            if stage:
                return "sync_stage"
            if self.domains[leaf] != dom:
                return "crossing"
            if leaf in self.sync_stages_of:
                return "synchronized"
            return "same_domain_flop" if nl.flops[leaf].reset_pins \
                else "nonreset"
        if stage:
            return "sync_stage"
        if leaf in nl.in_ports:
            declared = self.port_domains.get(base_name(nl.in_ports[leaf]))
            if declared and declared != "async" and dom == {declared}:
                return "declared_synchronous"
        return "unsynced"

    def reset_report(self) -> tuple[list, list]:
        """(trees, findings). A tree row is one source reaching one domain;
        a finding is {kind, net, aliases, domain, flops, clock_nets}."""
        nl = self.nl
        trees: dict[tuple, dict] = {}
        comb: dict[tuple, dict] = {}

        def bump(table, key, row_net_bit, ff, **fields):
            label = nl.in_ports.get(row_net_bit) or nl.label(row_net_bit)
            row = table.setdefault(key, {
                "net": base_name(label),
                "aliases": set(), "domain": sorted(self.domains[ff.q]),
                "flops": 0, "clock_nets": set(), **fields})
            row["aliases"].update(base_name(a) for a in nl.aliases(row_net_bit))
            row["flops"] += 1
            row["clock_nets"].add(nl.label(nl.canon(ff.clk)[0]))

        for ff in nl.flops.values():
            dom = tuple(sorted(self.domains[ff.q]))
            for pin_bit in ff.reset_pins:
                if pin_bit in CONST:
                    continue
                leaves, logic = nl.cone(pin_bit)
                if logic:
                    base = nl.canon(pin_bit)[0]
                    bump(comb, (base_name(nl.label(base)), dom), base, ff)
                for leaf in sorted(leaves):
                    if not (leaf in nl.flops or leaf in nl.in_ports
                            or leaf in nl.macros or leaf in nl.icgs):
                        continue    # undriven: synth's no_driver reports it
                    status = self._release_status(leaf, ff)
                    key = (base_name(nl.label(leaf)), dom, status)
                    bump(trees, key, leaf, ff, status=status,
                         through_logic=logic)
                    if logic:
                        trees[key]["through_logic"] = True

        def done(row):
            return {**row, "aliases": sorted(row["aliases"]),
                    "clock_nets": sorted(row["clock_nets"])}

        rows = sorted((done(r) for r in trees.values()),
                      key=lambda r: (r["net"], r["domain"], r["status"]))
        findings = [{**r, "kind": r["status"]} for r in rows
                    if r["status"] in ("unsynced", "nonreset")]
        findings += [{**done(r), "kind": "comb"} for _, r in
                     sorted(comb.items())]
        return rows, findings

    # ---- reporting -------------------------------------------------------

    def clocks(self) -> list[dict]:
        nl = self.nl
        rows: dict[int, dict] = {}
        for ff in nl.flops.values():
            if ff.clk is None or ff.clk in CONST:
                continue
            base, inv = nl.canon(ff.clk)
            row = rows.setdefault(base, {
                "net": nl.label(base), "aliases": nl.aliases(base),
                "domain": sorted(self.roots(base)),
                "kind": self._clock_kind(base), "flops": 0, "edges": set()})
            row["flops"] += 1
            row["edges"].add("neg" if ff.negedge ^ inv else "pos")
        for row in rows.values():
            row["edges"] = sorted(row["edges"])
        return sorted(rows.values(), key=lambda r: r["net"])

    def _clock_kind(self, base) -> str:
        nl = self.nl
        if base in nl.in_ports:
            return "port"
        if base in nl.flops:
            return "divided"
        if base in nl.macros:
            return "macro"
        gate = self.gates.get(base)
        if gate:
            return "muxed" if gate["kind"] == "mux" else "gated"
        return "unknown"

    def gate_rows(self) -> list[dict]:
        nl = self.nl
        rows = []
        for base, g in self.gates.items():
            rows.append({
                "net": nl.label(base), "aliases": nl.aliases(base),
                "kind": g["kind"], "safe": g["safe"], "why": g.get("why"),
                "clocks": [nl.label(b) for b in g["clocks"]],
                "enables": [nl.label(b) for b in g["enables"]]})
        return sorted(rows, key=lambda r: r["net"])

    def direct_flop_counts(self) -> dict:
        """{root port: flops reached without passing through a flop}: what
        a clock put on that port alone reaches in STA, the cross-check."""
        nl = self.nl
        counts: dict[str, int] = {}
        for ff in nl.flops.values():
            for root in self._direct_roots(ff.clk, set()):
                counts[root] = counts.get(root, 0) + 1
        return counts

    def _direct_roots(self, b, seen: set) -> set:
        nl = self.nl
        if b is None or b in CONST or b in seen:
            return set()
        seen.add(b)
        if b in nl.in_ports:
            return {base_name(nl.in_ports[b])}
        if b in nl.icgs:
            return self._direct_roots(nl.icgs[b], seen)
        c = nl.combs.get(b)
        if c is None:
            return set()
        out = set()
        for i in c.inputs:
            out |= self._direct_roots(i, seen)
        return out & self._port_clock_names()

    def _port_clock_names(self) -> set:
        return set(self.all_roots)


def waiver_matches(waiver: dict, names: list[str], key: str) -> bool:
    return any(fnmatchcase(n, waiver[key]) for n in names)

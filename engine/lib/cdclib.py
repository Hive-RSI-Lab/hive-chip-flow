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
`ports` says otherwise (`async`, or a domain's name). port_domains is keyed by
a port's name or by one bit of it (`ui_in[7]`), the bit winning.

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
    enable: int | None = None        # a yosys enable flop's E pin


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
        self.cells: dict[int, tuple] = {}    # output bit -> (name, type, conn)
        for name, cell in (module.get("cells") or {}).items():
            self._add_cell(name.lstrip("\\"), cell)

    def _add_cell(self, name: str, cell: dict) -> None:
        typ = cell["type"].lstrip("\\")
        conn = cell.get("connections") or {}
        dirs = cell.get("port_directions") or {}
        for pin, bits in conn.items():
            if dirs.get(pin) == "output":
                for b in bits:
                    if b not in CONST:
                        self.cells[b] = (name, typ, conn)

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
                      latch, pins(RESET_PINS), self._reset_value(typ),
                      bit("E") if m and not latch else None)
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
        self.assumed: set[str] = set()   # input ports given the default
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

    def port_domain(self, label: str) -> str | None:
        """The domain port_domains gives an input bit: by its own label
        (`ui_in[7]`) first, then by its bus (`ui_in`)."""
        return self.port_domains.get(label) or \
            self.port_domains.get(base_name(label))

    def source_domain(self, leaf) -> frozenset | None:
        """The leaf's domain; None for an input port with no domain known.
        An input port given the default (clock.primary, or the one domain)
        is recorded in self.assumed: nothing declared it."""
        nl = self.nl
        if leaf in nl.flops:
            return self.domains[leaf]
        if leaf in nl.in_ports:
            name = base_name(nl.in_ports[leaf])
            declared = self.port_domain(nl.in_ports[leaf])
            if declared == "async":
                return frozenset({f"async:{name}"})
            if declared:
                return frozenset({declared})
            if name in self.all_roots:
                return frozenset({name})
            if self.primary:
                self.assumed.add(name)
                return frozenset({self.primary})
            if len(self.all_roots) == 1:
                self.assumed.add(name)
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

    # ---- buses (#168) ----------------------------------------------------

    def _bus_key(self, leaf) -> str:
        """The source bus a crossing bit belongs to: a register or port with
        its index dropped, unless port_domains names that one bit (then the
        bit is a signal of its own)."""
        nl = self.nl
        if leaf in nl.in_ports:
            label = nl.in_ports[leaf]
            return label if label in self.port_domains else base_name(label)
        if leaf in nl.macros:
            return base_name(nl.macros[leaf])
        return base_name(nl.label(leaf))

    def _group_key(self, leaf, dom) -> str:
        """What a crossing bit's timing is relative to: its source domain,
        or one async port (a bit port_domains names, or the whole port).
        Bits in one group can change in the same source cycle."""
        nl = self.nl
        if leaf in nl.in_ports and self.port_domain(nl.in_ports[leaf]) == \
                "async":
            return "async:" + self._bus_key(leaf)
        return ",".join(sorted(dom))

    def sync_chains(self) -> dict:
        """{last stage Q bit: chain} for every data synchroniser: a flop
        reading one other-domain source through no logic whose Q feeds a
        clean chain (sync_stages > 0). The last stage is where the chain's
        value enters its domain."""
        if getattr(self, "_chains", None) is not None:
            return self._chains
        nl = self.nl
        out = {}
        for ff in nl.flops.values():
            if ff.latch:
                continue
            dst = self.domains[ff.q]
            for pin in ff.data[:1]:
                leaves, logic = nl.cone(pin)
                if logic or len(leaves) != 1:
                    continue
                leaf = next(iter(leaves))
                dom = self.source_domain(leaf)
                if not dom or dom == dst or not self.sync_stages(ff):
                    continue
                last, seen = ff, set()
                while last.q not in seen:
                    seen.add(last.q)
                    nxt = self._only_flop_load(last)
                    if nxt is None:
                        break
                    last = nxt
                out[last.q] = {"leaf": leaf, "first": ff.q, "last": last.q,
                               "src_dom": dom, "dst_dom": dst,
                               "bus": self._bus_key(leaf),
                               "group": self._group_key(leaf, dom)}
        self._chains = out
        return out

    def _select_ok(self, sel_bits, dst_dom, group) -> bool:
        """A select (or enable) that only the destination domain drives and
        that reads a synchroniser of the data's own group: a req/ack or
        mux-enable that says when the data is stable."""
        nl = self.nl
        chains = self.sync_chains()
        hit = False
        for s in sel_bits:
            leaves, _ = nl.cone(s)
            for leaf in leaves:
                ch = chains.get(leaf)
                if ch and ch["group"] == group and ch["dst_dom"] == dst_dom:
                    hit = True
                    continue
                dom = self.source_domain(leaf)
                if dom is None or (dom and dom != dst_dom):
                    return False
        return hit

    def _qualified(self, leaf, ff: Flop, pin, src_dom) -> bool:
        """The crossing bit `leaf` reaches `pin` only through a mux whose
        select, or a flop enable that, _select_ok qualifies."""
        nl = self.nl
        dst = self.domains[ff.q]
        group = self._group_key(leaf, src_dom)
        if ff.enable is not None and ff.data and pin == ff.data[0] and \
                self._select_ok([ff.enable], dst, group):
            return True
        stack, seen = [pin], set()
        while stack:
            b = stack.pop()
            if b in CONST or b is None or b in seen:
                continue
            seen.add(b)
            if b == leaf:
                return False
            c = nl.combs.get(b)
            if c is None:
                continue
            if c.kind == "mux" and c.select and \
                    self._select_ok(c.select, dst, group):
                continue
            stack.extend(c.inputs + c.select)
        return True

    def bus_findings(self) -> tuple[list, list, list]:
        """(multibit, reconvergence, bus reports).

        multibit: a source bus whose bits are synchronised one by one into
        one domain, two or more bits, and prove_gray can't show at most one
        bit changes per source clock: the receiver can see a value the
        source never held.

        reconvergence: one flop's D cone reads the last stages of two or
        more synchronisers of different source buses in one group (bits
        that can change in the same source cycle): each settles on its own
        cycle, so the logic sees a mix. Bits of ONE bus meeting again are
        the multibit rule's. Only the direct cone is read, not a path that
        passes through another flop first."""
        nl = self.nl
        chains = self.sync_chains()
        buses: dict[tuple, dict] = {}
        for ch in chains.values():
            dst = ",".join(sorted(ch["dst_dom"]))
            g = buses.setdefault((ch["bus"], dst), {
                "bus": ch["bus"], "from_domain": sorted(ch["src_dom"]),
                "to_domain": sorted(ch["dst_dom"]), "leaves": set(),
                "to": set(), "from_aliases": set(), "to_aliases": set()})
            g["leaves"].add(ch["leaf"])
            g.setdefault("stages", []).extend(self._chain_flops(ch))
            g["to"].add(base_name(nl.label(ch["last"])))
            g["from_aliases"].update(base_name(a)
                                     for a in nl.aliases(ch["leaf"]))
            for q in (ch["first"], ch["last"]):
                g["to_aliases"].update(base_name(a) for a in nl.aliases(q))

        def order(bits):
            def k(b):
                m = re.search(r"\[(\d+)\]$", nl.label(b))
                return (base_name(nl.label(b)), int(m.group(1)) if m else -1)
            return sorted(bits, key=k)

        multibit, reports = [], []
        for (bus, _), g in sorted(buses.items()):
            if len(g["leaves"]) < 2:
                continue
            proof = prove_gray(nl, order(g["leaves"]), self.reset_nets())
            self._cover_reset_steps(proof, g["stages"])
            row = {"bus": bus, "from": bus, "to": ", ".join(sorted(g["to"])),
                   "from_domain": g["from_domain"],
                   "to_domain": g["to_domain"], "bits": len(g["leaves"]),
                   "from_aliases": sorted(g["from_aliases"]),
                   "to_aliases": sorted(g["to_aliases"]), "gray": proof}
            reports.append(row)
            if not proof["proven"]:
                multibit.append(row)

        reconv: dict[tuple, dict] = {}
        for ff in nl.flops.values():
            dst = self.domains[ff.q]
            for pin in ff.data:
                leaves, _ = nl.cone(pin)
                by_group: dict[str, list] = {}
                for leaf in leaves:
                    ch = chains.get(leaf)
                    if ch and ch["dst_dom"] == dst:
                        by_group.setdefault(ch["group"], []).append(ch)
                for group, chs in by_group.items():
                    names = sorted({ch["bus"] for ch in chs})
                    if len(names) < 2:
                        continue
                    key = (group, base_name(nl.label(ff.q)), tuple(names))
                    r = reconv.setdefault(key, {
                        "group": group, "buses": names,
                        "from": ", ".join(names),
                        "to": base_name(nl.label(ff.q)),
                        "from_domain": sorted(chs[0]["src_dom"]),
                        "to_domain": sorted(dst),
                        "synchronisers": set(), "from_aliases": set(),
                        "to_aliases": set(), "bits": 0})
                    r["bits"] += 1
                    for ch in chs:
                        r["synchronisers"].add(base_name(nl.label(ch["last"])))
                        r["from_aliases"].update(
                            base_name(a) for a in nl.aliases(ch["leaf"]))
                    r["to_aliases"].update(base_name(a)
                                           for a in nl.aliases(ff.q))
        recon = []
        for r in reconv.values():
            for k in ("synchronisers", "from_aliases", "to_aliases"):
                r[k] = sorted(r[k])
            recon.append(r)
        recon.sort(key=lambda r: (r["to"], r["from"]))
        return multibit, recon, reports

    def _chain_flops(self, ch: dict) -> list:
        """A data synchroniser's stages, first to last."""
        nl = self.nl
        out, cur = [nl.flops[ch["first"]]], nl.flops[ch["first"]]
        while cur.q != ch["last"]:
            cur = self._only_flop_load(cur)
            if cur is None or cur in out:
                break
            out.append(cur)
        return out

    def _cover_reset_steps(self, proof: dict, stages: list) -> None:
        """Mark each sync reset step prove_gray held apart `covered` when
        every receiving stage is reset from the same port too (it is in
        reset while the bus jumps), with `why` either way."""
        nl = self.nl
        rn = {v: k for k, v in self.reset_nets().items()}
        for row in proof.get("sync_resets", []):
            src = self.reset_sources(rn[row["net"]]) if row["net"] in rn \
                else set()
            bare = sorted({base_name(nl.label(f.q)) for f in stages
                           if not (self._flop_reset_sources(f) & src)})
            row["covered"] = bool(src) and not bare
            ports = ", ".join(sorted(base_name(nl.in_ports[b])
                                     for b in src)) or "no port"
            row["why"] = (f"every receiving stage is reset from {ports} too"
                          if row["covered"] else
                          f"receiving stage(s) {', '.join(bare)} not reset "
                          f"from {ports}: they can sample the jump")

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
                    if status != "synchronized" and \
                            self._qualified(leaf, ff, pin_bit, dom):
                        add("qualified", leaf, ff, dst_dom, dom, stages + 1)
                    else:
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

    def reset_nets(self) -> dict:
        """{bit: label} of the design's reset nets, for prove_gray's sync
        reset (#244): an input port named as a reset (RESET_PORT_RE: rst_n,
        reset, sys_rst...), the last stage of a reset synchroniser, and the
        net (through buffers and inverters) and port or synchroniser leaves
        on any flop's async set/reset pin. A clear from anything else is a
        data path, never a reset."""
        if getattr(self, "_reset_nets", None) is not None:
            return self._reset_nets
        nl = self.nl
        out = {b: base_name(name) for b, name in nl.in_ports.items()
               if RESET_PORT_RE.match(base_name(name))}
        for chain in self.sync_stages_of.values():
            out[chain[-1].q] = nl.label(chain[-1].q)
        for ff in nl.flops.values():
            for pin in ff.reset_pins:
                if pin in CONST:
                    continue
                leaves, _ = nl.cone(pin)
                for b in [nl.canon(pin)[0], *leaves]:
                    if b in nl.in_ports or b in self.sync_stages_of or \
                            b == nl.canon(pin)[0]:
                        out.setdefault(b, nl.in_ports.get(b) and base_name(
                            nl.in_ports[b]) or nl.label(b))
        self._reset_nets = out
        return out

    def reset_sources(self, b) -> set:
        """The input ports a reset net comes from: its cone's ports, and a
        reset synchroniser's through the reset it synchronises."""
        nl = self.nl
        out, seen, todo = set(), set(), [b]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            leaves, _ = nl.cone(cur)
            for leaf in leaves:
                if leaf in nl.in_ports:
                    out.add(leaf)
                elif leaf in self.sync_stages_of:
                    pin = self.sync_stages_of[leaf][-1].reset_pins[0]
                    todo.append(pin)
        return out

    def _flop_reset_sources(self, ff: Flop) -> set:
        """Where a flop's own reset comes from: its async pins, and reset
        nets read in its D cone (a sync reset)."""
        nl = self.nl
        out = set()
        for pin in ff.reset_pins:
            if pin not in CONST:
                out |= self.reset_sources(pin)
        rn = self.reset_nets()
        for pin in ff.data:
            leaves, _ = nl.cone(pin)
            for leaf in leaves:
                if leaf in rn:
                    out |= self.reset_sources(leaf)
        return out

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
            declared = self.port_domain(nl.in_ports[leaf])
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


# ---- gray proof (#168) ---------------------------------------------------

GRAY_MAX_STATE = 24      # flops kept as state in the bus's cone of influence
GRAY_MAX_FREE = 10       # free inputs, all 2**N values tried in each state
GRAY_MAX_UNINIT = 8      # state flops with no async reset, started both ways
GRAY_MAX_STATES = 1 << 14    # reachable states explored

RESET_PORT_RE = re.compile(r"(?i)^(?:\w*_)?n?[as]?(?:rst|reset)"
                           r"(?:_?n|_?b|_ni)?$")
_FF_TYPE = re.compile(r"^\$_(DFF|DFFE|SDFF|SDFFE|SDFFCE|DFFSR|DFFSRE|ALDFF|"
                      r"ALDFFE)_([PN01]+)_$")


def _mux(a, b, s, full):
    return (a & (full ^ s)) | (b & s)


_GATE_EVAL = {
    "$_BUF_": lambda v, f: v["A"],
    "$_NOT_": lambda v, f: f ^ v["A"],
    "$_AND_": lambda v, f: v["A"] & v["B"],
    "$_NAND_": lambda v, f: f ^ (v["A"] & v["B"]),
    "$_OR_": lambda v, f: v["A"] | v["B"],
    "$_NOR_": lambda v, f: f ^ (v["A"] | v["B"]),
    "$_XOR_": lambda v, f: v["A"] ^ v["B"],
    "$_XNOR_": lambda v, f: f ^ v["A"] ^ v["B"],
    "$_ANDNOT_": lambda v, f: v["A"] & (f ^ v["B"]),
    "$_ORNOT_": lambda v, f: v["A"] | (f ^ v["B"]),
    "$_MUX_": lambda v, f: _mux(v["A"], v["B"], v["S"], f),
    "$_NMUX_": lambda v, f: f ^ _mux(v["A"], v["B"], v["S"], f),
    "$_AOI3_": lambda v, f: f ^ ((v["A"] & v["B"]) | v["C"]),
    "$_OAI3_": lambda v, f: f ^ ((v["A"] | v["B"]) & v["C"]),
    "$_AOI4_": lambda v, f: f ^ ((v["A"] & v["B"]) | (v["C"] & v["D"])),
    "$_OAI4_": lambda v, f: f ^ ((v["A"] | v["B"]) & (v["C"] | v["D"])),
    "$_MUX4_": lambda v, f: _mux(_mux(v["A"], v["B"], v["S"], f),
                                 _mux(v["C"], v["D"], v["S"], f), v["T"], f),
}


def _ff_model(typ: str):
    """(next-state pins, next(vals, q, full), async reset value or None) for
    a yosys flop type; None for one this can't simulate (latches, library
    cells). Async set/reset/load pins are not modelled: see prove_gray."""
    m = _FF_TYPE.match(typ)
    if not m:
        return None
    kind, pol = m.groups()

    def act(v, pin, p, full):
        return v[pin] if p == "P" else full ^ v[pin]

    if kind == "DFF" and len(pol) in (1, 3):
        return ("D",), (lambda v, q, f: v["D"]), \
            (pol[2] if len(pol) == 3 else None)
    if kind == "DFFE" and len(pol) in (2, 4):
        e = pol[-1]
        return ("D", "E"), (lambda v, q, f: _mux(q, v["D"], act(
            v, "E", e, f), f)), (pol[2] if len(pol) == 4 else None)
    if kind in ("SDFF", "SDFFE", "SDFFCE"):
        r, val = pol[1], pol[2]
        rv = (lambda f: f) if val == "1" else (lambda f: 0)
        if kind == "SDFF":
            return ("D", "R"), (lambda v, q, f: _mux(
                v["D"], rv(f), act(v, "R", r, f), f)), None
        e = pol[3]
        if kind == "SDFFE":
            return ("D", "R", "E"), (lambda v, q, f: _mux(
                _mux(q, v["D"], act(v, "E", e, f), f), rv(f),
                act(v, "R", r, f), f)), None
        return ("D", "R", "E"), (lambda v, q, f: _mux(
            q, _mux(v["D"], rv(f), act(v, "R", r, f), f),
            act(v, "E", e, f), f)), None
    if kind in ("DFFSR", "ALDFF"):
        return ("D",), (lambda v, q, f: v["D"]), None
    if kind in ("DFFSRE", "ALDFFE"):
        e = pol[-1]
        return ("D", "E"), (lambda v, q, f: _mux(q, v["D"], act(
            v, "E", e, f), f)), None
    return None


def _lane_pattern(k: int, lanes: int) -> int:
    """Lane j holds bit k of j."""
    period = 1 << (k + 1)
    unit = ((1 << (1 << k)) - 1) << (1 << k)
    out = 0
    for p in range(lanes // period):
        out |= unit << (p * period)
    return out


def _ternary(typ_eval, ins: dict):
    """A gate or next-state function over 0/1/X (2) inputs: 0 or 1 when
    every value of the X inputs gives it, else X."""
    xs = [p for p, v in ins.items() if v == 2]
    outs = set()
    for n in range(1 << len(xs)):
        v = dict(ins)
        for t, p in enumerate(xs):
            v[p] = n >> t & 1
        outs.add(typ_eval(v) & 1)
        if len(outs) > 1:
            return 2
    return outs.pop()


def prove_gray(nl: Netlist, bus: list, resets: dict | None = None) -> dict:
    """Whether the flops `bus` (Q bits) change at most one bit per clock, in
    every state reachable from reset. Explicit-state: the state is the flops
    on the bus's clock net and edge in its cone of influence; everything else
    it reads (ports, other domains, macros, x) is a free input, every value
    tried each cycle. A state flop whose next state reads only free inputs
    (a synchroniser stage, say) is made free too, until none is left: more
    behaviours than the real design has, so a proof holds and a failure may
    be a false alarm. Start: each flop with an async reset at its reset
    value, every other flop at every value. Async set/reset/load is not
    modelled once running: a reset drops the bus to its reset value, which
    the receiving side has to handle anyway.

    A synchronous reset (#244): `resets` is {bit: label} of the design's
    reset nets (Analysis.reset_nets). One the cone reads that, held at one
    value, forces every bus flop's next state to a constant whatever the
    state and the other inputs (three-valued) is the bus's sync reset: the
    proof starts from the state that reset leaves (flops it doesn't force
    start both ways) and holds it inactive, so its many-bit jump to the
    reset value is not an ordinary step; it is returned in `sync_resets`
    ({net, active, to}) for the caller to check against the receiver's own
    reset. A reset net that doesn't force the bus is a free input, and so
    is any other clear (a data-path signal): its jump still fails.

    {"proven": bool, "why": str, "states": n, "example": [from, to]?,
    "sync_resets": [...]?}. Anything it can't model (a latch, a library
    cell, a bus bit that is a port, too many flops or inputs) is not
    proven, never proven."""
    def fail(why, **extra):
        return {"proven": False, "why": why, **extra}

    resets = resets or {}

    if not bus:
        return fail("no bus bits")
    key = None
    for b in bus:
        ff = nl.flops.get(b)
        if ff is None:
            return fail(f"{nl.label(b)} is not a flop: a port or macro pin "
                        "can't be proven gray")
        k = (nl.canon(ff.clk), ff.negedge)
        if key is not None and k != key:
            return fail("the bus's bits are on different clocks or edges")
        key = k

    def model(q):
        name, typ, conn = nl.cells[q]
        return name, typ, conn, _ff_model(typ)

    forced_free: set = set()
    while True:
        state, free, combs = list(bus), [], []
        state_set, free_set, seen = set(bus), set(), set()
        reads: dict[int, set] = {}
        i = 0
        while i < len(state):
            q = state[i]
            i += 1
            name, typ, conn, mdl = model(q)
            if mdl is None:
                return fail(f"flop {name} ({typ}) can't be simulated")
            support = set()
            stack = [(conn[p][0], q) for p in mdl[0]]
            local_seen = set()
            while stack:
                b, owner = stack.pop()
                if b in CONST:
                    if b in ("x", "z"):
                        support.add(("x", owner))
                    continue
                if b in local_seen:
                    continue
                local_seen.add(b)
                if b in resets:
                    support.add(("reset", b))
                    continue
                ff = nl.flops.get(b)
                cell = nl.cells.get(b)
                if ff is not None:
                    same = (not ff.latch and b not in forced_free and
                            (nl.canon(ff.clk), ff.negedge) == key and
                            _ff_model(cell[1]) is not None)
                    if same:
                        support.add(b)
                        if b not in state_set:
                            state_set.add(b)
                            state.append(b)
                    else:
                        support.add(("free", b))
                    continue
                if cell is None or cell[1] not in _GATE_EVAL:
                    if cell is not None and cell[1].startswith("$_") and \
                            b not in nl.macros:
                        return fail(f"cell {cell[0]} ({cell[1]}) can't be "
                                    "simulated")
                    support.add(("free", b))
                    continue
                if b not in seen:
                    seen.add(b)
                    combs.append(b)
                stack.extend((bits[0], b) for pin, bits in cell[2].items()
                             if pin != "Y" and bits)
            reads[q] = support
        cut = {q for q in state if q not in bus and
               all(isinstance(s, tuple) for s in reads[q])}
        if not cut:
            break
        forced_free |= cut
    rst_bits = []
    for q in state:
        for s in reads[q]:
            if isinstance(s, tuple) and s not in free_set:
                free_set.add(s)
                if s[0] == "reset":
                    rst_bits.append(s[1])
                else:
                    free.append(s)
    if len(state) > GRAY_MAX_STATE:
        return fail(f"{len(state)} flops in its cone of influence, over "
                    f"{GRAY_MAX_STATE}")

    # comb cells in evaluation order
    order, done = [], set()
    for root in combs:
        stack = [(root, False)]
        while stack:
            b, expanded = stack.pop()
            if b in done:
                continue
            if expanded:
                done.add(b)
                order.append(b)
                continue
            stack.append((b, True))
            for pin, bits in nl.cells[b][2].items():
                if pin != "Y" and bits and bits[0] in nl.cells and \
                        bits[0] not in nl.flops and bits[0] not in done and \
                        bits[0] not in resets and \
                        nl.cells[bits[0]][1] in _GATE_EVAL:
                    stack.append((bits[0], False))
    models = [model(q) for q in state]
    index = {q: j for j, q in enumerate(state)}

    def tern_next(rst_val):
        """Each state flop's next value, 0/1/X, with the state and the free
        inputs X and each reset bit at rst_val's value (X if absent)."""
        tv = {}

        def tval(b):
            if b == "0":
                return 0
            if b == "1":
                return 1
            if b in CONST or b in index:
                return 2
            if b in resets:
                return rst_val.get(b, 2)
            return tv.get(b, 2)
        for b in order:
            _, typ, conn = nl.cells[b]
            tv[b] = _ternary(lambda v, t=typ: _GATE_EVAL[t](v, 1),
                             {p: tval(bits[0]) for p, bits in conn.items()
                              if p != "Y" and bits})
        out = []
        for j, q in enumerate(state):
            _, _, conn, mdl = models[j]
            ins = {p: tval(conn[p][0]) for p in mdl[0]}
            ins["\0q"] = 2
            out.append(_ternary(
                lambda v, f=mdl[1]: f(v, v["\0q"], 1), ins))
        return out

    bus_idx = [index[b] for b in bus]
    sync = {}      # reset bit -> (active 0/1, next values under it)
    for r in rst_bits:
        hits = []
        for act_v in (0, 1):
            nxt = tern_next({r: act_v})
            if all(nxt[j] != 2 for j in bus_idx):
                hits.append((act_v, nxt))
        if len(hits) == 1:
            sync[r] = hits[0]
        else:
            free.append(("free", r))    # not a reset of this bus
    if len(free) > GRAY_MAX_FREE:
        return fail(f"{len(free)} free inputs, over {GRAY_MAX_FREE}")
    lanes = 1 << len(free)
    full = (1 << lanes) - 1
    free_val = {}
    for k, s in enumerate(free):
        free_val[s] = _lane_pattern(k, lanes)
    xbits = {s[1]: free_val[s] for s in free if s[0] == "x"}
    held = {r: (0 if a else full) for r, (a, _) in sync.items()}

    def value(b, vals, owner):
        if b == "0":
            return 0
        if b == "1":
            return full
        if b in ("x", "z"):
            return xbits.get(owner, 0)
        if b in held:
            return held[b]
        if b in index:
            return vals[b]
        if ("free", b) in free_val:
            return free_val[("free", b)]
        return vals[b]

    def bus_value(st):
        return "".join("1" if st >> j & 1 else "0" for j in reversed(bus_idx))

    def starts(fixed, what):
        """Every state with fixed's 0/1 flops set, its X (None) ones both
        ways."""
        loose = [j for j, v in enumerate(fixed) if v not in (0, 1)]
        if len(loose) > GRAY_MAX_UNINIT:
            raise ValueError(f"{len(loose)} flops {what} in its cone, "
                             f"over {GRAY_MAX_UNINIT}")
        base = sum(1 << j for j, v in enumerate(fixed) if v == 1)
        out = []
        for n in range(1 << len(loose)):
            st = base
            for t, j in enumerate(loose):
                if n >> t & 1:
                    st |= 1 << j
            out.append(st)
        return out

    async_init = [None if m[3][2] is None else int(m[3][2]) for m in models]
    start = []
    try:
        # a sync reset: the run starts where it leaves the cone; the async
        # reset state too when it covers the whole bus
        if not sync or all(async_init[j] is not None for j in bus_idx):
            start += starts(async_init, "with no async reset")
        for r, (_, nxt) in sync.items():
            start += starts([v if v != 2 else None for v in nxt],
                            f"that sync reset {nl.label(r)} leaves unknown")
    except ValueError as exc:
        return fail(str(exc))
    sync_rows = [{"net": resets[r], "active": str(a),
                  "to": "".join(str(nxt[j]) for j in reversed(bus_idx))}
                 for r, (a, nxt) in sorted(sync.items(),
                                           key=lambda kv: resets[kv[0]])]
    extra = {"sync_resets": sync_rows} if sync_rows else {}

    seen_st, queue = set(start), list(start)
    while queue:
        st = queue.pop()
        vals = {q: (full if st >> j & 1 else 0) for j, q in enumerate(state)}
        for b in order:
            _, typ, conn = nl.cells[b]
            vals[b] = _GATE_EVAL[typ](
                {p: value(bits[0], vals, b) for p, bits in conn.items()
                 if p != "Y" and bits}, full)
        nxt = []
        for j, q in enumerate(state):
            _, _, conn, mdl = models[j]
            pins = {p: value(conn[p][0], vals, q) for p in mdl[0]}
            nxt.append(mdl[1](pins, vals[q], full) & full)
        one = two = 0
        for j in bus_idx:
            d = vals[state[j]] ^ nxt[j]
            two |= one & d
            one |= d
        if two:
            lane = (two & -two).bit_length() - 1
            to = sum(1 << j for j in range(len(state)) if nxt[j] >> lane & 1)
            return fail("two or more bits change in one clock",
                        states=len(seen_st),
                        example=[bus_value(st), bus_value(to)], **extra)
        parts = [(full, 0)]
        for j in range(len(state)):
            m = nxt[j]
            split = []
            for lanes_p, s in parts:
                hi = lanes_p & m
                if hi:
                    split.append((hi, s | 1 << j))
                if lanes_p ^ hi:
                    split.append((lanes_p ^ hi, s))
            parts = split
        for _, s in parts:
            if s not in seen_st:
                seen_st.add(s)
                if len(seen_st) > GRAY_MAX_STATES:
                    return fail(f"over {GRAY_MAX_STATES} reachable states")
                queue.append(s)
    held_why = "".join(f", sync reset {r['net']} held inactive (its step "
                       f"to {r['to']} reported apart)" for r in sync_rows)
    return {"proven": True, "states": len(seen_st),
            "why": f"at most one bit changes per clock in all "
                   f"{len(seen_st)} reachable states ({len(state)} flops, "
                   f"{len(free)} free inputs{held_why})", **extra}

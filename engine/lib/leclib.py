"""leclib.py - the lec gate's normal form: a view of a design as pure logic.

Logical equivalence here means: both views have the same state elements (key
points), and every input of a key point and every output port is the same
function of the key points' outputs and the input ports. Each view is reduced
to that with no state left, so what the solver sees is combinational:

    Q of a flop or latch    -> an input port  <key>.Q
    its C/S/R/D (or E/D)    -> output ports   <key>.C ...
    every output port bit   -> an output port, behind two inverters

Why not hand eqy the flops. eqy's own strategies abstract the clock
(`formalff -clk2ff`), so run as it comes it passes a flop on the wrong clock
edge, a set flop where the RTL has a reset flop, and a flop with no reset at
all. The flops here are first legalised to one cell, `$_DFFSR_PPP_` (rising
edge, active-high set and clear, the same mapping for every view), so edge
and reset are plain logic on C, S and R and get proved like D. A negedge flop
and a posedge flop behind an inverter then compare equal, as they should.

Why the inverter pairs. eqy cuts its partitions at every signal whose name matches
in both views. An output that also feeds other logic would be a cut: the
views share logic differently, the two sides of a cone then read different
free inputs, and a correct design fails. A pair of inverters in front of each
output (eqy's passes collapse a plain buffer back into an alias) and internal
names hidden leave the input ports as the only cuts. Each output's whole cone
is then proved on its own.

Key points are matched two ways. Netlist against netlist, by instance name:
the tools keep those, while CTS renames the Q net of a flop that drives a
clock. RTL against netlist, by net: RTL has no flop instances, and the netlist
names a Q after any one of the aliases the RTL has for it (a parent's wire, an
output port), so the RTL's key point takes whichever alias the netlist used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

KIND_BY_PIN = {"C": "clock", "S": "set", "R": "reset", "D": "data",
               "E": "enable"}
SUFFIX = "_ff"
# A flop inside a cell model is named <instance>_ff.<model wire>_ff; the
# instance was already named after its Q net.
_MODEL_WIRE = re.compile(r"_ff\.[^.]*_ff$")
_INSTANCE_WIRE = re.compile(r"\.[^.]*_ff$")
_CELL_RE = re.compile(r"^\s*cell\s+(\S+)\s+(\S+)\s*$")
_CONNECT_RE = re.compile(r"^\s*connect\s+\\(\S+)\s+(.+?)\s*$")
_WIRE_RE = re.compile(r"^(\s*wire\s+)(.*)$")

LEC_CELLS = """\
(* blackbox *) module lec_ff(input C, input S, input R, input D, output Q);
endmodule
(* blackbox *) module lec_latch(input E, input D, output Q);
endmodule
"""
LEC_MAP = """\
module \\$_DFFSR_PPP_ (input C, input S, input R, input D, output Q);
  lec_ff _TECHMAP_REPLACE_ (.C(C), .S(S), .R(R), .D(D), .Q(Q));
endmodule
module \\$_DLATCH_P_ (input E, input D, output Q);
  lec_latch _TECHMAP_REPLACE_ (.E(E), .D(D), .Q(Q));
endmodule
"""


class LecError(Exception):
    """A view that can't be put in normal form: the proof can't start."""


# ---- liberty ---------------------------------------------------------------

def _blocks(text: str, keyword: str):
    """(name, body) of each `keyword (name) { ... }` at any depth."""
    for m in re.finditer(rf"\b{keyword}\s*\(\s*\"?([^\")\s]*)\"?\s*\)\s*\{{",
                         text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield m.group(1), text[m.end():i - 1]


def scan_liberty(text: str) -> tuple[set[str], dict[str, str]]:
    """(sequential cells, {cell: why it has no model}) from a liberty file.

    read_liberty models a pin from its `function` and nothing else, so a cell
    is unmodelled when an output has no function (clock gates, `hold`) or a
    three_state (the model would drop the enable, and a fault on it)."""
    sequential: set[str] = set()
    unmodelled: dict[str, str] = {}
    for cell, body in _blocks(text, "cell"):
        if re.search(r"\b(ff|latch|statetable)\s*\(", body):
            sequential.add(cell)
        for pin, pbody in _blocks(body, "pin"):
            direction = re.search(r"\bdirection\s*:\s*\"?(\w+)", pbody)
            if not direction or direction.group(1) == "input":
                continue
            if "three_state" in pbody:
                unmodelled[cell] = f"pin {pin} is three-state"
            elif not re.search(r"\bfunction\s*:", pbody):
                unmodelled[cell] = f"pin {pin} has no function"
    return sequential, unmodelled


INSTANCE_RE = re.compile(r"^\s*(?:\(\*.*?\*\)\s*)?(\w+__\w+)\s+\S+\s*\(", re.M)


def cells_used(verilog: str) -> set[str]:
    """Library cell types instantiated in a Verilog text, stubs aside."""
    return set(INSTANCE_RE.findall(verilog))


# ---- yosys -----------------------------------------------------------------

def view_script(reads: list[str], top: str, liberty: str, cells_v: str,
                map_v: str, out: str, aliases: str, key_by: str) -> str:
    """The yosys script that writes a view's normal form, up to the rewrite.

    Liberty is read after the sources with -overwrite so a port stub of a
    cell in RTL (the PLL has two) gets the real function, not a blackbox.
    key_by "net": flop instances take their Q net's name, as written, before
    anything merges aliases. key_by "instance": they keep their own. Either
    way every other instance is hidden, and the flop inside a model then takes
    its name from the instance. `aliases` is the JSON that says which names
    share a net."""
    if key_by == "net":
        name_flops = [
            "splitnets",
            f"rename -wire -suffix {SUFFIX} t:*__dff* t:*__sdff* t:*__lat*",
            f"rename -hide t:*__* c:*{SUFFIX} %d",
        ]
    else:
        name_flops = ["rename -hide t:*__* t:*__dff* %d t:*__sdff* %d "
                      "t:*__lat* %d"]
    return "\n".join([
        *reads,
        f"read_liberty -overwrite -ignore_miss_func {liberty}",
        f"hierarchy -check -top {top}",
        "proc",
        *name_flops,
        "flatten",
        "delete t:$scopeinfo",
        f"hierarchy -top {top} -purge_lib",
        # -nordff: a register folded into a read port would lose its name.
        "memory -nordff",
        "splitnets",
        "simplemap",
        "dfflegalize -cell $_DFFSR_PPP_ 01 -cell $_DLATCH_P_ 01",
        f"read_verilog {cells_v}",
        f"techmap -map {map_v} t:$_DFFSR_PPP_ t:$_DLATCH_P_",
        f"rename -wire -suffix {SUFFIX} t:lec_ff t:lec_latch",
        "splitnets -ports",
        f"write_json {aliases}",
        "opt_clean -purge",
        f"write_rtlil {out}",
        "",
    ])


def alias_table(text: str, top: str) -> dict[str, set[str]]:
    """{name: every name on that net}, bit by bit, from write_json (which has
    already resolved aliases: names on one net share a bit number)."""
    nets = json.loads(text)["modules"][top]["netnames"]
    by_bit: dict[int, set[str]] = {}
    for name, net in nets.items():
        if net.get("hide_name"):
            continue
        bits = net["bits"]
        for i, bit in enumerate(bits):
            if isinstance(bit, int):
                by_bit.setdefault(bit, set()).add(
                    name if len(bits) == 1 else f"{name}[{i}]")
    return {n: names for names in by_bit.values() for n in names}


def name_q_nets(view: "View", aliases: dict[str, set[str]]) -> None:
    """Keyed by instance, Q's own wire is inside the cell model (`_143_.IQ1`).
    Name the design's net instead, the one a person can look up."""
    for name, kp in view.key_points.items():
        own = kp.pins.get("Q", "").lstrip("\\").replace(" ", "")
        nets = sorted(n for n in aliases.get(own, set())
                      if not n.startswith(f"{name}."))
        if nets:
            kp.net = nets[0]


def rekey_by_alias(view: "View", aliases: dict[str, set[str]],
                   other_keys: set[str]) -> None:
    """Give each key point the name the other view uses for its net."""
    renamed: dict[str, KeyPoint] = {}
    for name, kp in view.key_points.items():
        hits = sorted((aliases.get(name, set()) | {name}) & other_keys)
        new = name if name in hits else (hits[0] if hits else name)
        if new in renamed:
            raise LecError(f"two key points on the net {new}")
        kp.name = new
        renamed[new] = kp
    view.key_points = renamed


# ---- RTLIL rewrite ---------------------------------------------------------

@dataclass
class KeyPoint:
    name: str                     # the Q net, as in the netlist
    kind: str                     # ff | latch
    pins: dict[str, str] = field(default_factory=dict)  # pin -> signal
    net: str = ""                 # a design net on Q, for a finding

    def q_net(self) -> str:
        """The net on Q: `\\u.q` or `\\u.q [3]` -> u.q[3]."""
        return self.net or self.pins.get("Q", "").lstrip("\\").replace(" ", "")


@dataclass
class View:
    top: str
    lines: list[str]              # the top module's body, cells removed
    key_points: dict[str, KeyPoint]
    inputs: list[str]             # (wire decl text)
    outputs: list[str]
    has_init: bool = False


def key_name(cell_name: str, key_by: str = "net") -> str:
    """Net: `\\u.q_ff.IQ1_ff` or `\\u.q_ff` -> `u.q`. Instance: `\\_143_.IQ1_ff`
    -> `_143_`. An auto name stays as it is: a flop with no public name can't
    be matched, and says so."""
    name = cell_name[1:] if cell_name.startswith("\\") else cell_name
    if key_by == "instance":
        return _INSTANCE_WIRE.sub("", name)
    name = _MODEL_WIRE.sub(SUFFIX, name)
    return name[:-len(SUFFIX)] if name.endswith(SUFFIX) else name


def _top_body(text: str, top: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == f"module \\{top}"), None)
    if start is None:
        raise LecError(f"no module {top} in the view")
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].rstrip() == "end" and not lines[i].startswith(" "))
    return lines[start + 1:end]


def parse_view(text: str, top: str, key_by: str = "net") -> View:
    """Split the top module into its key points and everything else."""
    body = _top_body(text, top)
    has_init = any(ln.strip().startswith("attribute \\init") for ln in body)
    keep: list[str] = []
    keys: dict[str, KeyPoint] = {}
    inputs: list[str] = []
    outputs: list[str] = []
    i = 0
    while i < len(body):
        line = body[i]
        stripped = line.strip()
        if stripped.startswith("attribute "):
            i += 1
            continue
        m = _CELL_RE.match(line)
        if m and m.group(1) in ("\\lec_ff", "\\lec_latch"):
            kp = KeyPoint(key_name(m.group(2), key_by),
                          "ff" if m.group(1) == "\\lec_ff" else "latch")
            i += 1
            while body[i].strip() != "end":
                c = _CONNECT_RE.match(body[i])
                if c:
                    kp.pins[c.group(1)] = c.group(2)
                i += 1
            if kp.name in keys:
                raise LecError(f"two key points named {kp.name}")
            keys[kp.name] = kp
            i += 1
            continue
        w = _WIRE_RE.match(line)
        if w:
            words = w.group(2).split()
            name = words[-1]
            if "input" in words:
                if "width" in words:
                    raise LecError(f"multi-bit port {name}")
                inputs.append(name)
                keep.append(f"  wire {name}")
                i += 1
                continue
            if "output" in words:
                if "width" in words:
                    raise LecError(f"multi-bit port {name}")
                outputs.append(name)
                keep.append(f"  wire {name}")
                i += 1
                continue
        keep.append(line)
        i += 1
    return View(top, keep, keys, inputs, outputs, has_init)


def _privatise(lines: list[str], public: dict[str, str]) -> list[str]:
    """Hide every wire but the inputs, and every cell name: eqy cuts at any
    name both views share, and only input ports may be cuts."""
    def sub(text: str) -> str:
        return re.sub(r"\\\S+", lambda m: public.get(m.group(0), m.group(0)),
                      text)

    out, in_cell, n = [], False, 0
    for ln in lines:
        words = ln.split()
        if not words:
            continue
        if words[0] == "wire":
            out.append(sub(ln))
        elif words[0] == "cell":
            n += 1
            out.append(f"  cell {words[1]} $lec_cell${n}")
            in_cell = True
        elif words[0] == "end":
            out.append(ln)
            in_cell = False
        elif words[0] == "connect" and in_cell:
            out.append(f"    connect {words[1]} {sub(' '.join(words[2:]))}")
        elif words[0] == "connect":
            out.append(f"  {sub(' '.join(words))}")
        else:
            out.append(ln)
    return out


def points(view: View, other_keys: set[str]) -> list[str]:
    """The names render() compares: key-point pins, then output ports."""
    out = [f"{name}.{pin}" for name, kp in sorted(view.key_points.items())
           if name in other_keys
           for pin in ("C", "S", "R", "D", "E") if pin in kp.pins]
    return out + [o[1:] for o in view.outputs]


def render(view: View, other_keys: set[str],
           compare: set[str] | None = None) -> str:
    """The view as a module of pure logic. A key point the other view lacks
    keeps its Q as an input, unbound there, and its own pins are not compared:
    if anything observable reads it, the cones that read it can't match, and
    that is the failure. A key point only the other view has appears as an
    unused input so both views have the same ports. `compare`, if given,
    keeps only those of points() as outputs (to name failures one by one)."""
    public: dict[str, str] = {}
    for ln in view.lines:
        w = _WIRE_RE.match(ln)
        if w:
            name = w.group(2).split()[-1]
            if name.startswith("\\") and name not in view.inputs:
                public[name] = f"$lec${name[1:]}"
    lines = _privatise(view.lines, public)

    def private(sig: str) -> str:
        return re.sub(r"\\\S+", lambda m: public.get(m.group(0), m.group(0)),
                      sig)

    ports = list(view.inputs)
    outs: list[str] = []
    cells: list[str] = []
    n = 0

    extra: list[str] = []

    def buf(a: str, y: str) -> None:
        nonlocal n
        n += 1
        cells.extend([f"  cell $_BUF_ $lec_buf${n}",
                      f"    connect \\A {a}", f"    connect \\Y {y}", "  end"])

    def drive(a: str, y: str) -> None:
        nonlocal n
        n += 1
        mid = f"$lec_inv${n}"
        extra.append(f"  wire {mid}")
        cells.extend([f"  cell $_NOT_ {mid}$a", f"    connect \\A {a}",
                      f"    connect \\Y {mid}", "  end",
                      f"  cell $_NOT_ {mid}$b", f"    connect \\A {mid}",
                      f"    connect \\Y {y}", "  end"])

    for name, kp in sorted(view.key_points.items()):
        q = f"\\{name}.Q"
        extra.append(f"  wire {q}")
        ports.append(q)
        if "Q" in kp.pins:
            buf(q, private(kp.pins["Q"]))
        if name not in other_keys:
            continue
        for pin in ("C", "S", "R", "D", "E"):
            if pin in kp.pins and (compare is None
                                   or f"{name}.{pin}" in compare):
                y = f"\\{name}.{pin}"
                extra.append(f"  wire {y}")
                outs.append(y)
                drive(private(kp.pins[pin]), y)
    for name in sorted(other_keys - set(view.key_points)):
        q = f"\\{name}.Q"
        extra.append(f"  wire {q}")
        ports.append(q)
    for o in view.outputs:
        if compare is not None and o[1:] not in compare:
            continue
        extra.append(f"  wire {o}")
        outs.append(o)
        drive(public[o], o)
    decl = {p: f"input {i + 1}" for i, p in enumerate(ports)}
    decl.update({p: f"output {len(ports) + i + 1}" for i, p in enumerate(outs)})

    def declare(ln: str) -> str:
        w = _WIRE_RE.match(ln)
        if w and w.group(2) in decl:
            return f"  wire {decl[w.group(2)]} {w.group(2)}"
        return ln

    # RTLIL wants a wire declared before anything uses it.
    body = [declare(ln) for ln in lines] + [declare(ln) for ln in extra] + cells
    wires = [ln for ln in body if _WIRE_RE.match(ln)]
    rest = [ln for ln in body if not _WIRE_RE.match(ln)]
    return "\n".join(["autoidx 1", f"module \\{view.top}", *wires, *rest,
                      "end", ""])


# ---- added logic -----------------------------------------------------------

ZERO, ONE = "1'0", "1'1"


class Logic:
    """Gate-level cells appended to a view, on wires of their own."""

    def __init__(self, view: View, tag: str):
        self.view, self.tag, self.n = view, tag, 0

    def _wire(self) -> str:
        self.n += 1
        w = f"$lec_{self.tag}${self.n}"
        self.view.lines.append(f"  wire {w}")
        return w

    def _cell(self, kind: str, y: str | None = None, **pins: str) -> str:
        y = y or self._wire()
        self.n += 1
        self.view.lines.append(f"  cell $_{kind}_ $lec_{self.tag}$c{self.n}")
        self.view.lines.extend(f"    connect \\{p} {s}" for p, s in pins.items())
        self.view.lines.extend([f"    connect \\Y {y}", "  end"])
        return y

    def drive(self, a: str, y: str) -> None:
        self._cell("BUF", y, A=a)

    def not_(self, a: str) -> str:
        if a in (ZERO, ONE):
            return ONE if a == ZERO else ZERO
        return self._cell("NOT", A=a)

    def and_(self, *xs: str) -> str:
        xs = tuple(x for x in xs if x != ONE)
        if ZERO in xs:
            return ZERO
        if not xs:
            return ONE
        y = xs[0]
        for x in xs[1:]:
            y = self._cell("AND", A=y, B=x)
        return y

    def or_(self, *xs: str) -> str:
        xs = tuple(x for x in xs if x != ZERO)
        if ONE in xs:
            return ONE
        if not xs:
            return ZERO
        y = xs[0]
        for x in xs[1:]:
            y = self._cell("OR", A=y, B=x)
        return y

    def eq(self, a: str, b: str) -> str:
        if b in (ZERO, ONE):
            return a if b == ONE else self.not_(a)
        return self._cell("XNOR", A=a, B=b)

    def input(self, name: str) -> str:
        self.view.lines.append(f"  wire {name}")
        self.view.inputs.append(name)
        return name

    def output(self, name: str, a: str) -> None:
        self.view.lines.append(f"  wire {name}")
        self.view.outputs.append(name)
        self.drive(a, name)


def fix_constant(view: View, name: str, value: int) -> None:
    """Drop a key point proved constant and tie its Q to the constant."""
    kp = view.key_points.pop(name)
    if "Q" in kp.pins:
        Logic(view, f"const{len(view.lines)}").drive(ONE if value else ZERO,
                                                     kp.pins["Q"])


def constants_script(il: str, top: str, guess: dict[str, int],
                     view: View, timeout_s: int) -> str:
    """One induction step for every candidate at once: with each candidate's
    Q at its guessed value, its D gives the same value back and the reset
    that would give the other value can't fire. Proved, the set holds those
    values from the first time it has them on."""
    sets, proves = [], []
    for name, c in sorted(guess.items()):
        v = ONE if c else ZERO
        sets.append(f"-set {_q(name, 'Q')} {v}")
        pins = view.key_points[name].pins
        proves.append(f"-prove {_q(name, 'D')} {v}")
        other = "R" if c else "S"
        if other in pins:
            proves.append(f"-prove {_q(name, other)} {ZERO}")
    shown = [p.split()[1] for p in proves]
    return "\n".join([
        f"read_rtlil {il}", f"hierarchy -top {top}",
        f"sat -enable_undef -set-def-inputs {' '.join(sets)} "
        f"{' '.join(proves)} -show {','.join(shown)} -timeout {timeout_s} "
        f"{top}", ""])


def _q(name: str, pin: str) -> str:
    return f"\\{name}.{pin}"


def constants_failed(log: str, guess: dict[str, int]) -> set[str] | None:
    """Candidates the failing step showed off their value (None: no model)."""
    if "no model found: SUCCESS" in log:
        return set()
    if "model found: FAIL" not in log:
        return None
    vals = shown_values(log)
    bad = set()
    for name, c in guess.items():
        if vals.get(f"{name}.D") != str(c):
            bad.add(name)
        if vals.get(f"{name}.{'R' if c else 'S'}", "0") != "0":
            bad.add(name)
    return bad


SHOWN_RE = re.compile(r"^\s+\\(\S+)\s+(\S+)\s+\S+\s+\S+\s*$", re.M)


def shown_values(log: str) -> dict[str, str]:
    """{signal: decimal} from the table a failing sat run prints."""
    head = log.rfind("Signal Name")
    return dict(SHOWN_RE.findall(log[head:])) if head >= 0 else {}


# ---- one miter per link ----------------------------------------------------

def miter_script(top: str, gold_il: str, gate_il: str, timeout_s: int) -> str:
    """Both views in one combinational miter, the way eqy's sat strategy
    reads a partition: an x in gold is a don't-care, one in gate is any
    value."""
    return "\n".join([
        f"read_rtlil {gold_il}", f"rename {top} gold",
        f"read_rtlil {gate_il}", f"rename {top} gate",
        "setundef -anyseq gate",
        "miter -equiv -flatten -make_outcmp -ignore_gold_x gold gate miter",
        "hierarchy -top miter", "opt_clean",
        f"sat -prove trigger 0 -enable_undef -set-def-inputs -show-inputs "
        f"-show-outputs -timeout {timeout_s} miter", ""])


def abc_script(top: str, gold_il: str, gate_il: str, aig: str) -> str:
    """The same miter as one 2-valued AIG for ABC's cec, which proves a large
    one in seconds where sat takes hours. Gold's x bits are tied to 0: that
    picks one of the circuits gold allows, so gate equal to it is a pass.
    Gate's x bits stay free inputs, as in sat. Pass only: a miter ABC can't
    prove goes to sat, which names what differs."""
    return "\n".join([
        f"read_rtlil {gold_il}", f"rename {top} gold",
        f"read_rtlil {gate_il}", f"rename {top} gate",
        "opt_expr -mux_undef gold", "setundef -zero gold",
        "setundef -anyseq gate",
        "miter -equiv -flatten gold gate miter",
        "hierarchy -top miter", "flatten", "opt_clean", "techmap",
        "opt -fast", "setundef -anyseq", "aigmap", "opt_clean",
        f"write_aiger -zinit {aig}", ""])


ABC_CEC = "read_aiger {aig}; print_stats; &get -n; &cec -m\n"


def abc_verdict(output: str) -> bool:
    """True only if ABC proved the single-output miter equivalent."""
    return re.search(r"^Networks are equivalent\.", output, re.M) is not None


def miter_verdict(log: str) -> tuple[str, list[str], dict]:
    """("equal"|"different"|"unknown", points that differ, {point:
    counterexample}). One model can't show every point that differs, only
    those it makes differ: the caller asks again without them."""
    if "no model found: SUCCESS" in log:
        return "equal", [], {}
    if "model found: FAIL" not in log:
        return "unknown", [], {}
    vals = shown_values(log)
    inputs = {k[3:]: v for k, v in vals.items() if k.startswith("in_")}
    differ = {k[4:]: {"inputs": inputs, "gold": vals.get(f"gold_{k[4:]}"),
                      "gate": vals.get(f"gate_{k[4:]}")}
              for k, v in sorted(vals.items())
              if k.startswith("cmp_") and v != "1"}
    return "different", sorted(differ), differ


# ---- recoded FSMs ----------------------------------------------------------

RECODE_RE = re.compile(
    r"^Recoding FSM `\$fsm\$\\?(\S+?)\$\d+' from module `\\?(\S+?)' using "
    r"`(\w+)' encoding:\n((?:  .*\n)*)", re.M)
CODE_RE = re.compile(r"^\s+([01]+) -> ([01-]+)\s*$", re.M)


@dataclass
class Fsm:
    register: str                 # the state register, as named in the view
    encoding: str
    old: list[str]                # each state's code in RTL, MSB first
    new: list[str]                # its code after recoding


def recoded_fsms(log: str) -> list[Fsm]:
    """The state maps fsm_recode wrote in a synthesis log, last one wins."""
    found: dict[str, Fsm] = {}
    for reg, _module, enc, body in RECODE_RE.findall(log):
        codes = CODE_RE.findall(body)
        if not codes:
            continue
        old, new = [c[0] for c in codes], [c[1] for c in codes]
        if old == new:
            continue
        found[reg] = Fsm(reg, enc, old, new)
    return list(found.values())


def _bit(reg: str, width: int, j: int) -> str:
    return f"{reg}[{j}]" if width > 1 else reg


def _find_bit(view: View, aliases: dict[str, set[str]], net: str) -> str | None:
    if net in view.key_points:
        return net
    hits = sorted(n for n in aliases.get(net, set()) if n in view.key_points)
    return hits[0] if hits else None


def apply_fsm(fsm: Fsm, gold: View, gate: View,
              aliases: dict[str, set[str]]) -> dict:
    """Prove a recoded FSM against its RTL through the map, not by name.

    Both views get the same free state index s. Gold's state flops take
    old[s] and gate's take new[s], so every cone that reads the state is
    compared state by state. Each gate state flop's pins are then compared
    with gold's next state put through the map: D_j = new[old^-1(gold D)]_j,
    its clock with gold's, and a reset to gold's reset value through the map.
    An output only gold drives, `<register>.recoded.ok`, is 1 on the gate
    side and says gold's D (and reset value) is a state in the map, and its
    state flops share one clock and one reset. So a wrong map fails, and a
    wrong transition fails on the gate flop whose D it changes."""
    m, k, n = len(fsm.old[0]), len(fsm.new[0]), len(fsm.old)
    record = {"register": fsm.register, "encoding": fsm.encoding,
              "states": n, "gold_bits": [], "gate_bits": []}
    gold_bits = [_find_bit(gold, aliases, _bit(fsm.register, m, j))
                 for j in range(m)]
    if None in gold_bits:
        record["applied"] = False
        record["why"] = "the RTL view has no flop on every state bit"
        return record
    named = [_bit(fsm.register, k, j) for j in range(k)]
    gate_bits = [b if b in gate.key_points else None for b in named]
    spare = sorted(set(gate.key_points) - set(gold.key_points)
                   - {b for b in gate_bits if b})
    missing = [j for j, b in enumerate(gate_bits) if b is None]
    if missing and len(missing) == len(spare) == 1:
        # The netlist named the flop after a net it drives (an output).
        gate_bits[missing[0]] = spare[0]
    elif missing:
        record["applied"] = False
        record["why"] = (f"the netlist's state flops {[named[j] for j in missing]} "
                         "can't be told apart from its other unmatched flops")
        return record
    if any(gold.key_points[b].kind != "ff" for b in gold_bits) or any(
            gate.key_points[b].kind != "ff" for b in gate_bits):
        record["applied"] = False
        record["why"] = "a state bit is a latch"
        return record
    record["gold_bits"], record["gate_bits"] = gold_bits, gate_bits
    width = max(1, (n - 1).bit_length())
    reg = fsm.register

    def bitval(code: str, j: int) -> str:
        return code[len(code) - 1 - j]

    for view, bits, codes, tag in ((gold, gold_bits, fsm.old, "fsmg"),
                                   (gate, gate_bits, fsm.new, "fsmh")):
        lg = Logic(view, f"{tag}{len(view.lines)}")
        s = [lg.input(f"\\{reg}.state_index[{i}]") for i in range(width)]
        sel = [lg.and_(*(s[i] if (x >> i) & 1 else lg.not_(s[i])
                         for i in range(width))) for x in range(n)]
        # An index past the last state means state 0, so every index is one.
        sel[0] = lg.not_(lg.or_(*sel[1:])) if n > 1 else ONE
        pins = [view.key_points.pop(b).pins for b in bits]
        for j, p in enumerate(pins):
            lg.drive(lg.or_(*(sel[x] for x in range(n)
                              if bitval(codes[x], j) == "1")), p["Q"])
        point = f"\\{reg}.recoded"
        if view is gate:
            for j, p in enumerate(pins):
                for pin in ("C", "S", "R", "D"):
                    lg.output(f"{point}[{j}].{pin}", p.get(pin, ZERO))
            lg.output(f"{point}.ok", ONE)
            continue

        def encode(vec: list[str]) -> tuple[list[str], str]:
            match = [lg.and_(*(lg.eq(vec[j], ONE if bitval(fsm.old[x], j) == "1"
                                     else ZERO) for j in range(m)))
                     for x in range(n)]
            return ([lg.or_(*(match[x] for x in range(n)
                              if bitval(fsm.new[x], j) == "1"))
                     for j in range(k)], lg.or_(*match))

        d, d_ok = encode([p["D"] for p in pins])
        sets = [p.get("S", ZERO) for p in pins]
        resets = [lg.or_(p.get("S", ZERO), p.get("R", ZERO)) for p in pins]
        fired = resets[0]
        v, v_ok = encode(sets)
        clock = pins[0]["C"]
        ok = [d_ok, lg.or_(lg.not_(fired), v_ok)]
        for p, r in zip(pins, resets):
            ok.append(lg.eq(p["C"], clock))
            ok.append(lg.eq(r, fired))
            ok.append(lg.not_(lg.and_(p.get("S", ZERO), p.get("R", ZERO))))
        for j in range(k):
            lg.output(f"{point}[{j}].C", clock)
            lg.output(f"{point}[{j}].S", lg.and_(fired, v[j]))
            lg.output(f"{point}[{j}].R", lg.and_(fired, lg.not_(v[j])))
            lg.output(f"{point}[{j}].D", d[j])
        lg.output(f"{point}.ok", lg.and_(*ok))
    record["applied"] = True
    return record


# ---- eqy -------------------------------------------------------------------

def eqy_config(top: str, gold_il: str, gate_il: str, timeout_s: int) -> str:
    """Depth 1: there is no state left in either view."""
    return (f"[gold]\nread_rtlil {gold_il}\nhierarchy -top {top}\n\n"
            f"[gate]\nread_rtlil {gate_il}\nhierarchy -top {top}\n\n"
            f"[strategy sat]\nuse sat\ndepth 1\ntimeout {timeout_s}\n")


def partition_name(point: str) -> str:
    """What eqy calls a port in its partition list: `a[3].D` -> `a.3.D`."""
    return re.sub(r"\[(\d+)\]", r".\1", point)


def describe_point(point: str) -> dict:
    """{flop, pin, kind} or {output} for a compared port name."""
    head, dot, pin = point.rpartition(".")
    if dot and pin in KIND_BY_PIN:
        return {"key_point": head, "pin": pin, "pin_kind": KIND_BY_PIN[pin]}
    return {"output": point}


COUNTEREXAMPLE_RE = re.compile(
    r"^\s*\d+\s+\\__p([io])_(\S+?)(__gold|__gate)?\s+(\S+)\s+\S+\s+\S+\s*$",
    re.M)


def counterexample(log: str) -> dict:
    """Inputs and the two sides' value from a failing sat run's log."""
    head = log.rfind("Signal Name")
    if head < 0:
        return {}
    out: dict = {"inputs": {}}
    for io, name, side, value in COUNTEREXAMPLE_RE.findall(log[head:]):
        if io == "i":
            out["inputs"][name] = value
        elif side:
            out[side[2:]] = value
    return out


# ---- kepler-formal ---------------------------------------------------------

def kepler_verdict(output: str, returncode: int) -> dict:
    """kepler-formal exits 0 either way and says which in its log, and refuses
    (exit 1) when two netlists' boundaries differ by name."""
    if "No difference was found" in output:
        return {"verdict": "equal"}
    if "Difference was found" in output:
        return {"verdict": "different"}
    m = re.search(r"\[error\]\s*(.*)", output)
    why = m.group(1) if m else output[-300:]
    return {"verdict": "inconclusive",
            "reason": f"exit {returncode}: {why.strip()}"}

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
        "memory",
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


def render(view: View, other_keys: set[str]) -> str:
    """The view as a module of pure logic. A key point the other view lacks
    keeps its Q as an input, unbound there, and its own pins are not compared:
    if anything observable reads it, the cones that read it can't match, and
    that is the failure. A key point only the other view has appears as an
    unused input so both views have the same ports."""
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
            if pin in kp.pins:
                y = f"\\{name}.{pin}"
                extra.append(f"  wire {y}")
                outs.append(y)
                drive(private(kp.pins[pin]), y)
    for name in sorted(other_keys - set(view.key_points)):
        q = f"\\{name}.Q"
        extra.append(f"  wire {q}")
        ports.append(q)
    for o in view.outputs:
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

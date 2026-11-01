"""macrolib.py - the timing abstract of an analog hard macro (issue #109).

An analog macro enters the digital top as GDS, LEF and a blackbox, so OpenSTA
saw its pins as ideal. This writes one liberty per STA corner for the crossing
pins, from plain ngspice on the sized (or extracted) .subckt, and checks the
periods of the clocks the macro sources against what the analog side recorded.

    macro_timing:                        # analog/spec/spec.yaml
      static: {pll_en: vdd, vctrl: 1.6}  # every other pin: vdd, 0, or volts
      pins:                              # limits are the spec's, never derived
        pfd_up: {max_transition_ns: 2.0}
        vco_div2: {max_transition_ns: 1.0, max_capacitance_pf: 0.1}
      arcs:
        - {from: a, to: y, sense: negative_unate}
        - {from: clk, to: q, timing_type: rising_edge, static: {d: vdd},
           edges: [{from: rise, to: rise}]}
        - {to: vco_div2, source: true, enable: pll_en, settle_ns: 200,
           window_ns: 40, static: {vctrl: 1.6}}
      clocks:
        vco_div2: {record: reports/recorded/pex_pvt.json,
                   measure: vco_f_vdd_mhz, divide: 2}
      extracted: log/pex_pvt/pll_analog.pex.ordered.spice    # optional
      slews_ns: [0.1, 0.5]                                   # optional
      loads_pf: [0.005, 0.02, 0.1]                           # optional

Why an arc list and not lctime: lctime recognises combinational cells and flops
from a `function`, and a PFD, charge pump or VCO is neither. Each arc here says
which edge causes which, so nothing has to be recognised.

A `source` arc is an output nothing in the digital side launches, a running
oscillator. It is started by an edge on `enable` to its static level, and its
transition read from the running waveform at each load. It hangs on `enable` only because liberty needs a related pin to carry a
transition table: it has no delay tables, since no delay was measured, and
OpenSTA warns "missing cell_rise" for it. A create_clock on the pin starts the
clock there, and with a propagated clock OpenSTA takes the pin's slew from this
table. An ideal clock's flops see zero slew, though max_transition on the pin
still reads the table.

Per corner through the macro's `lib` view, not EXTRA_LIBS: LibreLane documents
EXTRA_LIBS as "loaded indiscriminately for all timing corners" (its io.tcl reads
each into every corner), so three corners of one cell would collide.

Input capacitance is the charge into the pin over a full 0-VDD ramp divided by
VDD, with the macro's outputs moving, so Miller charge is in it. A pin that draws
DC reads high. Only what was measured reaches the output: no power, no setup or
hold. A flop inside a macro driven by a digital clock gets its clock-to-output
arc from `edges`, and its setup and hold are not characterised.

Ceilings: a process's ceiling is the highest recorded frequency over every
recorded corner of that process, resistor skews included, because an ss STA
corner can meet an ss_pff VCO. A period longer than the ceiling is optimistic and
refused. One shorter by more than `period_margin` (PERIOD_MARGIN by default)
times something the macro cannot do and no longer follows the record; refused too.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import charlib  # noqa: E402
import checklib  # noqa: E402
import simlib  # noqa: E402

Refused = charlib.Refused

OUT_DIR = "macro_lib"
STA_LIBRARY = "gf180mcu_fd_sc_mcu7t5v0"
# LibreLane's three PVTs (ttlib's vendored tech.py STA_CORNERS)
STA_CORNERS = ("tt_025C_3v30", "ss_125C_3v00", "ff_n40C_3v60")
_PROCESS = {"tt": "typical", "ss": "ss", "ff": "ff"}
_CORNER_RE = re.compile(r"^(tt|ss|ff)_(n?)(\d+)C_(\d)v(\d\d)$")
DEFAULT_LOADS = 3
LOAD_FLOOR_PF = 0.005
DEFAULT_SLEWS_NS = (0.1, 0.5)
SETTLE_NS = 5.0
# Short by more than this and a period no longer follows the record (see the
# module docstring). A spec may name its own with its reason in a comment.
PERIOD_MARGIN = 0.05
# Longer by this much is rounding in how a period was typed, not optimism.
PERIOD_ROUNDING = 0.002
_MAX_RETRIES = 3
_ENABLE_AT_NS = 1.0
_INVERSE = {"rise": "fall", "fall": "rise"}


@dataclass
class Row:
    from_edge: str
    to_edge: str


@dataclass
class Arc:
    to: str
    source: bool = False
    frm: str | None = None
    timing_type: str = "combinational"
    sense: str | None = None
    rows: list = field(default_factory=list)
    static: dict = field(default_factory=dict)
    settle_ns: float = SETTLE_NS
    window_ns: float = 0.0
    step_ns: float | None = None


@dataclass
class MacroTiming:
    cell: str
    inputs: list
    outputs: list
    limits: dict
    static: dict
    arcs: list
    clocks: dict
    slews: list
    loads: list
    extracted: str | None = None


def corner_of(name: str) -> dict:
    """A LibreLane PVT name as a charlib corner: its process, temperature and
    supply are all in the name."""
    m = _CORNER_RE.match(name)
    if not m:
        raise Refused("bad_corner", f"cannot read an STA corner from {name!r}")
    p, neg, temp, v, frac = m.groups()
    return {"name": name, "process": _PROCESS[p], "short": p,
            "temp_c": -int(temp) if neg else int(temp),
            "vdd": float(f"{v}.{frac}")}


def _need(cond, msg):
    if not cond:
        raise Refused("macro_timing_incomplete", msg)


def _parse_arc(raw: dict, inputs: list, outputs: list) -> Arc:
    to = str(raw.get("to", "")).lower()
    _need(to in outputs, f"arc {raw}: `to` must be an output crossing pin "
          f"({outputs})")
    static = {str(k).lower(): v for k, v in (raw.get("static") or {}).items()}
    if raw.get("source"):
        enable = str(raw.get("enable", "")).lower()
        _need(enable in inputs,
              f"source arc to {to}: `enable` must name an input crossing pin "
              f"({inputs}), to carry its transition table in liberty")
        _need(float(raw.get("window_ns", 0)) > 0 and "settle_ns" in raw,
              f"source arc to {to}: give settle_ns (until it runs) and "
              "window_ns (at least two periods at its slowest)")
        return Arc(to=to, source=True, frm=enable, static=static,
                   settle_ns=float(raw["settle_ns"]),
                   window_ns=float(raw["window_ns"]),
                   step_ns=raw.get("step_ns"))
    frm = str(raw.get("from", "")).lower()
    _need(frm in inputs, f"arc {raw}: `from` must be an input crossing pin "
          f"({inputs})")
    sense = raw.get("sense")
    if raw.get("edges"):
        rows = [Row(str(e["from"]), str(e["to"])) for e in raw["edges"]]
    elif sense in ("positive_unate", "negative_unate"):
        flip = sense == "negative_unate"
        rows = [Row(e, _INVERSE[e] if flip else e) for e in ("rise", "fall")]
    else:
        raise Refused("macro_timing_incomplete",
                      f"arc {frm} -> {to}: give sense (positive_unate, "
                      "negative_unate) or edges")
    for r in rows:
        _need({r.from_edge, r.to_edge} <= {"rise", "fall"},
              f"arc {frm} -> {to}: an edge is rise or fall, not {r}")
    kind = str(raw.get("timing_type", "combinational"))
    _need(kind in ("combinational", "rising_edge", "falling_edge"),
          f"arc {frm} -> {to}: timing_type {kind!r} is not one of "
          "combinational, rising_edge, falling_edge")
    return Arc(to=to, frm=frm, timing_type=kind, sense=sense, rows=rows,
               static=static, settle_ns=float(raw.get("settle_ns", SETTLE_NS)),
               step_ns=raw.get("step_ns"))


def _default_loads(max_cap: float) -> list[float]:
    """Log-spaced up to the largest max_capacitance, so a table never has to
    extrapolate inside the spec's own limit."""
    low = min(LOAD_FLOOR_PF, max_cap / 4.0)
    ratio = (max_cap / low) ** (1.0 / (DEFAULT_LOADS - 1))
    return [float(f"{low * ratio ** i:.4g}") for i in range(DEFAULT_LOADS)]


def parse_spec(section: dict, cell: str, signal_pins: dict) -> MacroTiming:
    """`signal_pins` maps each crossing pin to its direction, d2a or a2d. Every
    crossing pin must have its limits, and every output an arc, or the abstract
    would leave a pin unconstrained while looking complete."""
    inputs = sorted(p.lower() for p, d in signal_pins.items() if d == "d2a")
    outputs = sorted(p.lower() for p, d in signal_pins.items() if d == "a2d")
    pins = {str(k).lower(): v or {} for k, v in (section.get("pins") or {}).items()}
    limits = {}
    for pin in inputs + outputs:
        # a bus takes its limits from the signal's name
        spec = pins.get(pin) or pins.get(pin.split("[")[0])
        _need(spec, f"macro_timing.pins has no limits for crossing pin {pin}")
        _need("max_transition_ns" in spec,
              f"macro_timing.pins.{pin}: max_transition_ns is the analog "
              "spec's to state")
        if pin in outputs:
            _need("max_capacitance_pf" in spec,
                  f"macro_timing.pins.{pin}: max_capacitance_pf is the analog "
                  "spec's to state")
        limits[pin] = spec
    unknown = sorted(set(pins) - {p.split("[")[0] for p in inputs + outputs}
                     - set(inputs + outputs))
    _need(not unknown, f"macro_timing.pins names {unknown}, not crossing pins")
    arcs = [_parse_arc(a, inputs, outputs) for a in section.get("arcs") or []]
    missing = sorted(set(outputs) - {a.to for a in arcs})
    _need(not missing, f"macro_timing.arcs has no arc to output {missing}")
    max_cap = max((float(limits[p]["max_capacitance_pf"]) for p in outputs),
                  default=0.1)
    loads = sorted(float(x) for x in section.get("loads_pf") or
                   _default_loads(max_cap))
    slews = sorted(float(x) for x in section.get("slews_ns") or DEFAULT_SLEWS_NS)
    clocks = {str(k).lower(): v for k, v in (section.get("clocks") or {}).items()}
    for pin, c in clocks.items():
        _need(pin in outputs, f"macro_timing.clocks names {pin}, not an output")
        _need(c.get("record") and c.get("measure"),
              f"macro_timing.clocks.{pin}: give the record and the measure "
              "the ceiling comes from")
        src = [a for a in arcs if a.to == pin]
        _need(src, f"macro_timing.clocks.{pin} has no arc")
    return MacroTiming(
        cell=cell, inputs=inputs, outputs=outputs, limits=limits,
        static={str(k).lower(): v for k, v in (section.get("static") or {}).items()},
        arcs=arcs, clocks=clocks, slews=slews, loads=loads,
        extracted=section.get("extracted"))


def level(value, vdd: float) -> float:
    if isinstance(value, str) and value.strip().lower() == "vdd":
        return vdd
    return float(value)


class Bench:
    """One deck per simulation. Ports are renamed by position, so a bus bit
    `code[3]` never reaches a source name or a .meas."""

    def __init__(self, cell, netlist: Path, ports, power, ground, pdk, corner,
                 vdd, work: Path, terms):
        self.cell, self.netlist, self.ports = cell, netlist, list(ports)
        self.power, self.ground = power.lower(), ground.lower()
        self.pdk, self.corner, self.vdd = pdk, corner, vdd
        self.work, self.terms = work, terms
        self.runs = 0
        self.node = {p.lower(): f"n{i}" for i, p in enumerate(self.ports)}

    def simulate(self, stim: dict, loads: dict, t_stop, step, measures,
                 timeout: float = 900.0) -> dict:
        models, design = charlib.model_files(self.pdk)
        out = [f"* macrolib {self.cell} {self.corner['name']}"]
        out += [f".lib {models} {s}" for s in charlib.model_sections(self.corner)]
        out += [f".include {design}", f".include {self.netlist}",
                f".temp {self.corner['temp_c']}"]
        for p in self.ports:
            low = p.lower()
            node = self.node[low]
            if low == self.power:
                out.append(f"V{node} {node} 0 {self.vdd:g}")
            elif low == self.ground:
                out.append(f"V{node} {node} 0 0")
            elif low in stim:
                out.append(f"V{node} {node} 0 {stim[low]}")
        conns = " ".join(self.node[p.lower()] for p in self.ports)
        out.append(f"Xdut {conns} {self.cell}")
        for pin, pf in loads.items():
            out.append(f"Cload_{self.node[pin]} {self.node[pin]} 0 {pf:g}p")
        out.append(f".tran {step:g}n {t_stop:g}n 0 {step:g}n")
        out += measures
        out.append(".end")
        self.runs += 1
        self.work.mkdir(parents=True, exist_ok=True)
        deck = self.work / f"bench_{self.runs}.cir"
        deck.write_text("\n".join(out) + "\n", encoding="utf-8")
        stdout, stderr, rc = simlib.run_ngspice(charlib.EDA_BIN, deck, self.work,
                                                timeout)
        text = "\n".join(line for line in (stdout + stderr).splitlines()
                         if not charlib._MEASURE_FAILED.search(line))
        errors = simlib.detect_engine_errors(text)
        if rc or errors:
            raise Refused("bench_failed",
                          f"{deck}: exit {rc}, {errors}: {stderr[-500:]}")
        return simlib.parse_measures(stdout)

    def statics(self, mt: MacroTiming, arc_static: dict, driven: set) -> dict:
        """Every undriven port's DC level; a pin nobody named is a refusal, not
        a floating node ngspice would quietly solve."""
        stim = {}
        for p in self.ports:
            low = p.lower()
            if low in (self.power, self.ground) or low in driven:
                continue
            if low in arc_static:
                stim[low] = level(arc_static[low], self.vdd)
            elif low in mt.static:
                stim[low] = level(mt.static[low], self.vdd)
            elif low not in mt.outputs:
                raise Refused("macro_timing_incomplete",
                              f"macro_timing.static has no level for pin {p}")
        return stim


def _edge_points(t, to_edge):
    return ((t.slew_lower, t.slew_upper) if to_edge == "rise"
            else (t.slew_upper, t.slew_lower))


def measure_input_cap(bench: Bench, mt: MacroTiming, pin: str) -> dict:
    """Charge over a 0-VDD ramp and back, each over VDD, in pF."""
    t0, ramp, tail = SETTLE_NS, 0.2, 3.0
    t1 = t0 + ramp + tail
    t_end = t1 + ramp + tail
    pwl = charlib._ramp_edges(bench.vdd, [(t0, ramp, True), (t1, ramp, False)],
                              t_end)
    stim = bench.statics(mt, {}, {pin})
    stim[pin] = pwl
    src = f"V{bench.node[pin]}"
    m = bench.simulate(
        stim, {o: mt.loads[0] for o in mt.outputs}, t_end, 0.002,
        [f".meas tran q_r INTEG i({src}) FROM={t0:g}n TO={t1:g}n",
         f".meas tran q_f INTEG i({src}) FROM={t1:g}n TO={t_end:g}n"])
    if not {"q_r", "q_f"} <= m.keys():
        raise Refused("bench_failed", f"no charge measured into {pin}")
    rise = abs(m["q_r"]) / bench.vdd * 1e12
    fall = abs(m["q_f"]) / bench.vdd * 1e12
    return {"capacitance": max(rise, fall), "rise_capacitance": rise,
            "fall_capacitance": fall}


def measure_row(bench: Bench, mt: MacroTiming, arc: Arc, row: Row, slew: float,
                load: float) -> tuple[float, float]:
    """(delay, library transition) in ns for one point of one edge. A window
    that closes before the output finishes is doubled, never read."""
    t = bench.terms
    vdd = bench.vdd
    ramp = charlib.ramp_of(slew, t)
    t0 = arc.settle_ns
    a, b = _edge_points(t, row.to_edge)
    edge = row.to_edge.upper()
    span = max(ramp, 0.5) * 12 + 10 * load / 0.01
    for _ in range(_MAX_RETRIES):
        t_stop = t0 + ramp + span
        step = arc.step_ns or max(min(ramp, span) / 100.0, 0.001)
        stim = bench.statics(mt, arc.static, {arc.frm})
        stim[arc.frm] = charlib._ramp_edges(
            vdd, [(t0, ramp, row.from_edge == "rise")], t_stop)
        m = bench.simulate(
            stim, {arc.to: load}, t_stop, step,
            [f".meas tran t_o WHEN v({bench.node[arc.to]})="
             f"{t.output_threshold * vdd:.6g} {edge}=1 TD={t0:g}n",
             f".meas tran t_a WHEN v({bench.node[arc.to]})={a * vdd:.6g} "
             f"{edge}=1 TD={t0:g}n",
             f".meas tran t_b WHEN v({bench.node[arc.to]})={b * vdd:.6g} "
             f"{edge}=1 TD={t0:g}n"])
        if {"t_o", "t_a", "t_b"} <= m.keys():
            delay = m["t_o"] * 1e9 - (t0 + t.input_threshold * ramp)
            return delay, charlib.to_table_transition(
                (m["t_b"] - m["t_a"]) * 1e9, t)
        span *= 3
    raise Refused(
        "arc_no_output",
        f"{arc.frm} {row.from_edge} -> {arc.to} {row.to_edge} at "
        f"{bench.corner['name']}, slew {slew} ns, load {load} pF: the output "
        f"never made that edge within {t0 + ramp + span / 3:g} ns; fix the arc "
        "or its static levels")


def measure_source(bench: Bench, mt: MacroTiming, arc: Arc, load: float) -> dict:
    """A running output's transitions at one load, from its own waveform."""
    t = bench.terms
    vdd = bench.vdd
    node = bench.node[arc.to]
    td = arc.settle_ns
    stim = bench.statics(mt, arc.static, set())
    # enabled by an edge, as in the chip: at DC a ring has a solution at mid-rail,
    # and with its output loaded only numerical noise would ever start it
    enabled = stim[arc.frm]
    if not 0 < td - _ENABLE_AT_NS:
        raise Refused("macro_timing_incomplete",
                      f"source arc to {arc.to}: settle_ns must be past the "
                      f"enable edge at {_ENABLE_AT_NS} ns")
    t_stop = td + arc.window_ns
    stim[arc.frm] = charlib._ramp_edges(
        vdd, [(_ENABLE_AT_NS, 0.1, enabled > vdd / 2)], t_stop)
    step = arc.step_ns or 0.002
    meas = [f".meas tran t_p1 WHEN v({node})={t.output_threshold * vdd:.6g} "
            f"RISE=1 TD={td:g}n",
            f".meas tran t_p2 WHEN v({node})={t.output_threshold * vdd:.6g} "
            f"RISE=2 TD={td:g}n"]
    for e, tag in (("RISE", "r"), ("FALL", "f")):
        a, b = _edge_points(t, e.lower())
        meas += [f".meas tran t_{tag}a WHEN v({node})={a * vdd:.6g} {e}=1 TD={td:g}n"]
        # settle_ns can land mid-edge, so the second threshold's first crossing
        # may belong to the edge before: two to choose from
        meas += [f".meas tran t_{tag}b{n} WHEN v({node})={b * vdd:.6g} {e}={n} "
                 f"TD={td:g}n" for n in (1, 2)]
    m = bench.simulate(stim, {arc.to: load}, t_stop, step, meas)
    def edge_ns(tag):
        start = m.get(f"t_{tag}a")
        ends = [m[k] for k in (f"t_{tag}b1", f"t_{tag}b2") if k in m]
        after = [t_end for t_end in ends if start is not None and t_end > start]
        return (after[0] - start) * 1e9 if after else None

    rise, fall = edge_ns("r"), edge_ns("f")
    if rise is None or fall is None or not {"t_p1", "t_p2"} <= m.keys():
        raise Refused(
            "source_not_running",
            f"{arc.to} at {bench.corner['name']}, load {load} pF: no two full "
            f"periods after {td:g} ns in a {arc.window_ns:g} ns window; lengthen "
            "settle_ns or window_ns, fix its static levels, or lower "
            "max_capacitance_pf if this load stops it")
    return {"rise_transition": charlib.to_table_transition(rise, t),
            "fall_transition": charlib.to_table_transition(fall, t),
            "f_mhz": 1e-6 / (m["t_p2"] - m["t_p1"])}


def _fmt(values) -> str:
    return ", ".join(f"{v:.5g}" for v in values)


def _rows(table) -> str:
    return ", \\\n            ".join(f'"{_fmt(r)}"' for r in table)


def _table(kind: str, template: str, slews, loads, table) -> str:
    return (f'        {kind} ({template}) {{\n'
            f'          index_1 ("{_fmt(slews)}");\n'
            f'          index_2 ("{_fmt(loads)}");\n'
            f'          values ({_rows(table)});\n        }}\n')


def _table_1d(kind: str, loads, values) -> str:
    return (f'        {kind} (macro_load) {{\n'
            f'          index_1 ("{_fmt(loads)}");\n'
            f'          values ("{_fmt(values)}");\n        }}\n')


_BIT = re.compile(r"^(.+)\[(\d+)\]$")


def _buses(mt: MacroTiming) -> dict:
    """{signal: [bit indices]} for the bussed crossing pins. A pin merely named
    code[0] is not a bus to OpenSTA, and the macro's Verilog stub has `code` as
    one port, so it would not link."""
    out = {}
    for pin in mt.inputs + mt.outputs:
        m = _BIT.match(pin)
        if m:
            out.setdefault(m.group(1), []).append(int(m.group(2)))
    return out


def _bus_types(mt: MacroTiming) -> str:
    text = []
    for name, bits in sorted(_buses(mt).items()):
        if sorted(bits) != list(range(len(bits))):
            raise Refused("macro_timing_incomplete",
                          f"bus {name} has bits {sorted(bits)}, not 0 to N-1")
        text.append(
            f"  type (macro_bus_{name}) {{\n    base_type : array;\n"
            f"    data_type : bit;\n    bit_width : {len(bits)};\n"
            f"    bit_from : {len(bits) - 1};\n    bit_to : 0;\n"
            f"    downto : true;\n  }}\n")
    return "".join(text)


def _pins_and_buses(mt: MacroTiming, pin_text: dict) -> str:
    text = [pin_text[p] for p in mt.inputs + mt.outputs if not _BIT.match(p)]
    for name, bits in sorted(_buses(mt).items()):
        first = f"{name}[0]"
        direction = "input" if first in mt.inputs else "output"
        text.append(f"    bus ({name}) {{\n      bus_type : macro_bus_{name};\n"
                    f"      direction : {direction};\n")
        text += [pin_text[f"{name}[{b}]"] for b in sorted(bits, reverse=True)]
        text.append("    }\n")
    return "".join(text)


def liberty_text(mt: MacroTiming, head: str, library_note: str, caps: dict,
                 arcs_out: dict, terms) -> str:
    n_s, n_l = len(mt.slews), len(mt.loads)
    out = [f"/* {library_note} */\n", head.rstrip().rstrip("}").rstrip(), "\n"]
    out.append(
        f'  lu_table_template (macro_slew_load) {{\n'
        f'    variable_1 : input_net_transition;\n'
        f'    variable_2 : total_output_net_capacitance;\n'
        f'    index_1 ("{_fmt(range(1, n_s + 1))}");\n'
        f'    index_2 ("{_fmt(range(1, n_l + 1))}");\n  }}\n'
        f'  lu_table_template (macro_load) {{\n'
        f'    variable_1 : total_output_net_capacitance;\n'
        f'    index_1 ("{_fmt(range(1, n_l + 1))}");\n  }}\n')
    out.append(_bus_types(mt))
    out.append(f"  cell ({mt.cell}) {{\n    is_macro_cell : true;\n")
    pin_text = {}
    for pin in mt.inputs:
        c = caps[pin]
        pin_text[pin] = (
            f'    pin ("{pin}") {{\n      direction : input;\n'
            f'      capacitance : {c["capacitance"]:.5g};\n'
            f'      rise_capacitance : {c["rise_capacitance"]:.5g};\n'
            f'      fall_capacitance : {c["fall_capacitance"]:.5g};\n'
            f'      max_transition : '
            f'{float(mt.limits[pin]["max_transition_ns"]):.5g};\n    }}\n')
    for pin in mt.outputs:
        lim = mt.limits[pin]
        text = [
            f'    pin ("{pin}") {{\n      direction : output;\n'
            f'      max_capacitance : {float(lim["max_capacitance_pf"]):.5g};\n'
            f'      max_transition : {float(lim["max_transition_ns"]):.5g};\n']
        for arc, tabs in arcs_out[pin]:
            text.append(f'      timing () {{\n        related_pin : "{arc.frm}";\n'
                        f'        timing_type : {arc.timing_type};\n')
            if arc.source:
                for k in ("rise_transition", "fall_transition"):
                    text.append(_table_1d(k, mt.loads, tabs[k]))
            else:
                if arc.sense:
                    text.append(f"        timing_sense : {arc.sense};\n")
                for k, v in sorted(tabs.items()):
                    text.append(_table(k, "macro_slew_load", mt.slews, mt.loads, v))
            text.append("      }\n")
        text.append("    }\n")
        pin_text[pin] = "".join(text)
    out.append(_pins_and_buses(mt, pin_text))
    out.append("  }\n}\n")
    return "".join(out)


def _interp(xs, ys, x) -> float:
    if x <= xs[0]:
        i = 0
    elif x >= xs[-1]:
        i = len(xs) - 2
    else:
        i = next(k for k in range(len(xs) - 1) if xs[k] <= x <= xs[k + 1])
    return ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / (xs[i + 1] - xs[i])


def limit_warnings(mt: MacroTiming, corner: str, arcs_out: dict) -> list[dict]:
    """An output that misses its own max_transition at its own max_capacitance:
    the two limits disagree, and STA only sees it once a net gets that heavy."""
    found = []
    for pin in mt.outputs:
        limit = float(mt.limits[pin]["max_transition_ns"])
        cap = float(mt.limits[pin]["max_capacitance_pf"])
        for arc, tabs in arcs_out[pin]:
            for kind in ("rise_transition", "fall_transition"):
                if kind not in tabs:
                    continue
                rows = tabs[kind] if not arc.source else [tabs[kind]]
                # the fastest input slew: the driver's own best
                at = _interp(mt.loads, rows[0], cap)
                if at > limit:
                    found.append({"pin": pin, "corner": corner, "edge": kind,
                                  "transition_ns": round(at, 4),
                                  "max_transition_ns": limit,
                                  "max_capacitance_pf": cap})
    return found


def characterize(mt: MacroTiming, *, netlist: Path, netlist_kind: str, ports,
                 power: str, ground: str, pdk: Path, out_dir: Path,
                 corners=STA_CORNERS, library: str = STA_LIBRARY,
                 work_dir: Path | None = None,
                 extra_report: dict | None = None) -> dict:
    """Writes <cell>__<corner>.lib for each corner and a report beside them. A
    rerun that fails leaves no earlier .lib, so nothing globs a stale table."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{mt.cell}__*.lib"):
        old.unlink()
    (out_dir / f"{mt.cell}.report.json").unlink(missing_ok=True)
    work_dir = Path(work_dir) if work_dir else out_dir
    # a name, not a path: out_dir is committed with the block and hashed
    report = {"cell": mt.cell, "netlist": Path(netlist).name, **(extra_report or {}),
              "netlist_kind": netlist_kind, "slews_ns": mt.slews,
              "loads_pf": mt.loads, "corners": {}, "warnings": []}
    for name in corners:
        corner = corner_of(name)
        vdd = corner["vdd"]
        template = (Path(pdk) / "libs.ref" / library / "lib"
                    / f"{library}__{name}.lib")
        if not template.is_file():
            raise Refused("bad_template", f"no template lib at {template}")
        text = template.read_text(encoding="utf-8", errors="replace")
        i = text.find("cell (")
        i2 = text.find("cell(")
        cut = min(x for x in (i, i2) if x >= 0)
        header_text = text[:cut]
        lib_name = f"{mt.cell}__{name}"
        parsed = charlib.parse_liberty(header_text.rstrip() + "\n}\n")
        terms = charlib.terms_of(parsed)
        head = charlib.corner_header(header_text, lib_name, vdd,
                                     corner["temp_c"], False)
        bench = Bench(mt.cell, netlist, ports, power, ground, pdk, corner, vdd,
                      work_dir / f"{mt.cell}__{name}.work", terms)
        caps = {pin: measure_input_cap(bench, mt, pin) for pin in mt.inputs}
        arcs_out = {pin: [] for pin in mt.outputs}
        running = {}
        for arc in mt.arcs:
            if arc.source:
                points = [measure_source(bench, mt, arc, c) for c in mt.loads]
                tabs = {k: [p[k] for p in points]
                        for k in ("rise_transition", "fall_transition")}
                running[arc.to] = [round(p["f_mhz"], 2) for p in points]
            else:
                tabs = {}
                for row in arc.rows:
                    delay, trans = zip(*[
                        zip(*[measure_row(bench, mt, arc, row, s, c)
                              for c in mt.loads]) for s in mt.slews])
                    tabs[f"cell_{row.to_edge}"] = [list(r) for r in delay]
                    tabs[f"{row.to_edge}_transition"] = [list(r) for r in trans]
            arcs_out[arc.to].append((arc, tabs))
        note = (f"macrolib (ngspice): {mt.cell} at {name}, {vdd:g} V, "
                f"{corner['temp_c']} C, process {corner['process']}, "
                f"{netlist_kind} netlist. Timing and input capacitance only.")
        path = out_dir / f"{lib_name}.lib"
        path.write_text(liberty_text(mt, charlib.write_liberty(head), note, caps,
                                     arcs_out, terms), encoding="utf-8")
        report["corners"][name] = {
            "lib": path.name, "vdd": vdd, "input_cap_pf": caps,
            **({"running_mhz_by_load": running} if running else {})}
        report["warnings"] += limit_warnings(mt, name, arcs_out)
    (out_dir / f"{mt.cell}.report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    return report


def _recorded(record: dict, measure: str) -> dict[str, float]:
    """{corner: value} from a pex_pvt record (facts.measured.<corner>.<m>) or a
    sim_pvt/pex_sim gate record (facts.results.<corner>.<bench>.<m>)."""
    facts = record.get("facts") or {}
    out = {}
    for corner, values in (facts.get("measured") or {}).items():
        if isinstance(values, dict) and isinstance(values.get(measure), (int, float)):
            out[corner] = float(values[measure])

    def walk(node, corner):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k == measure and isinstance(v, (int, float)):
                out[corner] = max(out.get(corner, -math.inf), float(v))
            elif isinstance(v, dict):
                walk(v, corner)
    results = facts.get("results")
    if isinstance(results, dict):
        for corner, values in results.items():
            walk(values, corner)
    return out


def _process_of(corner: str) -> str | None:
    head = corner.split("_", 1)[0]
    return head if head in _PROCESS else None


def clock_ceilings(mt: MacroTiming, analog: Path) -> dict:
    """{pin: {process: {fmax_mhz, corner, period_ns}}}: the fastest the side's
    own record ran at, per process."""
    out = {}
    for pin, c in mt.clocks.items():
        path = Path(analog) / c["record"]
        if not path.is_file():
            raise Refused("macro_clock_no_record",
                          f"macro_timing.clocks.{pin}: no record at {path}; run "
                          "the analog gate that writes it")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") != "pass":
            raise Refused("macro_clock_no_record",
                          f"{path} has status {record.get('status')!r}, not pass")
        values = _recorded(record, c["measure"])
        per = {}
        divide = float(c.get("divide", 1))
        for corner, mhz in values.items():
            proc = _process_of(corner)
            if proc and (proc not in per or mhz > per[proc]["recorded_mhz"]):
                per[proc] = {"recorded_mhz": mhz, "corner": corner,
                             "fmax_mhz": round(mhz / divide, 3),
                             "period_ns": round(divide * 1000.0 / mhz, 4)}
        missing = sorted({corner_of(n)["short"] for n in STA_CORNERS} - set(per))
        if missing:
            raise Refused(
                "macro_clock_no_record",
                f"{path} has no {c['measure']} at any corner of process "
                f"{missing}, so {pin} has no ceiling there; sweep them")
        out[pin] = {"margin": float(c.get("period_margin", PERIOD_MARGIN)),
                    "processes": per}
    return out


def judge_periods(ceilings: dict, seen: dict, instance: str) -> list[dict]:
    """Findings for `seen`: {corner_name: {pin_path_or_name: period_ns}}, every
    create_clock on a macro pin at each STA corner."""
    found = []
    for corner, clocks in seen.items():
        proc = corner_of(corner)["short"]
        for pin, ceiling in ceilings.items():
            period = clocks.get(pin)
            limit = ceiling["processes"][proc]
            if period is None:
                found.append({
                    "kind": "macro_clock_undefined", "pin": pin, "corner": corner,
                    "msg": f"the SDC creates no clock on the macro's {pin} at "
                           f"{corner}, which sources one up to "
                           f"{limit['fmax_mhz']} MHz ({limit['corner']})"})
                continue
            ref = limit["period_ns"]
            if period > ref * (1 + PERIOD_ROUNDING):
                why = (f"longer than the recorded ceiling's {ref} ns "
                       f"({limit['fmax_mhz']} MHz, {limit['corner']}): STA would "
                       "time slower than the macro can run")
            elif period < ref * (1 - ceiling["margin"]):
                why = (f"more than {ceiling['margin']:.0%} under the recorded "
                       f"ceiling's {ref} ns ({limit['fmax_mhz']} MHz, "
                       f"{limit['corner']}): it no longer follows the record")
            else:
                continue
            found.append({"kind": "macro_clock_disagrees", "pin": pin,
                          "corner": corner, "period_ns": period,
                          "ceiling_ns": ref,
                          "msg": f"{instance}/{pin}: period {period:g} ns at "
                                 f"{corner} is {why}"})
        extra = sorted(set(clocks) - set(ceilings))
        for pin in extra:
            found.append({
                "kind": "macro_clock_undeclared", "pin": pin, "corner": corner,
                "msg": f"the SDC creates a clock on the macro's {pin} at "
                       f"{corner}, and macro_timing.clocks names no record for "
                       "it: its period would be a typed number"})
    return found


# Runs the SDC under OpenSTA's Tcl with everything that needs a linked design
# stubbed, so its own procs (a period picked from get_libs, say) still run: the
# macro's liberty for the corner is what `get_libs` finds.
_EVAL_TCL = r"""
read_liberty {LIB}
foreach c [info commands] {
  if {[string match set_* $c] || [string match unset_* $c]
      || [string match all_* $c] || [string match group_* $c]
      || [string match report_* $c] || [string match remove_* $c]
      || $c in {get_ports get_pins get_nets get_cells get_clocks
                create_clock create_generated_clock
                current_design current_instance}} {
    rename $c {}
    proc $c {args} {return {}}
  }
}
# not empty: an SDC's own proc may refuse a lookup that finds nothing
foreach c {get_nets get_cells get_clocks all_inputs all_outputs all_clocks
           all_registers} {
  proc $c {args} {return [list OBJ {*}$args]}
}
proc get_ports {args} {return [list PORT {*}[lindex $args end]]}
proc get_pins {args} {return [list PIN {*}[lindex $args end]]}
proc create_clock {args} {
  set period {}; set name {}; set target {}
  for {set i 0} {$i < [llength $args]} {incr i} {
    set a [lindex $args $i]
    if {$a eq "-period"} {incr i; set period [lindex $args $i]
    } elseif {$a eq "-name"} {incr i; set name [lindex $args $i]
    } elseif {$a eq "-waveform"} {incr i
    } elseif {$a eq "-add"} {
    } else {set target $a}
  }
  foreach t [lrange $target 1 end] {puts "MACROLIB_CLOCK $period|$t"}
}
ENV
if {[catch {source {SDC}} err]} {
  puts "MACROLIB_SDC_ERROR $err"
}
exit
"""


def sdc_clocks(sdc: Path, lib: Path, work: Path, env: dict | None = None) -> dict:
    """{target_pin_or_port: period_ns} of every create_clock in `sdc` when
    `lib`'s corner is the one loaded. `eda sta` is the Tcl, so procs the SDC
    defines run as they do in a harden."""
    work.mkdir(parents=True, exist_ok=True)
    script = work / f"sdc_eval_{lib.stem}.tcl"
    env_lines = "\n".join(f"set ::env({k}) {{{v}}}" for k, v in (env or {}).items())
    script.write_text(_EVAL_TCL.replace("{LIB}", "{" + str(lib) + "}")
                      .replace("{SDC}", "{" + str(sdc) + "}")
                      .replace("ENV", env_lines), encoding="utf-8")
    proc = checklib.run_tool([str(charlib.EDA_BIN), "sta", "-no_init", "-exit",
                              str(script)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, wall=120)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    err = re.search(r"MACROLIB_SDC_ERROR (.*)", text)
    if err:
        raise Refused("macro_sdc_unreadable",
                      f"{sdc} did not evaluate at {lib.stem}: {err.group(1)}; "
                      "macro clock periods cannot be checked against it")
    if "MACROLIB_CLOCK" not in text and proc.returncode:
        raise Refused("macro_sdc_unreadable",
                      f"eda sta failed on {sdc}: {text[-400:]}")
    out = {}
    for line in re.findall(r"MACROLIB_CLOCK (.*)", text):
        period, _, target = line.partition("|")
        try:
            out[target.strip()] = float(period)
        except ValueError as exc:
            raise Refused("macro_sdc_unreadable",
                          f"{sdc}: create_clock on {target} has no numeric "
                          f"period ({period!r})") from exc
    return out


def macro_pins_of(clocks: dict, instance: str) -> dict:
    """create_clock targets on this macro's pins, by pin name."""
    prefix = instance + "/"
    return {t[len(prefix):].lower(): p for t, p in clocks.items()
            if t.startswith(prefix)}


def check_sdc(sdc: Path, lib_dir: Path, cell: str, ceilings: dict, instance: str,
              work: Path, env: dict | None = None,
              corners=STA_CORNERS) -> list[dict]:
    seen = {}
    for name in corners:
        lib = Path(lib_dir) / f"{cell}__{name}.lib"
        if not lib.is_file():
            raise Refused("macro_lib_missing", f"no {lib}; run the macro_lib gate")
        seen[name] = macro_pins_of(sdc_clocks(sdc, lib, work, env), instance)
    return judge_periods(ceilings, seen, instance)


def period_table_tcl(ceilings: dict) -> str:
    """The proc a top SDC calls so its period is the record's, not a literal:
    create_clock -period [macro_clock_period vco_div2] [get_pins u_analog/vco_div2].
    Each STA corner loads one process's liberty, and the tightest wins when several
    are loaded."""
    table = {pin: {p: v["period_ns"] for p, v in c["processes"].items()}
             for pin, c in sorted(ceilings.items())}
    rows = "\n".join(f"  {pin} {{{' '.join(f'{p} {t[p]:g}' for p in sorted(t))}}}"
                     for pin, t in table.items())
    return ("# Generated by the macro_lib gate from the analog side's record.\n"
            f"set ::macro_clock_periods_ns {{\n{rows}\n}}\n"
            "proc macro_clock_period {pin} {\n"
            "    set table [dict get $::macro_clock_periods_ns $pin]\n"
            "    set per {}\n"
            "    foreach lib [get_libs -quiet *] {\n"
            "        set n [get_name $lib]\n"
            "        dict for {corner p} $table {\n"
            '            if {[string match "*__${corner}_*" $n]} {lappend per $p}\n'
            "        }\n"
            "    }\n"
            "    if {[llength $per] == 0} {set per [dict values $table]}\n"
            "    return [tcl::mathfunc::min {*}$per]\n"
            "}\n")


def write_clocks(ceilings: dict, out_dir: Path, cell: str) -> None:
    (Path(out_dir) / f"{cell}.clocks.json").write_text(
        json.dumps(ceilings, indent=1, sort_keys=True), encoding="utf-8")


def deliver(ws: Path, cell: str, macros_dir: Path,
            corners=STA_CORNERS) -> dict:
    """Copy the gate's libs into top/macros; {corner: path}."""
    src = Path(ws) / OUT_DIR
    libs = {name: src / f"{cell}__{name}.lib" for name in corners}
    missing = sorted(n for n, p in libs.items() if not p.is_file())
    if missing:
        raise Refused("macro_lib_missing",
                      f"analog/spec/spec.yaml declares macro_timing, and "
                      f"{src} has no .lib at {missing}; run the macro_lib gate "
                      "before top_harden")
    out = {}
    macros_dir = Path(macros_dir)
    macros_dir.mkdir(parents=True, exist_ok=True)
    for name, path in libs.items():
        dst = macros_dir / path.name
        shutil.copyfile(path, dst)
        out[name] = str(dst.resolve())
    return out


def check_extracted(ws: Path, cell: str, extracted: Path) -> None:
    """A lib characterised on another extraction is stale: the file lives in
    the analog side's log/, which no freshness hash covers."""
    report = Path(ws) / OUT_DIR / f"{cell}.report.json"
    if not report.is_file():
        return  # deliver() refuses with what to run
    want = json.loads(report.read_text(encoding="utf-8")).get("extracted_sha256")
    have = hashlib.sha256(Path(extracted).read_bytes()).hexdigest()
    if want != have:
        raise Refused("macro_lib_missing",
                      f"{report.parent} was not characterised on the current "
                      f"{extracted.name}; re-run the macro_lib gate")

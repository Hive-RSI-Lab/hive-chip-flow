"""charlib.py - liberty characterisation of one cell over lctime (issue #130).

    characterize(cell, template_lib=..., schematic=..., extracted=...,
                 corners=[...], nominal_vdd=3.3, out_dir=...) -> report

Writes one <cell>__<corner>.lib per corner. Every corner is then re-simulated
at random table points with a plain ngspice bench that shares no code with
lctime (`validate`), and a table off by more than TOLERANCE is a finding: its
file is written as <cell>__<corner>.rejected.lib, never as the .lib.

The template is the target PDK lib: the index points of the cell's own tables
(or `reference_cell`'s, for a cell the PDK does not have), its thresholds and
its derate. The netlist is the extracted one when it exists, else the
schematic; the report and the .lib's head say which, because the two differ
(TOLERANCE_SHIPPED below).

What the wrapper corrects in lctime 0.0.26 (engine/scripts/lctime_shim.py has
the one it must patch at run time):
  - It wants quoted units, a lower-case .subckt, a pre-made workingdir/<cell>.
  - Its tables come out index_1=load, index_2=slew (constraints: related,
    constrained). The PDK has them the other way round, so they are transposed.
  - It ignores slew_derate_from_library. A library transition L means an actual
    threshold-to-threshold time of L*derate, so a table holds raw/derate
    (to_table_transition), or transitions read 0.34-0.48x on gf180 (derate 0.5,
    30/70). Same rule on the way in: it drives a linear 0-100% ramp as long as
    the number it is given, so it is given L*derate/(upper-lower) (ramp_of).
    The PDK's own driver is not linear (normalized_driver_waveform); only its
    30-70 slope is matched.
  - It never passes the temperature to the simulator, so every run was at
    25 C. A .temp card in the netlist it includes sets it (the self-check is what
    caught this; an in-process attempt to set its attribute had no effect).
  - Its min-pulse-width search crashes on dffq_1 ("Signal crosses threshold
    multiple times", at time steps 10, 2 and 1 ps), so the shim skips it.
  - Its hold tables are labelled by the data pulse's first edge (a "rise" hold
    is the falling edge after the clock). Liberty labels the edge that is
    constrained, so hold rise and fall are swapped.
  - Its setup is measured with hold near its own minimum, and its hold with
    setup near its minimum ("dependent" times): 1.9-4.2x the shipped values on
    extracted dffq_1. The shim skips that search too, and charlib bisects each
    table point on its own bench instead (measure_sequential, issue #172): the
    shortest setup (hold) time, the other side wide open, at which CRITERION
    holds. Each side is independent, as in the PDK's own lib; both at their
    minimum at once is not promised. min_pulse_width_high/low on the clock pin
    are bisected the same way, the worst over clock slews and both D values.

  - Its internal power is not used (issue #173): it labels the energy by the
    input edge, stops integrating at 99% of the swing, and sums the supply's and
    the input's charge, which cancel to about 0 on a gate. charlib measures
    every arc on its own bench instead (measure_power): the energy the supply
    pins (primary_power and nwell) deliver over a window that ends after the
    output has settled, less the load's share V*C*(v1-v0) on a rising output
    (Liberty's convention, the shipped lib's too). The self-check re-measures
    random points with the load's charge read through a 0 V probe over a longer
    window. Edges that leave every output alone are the inputs' hidden power.
    A flip-flop's Q arc holds the clock edge less CLK's hidden power, which a
    power tool adds on every edge.
  - Leakage is one DC operating point per input state at LEAKAGE_OPTIONS. A
    flip-flop's stored state is clocked in by a transient, whose node voltages
    seed the DC run (.nodeset), the worst of Q stored 0 and 1: a transient
    alone reads ~1e-9 A of its own error, and a DC run alone may pick either
    state. The PDK's shipped leakage is mostly ngspice's default gmin across
    the junctions: it reads 0.33-0.56x shipped at default gmin, 0.0007-0.04x
    at 1e-18.

Timing, input capacitance, internal power, leakage, max_transition (the largest
characterised slew, every pin) and max_capacitance (the largest characterised
load, every output: the tables are not extrapolated), and for a flip-flop
setup/hold and min_pulse_width. Nothing the wrapper did not measure at this
corner reaches the output; nothing is copied from the PDK's other corner.
merge_libraries puts several cells of one corner into one .lib. Latches and
tri-states are refused (lctime has no latch path); so is any flip-flop the
bench cannot drive (next_state not a single pin, a set or reset). Recovery/removal is not measured.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import os
import queue
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EDA_BIN = REPO / "bin" / "eda"
SHIM = REPO / "engine" / "scripts" / "lctime_shim.py"
_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import simlib  # noqa: E402

# Self-check: |table - bench| <= max(rel * |bench|, abs ns). lctime and the
# bench run the same models, so what is left is lctime's time step and threshold
# interpolation. A stated number; do not widen it to pass.
TOLERANCE = {"rel": 0.05, "abs_ns": 0.010}
# Setup, hold and min_pulse_width are measured by bisection on the bench
# (measure_sequential), against this criterion: the flop captured the value (Q
# crossed and ends there) no later than (1 + pushout_rel) x its clock-to-Q with
# a wide-open window, Q unloaded. Bisected to resolution_ns. On extracted
# dffq_1 this lands within 0.042 ns of the shipped setup/hold for clock and
# data slews up to 0.68 ns, 0.114 ns up to 2.2 ns.
CRITERION = {"pushout_rel": 0.03, "resolution_ns": 0.002}
# Constraint self-check, two-sided: at value + margin the criterion must hold,
# at value - margin it must fail (min_pulse_width: at one clock slew and D value
# at least, the value being their worst). margin = max(rel*|v|, abs) ns.
CONSTRAINT = {"margin_rel": 0.05, "margin_abs": 0.015}
DEFAULT_CHECKS = 2  # random table points per corner, each arc, each edge
# Power self-check: |table - bench| <= max(rel * |bench|, abs pJ). The bench
# reads the load's charge through a 0 V probe over a window 20 spans long
# instead of subtracting V*C*dV: a table that kept the load's energy, or cut
# the tail of the supply current, fails it at large loads.
POWER_TOLERANCE = {"rel": 0.05, "abs_pj": 0.002}
# Leakage is a DC operating point (seeded by a transient for a flip-flop) with
# gmin and abstol far below the cell's own currents: ngspice's default gmin
# (1e-12 S across every junction) is most of the PDK's shipped leakage.
LEAKAGE_OPTIONS = ".options gmin=1e-18 abstol=1e-18 reltol=1e-6"
# Against the shipped lib (extracted netlist, tt_025C_3v30): an output arc's
# internal power within max(rel * |shipped|, abs pJ) of shipped; an input's
# hidden power (a few fF of gate charge, so a larger share is noise) looser.
# Leakage at LEAKAGE_OPTIONS lands at 0.0007-0.04x shipped: the band is
# (0, shipped], since shipped carries ngspice's default gmin.
POWER_BAND_SHIPPED = {"output": {"rel": 0.08, "abs_pj": 0.002},
                      "hidden": {"rel": 0.35, "abs_pj": 0.002},
                      "leakage_ratio": (0.0, 1.0)}
# Known answer against the shipped tt_025C_3v30 lib: table/shipped must lie in
# these bands, by netlist and load. tests/test_charlib.py asserts them. The PDK
# characterised extracted cells, so the schematic reads low (no wire C inside the
# cell): 0.7x at 3.4 fF on an inverter, 0.6x clock-to-Q on a flip-flop whatever
# the load. Measured on extracted inv_1, nand2_1, dffq_1: delay 0.86-0.99,
# transition 0.90-1.00, input cap 0.81-1.11.
# Setup and hold: table - shipped in ns, at slews up to 0.7 ns (slower ones
# drift to 0.114 ns). min_pulse_width: table/shipped. Measured on extracted
# dffq_1: setup/hold within 0.042 ns, min_pulse_width 0.74 high, 0.94-0.99 low
# (the shipped high width is not the narrowest that captures).
TOLERANCE_SHIPPED = {
    ("extracted", "delay", "load>=20fF"): (0.85, 1.15),
    ("extracted", "transition", "load>=20fF"): (0.85, 1.15),
    ("extracted", "input_cap", "any"): (0.75, 1.15),
    ("schematic", "delay", "load>=20fF"): (0.80, 1.15),
    ("schematic", "delay", "load<20fF"): (0.60, 1.15),
    ("schematic", "transition", "load>=20fF"): (0.80, 1.15),
    ("extracted", "setup_hold_abs_ns", "slew<=0.7ns"): (-0.06, 0.06),
    ("extracted", "min_pulse_width", "any"): (0.70, 1.10),
}
MODEL_LIB = "sm141064.ngspice"
DESIGN_INCLUDE = "design.ngspice"


class Refused(ValueError):
    """No number can be given. `kind` is stable; str() says what to do."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


# --- a small Liberty reader/writer (one cell at a time, so speed is no issue)

@dataclass
class Group:
    name: str
    args: str = ""
    attrs: list = field(default_factory=list)  # (key, value, is_complex)
    groups: list = field(default_factory=list)

    def attr(self, key, default=None):
        for k, v, _ in self.attrs:
            if k == key:
                return v
        return default

    def set_attr(self, key, value, is_complex=False):
        for i, (k, _, _) in enumerate(self.attrs):
            if k == key:
                self.attrs[i] = (key, value, is_complex)
                return
        self.attrs.append((key, value, is_complex))

    def sub(self, name):
        return [g for g in self.groups if g.name == name]


_TOKEN = re.compile(r"""\s*(?:(?P<word>[A-Za-z_][\w.]*)\s*
                        (?:(?P<open>\()|(?P<colon>:))?|(?P<close>\}))""", re.X)


def _matching_paren(text, i):
    depth, quoted = 0, False
    while i < len(text):
        c = text[i]
        if c == '"':
            quoted = not quoted
        elif not quoted:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if not depth:
                    return i
        i += 1
    raise Refused("bad_liberty", "unbalanced parenthesis in a liberty file")


def _parse_body(text, i, group):
    while True:
        m = _TOKEN.match(text, i)
        if not m:
            if text[i:].strip(" \t\r\n;"):
                if text[i:].lstrip(" \t\r\n").startswith(";"):
                    i = text.index(";", i) + 1
                    continue
                raise Refused("bad_liberty",
                              f"cannot read liberty near {text[i:i + 60]!r}")
            return len(text)
        if m.group("close"):
            return m.end()
        key = m.group("word")
        if m.group("colon"):
            end = text.index("\n", m.end()) if "\n" in text[m.end():] else len(text)
            semi = _value_end(text, m.end(), end)
            group.attrs.append((key, text[m.end():semi].strip(), False))
            i = semi + 1 if semi < len(text) and text[semi] == ";" else semi
            continue
        if not m.group("open"):
            raise Refused("bad_liberty", f"stray word {key!r} in a liberty file")
        close = _matching_paren(text, m.end() - 1)
        args = text[m.end():close].replace("\\\n", " ").strip()
        j = close + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == "{":
            child = Group(key, args)
            i = _parse_body(text, j + 1, child)
            group.groups.append(child)
        else:
            group.attrs.append((key, args, True))
            i = j + 1 if j < len(text) and text[j] == ";" else j


def _value_end(text, start, line_end):
    """A simple attribute's value ends at its ';' or, if it has none, its line
    (quotes respected: a function may hold anything)."""
    quoted = False
    for i in range(start, line_end):
        c = text[i]
        if c == '"':
            quoted = not quoted
        elif c == ";" and not quoted:
            return i
    return line_end


def parse_liberty(text: str) -> Group:
    """The first top-level group of `text`."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    root = Group("")
    _parse_body(text, 0, root)
    if len(root.groups) != 1:
        raise Refused("bad_liberty",
                      f"expected one top-level group, read {len(root.groups)}")
    return root.groups[0]


def write_liberty(group: Group, indent: int = 0) -> str:
    pad = "  " * indent
    out = [f"{pad}{group.name}({group.args}) {{"]
    for key, value, is_complex in group.attrs:
        out.append(f"{pad}  {key}({value});" if is_complex
                   else f"{pad}  {key} : {value} ;")
    out.extend(write_liberty(g, indent + 1) for g in group.groups)
    out.append(f"{pad}}}")
    return "\n".join(out)


def numbers(value: str) -> list[float]:
    return [float(x) for x in re.findall(
        r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value)]


def table(g: Group) -> list[list[float]]:
    """A table group's `values` as rows; one quoted string is one row."""
    rows = re.findall(r'"([^"]*)"', g.attr("values", ""))
    return [numbers(r) for r in rows]


def _quoted_rows(rows):
    return ", \\\n".join('"' + ", ".join(f"{v:.6g}" for v in r) + '"'
                         for r in rows)


def _quoted_list(values):
    return '"' + ", ".join(f"{v:.6g}" for v in values) + '"'


def split_library(text: str, cell: str) -> tuple[str, str]:
    """(header, cell group) of `cell`, cut out as text: a PDK lib is 20 MB."""
    first = re.search(r"^\s*cell\s*\(", text, re.M)
    if not first:
        raise Refused("bad_template", "the template lib holds no cell group")
    m = re.search(r"^\s*cell\s*\(\s*\"?%s\"?\s*\)" % re.escape(cell), text, re.M)
    if not m:
        raise Refused("cell_not_in_template",
                      f"{cell} is not in the template lib: pass cell_text (its "
                      f"pins and functions) and reference_cell (a PDK cell to "
                      f"take index points from)")
    return text[:first.start()], text[m.start():_group_end(text, m.start())]


def _group_end(text, start):
    i, depth = text.index("{", start), 0
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    raise Refused("bad_liberty", "unbalanced brace in a liberty file")


# --- the template's terms

@dataclass(frozen=True)
class Terms:
    derate: float
    slew_lower: float   # fraction of VDD
    slew_upper: float
    input_threshold: float
    output_threshold: float


def terms_of(header: Group) -> Terms:
    def pct(*keys):
        values = {header.attr(k) for k in keys}
        if None in values:
            raise Refused("bad_template",
                          f"the template lib does not state {keys}")
        if len({float(v) for v in values}) != 1:
            raise Refused("asymmetric_thresholds",
                          f"{keys} differ; the bench assumes rise == fall")
        return float(values.pop()) / 100.0
    derate = header.attr("slew_derate_from_library", "1")
    return Terms(float(derate),
                 pct("slew_lower_threshold_pct_rise",
                     "slew_lower_threshold_pct_fall"),
                 pct("slew_upper_threshold_pct_rise",
                     "slew_upper_threshold_pct_fall"),
                 pct("input_threshold_pct_rise", "input_threshold_pct_fall"),
                 pct("output_threshold_pct_rise", "output_threshold_pct_fall"))


def ramp_of(library_slew: float, t: Terms) -> float:
    """The 0-100% length of the linear ramp a library transition stands for."""
    return library_slew * t.derate / (t.slew_upper - t.slew_lower)


def to_table_transition(raw: float, t: Terms) -> float:
    """A measured threshold-to-threshold time, as the library writes it."""
    return raw / t.derate


def index_points(cell_group: Group) -> dict:
    """{slews, loads, constraint}: the first cell-delay table's axes and the
    first constraint table's (the PDK uses one set per cell)."""
    slews = loads = constraint = None
    for pin in cell_group.sub("pin"):
        for timing in pin.sub("timing"):
            for tab in timing.groups:
                if (tab.name in ("cell_rise", "cell_fall") and slews is None
                        and tab.attr("index_2")):
                    slews = numbers(tab.attr("index_1"))
                    loads = numbers(tab.attr("index_2"))
                if (tab.name.endswith("_constraint") and constraint is None
                        and tab.attr("index_2")):
                    constraint = numbers(tab.attr("index_1"))
    if slews is None:
        raise Refused("no_index_points",
                      f"{cell_group.args} has no delay table to take index "
                      f"points from: pass reference_cell")
    return {"slews": slews, "loads": loads, "constraint": constraint or slews}


# --- what lctime reads

def corner_header(header_text: str, library: str, vdd: float,
                  temp_c: float, for_lctime: bool,
                  conditions: str | None = None) -> Group:
    """The template's header at this corner, without its table templates. Every
    voltage in it belongs to the PDK's corner, so all of them are rewritten.
    operating_conditions is named `conditions` (the corner), so the libs of
    several cells at one corner merge."""
    h = parse_liberty(header_text.rstrip() + "\n}\n")
    h.args = library
    # always with a point: lctime asserts a float, and "3" reads as an int
    v = f"{vdd:g}" if "." in f"{vdd:g}" else f"{vdd:g}.0"
    h.set_attr("nom_voltage", v)
    h.set_attr("nom_temperature", f"{temp_c:g}")
    h.set_attr("nom_process", "1")
    for i, (k, val, cx) in enumerate(h.attrs):
        if k == "voltage_map":
            net, _, level = val.partition(",")
            if float(level) != 0:
                h.attrs[i] = (k, f"{net.strip()}, {v}", cx)
        elif for_lctime and k.endswith("_unit") and not cx:
            h.attrs[i] = (k, '"' + val.strip('"') + '"', cx)
    keep = []
    for g in h.groups:
        if g.name == "operating_conditions":
            g.args = conditions or library
            g.set_attr("voltage", v)
            g.set_attr("temperature", f"{temp_c:g}")
        elif g.name in ("input_voltage", "output_voltage"):
            hi, lo = ("vih", "vil") if g.name == "input_voltage" else ("voh", "vol")
            g.set_attr(hi, v)
            g.set_attr(lo, "0")
            max_key = "vimax" if g.name == "input_voltage" else "vomax"
            g.set_attr(max_key, v)
        elif g.name in ("lu_table_template", "power_lut_template",
                        "normalized_driver_waveform"):
            continue
        keep.append(g)
    h.groups = keep
    return h


def bare_cell(cell_group: Group) -> Group:
    """The cell's interface only: pins, functions, its ff. lctime would otherwise
    carry the PDK corner's leakage and limits into its output."""
    if cell_group.sub("latch") or cell_group.sub("statetable"):
        raise Refused("unsupported_cell",
                      f"{cell_group.args} is a latch: lctime 0.0.26 aborts on "
                      f"those")
    out = Group("cell", cell_group.args)
    for k, v, cx in cell_group.attrs:
        if k == "area":
            out.attrs.append((k, v, cx))
    for g in cell_group.groups:
        if g.name in ("pg_pin", "ff"):
            out.groups.append(g)
        elif g.name == "pin":
            if g.attr("three_state"):
                raise Refused("unsupported_cell",
                              f"{cell_group.args}/{g.args} is tri-state: the "
                              f"bench has no Z arcs")
            pin = Group("pin", g.args)
            for k, v, cx in g.attrs:
                if k in ("direction", "function", "clock"):
                    pin.attrs.append((k, v, cx))
            out.groups.append(pin)
    return out


def minimal_cell(cell: str, inputs, outputs: dict, area: float = 0.0) -> str:
    """A cell group for a cell the PDK does not have: `outputs` maps pin to its
    liberty function. The pg pins are the gf180 stdcell ones."""
    pins = "".join(f"pin({p}) {{ direction : input ; }}\n" for p in inputs)
    pins += "".join(
        f'pin({p}) {{ direction : output ; function : "{f}" ; }}\n'
        for p, f in outputs.items())
    pg = "".join(
        f"pg_pin({n}) {{ voltage_name : {n} ; pg_type : {t} ; }}\n"
        for n, t in (("VPW", "pwell"), ("VNW", "nwell"),
                     ("VDD", "primary_power"), ("VSS", "primary_ground")))
    return f"cell({cell}) {{ area : {area:g} ;\n{pg}{pins}}}\n"


def prepare_netlist(text: str, cell: str) -> str:
    """The subckt blocks only, directives lower-case; lctime's parser finds
    nothing otherwise. A hierarchical cell keeps its children."""
    blocks = re.findall(r"(?ims)^\.subckt\b.*?^\.ends\b[^\n]*", text)
    if not any(re.match(r"(?i)\.subckt\s+%s\b" % re.escape(cell), b)
               for b in blocks):
        raise Refused("cell_not_in_netlist",
                      f"no .subckt {cell} in the netlist")
    out = []
    for b in blocks:
        b = re.sub(r"(?i)^\.subckt", ".subckt", b)
        out.append(re.sub(r"(?im)^\.ends\b.*$", ".ends", b))
    return "\n".join(out) + "\n"


def ports_of(netlist_text: str, cell: str) -> list[str]:
    m = re.search(r"(?im)^\.subckt\s+%s\b((?:[^\n]*\n\+)*[^\n]*)" %
                  re.escape(cell), netlist_text)
    if not m:
        raise Refused("cell_not_in_netlist", f"no .subckt {cell}")
    words = m.group(1).replace("\n+", " ").split()
    return [w for w in words if "=" not in w]


def temperature_card(corner: dict) -> str:
    return f".temp {corner['temp_c']}\n"


def model_files(pdk: Path) -> tuple[Path, Path]:
    d = Path(pdk) / "libs.tech" / "ngspice"
    return d / MODEL_LIB, d / DESIGN_INCLUDE


def model_sections(corner: dict) -> list[str]:
    """The .lib sections of a corner. The PDK's res_*/mimcap_* sections follow
    the corner's passive axis: a custom cell's poly resistor must move too."""
    process = corner["process"]
    passive = corner.get("passive", process if process in ("ss", "ff")
                         else "typical")
    return [process, f"res_{passive}", f"mimcap_{passive}"]


def run_lctime(cell, lib_in: Path, netlist: Path, out: Path, work: Path, *,
               pdk: Path, corner: dict, ramps, loads, clock_ramps=None,
               timeout: float = 1800.0) -> None:
    (work / cell).mkdir(parents=True, exist_ok=True)
    models, design = model_files(pdk)
    argv = [str(EDA_BIN), "python", str(SHIM), "-l", str(lib_in),
            "--cell", cell, "--spice", str(netlist), "-I", str(design),
            "-o", str(out), "--workingdir", str(work), "-j", "1",
            "--calc-mode", "worst",
            "--output-loads", ",".join(f"{c:.6g}" for c in loads),
            "--slew-times", ",".join(f"{r:.6g}" for r in ramps)]
    for section in model_sections(corner):
        argv += ["-L", f"{models} {section}"]
    if clock_ramps:
        argv += ["--related-pin-transition",
                 ",".join(f"{r:.6g}" for r in clock_ramps)]
    # lctime spawns `ngspice` by name, and `eda python` puts no tool on PATH
    tool_dir = work / "bin"
    tool_dir.mkdir(exist_ok=True)
    tool = tool_dir / "ngspice"
    tool.write_text(f'#!/bin/sh\nexec "{EDA_BIN}" ngspice "$@"\n', encoding="utf-8")
    tool.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tool_dir}{os.pathsep}{env.get('PATH', '')}"
    widest = max(clock_ramps or ramps)
    env["CHARLIB_CLOCK_PULSE_NS"] = str(max(2.0, 4 * widest))
    try:
        proc = subprocess.run(argv, cwd=str(work), capture_output=True,
                              text=True, errors="replace", timeout=timeout,
                              env=env)
    except subprocess.TimeoutExpired as exc:
        raise Refused("lctime_failed",
                      f"lctime timed out after {timeout:g} s") from exc
    (work / "lctime.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode or not out.is_file():
        raise Refused("lctime_failed",
                      f"lctime exit {proc.returncode}, see {work}/lctime.log: "
                      f"{(proc.stdout + proc.stderr)[-600:]}")


# --- from lctime's output to the library's conventions

def _orient(rows, index_1, index_2, want_1, want_2, name):
    """Rows of a table with index_1=want_1 and index_2=want_2, whichever way
    round lctime wrote it; anything else is a refusal, not a guess."""
    def same(a, b):
        return (len(a) == len(b) and
                all(abs(x - y) <= 1e-4 * max(abs(y), 1e-9) for x, y in zip(a, b)))
    # a square table with equal axes (a constraint) is decided by the caller
    if same(index_1, want_2) and same(index_2, want_1):
        return [list(r) for r in zip(*rows)]
    if same(index_1, want_1) and same(index_2, want_2):
        return rows
    raise Refused("table_axes",
                  f"{name}: lctime's axes {index_1} x {index_2} are not the "
                  f"requested {want_1} x {want_2}")


def delay_template(n_slew, n_load) -> str:
    """Template names carry their size, so libraries of cells characterised
    over different index subsets still merge (merge_libraries)."""
    return f"charlib_delay_{n_slew}x{n_load}"


def constraint_template(n_data, n_clock) -> str:
    return f"charlib_constraint_{n_data}x{n_clock}"


def power_template(n_slew, n_load=None) -> str:
    return (f"charlib_power_{n_slew}x{n_load}" if n_load
            else f"charlib_power_{n_slew}")


def _template_groups(n_slew, n_load, n_data, n_clock):
    delay = Group("lu_table_template", delay_template(n_slew, n_load))
    delay.attrs = [("variable_1", "input_net_transition", False),
                   ("variable_2", "total_output_net_capacitance", False),
                   ("index_1", _quoted_list(range(1, n_slew + 1)), True),
                   ("index_2", _quoted_list(range(1, n_load + 1)), True)]
    groups = [delay]
    if n_data:
        cnst = Group("lu_table_template", constraint_template(n_data, n_clock))
        cnst.attrs = [("variable_1", "constrained_pin_transition", False),
                      ("variable_2", "related_pin_transition", False),
                      ("index_1", _quoted_list(range(1, n_data + 1)), True),
                      ("index_2", _quoted_list(range(1, n_clock + 1)), True)]
        groups.append(cnst)
    return groups


def _power_template_groups(n_slew, n_load):
    two = Group("power_lut_template", power_template(n_slew, n_load))
    two.attrs = [("variable_1", "input_transition_time", False),
                 ("variable_2", "total_output_net_capacitance", False),
                 ("index_1", _quoted_list(range(1, n_slew + 1)), True),
                 ("index_2", _quoted_list(range(1, n_load + 1)), True)]
    one = Group("power_lut_template", power_template(n_slew))
    one.attrs = [("variable_1", "input_transition_time", False),
                 ("index_1", _quoted_list(range(1, n_slew + 1)), True)]
    return [two, one]


def assemble(raw_text: str, header: Group, t: Terms, slews, loads, ramps,
             data_slews=None, clock_slews=None) -> tuple[Group, str]:
    """assemble_library, written out."""
    cell, lib = assemble_library(raw_text, header, t, slews, loads, ramps,
                                 data_slews, clock_slews)
    return cell, write_liberty(lib) + "\n"


def assemble_library(raw_text: str, header: Group, t: Terms, slews, loads,
                     ramps, data_slews=None, clock_slews=None
                     ) -> tuple[Group, Group]:
    """lctime's output, at the library's axes and with the library's transition
    meaning, in a library holding what was measured at this corner and no more."""
    raw = parse_liberty(raw_text)
    cells = raw.sub("cell")
    if len(cells) != 1:
        raise Refused("lctime_failed", f"lctime wrote {len(cells)} cells")
    src = cells[0]
    cell = Group("cell", src.args)
    cell.attrs = [a for a in src.attrs if a[0] == "area"]
    cell.groups = [g for g in src.groups if g.name in ("pg_pin", "ff")]
    for pin in src.sub("pin"):
        new = Group("pin", pin.args)
        new.attrs = [a for a in pin.attrs if a[0] in
                     ("direction", "function", "clock", "capacitance",
                      "rise_capacitance", "fall_capacitance")]
        new.groups = [_timing(tg, t, slews, loads, ramps, data_slews, clock_slews)
                      for tg in pin.sub("timing")]
        cell.groups.append(new)
    lib = Group(header.name, header.args, list(header.attrs), list(header.groups))
    lib.groups += _template_groups(len(slews), len(loads),
                                   len(data_slews or ()), len(clock_slews or ()))
    lib.groups.append(cell)
    return cell, lib


def _timing(tg, t, slews, loads, ramps, data_slews, clock_slews) -> Group:
    new = Group("timing", tg.args)
    new.attrs = list(tg.attrs)
    kind = str(tg.attr("timing_type", "")).strip('"')
    for tab in tg.groups:
        rows = table(tab)
        i1, i2 = numbers(tab.attr("index_1", "")), numbers(tab.attr("index_2", ""))
        out = Group(tab.name)
        if tab.name.endswith("_constraint"):
            # lctime: index_1 related (clock), index_2 constrained (data), both
            # given in the same units - a square table cannot be told apart by
            # its axes when the two lists are equal, so this is by contract
            # (characterize_flip_flop_setup_hold's meshgrid).
            rows = [list(r) for r in zip(*rows)]
            out.args = constraint_template(len(data_slews), len(clock_slews))
            out.attrs = [("index_1", _quoted_list(data_slews), True),
                         ("index_2", _quoted_list(clock_slews), True)]
            if kind.startswith("hold"):
                # Liberty labels the edge constrained, lctime the pulse's first
                out.name = ("fall_constraint" if tab.name == "rise_constraint"
                            else "rise_constraint")
        else:
            rows = _orient(rows, i1, i2, ramps, loads, tab.name)
            if tab.name.endswith("_transition"):
                rows = [[to_table_transition(v, t) for v in r] for r in rows]
            out.args = delay_template(len(slews), len(loads))
            out.attrs = [("index_1", _quoted_list(slews), True),
                         ("index_2", _quoted_list(loads), True)]
        out.attrs.append(("values", _quoted_rows(rows), True))
        new.groups.append(out)
    return new


# --- the self-check: a plain ngspice bench, no lctime code

_FN_TOKEN = re.compile(r"\s*([A-Za-z_]\w*|[!()&|^*+'])")


def eval_function(function: str, values: dict) -> bool:
    """A liberty function at `values`: ! ' & * | + ^, juxtaposition is AND."""
    tokens = _FN_TOKEN.findall(function.strip('" '))
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def or_():
        v = xor_()
        while peek() in ("|", "+"):
            take()
            r = xor_()
            v = v or r
        return v

    def xor_():
        v = and_()
        while peek() == "^":
            take()
            v = v != and_()
        return v

    def and_():
        v = unary()
        while peek() in ("&", "*") or (peek() not in (None, ")", "|", "+", "^")):
            if peek() in ("&", "*"):
                take()
            r = unary()
            v = v and r
        return v

    def unary():
        if peek() == "!":
            take()
            return not unary()
        tok = take()
        if tok == "(":
            v = or_()
            take()
        elif tok in ("0", "1"):
            v = tok == "1"
        else:
            if tok not in values:
                raise Refused("bad_function",
                              f"{function!r} names {tok}, which has no value")
            v = values[tok]
        while peek() == "'":
            take()
            v = not v
        return v

    return or_()


_MEASURE_FAILED = re.compile(r"(?i)^\s*(error: measure\b|\.meas\b.*failed!)")


@dataclass
class Bench:
    cell: str
    netlist: Path
    ports: list
    pg: dict      # pg pin -> pg_type
    pdk: Path
    corner: dict
    vdd: float
    work: Path
    terms: Terms
    runs: int = 0

    def _deck(self, stim: dict, load_pin: str, load_pf: float, t_stop_ns: float,
              t_step_ns: float, measures: list[str], options: str = "",
              probe: bool = False, analysis: str = "") -> str:
        models, design = model_files(self.pdk)
        out = [f"* charlib self-check {self.cell}"]
        out += [f".lib {models} {s}" for s in model_sections(self.corner)]
        out += [f".include {design}", f".include {self.netlist}",
                f".temp {self.corner['temp_c']}"]
        conns = []
        for p in self.ports:
            conns.append(p)
            kind = self.pg.get(p)
            if kind in ("primary_power", "nwell"):
                out.append(f"V_{p} {p} 0 {self.vdd:g}")
            elif kind in ("primary_ground", "pwell"):
                out.append(f"V_{p} {p} 0 0")
            elif p in stim:
                out.append(f"V_{p} {p} 0 {stim[p]}")
        out.append(f"Xdut {' '.join(conns)} {self.cell}")
        if probe:
            # the load's charge read through a 0 V source (the power self-check)
            out.append(f"V_probe {load_pin} {load_pin}__load 0")
            out.append(f"Cload {load_pin}__load 0 {load_pf:g}p")
        else:
            out.append(f"Cload {load_pin} 0 {load_pf:g}p")
        if options:
            out.append(options)
        out.append(analysis or
                   f".tran {t_step_ns:g}n {t_stop_ns:g}n 0 {t_step_ns:g}n")
        out += measures
        out.append(".end")
        return "\n".join(out) + "\n"

    def simulate(self, stim, load_pin, load_pf, t_stop_ns, t_step_ns,
                 measures, timeout: float = 600.0, options: str = "",
                 probe: bool = False, analysis: str = "") -> dict:
        self.runs += 1
        deck = self.work / f"check_{self.runs}.cir"
        deck.write_text(self._deck(stim, load_pin, load_pf, t_stop_ns,
                                   t_step_ns, measures, options, probe,
                                   analysis),
                        encoding="utf-8")
        stdout, stderr, rc = simlib.run_ngspice(EDA_BIN, deck, self.work, timeout)
        # a measure that never happened is the bench's answer ("Q did not move"),
        # read from its absence below; anything else ngspice says is an error
        text = "\n".join(line for line in (stdout + stderr).splitlines()
                         if not _MEASURE_FAILED.search(line))
        errors = simlib.detect_engine_errors(text)
        if rc or errors:
            raise Refused("bench_failed",
                          f"{deck.name}: exit {rc}, {errors}: {stderr[-500:]}")
        return simlib.parse_measures(stdout)


def _ramp_edges(vdd, edges, end_ns):
    """PWL(...) for [(t_start_ns, ramp_ns, level_after)...]; level before the
    first is its opposite."""
    level = 0.0 if edges[0][2] else vdd
    points = [(0.0, level)]
    for start, ramp, after in edges:
        target = vdd if after else 0.0
        points += [(start, level), (start + ramp, target)]
        level = target
    points.append((end_ns, level))
    return "PWL(" + " ".join(f"{t:.6g}n {v:g}" for t, v in points) + ")"


def _when(name, node, level, edge, cross="LAST"):
    return f".meas tran {name} WHEN v({node})={level:.6g} {edge}={cross}"


def _within(table_v, bench_v):
    return abs(table_v - bench_v) <= max(TOLERANCE["rel"] * abs(bench_v),
                                         TOLERANCE["abs_ns"])


def _point(kind, table_v, bench_v, **where):
    return {"table": kind, "value_ns": table_v, "bench_ns": bench_v,
            "ok": bench_v is not None and _within(table_v, bench_v), **where}


def _check_edge(bench: Bench, stim_for, related, out_pin, out_rises, slews, loads,
                tables, i, j, t_edge_ns, **where) -> list[dict]:
    """One simulation at table point (i, j): an output edge's delay and its
    transition. `stim_for(ramp)` gives the stimulus; the input's threshold is
    crossed at t_ref(ramp)."""
    t = bench.terms
    vdd = bench.vdd
    ramp = ramp_of(slews[i], t)
    edge = "RISE" if out_rises else "FALL"
    delay_name, trans_name = (("cell_rise", "rise_transition") if out_rises
                              else ("cell_fall", "fall_transition"))
    delay_tab, trans_tab = tables[delay_name][i][j], tables[trans_name][i][j]
    span = max(abs(delay_tab), trans_tab, ramp, 0.05)
    t_stop = t_edge_ns + ramp + 12 * span
    step = max(min(ramp, span) / 100.0, 0.0005)
    first, second = ((t.slew_lower, t.slew_upper) if out_rises
                     else (t.slew_upper, t.slew_lower))
    measures = [
        _when("t_o50", out_pin, t.output_threshold * vdd, edge),
        _when("t_a", out_pin, first * vdd, edge),
        _when("t_b", out_pin, second * vdd, edge),
    ]
    m = bench.simulate(stim_for(ramp), out_pin, loads[j], t_stop, step, measures)
    base = dict(i=i, j=j, slew=slews[i], load=loads[j], **where)
    # the input's threshold, on a linear ramp starting at t_edge_ns
    t_in = t_edge_ns + t.input_threshold * ramp
    if not {"t_o50", "t_a", "t_b"} <= m.keys():
        return [_point(delay_name, delay_tab, None, **base),
                _point(trans_name, trans_tab, None, **base)]
    delay = m["t_o50"] * 1e9 - t_in
    trans = to_bench_transition((m["t_b"] - m["t_a"]) * 1e9, t)
    return [_point(delay_name, delay_tab, delay, **base),
            _point(trans_name, trans_tab, trans, **base)]


def to_bench_transition(raw_ns: float, t: Terms) -> float:
    """The bench's own derate, deliberately not to_table_transition: a planted
    fault in the wrapper must not move the reference it is checked against."""
    return raw_ns / t.derate


def _tables_of(timing: Group) -> dict:
    return {g.name: table(g) for g in timing.groups}


def _check_combinational(bench, cell, rng, slews, loads, n) -> list[dict]:
    points = []
    inputs = [p.args for p in cell.sub("pin") if p.attr("direction") == "input"]
    for pin in cell.sub("pin"):
        fn = pin.attr("function")
        for timing in pin.sub("timing"):
            related = timing.attr("related_pin").strip('"')
            tables = _tables_of(timing)
            others = [p for p in inputs if p != related]
            for _ in range(n):
                i, j = rng.randrange(len(slews)), rng.randrange(len(loads))
                for out_rises in (True, False):
                    points += _worst_of_conditions(
                        bench, fn, pin.args, related, others, out_rises, slews,
                        loads, tables, i, j)
    return points


def _worst_of_conditions(bench, fn, out_pin, related, others, out_rises, slews,
                         loads, tables, i, j) -> list[dict]:
    """lctime reports the worst of the side-input conditions (--calc-mode
    worst), so the bench runs them all and takes the worst."""
    per_condition = []
    t0 = 1.0
    for combo in itertools.product((False, True), repeat=len(others)):
        side = dict(zip(others, combo))
        at_low = eval_function(fn, {**side, related: False})
        at_high = eval_function(fn, {**side, related: True})
        if at_low == at_high:
            continue
        in_rises = at_high == out_rises
        stim = {p: f"{bench.vdd if v else 0:g}" for p, v in side.items()}

        def stim_for(ramp, stim=stim, in_rises=in_rises):
            return {**stim, related: _ramp_edges(
                bench.vdd, [(t0, ramp, in_rises)], t0 + ramp + 1)}
        per_condition.append(_check_edge(
            bench, stim_for, related, out_pin, out_rises, slews, loads, tables,
            i, j, t0, arc=f"{related}->{out_pin}"))
    if not per_condition:
        raise Refused("bad_function",
                      f"{fn!r} does not depend on {related} but lctime wrote an "
                      f"arc for it")
    merged = []
    for k in range(len(per_condition[0])):
        worst = max(per_condition, key=lambda p: (p[k]["bench_ns"] is None,
                                                  p[k]["bench_ns"] or 0.0))[k]
        merged.append(worst)
    return merged


def _ff_terms(cell) -> tuple[str, str, bool]:
    ffs = cell.sub("ff")
    # lctime rewrites these with parentheses: "(D)", "(CLK)"
    clocked = ffs[0].attr("clocked_on").strip('"() ')
    falling = clocked.startswith("!")
    clock = clocked.lstrip("!").strip("() ")
    data = ffs[0].attr("next_state").strip('"() ')
    pins = {p.args for p in cell.sub("pin")}
    if clock not in pins or data not in pins or ffs[0].attr("clear") or \
            ffs[0].attr("preset"):
        raise Refused("unsupported_cell",
                      f"{cell.args}: the bench drives a flip-flop with a single "
                      f"data pin, one clock and no set/reset; this one is "
                      f"next_state={data!r}, clocked_on={clocked!r}")
    if falling:
        raise Refused("unsupported_cell",
                      f"{cell.args}: falling-edge flip-flops are not checked")
    return clock, data, falling


def _ff_stim(bench, clock, data, data_edges, clock_ramp, t_edge, t_end):
    """Two rising clock edges: t_edge/2 loads the initial state, t_edge is the
    one measured. data_edges is the D waveform."""
    pre = t_edge / 2.0
    width = max(2.0, 2 * clock_ramp)
    return {clock: _ramp_edges(bench.vdd, [(pre, clock_ramp, True),
                                           (pre + width, clock_ramp, False),
                                           (t_edge, clock_ramp, True)], t_end),
            data: data_edges}


def _check_ff_arc(bench, cell, clock, data, pin, tables, rng, slews, loads,
                  n) -> list[dict]:
    t = bench.terms
    points = []
    for _ in range(n):
        i, j = rng.randrange(len(slews)), rng.randrange(len(loads))
        for out_rises in (True, False):
            def stim_for(ramp, out_rises=out_rises):
                t_edge = 12.0 + 4 * ramp
                d = _ramp_edges(bench.vdd, [(t_edge - 3.0, 0.01, out_rises)],
                                t_edge + 100)
                return _ff_stim(bench, clock, data, d, ramp, t_edge, t_edge + 100)
            ramp = ramp_of(slews[i], t)
            points += _check_edge(bench, stim_for, clock, pin.args, out_rises,
                                  slews, loads, tables, i, j, 12.0 + 4 * ramp,
                                  arc=f"{clock}->{pin.args}")
    return points


def _pulse_points(vdd, rest_high, first, second, ramp):
    """A pulse away from the rest rail: its leading ramp starts at `first`, its
    trailing ramp at `second`, both `ramp` long and at the same slope, so the
    width between the two threshold crossings is second-first (at a 50%
    threshold) whatever the shape. Narrower than its ramp it is a triangle whose
    edges meet short of the other rail, as a real slow edge would; one that
    would not leave the rest rail at all is no points."""
    rest, other = (vdd, 0.0) if rest_high else (0.0, vdd)
    if second - first >= ramp:
        return [(first, rest), (first + ramp, other), (second, other),
                (second + ramp, rest)]
    frac = (second - first + ramp) / (2.0 * ramp)
    if frac <= 0:
        return []
    return [(first, rest), ((first + second + ramp) / 2.0,
                            rest + (other - rest) * frac), (second + ramp, rest)]


def _pwl(points, start_level, end_level, end_ns):
    """PWL(...) from 0 to end_ns; times must keep increasing."""
    points = [(0.0, start_level)] + list(points) + [(end_ns, end_level)]
    return "PWL(" + " ".join(f"{t:.6g}n {v:g}" for t, v in points) + ")"


def _data_pulse(vdd, first, second, ramp, high, end_ns):
    """D leaves its rest level at `first` and heads back at `second`
    (_pulse_points). PWL times must keep increasing."""
    rest = 0.0 if high else vdd
    return _pwl(_pulse_points(vdd, not high, first, second, ramp), rest, rest,
                end_ns)


def _q_delay(bench, stim, q, new_high, t_clk, t_from, t_stop, step):
    """Clock threshold (t_clk) to Q's first threshold crossing after t_from, Q
    unloaded. None when Q never crossed, or ends anywhere but `new_high`: a
    glitch through the threshold that falls back is a wrong captured value."""
    edge = "RISE" if new_high else "FALL"
    m = bench.simulate(stim, q, 0.0, t_stop, step, [
        f".meas tran t_q WHEN v({q})="
        f"{bench.terms.output_threshold * bench.vdd:.6g} {edge}=1 "
        f"TD={t_from:.6g}n",
        f".meas tran v_end FIND v({q}) AT={t_stop - 0.5:.6g}n"])
    if "t_q" not in m or "v_end" not in m:
        return None
    if (m["v_end"] > bench.vdd / 2) != new_high:
        return None
    return m["t_q"] * 1e9 - t_clk


def _passes(delay, ref):
    """CRITERION: Q captured the value, no later than (1 + pushout_rel) x ref."""
    return delay is not None and delay <= (1 + CRITERION["pushout_rel"]) * ref


def _capture_delay(bench, clock, data, q, sampled_high, setup_ns, hold_ns,
                   clock_ramp, data_ramp):
    """The delay from the clock's threshold to Q's, with D at `sampled_high`
    only for the window setup_ns before to hold_ns after that threshold, both
    measured at D's threshold. None when Q never moved or ends at the wrong
    value (_q_delay)."""
    t = bench.terms
    t_edge = 12.0 + 4 * clock_ramp + 4 * data_ramp + 2 * abs(setup_ns)
    t_clk = t_edge + t.input_threshold * clock_ramp
    half = t.input_threshold * data_ramp
    d_first = t_clk - setup_ns - half
    d_second = t_clk + hold_ns - half
    t_stop = t_clk + max(hold_ns, 0.0) + clock_ramp + data_ramp + 12.0
    if d_second <= d_first:
        return None  # no pulse at all: D never carried the value
    d = _data_pulse(bench.vdd, d_first, d_second, data_ramp, sampled_high,
                    t_stop + 1)
    stim = _ff_stim(bench, clock, data, d, clock_ramp, t_edge, t_stop + 1)
    return _q_delay(bench, stim, q, sampled_high, t_clk, t_clk - clock_ramp,
                    t_stop, max(min(clock_ramp, data_ramp) / 50.0, 0.001))


def _bisect(passes, lo, hi, resolution, what):
    """The smallest x in [lo, hi] that passes, to `resolution`, for a check
    that fails at lo and passes at hi (anything else is a refusal: the bracket
    or the criterion is wrong, and a number from it would be made up)."""
    if not passes(hi):
        raise Refused("bench_failed", f"{what}: fails even at {hi:.4g} ns")
    if passes(lo):
        raise Refused("bench_failed", f"{what}: passes even at {lo:.4g} ns")
    while hi - lo > resolution:
        mid = (lo + hi) / 2.0
        if passes(mid):
            hi = mid
        else:
            lo = mid
    return hi


# Liberty's label -> the value D carries at the clock: a rising setup is D
# arriving high; a rising hold is D leaving low, so the sampled value was low.
SAMPLED_HIGH = {("setup", "rise"): True, ("setup", "fall"): False,
                ("hold", "rise"): False, ("hold", "fall"): True}


def _constraint_delay(bench, clock, data, q, kind, edge, dr, cr):
    """delay(v): clock-to-Q with `kind` (setup or hold) at v and the other side
    wide open; and the wide-open reference."""
    sampled_high = SAMPLED_HIGH[(kind, edge)]
    wide = 3.0 + dr + cr

    def delay(v):
        s, h = (v, wide) if kind == "setup" else (wide, v)
        return _capture_delay(bench, clock, data, q, sampled_high, s, h, cr, dr)
    ref = delay(wide)
    if ref is None:
        raise Refused("bench_failed",
                      f"{q} never moved with a {wide:g} ns data window")
    return delay, ref, wide


def measure_constraint(bench, clock, data, q, kind, edge, dr, cr) -> float:
    """One setup or hold value by bisection (issue #172): the shortest `kind`
    time, the other side wide open, at which CRITERION still holds. Each side
    is independent, as in the PDK's own lib; both at their minimum at once is
    not promised."""
    delay, ref, wide = _constraint_delay(bench, clock, data, q, kind, edge, dr, cr)
    return _bisect(lambda v: _passes(delay(v), ref), -(dr + cr) - 0.5, wide,
                   CRITERION["resolution_ns"],
                   f"{kind}_{edge} at data ramp {dr:.4g}, clock ramp {cr:.4g}")


def _pulse_delay(bench, clock, data, q, high, new_high, width, cr, dr):
    """Clock-to-Q of the edge that must load `new_high` over Q's old value,
    with a clock pulse `width` wide between its threshold crossings (a 50%
    input threshold: the width is the ramps' start-to-start time). `high`: the
    measured rising edge's own high phase is `width`. Not `high`: the low phase
    before the measured rising edge is `width`, D having changed while the
    clock was high."""
    t = bench.terms
    th = t.input_threshold
    vdd = bench.vdd
    old_v, new_v = (0.0, vdd) if new_high else (vdd, 0.0)
    pre = 4.0 + 2 * cr + dr          # loads the old value
    if high:
        t_edge = pre + 2 * cr + 6.0 + dr
        first = t_edge
        second = first + width - (1 - 2 * th) * cr
        clk = (_pulse_points(vdd, False, pre, pre + 2.0 + cr, cr) +
               _pulse_points(vdd, False, first, second, cr))
        t_clk = t_edge + th * cr
        d_change = t_edge - 3.0 - dr
        t_stop = t_clk + max(width, 0.0) + cr + 12.0
        clock_wave = _pwl(clk, 0.0, 0.0, t_stop + 1)
    else:
        first = pre + 2 * cr + 6.0 + dr
        second = first + width - (2 * th - 1) * cr
        clk = ([(pre, 0.0), (pre + cr, vdd)] +
               _pulse_points(vdd, True, first, second, cr))
        t_clk = second + th * cr
        d_change = pre + cr + 1.0
        t_stop = t_clk + cr + 12.0
        clock_wave = _pwl(clk, 0.0, vdd, t_stop + 1)
    d = _pwl([(d_change, old_v), (d_change + dr, new_v)], old_v, new_v,
             t_stop + 1)
    return _q_delay(bench, {clock: clock_wave, data: d}, q, new_high, t_clk,
                    first, t_stop, max(cr / 50.0, 0.001))


def measure_pulse_width(bench, clock, data, q, high, new_high, cr, dr) -> float:
    """min_pulse_width by bisection: the narrowest high (or low) clock phase at
    which the flop still loads `new_high` to CRITERION, against a wide pulse."""
    wide = 5.0 + cr

    def delay(w):
        return _pulse_delay(bench, clock, data, q, high, new_high, w, cr, dr)
    ref = delay(wide)
    if ref is None:
        raise Refused("bench_failed",
                      f"{q} never moved with a {wide:g} ns clock pulse")
    return _bisect(lambda w: _passes(delay(w), ref), 0.0, wide,
                   CRITERION["resolution_ns"],
                   f"min_pulse_width_{'high' if high else 'low'} at clock "
                   f"ramp {cr:.4g}")


def _jobs() -> int:
    return max(1, int(os.environ.get("CHARLIB_JOBS", "0") or 0)
               or min(8, os.cpu_count() or 1))


def _pool_map(bench, fn, tasks, jobs):
    """[fn(bench, task)...], each worker on its own Bench (its own deck
    directory). The parent bench counts every run."""
    if jobs <= 1 or len(tasks) <= 1:
        return [fn(bench, task) for task in tasks]
    free = queue.Queue()
    workers = []
    for k in range(min(jobs, len(tasks))):
        w = bench.work / f"w{k}"
        w.mkdir(parents=True, exist_ok=True)
        workers.append(dataclasses.replace(bench, work=w, runs=0))
        free.put(workers[-1])

    def run(task):
        b = free.get()
        try:
            return fn(b, task)
        finally:
            free.put(b)
    try:
        with ThreadPoolExecutor(len(workers)) as pool:
            return list(pool.map(run, tasks))
    finally:
        bench.runs += sum(b.runs for b in workers)


def measure_sequential(bench, cell, data_slews, clock_slews,
                       jobs: int | None = None) -> dict:
    """Setup, hold (every data x clock slew point) and min_pulse_width (every
    clock slew, both D values) by bisection on the bench."""
    clock, data, _ = _ff_terms(cell)
    q = next(p.args for p in cell.sub("pin") if p.attr("direction") == "output")
    t = bench.terms
    jobs = jobs or _jobs()
    cons = [(kind, edge, i, j) for (kind, edge) in SAMPLED_HIGH
            for i in range(len(data_slews)) for j in range(len(clock_slews))]

    def one_constraint(b, task):
        kind, edge, i, j = task
        return measure_constraint(b, clock, data, q, kind, edge,
                                  ramp_of(data_slews[i], t),
                                  ramp_of(clock_slews[j], t))
    dr0 = ramp_of(min(data_slews), t)
    pulses = [(high, new_high, j) for high in (True, False)
              for new_high in (True, False) for j in range(len(clock_slews))]

    def one_pulse(b, task):
        high, new_high, j = task
        return measure_pulse_width(b, clock, data, q, high, new_high,
                                   ramp_of(clock_slews[j], t), dr0)
    values = _pool_map(bench, one_constraint, cons, jobs)
    widths = _pool_map(bench, one_pulse, pulses, jobs)
    tables = {(kind, edge): [[None] * len(clock_slews) for _ in data_slews]
              for (kind, edge) in SAMPLED_HIGH}
    for (kind, edge, i, j), v in zip(cons, values):
        tables[(kind, edge)][i][j] = round(v, 4)
    mpw = {}
    for high in (True, False):
        name = f"min_pulse_width_{'high' if high else 'low'}"
        rows = [{"clock_slew": clock_slews[j], "d_high": new_high,
                 "width_ns": round(w, 4)}
                for (h, new_high, j), w in zip(pulses, widths) if h == high]
        worst = max(rows, key=lambda r: r["width_ns"])
        mpw[name] = {"value_ns": worst["width_ns"], "at": worst, "all": rows}
    return {"clock": clock, "data": data, "q": q, "criterion": CRITERION,
            "data_slews": list(data_slews), "clock_slews": list(clock_slews),
            "tables": {f"{k}_{e}": rows for (k, e), rows in tables.items()},
            "min_pulse_width": mpw}


def install_sequential(cell, measured) -> None:
    """The bisected tables over lctime's placeholders (the shim skips its own
    setup/hold), and min_pulse_width on the clock pin. A constraint table that
    is not there, or one too many, is a refusal: nothing of lctime's may
    survive under charlib's label."""
    data = next(p for p in cell.sub("pin") if p.args == measured["data"])
    done = set()
    for timing in data.sub("timing"):
        kind = timing.attr("timing_type").strip('"').split("_")[0]
        for g in timing.groups:
            key = f"{kind}_{g.name.split('_')[0]}"
            if not g.name.endswith("_constraint") or key not in measured["tables"]:
                continue
            if key in done:
                raise Refused("lctime_failed", f"{cell.args}: two {key} tables")
            g.set_attr("values", _quoted_rows(measured["tables"][key]), True)
            done.add(key)
    if done != set(measured["tables"]):
        raise Refused("lctime_failed",
                      f"{cell.args}/{measured['data']}: constraint tables "
                      f"{sorted(set(measured['tables']) - done)} are missing")
    clock = next(p for p in cell.sub("pin") if p.args == measured["clock"])
    for name, m in measured["min_pulse_width"].items():
        clock.set_attr(name, f"{m['value_ns']:.4g}")


def _constraint_tables(cell, data) -> dict:
    pin = next(p for p in cell.sub("pin") if p.args == data)
    tab = {}
    for timing in pin.sub("timing"):
        kind = timing.attr("timing_type").strip('"').split("_")[0]
        for g in timing.groups:
            tab[(kind, g.name.split("_")[0])] = table(g)
    if set(SAMPLED_HIGH) - tab.keys():
        raise Refused("lctime_failed",
                      f"{cell.args}/{data}: setup/hold tables are missing")
    return tab


def _margin(value, rel, abs_ns):
    return max(rel * abs(value), abs_ns)


def _check_constraints(bench, cell, clock, data, q, rng, data_slews, clock_slews,
                       n) -> list[dict]:
    """Two-sided, each side on its own (the other wide open, as measured): at
    table + margin the flop must meet CRITERION, at table - margin it must
    not. Optimistic and pessimistic tables both fail."""
    t = bench.terms
    c = CONSTRAINT
    tab = _constraint_tables(cell, data)
    points = []
    for _ in range(n):
        i, j = rng.randrange(len(data_slews)), rng.randrange(len(clock_slews))
        dr, cr = ramp_of(data_slews[i], t), ramp_of(clock_slews[j], t)
        for kind, edge in SAMPLED_HIGH:
            v = tab[(kind, edge)][i][j]
            delay, ref, _ = _constraint_delay(bench, clock, data, q, kind, edge,
                                              dr, cr)
            m = _margin(v, c["margin_rel"], c["margin_abs"])
            where = dict(table=f"{kind}_{edge}", value_ns=v, margin_ns=m, i=i,
                         j=j, data_slew=data_slews[i], clock_slew=clock_slews[j],
                         reference_ns=ref)
            longer, shorter = delay(v + m), delay(v - m)
            points += [
                {"check": "not_optimistic", "delay_ns": longer,
                 "ok": _passes(longer, ref), **where},
                {"check": "not_pessimistic", "delay_ns": shorter,
                 "ok": not _passes(shorter, ref), **where}]
    return points


def _check_pulse_widths(bench, cell, clock, data, q, data_slews,
                        clock_slews) -> list[dict]:
    """min_pulse_width, both sides: + margin meets CRITERION at every clock
    slew and D value; - margin fails at one of them at least (the value is
    their worst)."""
    t = bench.terms
    c = CONSTRAINT
    pin = next(p for p in cell.sub("pin") if p.args == clock)
    dr0 = ramp_of(min(data_slews), t)
    points = []
    for high in (True, False):
        name = f"min_pulse_width_{'high' if high else 'low'}"
        if pin.attr(name) is None:
            points.append({"table": name, "check": "present", "ok": False})
            continue
        v = float(pin.attr(name))
        m = _margin(v, c["margin_rel"], c["margin_abs"])
        longer_ok, shorter_fails = True, False
        for j, slew in enumerate(clock_slews):
            cr = ramp_of(slew, t)
            for new_high in (True, False):
                def delay(w, cr=cr, new_high=new_high):
                    return _pulse_delay(bench, clock, data, q, high, new_high,
                                        w, cr, dr0)
                ref = delay(5.0 + cr)
                longer_ok = longer_ok and _passes(delay(v + m), ref)
                shorter_fails = shorter_fails or not _passes(delay(v - m), ref)
        where = dict(table=name, value_ns=v, margin_ns=m)
        points += [{"check": "not_optimistic", "ok": longer_ok, **where},
                   {"check": "not_pessimistic", "ok": shorter_fails, **where}]
    return points


# --- internal power, leakage and limits (issue #173), on the bench

def _supplies(bench) -> list[str]:
    return [p for p, k in bench.pg.items() if k in ("primary_power", "nwell")]


def _window_measures(bench, name, t_from, t_to, node=None) -> list[str]:
    """The charge each supply delivered over [t_from, t_to], and `node` at
    both ends (so the load's share is read off where the output really was,
    settled or not)."""
    ms = [f".meas tran {name}_q_{p} INTEG i(V_{p}) FROM={t_from:.6g}n "
          f"TO={t_to:.6g}n" for p in _supplies(bench)]
    if node:
        ms += [f".meas tran {name}_v0 FIND v({node}) AT={t_from:.6g}n",
               f".meas tran {name}_v1 FIND v({node}) AT={t_to:.6g}n"]
    return ms


def _measured(m, keys, what):
    missing = [k for k in keys if k not in m]
    if missing:
        raise Refused("bench_failed", f"{what}: no {missing} from ngspice")


def _supply_energy(bench, m, name) -> float:
    """pJ out of the supplies (a source's current is into its + node)."""
    keys = [f"{name}_q_{p.lower()}" for p in _supplies(bench)]
    _measured(m, keys, name)
    return -bench.vdd * sum(m[k] for k in keys) * 1e12


def _load_energy(vdd, load_pf, v0, v1) -> float:
    """pJ the supply put into the load while the output went v0 -> v1: C*V*dV
    on a rise (half of it stored, half burnt in the pull-up), none on a fall,
    where the load empties into ground."""
    return vdd * load_pf * max(0.0, v1 - v0)


def _settle(tables, out_rises, i, j, ramp) -> float:
    delay, trans = (("cell_rise", "rise_transition") if out_rises
                    else ("cell_fall", "fall_transition"))
    return max(abs(tables[delay][i][j]), tables[trans][i][j], ramp, 0.05)


def _arc_energy(bench, stim, out_pin, load, t_on, ramp, span,
                check=False) -> float:
    """Internal energy (pJ) of one output edge: the supplies' energy from just
    before the input's edge at t_on until well after the output settled, less
    the load's. The self-check (check=True) reads the load's charge through a
    0 V probe instead of _load_energy, over a longer window, at a finer step."""
    k, div = (20, 150.0) if check else (12, 100.0)
    t_from, t_to = t_on - 0.05, t_on + ramp + k * span
    step = max(min(ramp, span) / div, 0.0005)
    ms = _window_measures(bench, "e", t_from, t_to, None if check else out_pin)
    if check:
        ms.append(f".meas tran q_load INTEG i(V_probe) FROM={t_from:.6g}n "
                  f"TO={t_to:.6g}n")
    # run past the window: ngspice may not FIND at exactly its stop time
    m = bench.simulate(stim, out_pin, load, t_to + 20 * step, step, ms,
                       probe=check)
    e = _supply_energy(bench, m, "e")
    if check:
        _measured(m, ["q_load"], "load probe")
        return e - bench.vdd * max(0.0, m["q_load"]) * 1e12
    _measured(m, ["e_v0", "e_v1"], out_pin)
    return e - _load_energy(bench.vdd, load, m["e_v0"], m["e_v1"])


def _windows_energy(bench, stim, quiet_pin, load, windows, step) -> list[float]:
    """Supply energy (pJ) in each [t_from, t_to] window of one run whose output
    does not switch: nothing goes into the load."""
    ms = []
    for k, (a, b) in enumerate(windows):
        ms += _window_measures(bench, f"w{k}", a, b)
    m = bench.simulate(stim, quiet_pin, load, windows[-1][1] + 20 * step,
                       step, ms)
    return [_supply_energy(bench, m, f"w{k}") for k in range(len(windows))]


def when_of(side: dict) -> str:
    """Liberty's `when` for a side-input state, "" for none."""
    return "&".join(p if v else f"!{p}" for p, v in side.items())


def _side_states(fn, related, others):
    """(side, toggles): every state of the other inputs, and whether `related`
    switches the output in it."""
    for combo in itertools.product((False, True), repeat=len(others)):
        side = dict(zip(others, combo))
        yield side, (eval_function(fn, {**side, related: False}) !=
                     eval_function(fn, {**side, related: True}))


def _gap(ramp) -> float:
    """Time between two edges of a hidden-power run: the edge and settling."""
    return 2.0 * ramp + 3.0


def _comb_arc_task(cell):
    fn_of = {p.args: p.attr("function") for p in cell.sub("pin")}
    inputs = [p.args for p in cell.sub("pin") if p.attr("direction") == "input"]
    tables = {}
    for pin in cell.sub("pin"):
        for timing in pin.sub("timing"):
            tables[(pin.args, timing.attr("related_pin").strip('"'))] = \
                _tables_of(timing)

    def stim_and_span(b, out, related, side, in_rises, i, j, slews):
        ramp = ramp_of(slews[i], b.terms)
        out_rises = eval_function(fn_of[out], {**side, related: in_rises})
        if (out, related) not in tables:
            raise Refused("lctime_failed",
                          f"{cell.args}: no {related}->{out} timing to size "
                          f"the power window")
        span = _settle(tables[(out, related)], out_rises, i, j, ramp)
        t0 = 1.0
        stim = {p: f"{b.vdd if v else 0:g}" for p, v in side.items()}
        stim[related] = _ramp_edges(b.vdd, [(t0, ramp, in_rises)],
                                    t0 + ramp + 1)
        return stim, ramp, span, t0, out_rises
    return inputs, stim_and_span


def measure_power(bench, cell, slews, loads, jobs: int | None = None) -> dict:
    """Internal power of every arc and hidden power of every input on the
    bench (_arc_energy), and leakage (measure_leakage). See install_power for
    where each lands in the .lib."""
    jobs = jobs or _jobs()
    if cell.sub("ff"):
        out = _measure_ff_power(bench, cell, slews, loads, jobs)
    else:
        out = _measure_comb_power(bench, cell, slews, loads, jobs)
    out["leakage"] = measure_leakage(bench, cell, slews, jobs)
    out.update(slews=list(slews), loads=list(loads), units="pJ, uW",
               leakage_options=LEAKAGE_OPTIONS)
    return out


def _measure_comb_power(bench, cell, slews, loads, jobs) -> dict:
    inputs, stim_and_span = _comb_arc_task(cell)
    arcs, hidden = [], []
    for pin in cell.sub("pin"):
        if pin.attr("direction") != "output":
            continue
        fn = pin.attr("function")
        for related in inputs:
            others = [p for p in inputs if p != related]
            for side, toggles in _side_states(fn, related, others):
                if toggles:
                    arcs.append((pin.args, related, side))
    outs = [p.attr("function") for p in cell.sub("pin")
            if p.attr("direction") == "output"]
    for related in inputs:
        others = [p for p in inputs if p != related]
        for combo in itertools.product((False, True), repeat=len(others)):
            side = dict(zip(others, combo))
            if not any(eval_function(fn, {**side, related: False}) !=
                       eval_function(fn, {**side, related: True})
                       for fn in outs):
                hidden.append((related, side))
    tasks = [(a, in_rises, i, j) for a in range(len(arcs))
             for in_rises in (True, False) for i in range(len(slews))
             for j in range(len(loads))]

    def one_arc(b, task):
        a, in_rises, i, j = task
        out, related, side = arcs[a]
        stim, ramp, span, t0, out_rises = stim_and_span(
            b, out, related, side, in_rises, i, j, slews)
        return out_rises, _arc_energy(b, stim, out, loads[j], t0, ramp, span)
    results = _pool_map(bench, one_arc, tasks, jobs)
    groups = []
    for a, (out, related, side) in enumerate(arcs):
        rows = {True: [[None] * len(loads) for _ in slews],
                False: [[None] * len(loads) for _ in slews]}
        for (k, _, i, j), (out_rises, e) in zip(tasks, results):
            if k == a:
                rows[out_rises][i][j] = round(e, 6)
        groups.append({"pin": out, "related_pin": related,
                       "when": when_of(side), "rise": rows[True],
                       "fall": rows[False]})
    out_pin = next(p.args for p in cell.sub("pin")
                   if p.attr("direction") == "output")
    htasks = [(h, i) for h in range(len(hidden)) for i in range(len(slews))]

    def one_hidden(b, task):
        h, i = task
        related, side = hidden[h]
        ramp = ramp_of(slews[i], b.terms)
        g, t0 = _gap(ramp), 1.0
        stim = {p: f"{b.vdd if v else 0:g}" for p, v in side.items()}
        stim[related] = _ramp_edges(b.vdd, [(t0, ramp, True),
                                            (t0 + g, ramp, False)], t0 + 2 * g)
        rise, fall = _windows_energy(
            b, stim, out_pin, loads[0],
            [(t0 - 0.05, t0 + g - 0.05), (t0 + g - 0.05, t0 + 2 * g)],
            max(ramp / 100.0, 0.0005))
        return rise, fall
    hres = _pool_map(bench, one_hidden, htasks, jobs)
    inputs_out = []
    for h, (related, side) in enumerate(hidden):
        rise = [round(r[0], 6) for (k, _), r in zip(htasks, hres) if k == h]
        fall = [round(r[1], 6) for (k, _), r in zip(htasks, hres) if k == h]
        inputs_out.append({"pin": related, "when": when_of(side),
                           "rise": rise, "fall": fall})
    return {"arcs": groups, "hidden": inputs_out}


def _ff_power_stim(bench, clock, data, clock_edges, data_edges, d_rest, end):
    """A flop's stimulus from explicit edges; D sits at d_rest when it has
    none."""
    stim = {clock: _ramp_edges(bench.vdd, clock_edges, end)}
    stim[data] = (_ramp_edges(bench.vdd, data_edges, end) if data_edges
                  else f"{bench.vdd if d_rest else 0:g}")
    return stim


def _ff_arc_stim(bench, clock, data, q_rises, ramp, dr0):
    """Q's old value loaded by a first clock pulse, D moved to the new value
    while the clock is low, then the measured rising edge at t_edge."""
    g = _gap(ramp)
    t_edge = 1.0 + 3 * g
    clk = [(1.0, ramp, True), (1.0 + g, ramp, False), (t_edge, ramp, True)]
    d = [(1.0 + 2 * g, dr0, q_rises)]
    return _ff_power_stim(bench, clock, data, clk, d, None, t_edge + 400), t_edge


def _measure_ff_power(bench, cell, slews, loads, jobs) -> dict:
    """Conventions, stated: the Q arc (related_pin CLK) is the rising clock
    edge that switches Q, less the load's energy and less CLK's hidden rise
    power at that slew with D at Q's new value: a power tool adds the hidden
    power on every clock edge, so the arc holds only the extra for Q switching
    (as the shipped lib does). CLK's hidden power is a
    clock edge that leaves Q alone, when D (= Q) is held at each value; D's is
    D switching while the clock is held low or high, the worst of Q stored 0
    and 1."""
    clock, data, _ = _ff_terms(cell)
    q_pin = next(p for p in cell.sub("pin") if p.attr("direction") == "output")
    q = q_pin.args
    timing = next((tg for tg in q_pin.sub("timing")
                   if tg.attr("related_pin").strip('"') == clock), None)
    if timing is None:
        raise Refused("lctime_failed", f"{cell.args}: no {clock}->{q} timing")
    tables = _tables_of(timing)
    t = bench.terms
    dr0 = ramp_of(min(slews), t)
    tasks = [(q_rises, i, j) for q_rises in (True, False)
             for i in range(len(slews)) for j in range(len(loads))]

    def one_arc(b, task):
        q_rises, i, j = task
        ramp = ramp_of(slews[i], t)
        stim, t_edge = _ff_arc_stim(b, clock, data, q_rises, ramp, dr0)
        span = _settle(tables, q_rises, i, j, ramp)
        return _arc_energy(b, stim, q, loads[j], t_edge, ramp, span)
    res = _pool_map(bench, one_arc, tasks, jobs)
    rows = {True: [[None] * len(loads) for _ in slews],
            False: [[None] * len(loads) for _ in slews]}
    for (q_rises, i, j), e in zip(tasks, res):
        rows[q_rises][i][j] = round(e, 6)
    arcs = [{"pin": q, "related_pin": clock, "when": "", "rise": rows[True],
             "fall": rows[False]}]

    def one_clock(b, task):
        d_high, i = task
        ramp = ramp_of(slews[i], t)
        g = _gap(ramp)
        clk = [(1.0, ramp, True), (1.0 + g, ramp, False),
               (1.0 + 2 * g, ramp, True), (1.0 + 3 * g, ramp, False)]
        stim = _ff_power_stim(b, clock, data, clk, None, d_high, 1.0 + 4 * g)
        return _windows_energy(b, stim, q, loads[0],
                               [(1.0 + 2 * g - 0.05, 1.0 + 3 * g - 0.05),
                                (1.0 + 3 * g - 0.05, 1.0 + 4 * g)],
                               max(ramp / 100.0, 0.0005))

    def one_data(b, task):
        clk_high, q_high, i = task
        ramp = ramp_of(slews[i], t)
        g = max(_gap(ramp), _gap(dr0))
        clk = [(1.0, dr0, True)] + ([] if clk_high else [(1.0 + g, dr0, False)])
        # D loads q_high at the first edge, then rises and falls (or falls
        # and rises) with the clock held
        d = [(1.0 + 2 * g, ramp, not q_high), (1.0 + 3 * g, ramp, q_high)]
        stim = _ff_power_stim(b, clock, data, clk, d, None, 1.0 + 4 * g)
        first, second = _windows_energy(
            b, stim, q, loads[0], [(1.0 + 2 * g - 0.05, 1.0 + 3 * g - 0.05),
                                   (1.0 + 3 * g - 0.05, 1.0 + 4 * g)],
            max(ramp / 100.0, 0.0005))
        return (second, first) if q_high else (first, second)  # (rise, fall)
    ctasks = [(d_high, i) for d_high in (False, True) for i in range(len(slews))]
    dtasks = [(clk_high, q_high, i) for clk_high in (False, True)
              for q_high in (False, True) for i in range(len(slews))]
    cres = _pool_map(bench, one_clock, ctasks, jobs)
    dres = _pool_map(bench, one_data, dtasks, jobs)
    hidden = []
    for d_high in (False, True):
        got = [r for (d, _), r in zip(ctasks, cres) if d == d_high]
        hidden.append({"pin": clock, "when": data if d_high else f"!{data}",
                       "rise": [round(r[0], 6) for r in got],
                       "fall": [round(r[1], 6) for r in got]})
    for clk_high in (False, True):
        by_q = [[r for (c, qh, _), r in zip(dtasks, dres)
                 if c == clk_high and qh == q_high] for q_high in (False, True)]
        hidden.append({"pin": data, "when": clock if clk_high else f"!{clock}",
                       "rise": [round(max(a[0], b[0]), 6)
                                for a, b in zip(*by_q)],
                       "fall": [round(max(a[1], b[1]), 6)
                                for a, b in zip(*by_q)]})
    for q_rises in (True, False):
        clock_rise = hidden[1 if q_rises else 0]["rise"]
        for i, row in enumerate(rows[q_rises]):
            row[:] = [round(e - clock_rise[i], 6) for e in row]
    return {"arcs": arcs, "hidden": hidden}


def _clock_hidden_rise(cell, clock, data, d_high, i) -> float:
    """CLK's tabulated hidden rise power at slew index `i`, D held at d_high."""
    want = data if d_high else f"!{data}"
    pin = next(p for p in cell.sub("pin") if p.args == clock)
    ip = next(ip for ip in pin.sub("internal_power")
              if (ip.attr("when") or "").strip('"') == want)
    return table(next(g for g in ip.groups if g.name == "rise_power"))[0][i]


def _leak_uw(bench, m) -> float:
    keys = [f"i_{p.lower()}" for p in _supplies(bench)]
    _measured(m, keys, "leakage")
    return -bench.vdd * sum(m[k] for k in keys) * 1e6


def _internal_nodes(netlist: Path, cell: str, ports) -> list[str]:
    """The plain-named nets inside `cell`'s subckt that are not ports (an
    extracted netlist's resistor taps, `VSS.t4`, are left to the solver)."""
    lines, inside = [], False
    for raw in netlist.read_text(errors="replace").splitlines():
        low = raw.strip().lower()
        if low.startswith(".subckt"):
            inside = low.split()[1:2] == [cell.lower()]
            continue
        if low.startswith(".ends"):
            inside = False
        elif inside and low.startswith("+") and lines:
            lines[-1] += " " + raw.strip()[1:]
        elif inside and low and low[0] in "xm":
            lines.append(raw.strip())
    skip = {p.lower() for p in ports} | {"0"}
    out = []
    for line in lines:
        toks = [t for t in line.split()[1:] if "=" not in t][:-1]
        for t in toks:
            if "." not in t and t.lower() not in skip and t not in out:
                out.append(t)
    return out


def measure_leakage(bench, cell, slews, jobs: int | None = None) -> dict:
    """Leakage (uW) out of the supplies in every input state, at
    LEAKAGE_OPTIONS. A gate is one DC run per state; a flip-flop is held in
    each (clock, D) state, the worst of Q stored 0 and 1: a transient clocks
    the state in and a DC run starts from its node voltages. The cell's value
    is the worst state."""
    inputs = [p.args for p in cell.sub("pin") if p.attr("direction") == "input"]
    out_pin = next(p.args for p in cell.sub("pin")
                   if p.attr("direction") == "output")
    jobs = jobs or _jobs()
    states = [dict(zip(inputs, c))
              for c in itertools.product((False, True), repeat=len(inputs))]
    sup = _supplies(bench)
    if cell.sub("ff"):
        clock, data, _ = _ff_terms(cell)
        if set(inputs) != {clock, data}:
            raise Refused("unsupported_cell",
                          f"{cell.args}: leakage is held for CLK and D only")
        r = ramp_of(min(slews), bench.terms)
        g = _gap(r)
        t_end = 1.0 + 3 * g + 10.0
        nodes = _internal_nodes(bench.netlist, bench.cell, bench.ports)

        def one(b, task):
            state, q_high = task
            clk = [(1.0, r, True)] + ([] if state[clock]
                                      else [(1.0 + g, r, False)])
            d = ([(1.0 + 2 * g, r, state[data])]
                 if state[data] != q_high else None)
            stim = _ff_power_stim(b, clock, data, clk, d, q_high, t_end)
            # a held transient cannot read a current this small (its own
            # error is ~1e-9 A), and a DC run alone may pick either stored
            # state: settle by transient, then start the DC there
            m = b.simulate(stim, out_pin, 0.0, t_end + 1.0, max(r / 50.0, 0.001),
                           [f".meas tran n{k} FIND v(xdut.{n}) AT={t_end:.6g}n"
                            for k, n in enumerate(nodes)])
            _measured(m, [f"n{k}" for k in range(len(nodes))], "leakage")
            held = {clock: f"{b.vdd if state[clock] else 0:g}",
                    data: f"{b.vdd if state[data] else 0:g}"}
            nodeset = " ".join(f"v(xdut.{n})={m[f'n{k}']:.6g}"
                               for k, n in enumerate(nodes))
            m = b.simulate(held, out_pin, 0.0, 0, 0, [
                f".meas dc i_{p} FIND i(V_{p}) AT=0" for p in sup] +
                [f".meas dc q FIND v({out_pin}) AT=0"],
                options=LEAKAGE_OPTIONS,
                analysis=(f".nodeset {nodeset}\n" if nodes else "") +
                         "V_charlib_dc charlib_dc 0 0\n"
                         ".dc V_charlib_dc 0 1 1")
            _measured(m, ["q"], "leakage")
            if (m["q"] > b.vdd / 2) != q_high:
                raise Refused("bench_failed",
                              f"{cell.args}: the DC run lost the stored Q "
                              f"({when_of(state)}, Q={int(q_high)})")
            return _leak_uw(b, m)
        tasks = [(s, qh) for s in states for qh in (False, True)]
        res = _pool_map(bench, one, tasks, jobs)
        values = [max(res[2 * k], res[2 * k + 1]) for k in range(len(states))]
    else:
        def one(b, state):
            stim = {p: f"{b.vdd if v else 0:g}" for p, v in state.items()}
            m = b.simulate(stim, out_pin, 0.0, 0, 0, [
                f".meas dc i_{p} FIND i(V_{p}) AT=0" for p in sup],
                options=LEAKAGE_OPTIONS,
                analysis="V_charlib_dc charlib_dc 0 0\n"
                         ".dc V_charlib_dc 0 1 1")
            return _leak_uw(b, m)
        values = _pool_map(bench, one, states, jobs)
    rows = [{"when": when_of(s), "value_uw": float(f"{v:.6g}")}
            for s, v in zip(states, values)]
    return {"states": rows, "cell_uw": max(r["value_uw"] for r in rows)}


def _power_table(name, template, slews, loads, rows) -> Group:
    g = Group(name, template)
    g.attrs = [("index_1", _quoted_list(slews), True)]
    if loads is not None:
        g.attrs.append(("index_2", _quoted_list(loads), True))
        g.attrs.append(("values", _quoted_rows(rows), True))
    else:
        g.attrs.append(("values", _quoted_rows([rows]), True))
    return g


def install_power(lib: Group, cell: Group, power: dict) -> None:
    """internal_power on the outputs (related_pin, and `when` for the side
    inputs' state) and on the inputs for the edges that leave every output
    alone (`when`, no related_pin), leakage_power per input state and
    cell_leakage_power (the worst state), and their templates."""
    slews, loads = power["slews"], power["loads"]
    two, one = power_template(len(slews), len(loads)), power_template(len(slews))
    pins = {p.args: p for p in cell.sub("pin")}
    for arc in power["arcs"]:
        g = Group("internal_power")
        g.attrs = [("related_pin", f'"{arc["related_pin"]}"', False)]
        if arc["when"]:
            g.attrs.append(("when", f'"{arc["when"]}"', False))
        g.groups = [_power_table("rise_power", two, slews, loads, arc["rise"]),
                    _power_table("fall_power", two, slews, loads, arc["fall"])]
        pins[arc["pin"]].groups.append(g)
    for h in power["hidden"]:
        g = Group("internal_power")
        if h["when"]:
            g.attrs.append(("when", f'"{h["when"]}"', False))
        g.groups = [_power_table("rise_power", one, slews, None, h["rise"]),
                    _power_table("fall_power", one, slews, None, h["fall"])]
        pins[h["pin"]].groups.append(g)
    leak = power["leakage"]
    cell.set_attr("cell_leakage_power", f"{leak['cell_uw']:.6g}")
    primary = next((g.args for g in cell.sub("pg_pin")
                    if g.attr("pg_type") == "primary_power"), None)
    groups = []
    for state in leak["states"]:
        g = Group("leakage_power")
        if primary:
            g.attrs.append(("related_pg_pin", primary, False))
        if state["when"]:
            g.attrs.append(("when", f'"{state["when"]}"', False))
        g.attrs.append(("value", f'"{state["value_uw"]:.6g}"', False))
        groups.append(g)
    first_pin = next(k for k, g in enumerate(cell.groups) if g.name == "pin")
    cell.groups[first_pin:first_pin] = groups
    have = {(g.name, g.args) for g in lib.groups}
    lib.groups[-1:-1] = [g for g in _power_template_groups(len(slews), len(loads))
                         if (g.name, g.args) not in have]


def install_limits(cell: Group, slews, loads) -> dict:
    """max_transition on every pin is the largest characterised slew and
    max_capacitance on every output the largest characterised load: the
    tables are not extrapolated, the rule the PDK's own lib follows (its 8.6 ns
    and its largest load index)."""
    limits = {"max_transition": max(slews), "max_capacitance": max(loads)}
    for pin in cell.sub("pin"):
        pin.set_attr("max_transition", f"{limits['max_transition']:.6g}")
        if pin.attr("direction") == "output":
            pin.set_attr("max_capacitance", f"{limits['max_capacitance']:.6g}")
    return limits


def _check_power(bench, cell, rng, slews, loads, n) -> list[dict]:
    """`n` random points of every output power table, re-measured with the load
    read through a probe (not _load_energy) over a longer window. A table off
    by more than POWER_TOLERANCE is a finding."""
    t = bench.terms
    points = []
    ff = bool(cell.sub("ff"))
    if ff:
        clock, data, _ = _ff_terms(cell)
        dr0 = ramp_of(min(slews), t)
    else:
        _, stim_and_span = _comb_arc_task(cell)
    fn_of = {p.args: p.attr("function") for p in cell.sub("pin")}
    for pin in cell.sub("pin"):
        timing = {tg.attr("related_pin").strip('"'): _tables_of(tg)
                  for tg in pin.sub("timing")}
        for ip in pin.sub("internal_power"):
            related = ip.attr("related_pin")
            if related is None:
                continue
            related = related.strip('"')
            when = (ip.attr("when") or '""').strip('"')
            side = {}
            for term in filter(None, when.split("&")):
                side[term.lstrip("!")] = not term.startswith("!")
            tabs = {g.name: table(g) for g in ip.groups}
            for _ in range(n):
                i, j = rng.randrange(len(slews)), rng.randrange(len(loads))
                for out_rises in (True, False):
                    ramp = ramp_of(slews[i], t)
                    if ff:
                        stim, t_on = _ff_arc_stim(bench, clock, data, out_rises,
                                                  ramp, dr0)
                        span = _settle(timing[related], out_rises, i, j, ramp)
                    else:
                        in_rises = eval_function(fn_of[pin.args],
                                                 {**side, related: True}) \
                            == out_rises
                        stim, ramp, span, t_on, _ = stim_and_span(
                            bench, pin.args, related, side, in_rises, i, j,
                            slews)
                    e = _arc_energy(bench, stim, pin.args, loads[j], t_on, ramp,
                                    span, check=True)
                    if ff:
                        e -= _clock_hidden_rise(cell, clock, data, out_rises, i)
                    name = "rise_power" if out_rises else "fall_power"
                    v = tabs[name][i][j]
                    ok = abs(v - e) <= max(POWER_TOLERANCE["rel"] * abs(e),
                                           POWER_TOLERANCE["abs_pj"])
                    points.append({"table": name, "value_pj": v, "bench_pj": e,
                                   "ok": ok, "i": i, "j": j, "slew": slews[i],
                                   "load": loads[j],
                                   "arc": f"{related}->{pin.args}",
                                   "when": when})
    return points


# --- one corner, many cells

_MERGE_SKIP = ("library",)


def merge_libraries(texts: list[str], name: str) -> str:
    """One .lib of several cells at one corner. Every header attribute and
    group (units, thresholds, voltages, operating conditions) must be the
    same in all, else Refused("corner_mismatch"); templates are kept once by
    name, and two of one name that differ are refused; so is a cell twice."""
    if not texts:
        raise Refused("nothing_to_merge", "no libraries given")
    libs = [parse_liberty(t) for t in texts]
    tmpl_kinds = ("lu_table_template", "power_lut_template")

    def head(lib):
        return ([a for a in lib.attrs],
                [write_liberty(g) for g in lib.groups
                 if g.name not in tmpl_kinds and g.name != "cell"])
    ref = head(libs[0])
    out = Group(libs[0].name, name, list(libs[0].attrs),
                [g for g in libs[0].groups
                 if g.name not in tmpl_kinds and g.name != "cell"])
    templates, cells = {}, {}
    for k, lib in enumerate(libs):
        if head(lib) != ref:
            a, b = ref, head(lib)
            diff = sorted({str(x) for x in a[0]} ^ {str(x) for x in b[0]})
            raise Refused("corner_mismatch",
                          f"library {k} ({lib.args}) has another header than "
                          f"library 0 ({libs[0].args}): {diff[:4] or 'groups'}")
        for g in lib.groups:
            if g.name in tmpl_kinds:
                key = (g.name, g.args)
                text = write_liberty(g)
                if templates.setdefault(key, (g, text))[1] != text:
                    raise Refused("template_conflict",
                                  f"{g.name}({g.args}) differs between "
                                  f"libraries")
            elif g.name == "cell":
                if g.args in cells:
                    raise Refused("duplicate_cell",
                                  f"cell {g.args} is in two libraries")
                cells[g.args] = g
    out.groups += [g for g, _ in templates.values()] + list(cells.values())
    return write_liberty(out) + "\n"


def make_bench(cell: Group, *, netlist: Path, pdk: Path, corner: dict,
               vdd: float, terms: Terms, work: Path) -> Bench:
    work.mkdir(parents=True, exist_ok=True)
    text = netlist.read_text(encoding="utf-8", errors="replace")
    pg = {g.args: g.attr("pg_type") for g in cell.sub("pg_pin")}
    bench = Bench(cell.args, netlist, ports_of(text, cell.args), pg, pdk, corner,
                  vdd, work, terms)
    missing = (set(pg) | {p.args for p in cell.sub("pin")}) - set(bench.ports)
    if missing:
        raise Refused("pins_missing",
                      f"{cell.args}: netlist ports {bench.ports} lack "
                      f"{sorted(missing)}; an extraction must label them")
    return bench


def validate(cell: Group, *, netlist: Path, pdk: Path, corner: dict, vdd: float,
             terms: Terms, slews, loads, data_slews=None, clock_slews=None,
             work: Path, checks: int = DEFAULT_CHECKS, seed: int = 0) -> dict:
    """Re-simulate `checks` random points per arc per corner. Bench failures are
    refusals; an off table is a point with ok false."""
    bench = make_bench(cell, netlist=netlist, pdk=pdk, corner=corner, vdd=vdd,
                       terms=terms, work=work)
    rng = random.Random(seed)
    if cell.sub("ff"):
        clock, data, _ = _ff_terms(cell)
        points = []
        q = None
        for pin in cell.sub("pin"):
            if pin.attr("direction") != "output":
                continue
            q = q or pin.args
            for timing in pin.sub("timing"):
                points += _check_ff_arc(bench, cell, clock, data, pin,
                                        _tables_of(timing), rng, slews, loads,
                                        checks)
        points += _check_constraints(bench, cell, clock, data, q, rng, data_slews,
                                     clock_slews, checks)
        points += _check_pulse_widths(bench, cell, clock, data, q, data_slews,
                                      clock_slews)
    else:
        points = _check_combinational(bench, cell, rng, slews, loads, checks)
    if any(p.sub("internal_power") for p in cell.sub("pin")):
        points += _check_power(bench, cell, rng, slews, loads, checks)
    bad = [p for p in points if not p["ok"]]
    return {"points": len(points), "simulations": bench.runs,
            "failed": bad, "ok": not bad and bool(points)}


# --- comparing with a shipped lib, the known answer

def compare_shipped(cell: Group, shipped_text: str) -> list[dict]:
    """table/shipped at every shared point of every delay and transition table.
    Where the shipped lib has several `when` arcs, the worst of them (lctime's
    table is the worst too). Same index points required."""
    shipped = parse_liberty(shipped_text)
    out = []
    for pin in cell.sub("pin"):
        spin = next(p for p in shipped.sub("pin") if p.args == pin.args)
        for timing in pin.sub("timing"):
            related = timing.attr("related_pin").strip('"')
            for g in timing.groups:
                if g.name.endswith("_constraint"):
                    continue
                candidates = [
                    tab for st in spin.sub("timing")
                    if st.attr("related_pin").strip('"') == related
                    for tab in st.sub(g.name)]
                if not candidates:
                    continue
                for i, s in enumerate(numbers(g.attr("index_1"))):
                    for j, c in enumerate(numbers(g.attr("index_2"))):
                        best = max(_lookup(tab, s, c) for tab in candidates)
                        v = table(g)[i][j]
                        out.append({"table": g.name, "arc": f"{related}->{pin.args}",
                                    "slew": s, "load": c, "value": v,
                                    "shipped": best, "ratio": v / best})
    return out


def compare_constraints(cell: Group, shipped_text: str) -> list[dict]:
    """Setup/hold: table - shipped (ns) at every point; min_pulse_width:
    table/shipped. Same index points required."""
    shipped = parse_liberty(shipped_text)
    out = []
    for pin in cell.sub("pin"):
        spin = next(p for p in shipped.sub("pin") if p.args == pin.args)
        for name in ("min_pulse_width_high", "min_pulse_width_low"):
            v, ship = pin.attr(name), spin.attr(name)
            if v is not None and ship is not None:
                out.append({"table": name, "pin": pin.args, "value": float(v),
                            "shipped": float(ship),
                            "ratio": float(v) / float(ship)})
        for timing in pin.sub("timing"):
            kind = str(timing.attr("timing_type", "")).strip('"')
            for g in timing.groups:
                if not g.name.endswith("_constraint"):
                    continue
                cands = [tab for st in spin.sub("timing")
                         if str(st.attr("timing_type", "")).strip('"') == kind
                         for tab in st.sub(g.name)]
                if len(cands) != 1:
                    raise Refused("table_axes", f"{pin.args} {kind} {g.name}: "
                                  f"{len(cands)} shipped tables")
                for i, s in enumerate(numbers(g.attr("index_1"))):
                    for j, c in enumerate(numbers(g.attr("index_2"))):
                        v = table(g)[i][j]
                        ship = _lookup(cands[0], s, c)
                        out.append({"table": f"{kind.split('_')[0]}_"
                                    f"{g.name.split('_')[0]}",
                                    "pin": pin.args, "data_slew": s,
                                    "clock_slew": c, "value": v,
                                    "shipped": ship, "diff": v - ship})
    return out


def _lookup(tab: Group, s, c):
    i = _nearest(numbers(tab.attr("index_1")), s)
    j = _nearest(numbers(tab.attr("index_2")), c)
    return table(tab)[i][j]


def _nearest(values, x):
    best = min(range(len(values)), key=lambda k: abs(values[k] - x))
    if abs(values[best] - x) > 1e-3 * max(abs(x), 1e-9):
        raise Refused("table_axes", f"{x} is not an index point of {values}")
    return best


# --- the whole run

def pick(points, indices):
    """A subset of the template's own points, never a new one."""
    if indices is None:
        return list(points)
    try:
        return [points[i] for i in indices]
    except IndexError as exc:
        raise Refused("bad_index",
                      f"index {indices} is outside the template's "
                      f"{len(points)} points") from exc


def choose_netlist(extracted, schematic) -> tuple[Path, str]:
    for path, kind in ((extracted, "extracted"), (schematic, "schematic")):
        if path and Path(path).is_file() and Path(path).stat().st_size:
            return Path(path), kind
    raise Refused("no_netlist",
                  f"neither the extracted netlist ({extracted}) nor the "
                  f"schematic ({schematic}) exists")


def _write_verdict(out_dir: Path, library: str, ok: bool, text: str) -> Path:
    """One file per corner, this run's verdict: a rerun into the same directory
    must not leave an earlier pass's .lib beside its rejection, or the reverse."""
    good = out_dir / f"{library}.lib"
    rejected = out_dir / f"{library}.rejected.lib"
    path, other = (good, rejected) if ok else (rejected, good)
    other.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def characterize(cell: str, *, template_lib: Path, corners: list[dict],
                 nominal_vdd: float, out_dir: Path, pdk: Path, schematic=None,
                 extracted=None, cell_text: str | None = None,
                 reference_cell: str | None = None, slew_indices=None,
                 load_indices=None, constraint_indices=None,
                 checks: int = DEFAULT_CHECKS, seed: int = 0) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    netlist_src, netlist_kind = choose_netlist(extracted, schematic)
    text = Path(template_lib).read_text(encoding="utf-8", errors="replace")
    if cell_text is None:
        header_text, cell_text = split_library(text, cell)
        ref_text = cell_text
    else:
        header_text, ref_text = split_library(text, reference_cell or "")
    cell_group = parse_liberty(cell_text)
    bare = bare_cell(cell_group)
    pts = index_points(parse_liberty(ref_text))
    slews = pick(pts["slews"], slew_indices)
    loads = pick(pts["loads"], load_indices)
    sequential = bool(bare.sub("ff"))
    cslews = pick(pts["constraint"], constraint_indices) if sequential else None
    terms = terms_of(parse_liberty(header_text.rstrip() + "\n}\n"))
    report = {"cell": cell, "netlist": str(netlist_src),
              "netlist_kind": netlist_kind, "terms": terms.__dict__,
              "slews": slews, "loads": loads, "tolerance": TOLERANCE,
              "power_tolerance": POWER_TOLERANCE,
              "constraint_slews": cslews, "corners": {}}
    netlist_text = prepare_netlist(
        netlist_src.read_text(encoding="utf-8", errors="replace"), cell)
    ok = True
    for corner in corners:
        name = corner["name"]
        vdd = nominal_vdd * (1 + corner.get("supply_pct", 0) / 100.0)
        work = out_dir / f"{cell}__{name}.work"
        work.mkdir(parents=True, exist_ok=True)
        netlist = work / "netlist.sp"
        # lctime's own deck stays at 25 C; this file is the one it includes
        netlist.write_text(temperature_card(corner) + netlist_text,
                           encoding="utf-8")
        library = f"{cell}__{name}"
        lct = corner_header(header_text, library, vdd, corner["temp_c"], True,
                            name)
        lib_in = work / "in.lib"
        lct_lib = Group(lct.name, lct.args, lct.attrs, lct.groups + [bare])
        lib_in.write_text(write_liberty(lct_lib) + "\n", encoding="utf-8")
        ramps = [ramp_of(s, terms) for s in slews]
        cramps = [ramp_of(s, terms) for s in cslews] if sequential else None
        raw = work / "lctime.lib"
        run_lctime(cell, lib_in, netlist, raw, work, pdk=pdk, corner=corner,
                   ramps=ramps, loads=loads,
                   clock_ramps=cramps)
        head = corner_header(header_text, library, vdd, corner["temp_c"], False,
                             name)
        out_cell, out_lib = assemble_library(
            raw.read_text(encoding="utf-8", errors="replace"), head, terms,
            slews, loads, ramps, data_slews=slews if sequential else None,
            clock_slews=cslews)
        measured = None
        if sequential:
            bench = make_bench(out_cell, netlist=netlist, pdk=pdk,
                               corner=corner, vdd=vdd, terms=terms,
                               work=work / "measure")
            measured = measure_sequential(bench, out_cell, slews, cslews)
            measured["simulations"] = bench.runs
            install_sequential(out_cell, measured)
        power = measure_power(make_bench(out_cell, netlist=netlist, pdk=pdk,
                                         corner=corner, vdd=vdd, terms=terms,
                                         work=work / "power"),
                              out_cell, slews, loads)
        install_power(out_lib, out_cell, power)
        limits = install_limits(out_cell, slews, loads)
        lib_text = write_liberty(out_lib) + "\n"
        check = validate(out_cell, netlist=netlist, pdk=pdk, corner=corner,
                         vdd=vdd, terms=terms, slews=slews, loads=loads,
                         data_slews=slews if sequential else None,
                         clock_slews=cslews, work=work / "check", checks=checks,
                         seed=seed)
        banner = (f"/* charlib (lctime): {cell} at {name}, {vdd:g} V, "
                  f"{corner['temp_c']} C, process {corner['process']}, "
                  f"{netlist_kind} netlist. Timing, input capacitance"
                  f"{', setup/hold, min_pulse_width' if sequential else ''}"
                  f", internal power, leakage (gmin 1e-18) and limits"
                  f" (the largest characterised slew and load). */\n")
        path = _write_verdict(out_dir, library, check["ok"], banner + lib_text)
        report["corners"][name] = {"lib": str(path), "vdd": vdd, "check": check}
        if measured:
            report["corners"][name]["sequential"] = measured
        report["corners"][name].update(power=power, limits=limits)
        ok = ok and check["ok"]
    report["ok"] = ok
    (out_dir / f"{cell}.report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8")
    return report

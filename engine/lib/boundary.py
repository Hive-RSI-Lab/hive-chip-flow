"""boundary.py - the electrical contract at the analog/digital boundary (#110).

interface.yaml says what level each crossing signal runs at and what load it
sees; engine/reference/levels.yaml says what a level means. This module reads
both and measures the analog netlist against them: every a2d pin's swing and
10-90% edge into its declared load, and every d2a pin's switching point, at
the three STA corners.

`interface.yaml` version 2 is the typed form:

    version: 2
    signals:
      - {name: osc_out, direction: a2d, level: cmos_3v3, domain: clk_free,
         width: 1, load: {pin: gf180mcu_fd_sc_mcu7t5v0__dffq_1/CLK, count: 1}}
      - {name: osc_en, direction: d2a, level: cmos_3v3, domain: clk, width: 1,
         load: {cap_ff: 180, note: two NAND gates of 20 um2}}

`load` is exactly one of `cap_ff` (a number, femtofarads) or `pin` (a liberty
`<cell>/<pin>`, its capacitance read per corner), with an optional `count` of
such pins and a free-text `note` no check reads.

The stimulus is the analog spec's to state, in analog/spec/spec.yaml:

    boundary:
      static: {vbias: 1.2, vout: open}   # every other pin: a level, or open
      a2d:
        # a running output, started by an edge on `source`
        osc_out: {source: osc_en, settle_ns: 1500, window_ns: 2500, step_ns: 0.02}
        # or an output that follows a pulse on the input `from`
        q: {from: a, settle_ns: 5, window_ns: 100}
      d2a:
        # ramped once from vol to voh (`ramp: down` the other way) while
        # `observe`, a port or a net such as xdut.n0, is watched for vdd/2
        osc_en: {observe: xdut.n0, ramp_ns: 2000}

The netlist is the sized schematic top_harden delivers.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import charlib  # noqa: E402
import macrolib  # noqa: E402

Refused = macrolib.Refused

LEVELS_PATH = _LIB.parent / "reference" / "levels.yaml"
TYPED_VERSION = 2
# The two released blocks still on interface version 1, each with the reason.
# They stay on the old form, and do not owe the boundary gate, until they are
# measured and re-released; every other block's split refuses version 1.
V1_BLOCKS = {
    "pll": "released before the boundary gate; not yet measured at the boundary",
    "r2r_dac": "released before the boundary gate; not yet measured at the boundary",
}
OPEN = "open"
EDGE_LOW, EDGE_HIGH = 0.1, 0.9
DEFAULT_STEP_NS = 0.01
DEFAULT_SETTLE_NS = 5.0
DEFAULT_RAMP_NS = 1000.0
_ENABLE_AT_NS = 1.0
_NET_RE = re.compile(r"^[a-z_][a-z0-9_.\[\]]*$")
_NUMBER = r"([-+0-9.eE]+)"


def is_v1_block(ws: Path) -> bool:
    """A committed block named in V1_BLOCKS, under the name it is released as."""
    ws = Path(ws).resolve()
    return ws.parent.name == "blocks" and ws.name in V1_BLOCKS


def interface_version(data: dict) -> int | None:
    version = data.get("version")
    return version if isinstance(version, int) else None


def load_levels(path: Path = LEVELS_PATH) -> dict:
    import yaml
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data["levels"]


def load_problem(load) -> str | None:
    """Why a `load` value is not the typed form, or None when it is."""
    if not isinstance(load, dict):
        return (f"load {load!r} is not typed: it must be a mapping with "
                "cap_ff or pin")
    unknown = sorted(set(load) - {"cap_ff", "pin", "count", "note"})
    if unknown:
        return f"load has unknown fields {unknown}"
    if ("cap_ff" in load) == ("pin" in load):
        return "load must have exactly one of cap_ff and pin"
    if "cap_ff" in load:
        cap = load["cap_ff"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            return f"load cap_ff {cap!r} must be a positive number of femtofarads"
    else:
        pin = load["pin"]
        if not isinstance(pin, str) or pin.count("/") != 1 or not all(pin.split("/")):
            return f"load pin {pin!r} must read <liberty cell>/<pin>"
    count = load.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return f"load count {count!r} must be a whole number of at least 1"
    return None


def comparable_load(load):
    """The part of a load two specs must agree on: a note is prose."""
    if isinstance(load, dict):
        return {k: v for k, v in load.items() if k != "note"}
    return load


def load_text(load) -> str:
    """One line for a document: a v1 string, or the typed form in words."""
    if not isinstance(load, dict):
        return "" if load is None else str(load)
    what = f"{load['cap_ff']} fF" if "cap_ff" in load else str(load.get("pin"))
    count = load.get("count", 1)
    return f"{count} x {what}" if count != 1 else what


# ---- the liberty

def library_text(pdk, corner: str, library: str = macrolib.STA_LIBRARY) -> str:
    path = Path(pdk) / "libs.ref" / library / "lib" / f"{library}__{corner}.lib"
    if not path.is_file():
        raise Refused("boundary_incomplete", f"no liberty at {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _block_after(text: str, start: int, end_re: str) -> str:
    """From `start`, just past a group's head, up to the next head."""
    next_group = re.compile(end_re, re.M).search(text, start)
    return text[start:next_group.start() if next_group else len(text)]


def liberty_pin(text: str, cell: str, pin: str) -> dict:
    """{capacitance_pf, max_transition_ns} of one cell pin. The liberty is
    searched as text: parsing 20 MB per corner to read two numbers is minutes."""
    head = re.search(rf'^\s*cell\s*\(\s*"?{re.escape(cell)}"?\s*\)', text, re.M)
    if not head:
        raise Refused("boundary_incomplete", f"the liberty has no cell {cell}")
    body = _block_after(text, head.end(), r"^\s*cell\s*\(")
    start = re.search(rf'^\s*pin\s*\(\s*"?{re.escape(pin)}"?\s*\)', body, re.M)
    if not start:
        raise Refused("boundary_incomplete", f"cell {cell} has no pin {pin}")
    block = _block_after(body, start.end(), r"^\s*pin\s*\(")
    cap = re.search(rf"(?<!\w)capacitance\s*:\s*{_NUMBER}", block)
    limit = re.search(rf"(?<!\w)max_transition\s*:\s*{_NUMBER}", block)
    if not cap:
        raise Refused("boundary_incomplete",
                      f"{cell}/{pin} has no capacitance in the liberty: it is "
                      "not an input to load a signal with")
    return {"capacitance_pf": float(cap.group(1)),
            "max_transition_ns": float(limit.group(1)) if limit else None}


def resolve_load(load: dict, library: str) -> dict:
    """{pf, max_transition_ns}: the load in pF at this corner, and the limit
    the receiving pin itself sets (None for a plain capacitance)."""
    count = load.get("count", 1)
    if "cap_ff" in load:
        return {"pf": load["cap_ff"] * count / 1000.0, "max_transition_ns": None}
    cell, pin = load["pin"].split("/")
    found = liberty_pin(library, cell, pin)
    return {"pf": found["capacitance_pf"] * count,
            "max_transition_ns": found["max_transition_ns"]}


# ---- the declaration

@dataclass
class Output:
    pin: str
    source: str | None = None
    frm: str | None = None
    settle_ns: float = DEFAULT_SETTLE_NS
    window_ns: float = 100.0
    step_ns: float = DEFAULT_STEP_NS


@dataclass
class Input:
    pin: str
    observe: str = ""
    rising: bool = True
    settle_ns: float = DEFAULT_SETTLE_NS
    ramp_ns: float = DEFAULT_RAMP_NS
    step_ns: float = DEFAULT_STEP_NS


@dataclass
class Boundary:
    static: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)


def _need(cond, msg: str) -> None:
    if not cond:
        raise Refused("boundary_incomplete", msg)


def _positive(entry: dict, key: str, default: float, where: str) -> float:
    value = entry.get(key, default)
    _need(not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0,
          f"{where}: {key} must be a positive number of nanoseconds, got {value!r}")
    return float(value)


def _keys(entry, allowed: set, where: str) -> None:
    _need(isinstance(entry, dict), f"{where} must be a mapping")
    extra = sorted(set(entry) - allowed)
    _need(not extra, f"{where}: unknown fields {extra}")


def parse_section(section: dict, crossing: dict[str, str]) -> Boundary:
    """`section` is the analog spec's boundary:, `crossing` {pin: a2d|d2a}.
    Every crossing pin must be declared under its own direction, and only they."""
    _keys(section, {"static", "a2d", "d2a"}, "boundary")
    a2d = {str(k).lower(): v for k, v in (section.get("a2d") or {}).items()}
    d2a = {str(k).lower(): v for k, v in (section.get("d2a") or {}).items()}
    for direction, entries in (("a2d", a2d), ("d2a", d2a)):
        want = {p for p, d in crossing.items() if d == direction}
        _need(want == set(entries),
              f"boundary.{direction} must name exactly the {direction} "
              f"crossing pins {sorted(want)}, got {sorted(entries)}")
    out = Boundary(static={str(k).lower(): v
                           for k, v in (section.get("static") or {}).items()})
    for pin, raw in a2d.items():
        where = f"boundary.a2d.{pin}"
        _keys(raw, {"source", "from", "settle_ns", "window_ns", "step_ns"}, where)
        _need(("source" in raw) != ("from" in raw),
              f"{where} needs exactly one of source (the pin whose edge starts "
              "a running output) and from (the input that pulses it)")
        _need("window_ns" in raw, f"{where} has no window_ns")
        out.outputs[pin] = Output(
            pin=pin,
            source=str(raw["source"]).lower() if "source" in raw else None,
            frm=str(raw["from"]).lower() if "from" in raw else None,
            settle_ns=_positive(raw, "settle_ns", DEFAULT_SETTLE_NS, where),
            window_ns=_positive(raw, "window_ns", 0, where),
            step_ns=_positive(raw, "step_ns", DEFAULT_STEP_NS, where))
    for pin, raw in d2a.items():
        where = f"boundary.d2a.{pin}"
        _keys(raw, {"observe", "ramp", "settle_ns", "ramp_ns", "step_ns"}, where)
        _need(isinstance(raw.get("observe"), str) and raw["observe"].strip(),
              f"{where} has no observe: a d2a pin's switching point cannot be "
              "measured without a net that follows it")
        ramp = raw.get("ramp", "up")
        _need(ramp in ("up", "down"), f"{where}: ramp must be up or down")
        out.inputs[pin] = Input(
            pin=pin, observe=raw["observe"].strip().lower(), rising=ramp == "up",
            settle_ns=_positive(raw, "settle_ns", DEFAULT_SETTLE_NS, where),
            ramp_ns=_positive(raw, "ramp_ns", DEFAULT_RAMP_NS, where),
            step_ns=_positive(raw, "step_ns", DEFAULT_STEP_NS, where))
    return out


# ---- the arithmetic: pure, so it is tested without a simulator

def table_transition(edge_ns: float, level: dict) -> float:
    """A 10-90% edge in the unit of the liberty's max_transition: the same
    ramp measured between the library's slew thresholds, then derated."""
    span = (level["slew_upper"] - level["slew_lower"]) / (EDGE_HIGH - EDGE_LOW)
    return edge_ns * span / level["slew_derate"]


def transition_limit(level: dict, corner: str, pin_limit: float | None) -> float:
    limit = level["corners"][corner]["max_transition_ns"]
    return min(limit, pin_limit) if pin_limit is not None else limit


def judge_output(pin: str, corner: str, level: dict, measured: dict,
                 limit: float) -> list[tuple[str, str]]:
    """(kind, message) for an a2d pin at one corner."""
    vdd = level["corners"][corner]["vdd"]
    vih, vil = level["vih_frac"] * vdd, level["vil_frac"] * vdd
    high, low = measured["v_high"], measured["v_low"]
    out = []
    if high < vih:
        out.append(("boundary_swing",
                    f"{pin} at {corner}: its high level {high:.3g} V is below "
                    f"vih {vih:.3g} V, so a digital input does not read it as 1"))
    if low > vil:
        out.append(("boundary_swing",
                    f"{pin} at {corner}: its low level {low:.3g} V is above "
                    f"vil {vil:.3g} V, so a digital input does not read it as 0"))
    if out:
        return out
    for edge in ("rise", "fall"):
        value = measured[f"{edge}_transition_ns"]
        if value is None:
            out.append(("boundary_transition",
                        f"{pin} at {corner}: no complete 10-90% {edge} in the "
                        "window, so its edge cannot be held to the limit"))
        elif value > limit:
            out.append(("boundary_transition",
                        f"{pin} at {corner}: {edge} transition {value:.3g} ns "
                        f"is over the {limit:.3g} ns limit into "
                        f"{measured['load_pf']:.4g} pF"))
    return out


def judge_input(pin: str, corner: str, level: dict,
                threshold: float | None) -> list[tuple[str, str]]:
    """(kind, message) for a d2a pin at one corner."""
    vdd = level["corners"][corner]["vdd"]
    vih, vil = level["vih_frac"] * vdd, level["vil_frac"] * vdd
    if threshold is None:
        return [("boundary_threshold",
                 f"{pin} at {corner}: the observed net never crossed vdd/2 "
                 "while the pin was ramped, so no switching point exists")]
    if not vil < threshold < vih:
        return [("boundary_threshold",
                 f"{pin} at {corner}: switches at {threshold:.3g} V, outside "
                 f"the {vil:.3g} to {vih:.3g} V a digital driver is sure to "
                 "cross")]
    return []


# ---- the benches

def _pin_stimulus(bench, decl: Boundary, driven: set) -> dict:
    """Each undriven input's DC level. A pin nobody named is a refusal, not a
    floating node ngspice would quietly solve."""
    stim = {}
    for port in bench.ports:
        low = port.lower()
        if low in (bench.power, bench.ground) or low in driven or low in decl.outputs:
            continue
        _need(low in decl.static, f"boundary.static has no level for pin {port}")
        value = decl.static[low]
        if isinstance(value, str) and value.strip().lower() == OPEN:
            continue
        stim[low] = macrolib.level(value, bench.vdd)
    return stim


def measure_output(bench, decl: Boundary, out: Output, level: dict, load: dict) -> dict:
    """Swing and 10-90% edges of one a2d pin into its load."""
    vdd = bench.vdd
    node = bench.node[out.pin]
    td = out.settle_ns
    t_stop = td + out.window_ns
    if out.source:
        _need(out.source in decl.static,
              f"boundary.static has no level for {out.source}, which "
              f"boundary.a2d.{out.pin} names as its source")
        _need(td > _ENABLE_AT_NS,
              f"boundary.a2d.{out.pin}: settle_ns must be past the enable edge "
              f"at {_ENABLE_AT_NS} ns")
        stim = _pin_stimulus(bench, decl, {out.pin})
        enabled = stim[out.source] > vdd / 2
        stim[out.source] = charlib._ramp_edges(
            vdd, [(_ENABLE_AT_NS, 0.1, enabled)], t_stop)
    else:
        _need(out.frm in bench.node, f"boundary.a2d.{out.pin}: no port {out.frm}")
        stim = _pin_stimulus(bench, decl, {out.pin, out.frm})
        half = out.window_ns / 2
        stim[out.frm] = charlib._ramp_edges(
            vdd, [(td, 0.1, True), (td + half, 0.1, False)], t_stop)
    low_v, high_v = EDGE_LOW * vdd, EDGE_HIGH * vdd
    measures = [f".meas tran v_high MAX v({node}) FROM={td:g}n TO={t_stop:g}n",
                f".meas tran v_low MIN v({node}) FROM={td:g}n TO={t_stop:g}n"]
    for edge, tag, first, last in (("RISE", "r", low_v, high_v),
                                   ("FALL", "f", high_v, low_v)):
        measures.append(f".meas tran t_{tag}a WHEN v({node})={first:.6g} "
                        f"{edge}=1 TD={td:g}n")
        # settle_ns can land mid-edge, so the end's first crossing may belong
        # to the edge before: two to choose from
        measures += [f".meas tran t_{tag}b{n} WHEN v({node})={last:.6g} "
                     f"{edge}={n} TD={td:g}n" for n in (1, 2)]
    meas = bench.simulate(stim, {out.pin: load["pf"]}, t_stop, out.step_ns, measures)
    if "v_high" not in meas or "v_low" not in meas:
        raise Refused("boundary_incomplete",
                      f"{out.pin} at {bench.corner['name']}: the bench reported "
                      "no output level")

    def edge_ns(tag: str):
        start = meas.get(f"t_{tag}a")
        ends = [meas[k] for k in (f"t_{tag}b1", f"t_{tag}b2") if k in meas]
        after = [end for end in ends if start is not None and end > start]
        return (after[0] - start) * 1e9 if after else None

    rise, fall = edge_ns("r"), edge_ns("f")
    return {"v_high": meas["v_high"], "v_low": meas["v_low"],
            "rise_ns": rise, "fall_ns": fall, "load_pf": load["pf"],
            "rise_transition_ns": None if rise is None else table_transition(rise, level),
            "fall_transition_ns": None if fall is None else table_transition(fall, level)}


def observed_node(bench, name: str, netlist_text: str) -> str:
    if name in bench.node:
        return bench.node[name]
    _need(_NET_RE.match(name), f"boundary: cannot read {name!r} as a net")
    leaf = name.rsplit(".", 1)[-1]
    _need(re.search(rf"(?i)(?<![\w.]){re.escape(leaf)}(?![\w.])", netlist_text),
          f"boundary: the netlist has no net {name!r} to observe")
    return name


def measure_input(bench, decl: Boundary, inp: Input, level: dict,
                  netlist_text: str) -> dict:
    """The d2a pin's voltage when the net following it crosses vdd/2."""
    vdd = bench.vdd
    v_from = (level["vol_frac"] if inp.rising else level["voh_frac"]) * vdd
    v_to = (level["voh_frac"] if inp.rising else level["vol_frac"]) * vdd
    start, t_stop = inp.settle_ns, inp.settle_ns + inp.ramp_ns
    stim = _pin_stimulus(bench, decl, {inp.pin})
    stim[inp.pin] = (f"PWL(0n {v_from:g} {start:g}n {v_from:g} "
                     f"{t_stop:g}n {v_to:g})")
    node = observed_node(bench, inp.observe, netlist_text)
    meas = bench.simulate(stim, {}, t_stop, inp.step_ns, [
        f".meas tran t_sw WHEN v({node})={vdd / 2:.6g} CROSS=1 TD={start:g}n"])
    crossed = meas.get("t_sw")
    if crossed is None:
        return {"threshold_v": None}
    done = (crossed * 1e9 - start) / inp.ramp_ns
    return {"threshold_v": v_from + (v_to - v_from) * done}

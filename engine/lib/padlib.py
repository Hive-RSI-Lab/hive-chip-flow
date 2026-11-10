"""padlib.py - the Tiny Tapeout analog (ua) pad model and its insertion into
a bench (issue #112).

A block port that leaves the tile on a `ua[k]` pin does not see the bench's
load directly. On the shuttle it sees the route from the project to the
pad, then the pad's ESD clamps, then the board. A bench that ties a 2 pF
load straight onto that port measures a pin no silicon has.

The model, every number cited:

- **Route from project to pad: 500 ohm series, 5 pF shunt.** Tiny Tapeout's
  analog spec (https://tinytapeout.com/specs/analog/, "Specifications and
  limitations"): "The path between the analog pads and your project (the ua
  pins) is expected to have the following properties: Resistance: < 500
  ohm, Capacitance: < 5 pf, Max current: 4 mA". The page gives these
  figures for sky130A and none for GF180, so they are the only published
  bound and are used as is. They are maxima, and a pad model is a worst
  case, so the model uses them at their limit. No spec field can lower
  them.
- **ESD: the GF180 analog pad's own diodes.** TT publishes no ESD figures.
  The PDK's analog signal pad, `gf180mcu_fd_io__asig_5p0`
  (libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice), clamps its pad
  with `diode_nd2ps_06v0 m=4 area=150e-12 pj=106e-6` to DVSS and
  `diode_pd2nw_06v0 m=4 area=150e-12 pj=106e-6` to DVDD. The same two
  diodes, with the PDK's own models (sm141064.spice `.lib diode_<corner>`
  at the run's process corner), sit at the pad node here. DVDD is the
  bench's VDD, so a pad held inside the rails keeps both diodes reverse
  biased: the diodes add their junction capacitance and leakage, and clamp
  a pad driven past a rail.
- **Board: optional.** spec.yaml `ua_pad: {board_c: F, board_r: ohm}` adds a
  shunt C and/or a shunt R from the pad node to ground, for a probe, a
  header or a scope input. Leaving it out means no board load. It can only
  add load.

Topology, with `die` the block's port and `pad` the bench's node:

    die --[500 ohm]-- pad --+-- 5 pF -- 0
                            +-- ESD diodes to 0 and to DVDD
                            +-- board_c, board_r to 0 (when given)

The R sits on the die side and the C on the pad side, so a port driving the
pin charges the C through the R: the slower of the two orderings, since the
die side then also sees the C through the R.

Insertion (insert_pads): the bench is left as written, except that every
instance of the spec's `top` has each ua-pin connection renamed from node N
to `N__ua_die_<instance>_<port>`, and a pad instance joins that die node
to N. Every source, load and probe the bench put on N then sits on the
board side of the pad, and two ua ports a bench ties together (a
follower's out and inn) meet on the board, each through its own pad.
A bench in which no instance of `top` can be found, or whose instance's
node count does not match the top's ports, is refused (PadRefused): with no
pad in the deck, a "pad" run would only be a second bare run.

Conditions (conditions_of): a spec with `ua_pins` runs each bench twice,
"no_pad" (the bench as written) and "pad" (insert_pads). Every bound is
scored in both. A bound marked `pad: bare` (in the spec measure and its
sidecar bound alike) is scored in the no_pad run only: the spec says
plainly that the measure assumes a bare pin. `pad: pad` scores it in the
pad run only. Without `ua_pins` there is one run and no pad. This module is
pure: no filesystem, no subprocess.
"""
from __future__ import annotations

import math
import re

# ---------------------------------------------------------------- the model

#: TT analog spec, "Resistance: < 500 ohm" (the route from pad to project)
R_SERIES_OHM = 500.0
#: TT analog spec, "Capacitance: < 5 pf"
C_SHUNT_F = 5e-12
#: TT analog spec, "Max current: 4 mA" (reported against, never modelled)
I_MAX_A = 4e-3
#: gf180mcu_fd_io__asig_5p0's clamp diodes: (model, terminal order, m, area
#: m^2, perimeter m). D2 DVSS ASIG5V diode_nd2ps_06v0 (anode DVSS), D3
#: ASIG5V DVDD diode_pd2nw_06v0 (anode pad).
ESD_DIODES = (
    ("diode_nd2ps_06v0", "to_vss", 4, 150e-12, 106e-6),
    ("diode_pd2nw_06v0", "to_vdd", 4, 150e-12, 106e-6),
)
SOURCE = ("Tiny Tapeout analog spec https://tinytapeout.com/specs/analog/ "
          "(R < 500 ohm, C < 5 pF, 4 mA); ESD from the GF180 PDK's "
          "gf180mcu_fd_io__asig_5p0")
SUBCKT = "tt_ua_pad"
DIE_SUFFIX = "__ua_die"
CONDITIONS = ("no_pad", "pad")
PAD_SCOPES = ("both", "bare", "pad")
# the PDK's diode sections exist for these processes only
DIODE_CORNERS = {"typical": "typical", "tt": "typical", "ss": "ss",
                 "ff": "ff"}
BOARD_KEYS = ("board_c", "board_r")


class PadRefused(ValueError):
    """The pad model cannot be put into this bench. `kind` is the
    violation kind (ua_pad_not_inserted)."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


# ------------------------------------------------------------- spec fields

def ua_pins_of(spec: dict) -> list[str]:
    """The spec's `ua_pins` (a list of the top's port names), [] when the
    field is absent. Raises ValueError on a malformed field: a pin listed
    twice, a non-string, an empty list (leave the field out instead), or a
    msde-style {pin: k} map. That map belongs in interface.yaml, and here
    only its keys would count."""
    if "ua_pins" not in spec:
        return []
    pins = spec["ua_pins"]
    if isinstance(pins, dict):
        raise ValueError("spec.yaml 'ua_pins' is a list of the top's port "
                         "names on ua pads (`ua_pins: [out]`); the "
                         "{pin: ua index} map belongs in a msde "
                         "interface.yaml")
    if not isinstance(pins, list) or not pins:
        raise ValueError("spec.yaml 'ua_pins' must be a non-empty list of "
                         "the top's port names that sit on ua pads; leave "
                         "the field out when no port leaves the tile")
    out = []
    for p in pins:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"spec.yaml 'ua_pins' entry {p!r} is not a "
                             "port name")
        if p.lower() in (q.lower() for q in out):
            raise ValueError(f"spec.yaml 'ua_pins' lists {p!r} twice")
        out.append(p)
    return out


def board_of(spec: dict) -> dict[str, float]:
    """spec.yaml `ua_pad` (the optional board load) as {board_c?, board_r?},
    {} when absent. Each value must be a finite number > 0; any other key
    is refused, so a typo can never pass as "no board load" and no field
    can reach the TT figures. Raises ValueError."""
    if "ua_pad" not in spec:
        return {}
    pad = spec["ua_pad"]
    if not isinstance(pad, dict) or not pad:
        raise ValueError("spec.yaml 'ua_pad' must be a mapping with "
                         "board_c (F) and/or board_r (ohm); leave it out "
                         "for no board load")
    extra = sorted(set(pad) - set(BOARD_KEYS))
    if extra:
        raise ValueError(f"spec.yaml 'ua_pad' has {extra}: only "
                         f"{', '.join(BOARD_KEYS)} (a board load) may be "
                         "set. The pad's own R, C and ESD are TT's "
                         "published figures and no spec changes them")
    out = {}
    for k, v in pad.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(v) or v <= 0:
            raise ValueError(f"spec.yaml 'ua_pad.{k}' must be a finite "
                             "number > 0")
        out[k] = float(v)
    if not ua_pins_of(spec):
        raise ValueError("spec.yaml 'ua_pad' sets a board load but there "
                         "is no 'ua_pins' list for it to load")
    return out


def pad_scope(bound: dict) -> str:
    """A bound's or a spec measure's `pad` scope: "both" (the default),
    "bare" or "pad". Raises ValueError on any other value."""
    scope = bound.get("pad", "both")
    if scope not in PAD_SCOPES:
        raise ValueError(f"'pad' must be one of {', '.join(PAD_SCOPES)}, "
                         f"not {scope!r}")
    return scope


def scored_in(bound: dict, condition: str | None) -> bool:
    """Whether `bound` is scored in a run of `condition` ("pad", "no_pad",
    or None for a spec without ua_pins, where every bound is scored)."""
    if condition is None:
        return True
    scope = bound.get("pad", "both")
    return scope == "both" or (scope == "bare") == (condition == "no_pad")


def conditions_of(spec: dict) -> list[str | None]:
    """The runs one bench gets: ["no_pad", "pad"] when the spec lists
    ua_pins, [None] (one bare run, no condition) otherwise."""
    return list(CONDITIONS) if ua_pins_of(spec) else [None]


# ------------------------------------------------------------ the subckt

def diode_corner(process: str) -> str:
    """The PDK's diode section for a run's process corner: typical, ss or
    ff (sf/fs have none and take typical)."""
    return DIODE_CORNERS.get(str(process).lower(), "typical")


def pad_subckt(pdk: str, process: str, vdd: float,
               board: dict | None = None, esd: bool = True) -> str:
    """The pad model's SPICE: the diode section's `.lib` line and the
    `tt_ua_pad die pad` subckt. `pdk` is the PDK root the bench's {{PDK}}
    names, `vdd` the DVDD the ESD's upper diode returns to. `esd=False`
    leaves the diodes out: only the known-answer RC test uses it (its tau
    is then exact); no spec field and no gate reaches it."""
    board = board or {}
    lines = [
        f"* {SUBCKT}: Tiny Tapeout ua pad model (engine/lib/padlib.py)",
        f"* {SOURCE}",
    ]
    if esd:
        lines.append(f".lib '{pdk}/libs.tech/ngspice/sm141064.spice' "
                     f"diode_{diode_corner(process)}")
    lines += [
        f".subckt {SUBCKT} die pad",
        f"rroute die pad {R_SERIES_OHM:.6g}",
        f"croute pad 0 {C_SHUNT_F:.6g}",
        f"vdvdd dvdd 0 {vdd:.6g}",
    ]
    for i, (model, side, m, area, pj) in enumerate(ESD_DIODES if esd
                                                    else ()):
        anode, cathode = ("0", "pad") if side == "to_vss" else ("pad", "dvdd")
        lines.append(f"desd{i} {anode} {cathode} {model} m={m} "
                     f"area={area:.6g} pj={pj:.6g}")
    if "board_c" in board:
        lines.append(f"cboard pad 0 {board['board_c']:.6g}")
    if "board_r" in board:
        lines.append(f"rboard pad 0 {board['board_r']:.6g}")
    lines.append(f".ends {SUBCKT}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- insertion

def top_ports(netlist_text: str, top: str) -> list[str]:
    """The ports of `.subckt top ...` in the netlist (params dropped).
    Raises PadRefused when the netlist does not declare it."""
    import netlistlib
    pins = netlistlib.subckt_pins(netlist_text).get(top.lower())
    if pins is None:
        raise PadRefused("ua_pad_not_inserted",
                         f"the netlist declares no `.subckt {top}`, so the "
                         "ua pins' positions on its instance are unknown")
    return [p for p in pins if "=" not in p and p.lower() != "params:"]


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """[(first line index, last line index, joined text)] for every element
    line of `text` with its `+` continuations, comments kept out."""
    raw = text.splitlines()
    out: list[tuple[int, int, str]] = []
    for i, line in enumerate(raw):
        s = line.strip()
        if not s or s.startswith("*"):
            continue
        if s.startswith("+") and out:
            a, _b, joined = out[-1]
            out[-1] = (a, i, joined + " " + s[1:].strip())
        else:
            out.append((i, i, s))
    return out


def insert_pads(bench_text: str, top: str, ports: list[str],
                ua_pins: list[str], pad_text: str) -> tuple[str, list[dict]]:
    """The bench with a pad on every ua pin of every `top` instance, and
    [{instance, port, node}] for each pad put in. `pad_text` is
    pad_subckt()'s SPICE, added once ahead of the first instance. Raises
    PadRefused (ua_pad_not_inserted) when no instance of top is found, when
    an instance's node count is not its port count, or when a ua pin is
    not one of the top's ports."""
    lower = [p.lower() for p in ports]
    for pin in ua_pins:
        if pin.lower() not in lower:
            raise PadRefused("ua_pad_not_inserted",
                             f"ua pin {pin!r} is not a port of {top} "
                             f"({', '.join(ports)})")
    idx = [lower.index(p.lower()) for p in ua_pins]
    raw = bench_text.splitlines()
    inserted: list[dict] = []
    edits: list[tuple[int, int, str]] = []
    for a, b, joined in _logical_lines(bench_text):
        if joined[0] not in "xX" or joined.lower().startswith((".",)):
            continue
        toks = joined.split()
        names = [t for t in toks[1:] if "=" not in t]
        if not names or names[-1].lower() != top.lower():
            continue
        nodes = names[:-1]
        if len(nodes) != len(ports):
            raise PadRefused("ua_pad_not_inserted",
                             f"instance {toks[0]} of {top} connects "
                             f"{len(nodes)} nodes, and {top} has "
                             f"{len(ports)} ports, so the ua pins cannot be "
                             "located on it")
        new_nodes = list(nodes)
        pads = []
        for k, pin in zip(idx, ua_pins):
            node = nodes[k]
            if node == "0" or node.lower() == "gnd":
                raise PadRefused("ua_pad_not_inserted",
                                 f"instance {toks[0]} ties ua pin {pin} to "
                                 "ground: a pin on a ua pad is driven "
                                 "through the pad, so ground it outside it")
            # one die node per instance and port: two ua pins a bench ties
            # to one node (a follower's out and inn) meet on the board,
            # each through its own pad, never shorted on the die
            die = f"{node}{DIE_SUFFIX}_{toks[0]}_{pin}"
            new_nodes[k] = die
            pads.append(f"xuapad_{toks[0]}_{pin} {die} {node} {SUBCKT}")
            inserted.append({"instance": toks[0], "port": pin, "node": node})
        params = [t for t in toks[1:] if "=" in t]
        line = " ".join([toks[0], *new_nodes, names[-1], *params])
        edits.append((a, b, "\n".join([line, *pads])))
    if not inserted:
        raise PadRefused("ua_pad_not_inserted",
                         f"no instance of {top} in the bench, so the ua "
                         "pad model has nowhere to go")
    for a, b, new in reversed(edits):
        raw[a:b + 1] = [new]
    first = edits[0][0]
    raw[first:first] = [pad_text.rstrip("\n")]
    return "\n".join(raw) + ("\n" if bench_text.endswith("\n") else ""), \
        inserted


CORE_SUFFIX = "__core"


def wrap_subckt_pads(netlist_text: str, ua_pins: list[str],
                     pad_text: str) -> tuple[str, str, list[dict]]:
    """The msde cosim netlist with a pad on every ua pin of its block.

    A cosim bench loads one `.subckt` from its analog netlist through
    cocotbext-ams, which instantiates it itself, so there is no bench
    instance line to rewrite. Instead the block's subckt is renamed
    `<name>__core`, and a wrapper with the block's own name and ports puts
    the core behind a pad on each ua pin: the bench loads the same name and
    gets the pin through the pad. The block is the one subckt whose ports
    hold every ua pin and that no other such subckt instantiates: the
    outermost one carrying the pins. It may sit under the subckt the bench
    loads, since cocotbext-ams gives that one only the bridged digital
    pins and the supplies, so a block whose ua pins are analog only (a
    PLL's VCTRL) keeps them on an inner subckt the bench's wrapper
    instantiates. None, or more than one, is refused (PadRefused), since a
    pad put on the wrong subckt would leave the real pin bare.

    Returns (text, block name, [{instance, port, node}])."""
    import netlistlib
    pins = netlistlib.subckt_pins(netlist_text)
    want = {p.lower() for p in ua_pins}
    carriers = {n for n, ports in pins.items()
                if want <= {q.lower() for q in ports}}
    # what each carrier (and the file's top level) instantiates
    used = set()
    current = None
    for _a, _b, joined in _logical_lines(netlist_text):
        toks = joined.split()
        word = toks[0].lower()
        if word == ".subckt" and len(toks) > 1:
            current = toks[1].lower()
        elif word.startswith(".ends"):
            current = None
        elif word[0] == "x" and (current is None or current in carriers):
            names = [t for t in toks[1:] if "=" not in t]
            if names:
                used.add(names[-1].lower())
    blocks = [n for n in pins if n in carriers and n not in used]
    if len(blocks) != 1:
        found = ", ".join(sorted(pins)) or "none"
        raise PadRefused(
            "ua_pad_not_inserted",
            f"{'no' if not blocks else 'more than one'} outermost subckt in "
            f"the cosim netlist has every ua pin ({', '.join(ua_pins)}) as "
            f"a port (subckts: {found}), so the pad model has nowhere "
            "definite to go")
    block = blocks[0]
    ports = [q for q in pins[block] if "=" not in q
             and q.lower() != "params:"]
    raw = netlist_text.splitlines()
    head = re.compile(r"^(\s*\.subckt\s+)(\S+)(.*)$", re.I)
    ends = re.compile(r"^(\s*\.ends\s+)(\S+)(.*)$", re.I)
    name = None
    inside = False
    for i, line in enumerate(raw):
        m = head.match(line)
        if m and m.group(2).lower() == block:
            name = m.group(2)
            raw[i] = m.group(1) + name + CORE_SUFFIX + m.group(3)
            inside = True
            continue
        m = ends.match(line)
        if inside and m and m.group(2).lower() == block:
            raw[i] = m.group(1) + name + CORE_SUFFIX + m.group(3)
        if inside and re.match(r"^\s*\.ends\b", line, re.I):
            inside = False
    core = [f"{q}{DIE_SUFFIX}_{q}" if q.lower() in want else q
            for q in ports]
    pads = [f"xuapad_{q} {q}{DIE_SUFFIX}_{q} {q} {SUBCKT}"
            for q in ports if q.lower() in want]
    wrapper = [
        "",
        f"* {name}: the block behind the Tiny Tapeout ua pad on "
        f"{', '.join(ua_pins)} (padlib.wrap_subckt_pads)",
        pad_text.rstrip("\n"),
        f".subckt {name} {' '.join(ports)}",
        f"xcore {' '.join(core)} {name}{CORE_SUFFIX}",
        *pads,
        f".ends {name}",
    ]
    inserted = [{"instance": name, "port": q, "node": q}
                for q in ports if q.lower() in want]
    return "\n".join(raw + wrapper) + "\n", name, inserted


def tau_s(r_source: float, c_extra: float = 0.0,
          board: dict | None = None) -> float:
    """The pad's time constant driven by an ideal step through
    `r_source` with the ESD left out: (r_source + R) * (C + board_c +
    c_extra), with board_r (when given) as a divider on the pad node. The
    known answer the pad test settles against."""
    board = board or {}
    c = C_SHUNT_F + board.get("board_c", 0.0) + c_extra
    r = r_source + R_SERIES_OHM
    if "board_r" in board:
        r = r * board["board_r"] / (r + board["board_r"])
    return r * c

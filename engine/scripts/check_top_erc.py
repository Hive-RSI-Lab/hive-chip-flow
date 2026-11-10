#!/usr/bin/env python
"""check_top_erc.py - msde's top_erc gate: voltage-aware rule checks with CVC
on the assembled top (GitHub issue 111).

    check_top_erc.py --workspace MSDE_WS [--out FILE]

The netlist is the schematic side top_lvs builds: LibreLane's powered
gate-level netlist, each standard cell expanded through the PDK's own spice
and the analog macro through its sized .subckt (top/macros/<cell>.spice).
All of it is flattened to transistors here, so a net that crosses from the
digital side into the macro is one net to CVC. What CVC then looks for is
check_analog_erc.py's list: floating gates, devices over their voltage rating,
forward-biased junctions, wells on the wrong net, supply-to-supply leaks.

Three runs, each its own CVC run under log/top_erc/<mode>:
  operating  every tile input and analog pad swings 0 V to the supply.
  held       the digital side held in reset: `rst_n` and `clk` at 0 V and
             every d2a crossing signal of interface.yaml at 0 V (a pll_en=0
             state). CVC does not reason through flops, so each crossing pin
             is cut from its digital driver and tied low. An active-low d2a
             signal is therefore seen enabled here; its disabled state is
             inside the operating run's range.
  analog     each macro alone: its power pins at the supply and ground, every
             other pin 0 V to the supply. A digital side that happens to
             drive a macro input cannot hide a floating node behind it.

The supply is analog/spec/spec.yaml's supply.vdd. A power net is one of the
tile's rails (POWER_PORTS, GROUND_PORTS); on a macro, its spec `power` pins.
Device ratings are check_analog_erc.py's tables: each device's own GF180
operating class plus 10%.

Which findings are errors. check_analog_erc.py reports CVC's leak sections as
warnings because a lone analog block can mean one (a ladder driven from its
pins). Here a path between two supply rails through devices CVC has proven
on cannot be an intended signal, so a `leak` finding is an error; a possible
leak (a gate CVC cannot resolve) stays a warning. Waivers are
spec/erc_waivers.yaml at the workspace root, in check_analog_erc.py's format,
the device named by its full path (u_analog/xmp1).

A device, cell or pin this gate cannot place is a refusal (exit 2), never a
pass: CVC would otherwise see a smaller circuit than the one being taped out.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
import check_analog_erc as erc  # noqa: E402
import check_lvs  # noqa: E402
import check_top_harden  # noqa: E402
import checklib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

import yaml  # noqa: E402

SCRIPT = "check_top_erc"
TOOL = "top_erc"
POWER_PORTS = ("VPWR", "VDPWR", "VAPWR")
GROUND_PORTS = ("VGND",)
RESET_PORTS = ("rst_n", "clk")
SEP = "."           # between instance path levels in a flat net name
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/|\(\*.*?\*\)", re.S)
DECL_RE = re.compile(r"^(input|output|inout|wire)\s*(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*(.*)$",
                     re.S)
INSTANCE_RE = re.compile(r"^(\\\S+|[A-Za-z_][\w$]*)\s+(\\\S+|[A-Za-z_][\w$]*)\s*\((.*)\)$", re.S)
CONN_RE = re.compile(r"\.\s*(\\\S+|[\w$]+)\s*\(\s*([^()]*?)\s*\)", re.S)


def _name(token: str) -> str:
    """A Verilog identifier or bit select as one net name: an escaped
    identifier loses its backslash, a bit select its spaces."""
    t = token.strip()
    if t.startswith("\\"):
        return t[1:].strip()
    return re.sub(r"\s+", "", t)


def parse_powered_netlist(text: str) -> dict:
    """LibreLane's structural powered netlist as {top, ports: {name: dir},
    widths: {bus: (msb, lsb)}, assigns: [(lhs, rhs)], instances: [(cell, name,
    {pin: expr})]}, a port or bus expanded to its bits in `ports`. Anything
    but one flat module of cells refuses."""
    stmts = [s.strip() for s in COMMENT_RE.sub(" ", text).split(";")]
    top, ports, widths, assigns, instances = None, {}, {}, [], []
    for raw in stmts:
        s = raw
        if s.startswith("endmodule"):
            s = s[len("endmodule"):].strip()
        if not s:
            continue
        if s.startswith("module"):
            if top is not None:
                raise CheckError("the powered netlist holds more than one "
                                 "module; the gate reads one flat top")
            m = re.match(r"module\s+(\\\S+|[\w$]+)", s)
            top = _name(m.group(1)) if m else None
            continue
        m = DECL_RE.match(s)
        if m:
            kind, msb, lsb, names = m.groups()
            for name in (_name(n) for n in names.split(",")):
                bits = [name]
                if msb is not None:
                    widths[name] = (int(msb), int(lsb))
                    bits = _bus_bits(name, int(msb), int(lsb))
                if kind != "wire":
                    ports.update({b: kind for b in bits})
            continue
        if s.startswith("assign"):
            lhs, _, rhs = s[len("assign"):].partition("=")
            assigns.append((_name(lhs), _name(rhs)))
            continue
        m = INSTANCE_RE.match(s)
        if not m:
            raise CheckError(f"the powered netlist has a statement the gate "
                             f"cannot read: {s[:120]!r}")
        conns = {_name(p): e.strip() for p, e in CONN_RE.findall(m.group(3))}
        instances.append((_name(m.group(1)), _name(m.group(2)), conns))
    if top is None:
        raise CheckError("the powered netlist has no module")
    return {"top": top, "ports": ports, "widths": widths, "assigns": assigns,
            "instances": instances}


def _bus_bits(name: str, msb: int, lsb: int) -> list[str]:
    """A bus's bits in Verilog concatenation order, most significant first."""
    step = -1 if msb >= lsb else 1
    return [f"{name}[{i}]" for i in range(msb, lsb + step, step)]


def parse_subckts(text: str) -> dict:
    """{lowercased cell: (ports, device lines)} of a spice library."""
    lines = []
    for raw in text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    cells, current = {}, None
    for line in lines:
        t = line.split("$")[0].split()
        if not t or t[0].startswith("*"):
            continue
        head = t[0].lower()
        if head == ".subckt":
            current = []
            cells[t[1].lower()] = ([x for x in t[2:] if "=" not in x], current)
        elif head == ".ends":
            current = None
        elif current is not None and not head.startswith("."):
            current.append(t)
    return cells


class Nets:
    """Union-find over a top's net names: `assign a = b` makes one net, named
    for a top port when one of the two is a port, so a finding reads as the
    pin a person knows."""

    def __init__(self, ports):
        self.ports, self.parent = set(ports), {}

    def find(self, net: str) -> str:
        root = net
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        while self.parent.get(net, net) != root:
            self.parent[net], net = root, self.parent[net]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            keep, drop = (ra, rb) if ra in self.ports or rb not in self.ports \
                else (rb, ra)
            self.parent[drop] = keep


def _const(expr: str) -> bool:
    return bool(re.fullmatch(r"\d*'[bdhBDH][0-9a-fA-FxXzZ]+", expr))


def _macro_pin_bits(pin: str, ports: list[str]) -> list[str]:
    """The .subckt ports of Verilog bus pin `pin`, highest index first, for
    `pin[3]`, `pin<3>` or `pin_3` spellings. Empty when it is not a bus."""
    found = {}
    for p in ports:
        m = re.fullmatch(re.escape(pin) + r"(?:\[(\d+)\]|<(\d+)>|_(\d+))", p)
        if m:
            found[int(next(g for g in m.groups() if g is not None))] = p
    return [found[i] for i in sorted(found, reverse=True)]


def _expr_bits(expr: str, widths: dict, nets: Nets) -> list[str]:
    """A connection expression's bits, most significant first."""
    if _const(expr):
        raise CheckError(f"a pin is tied to the constant {expr!r} in the "
                         "powered netlist, with no tie cell - the gate has "
                         "no voltage for it")
    if expr.startswith("{"):
        out = []
        for part in expr.strip("{}").split(","):
            out += _expr_bits(part.strip(), widths, nets)
        return out
    name = _name(expr)
    if name in widths:
        return [nets.find(b) for b in _bus_bits(name, *widths[name])]
    return [nets.find(name)]


def flatten(design: dict, library: dict, ground: str) -> tuple[str, dict]:
    """(one flat netlist of PDK devices as check_analog_erc.to_cdl reads it,
    {flat device name: its instance path}). Nets inside a cell are
    `<instance>.<net>`, spice's global `0` is `ground`."""
    nets = Nets(design["ports"])
    for lhs, rhs in design["assigns"]:
        if _const(rhs):
            raise CheckError(f"assign {lhs} = {rhs}: a net tied to a constant "
                             "with no tie cell has no voltage for the gate")
        nets.union(lhs, rhs)
    out, paths = [], {}

    def emit(prefix, path, t, conn):
        device, seq = t[0], len(paths)
        letter = "D" if device[0] in "dD" else "X"
        first = next((i for i, tok in enumerate(t) if "=" in tok), len(t))
        terms = [conn.get(n, ground if n == "0" else f"{prefix}{n}")
                 for n in t[1:first - 1]]
        name = f"{letter}{seq}"
        paths[name] = path + device
        # the PDK spice mixes `0.6U` with `6e-07`; CVC reads a lower-case
        # suffix only
        out.append(" ".join([name, *terms, t[first - 1],
                             *(tok.lower() for tok in t[first:])]))

    def expand(cell, conn, prefix, path, stack):
        if cell in stack:
            raise CheckError(f"subcircuit {cell} instantiates itself")
        _ports, body = library[cell]
        for t in body:
            head = t[0][0].lower()
            if head not in "xdm":
                raise CheckError(
                    f"{path}{t[0]}: a {t[0][0].upper()} element in {cell}; "
                    "the gate reads PDK devices and subcircuits only - "
                    "CVC would check a smaller circuit than the real one")
            first = next((i for i, tok in enumerate(t) if "=" in tok), len(t))
            model = t[first - 1]
            if erc.device_class(model) is not None:
                emit(prefix, path, t, conn)
                continue
            sub = model.lower()
            if head == "m" or sub not in library:
                raise CheckError(f"{path}{t[0]} is a {model!r}, neither a "
                                 "PDK device with a rating nor a subcircuit "
                                 "in the netlist")
            if any("{" in tok for tok in t):
                raise CheckError(f"{path}{t[0]} has an unresolved parameter")
            sub_ports = library[sub][0]
            actual = t[1:first - 1]
            if len(actual) != len(sub_ports):
                raise CheckError(f"{path}{t[0]} connects {len(actual)} nets "
                                 f"to {model}'s {len(sub_ports)} ports")
            inner = {p: conn.get(n, ground if n == "0" else f"{prefix}{n}")
                     for p, n in zip(sub_ports, actual)}
            expand(sub, inner, f"{prefix}{t[0]}{SEP}", f"{path}{t[0]}/",
                   stack | {cell})

    for cell_name, inst, conns in design["instances"]:
        cell = cell_name.lower()
        if cell not in library:
            raise CheckError(f"{inst} is a {cell_name}, which is in neither "
                             "the standard-cell spice nor a macro .subckt")
        ports = library[cell][0]
        bound = {}
        for pin, expr in conns.items():
            if pin in ports:
                if expr:
                    bits = _expr_bits(expr, design["widths"], nets)
                    if len(bits) != 1:
                        raise CheckError(f"{inst}.{pin} is one pin connected "
                                         f"to {len(bits)} nets")
                    bound[pin] = bits[0]
                continue
            sub_bits = _macro_pin_bits(pin, ports)
            if not sub_bits:
                raise CheckError(f"{inst} connects pin {pin!r}, which "
                                 f"{cell_name}'s .subckt does not have "
                                 f"(ports: {ports})")
            if expr:
                bits = _expr_bits(expr, design["widths"], nets)
                if len(bits) != len(sub_bits):
                    raise CheckError(f"{inst}.{pin} is {len(sub_bits)} bits "
                                     f"connected to {len(bits)} nets")
                bound.update(zip(sub_bits, bits))
        # a pin with no connection is its own net: nothing drives it, so a
        # gate on it is CVC's floating gate
        conn = {p: bound.get(p, f"{inst}{SEP}{p}") for p in ports}
        expand(cell, conn, f"{inst}{SEP}", f"{inst}/", frozenset())

    pins = [nets.find(p) for p in design["ports"]]
    pins = list(dict.fromkeys(pins))
    text = "\n".join([f".subckt {design['top']} {' '.join(pins)}", *out,
                      ".ends", ""])
    return text, paths


def rail_voltages(port_dirs: dict, vdd: float) -> dict[str, dict]:
    """Each tile port's voltage for the operating run. An output is left off:
    CVC propagates what drives it."""
    out = {}
    for port, direction in port_dirs.items():
        if port in POWER_PORTS:
            out[port] = {"power": vdd}
        elif port in GROUND_PORTS:
            out[port] = {"power": 0.0}
        elif direction != "output":
            out[port] = {"min": 0.0, "max": vdd}
    if not any("power" in v and v["power"] == 0 for v in out.values()):
        raise CheckError(f"the top has no ground port {GROUND_PORTS}")
    if not any("power" in v and v["power"] > 0 for v in out.values()):
        raise CheckError(f"the top has no supply port {POWER_PORTS}")
    return out


def run_mode(ws: Path, mode: str, top: str, flat: str, paths: dict,
             pins: dict, where: str) -> tuple[list[dict], dict]:
    """One CVC run: (violations, facts)."""
    devices, ports, cdl = erc.to_cdl(flat, top)
    for dev in devices.values():
        dev["name"] = paths.get(dev["name"], dev["name"])
    ground = {n for n, v in pins.items() if v.get("power") == 0}
    missing = [n for n in pins if n.lstrip("/") not in
               {*ports, *(net for d in devices.values()
                          for net in d["nets"].values())}]
    if missing:
        raise CheckError(f"{mode}: the power file names {sorted(missing)}, "
                         "which no device or port of the flat netlist has")
    log, err = erc.run_cvc(ws / "log" / "top_erc" / mode, top, cdl,
                           erc.model_file(devices), erc.power_file(pins),
                           timeout=900.0)
    counts = erc.parse_counts(log)
    violations = erc.substrate_findings(top, devices, pins, where) + \
        erc.findings(ws, top, erc.parse_errors(err), counts, devices, [],
                     erc.load_waivers(ws), where)
    for v in violations:
        v["check"] = TOOL
        v["mode"] = mode
        if v["kind"] == "leak":
            v["severity"] = "error"
    facts = {"devices": len({d["name"] for d in devices.values()}),
             "grounds": sorted(ground),
             "cvc_counts": {k: c for k, c in counts.items() if c}}
    return violations, facts


def hold_crossings(design: dict, spec: dict, interface: dict,
                   library: dict) -> tuple[dict, list[str]]:
    """(a copy of `design` with each d2a crossing pin of the macro cut from
    the digital net that drives it, the flat names of the cut pins). The pin
    is then a net of its own for the power file to hold; holding the driven
    net instead would have CVC report the driving buffer's pull-up as a leak
    into a rail that is not there."""
    conns = {name: dict(c) for _cell, name, c in design["instances"]}
    held = []
    for sig in interface.get("signals") or []:
        if sig.get("direction") != "d2a":
            continue
        found = False
        for macro in spec.get("macros") or []:
            inst = macro["instance"]
            ports = library[macro["cell"].lower()][0]
            for pin, port in (macro.get("pins") or {}).items():
                if port != sig["name"]:
                    continue
                if pin not in conns.get(inst, {}):
                    raise CheckError(f"{inst} has no pin {pin} in the powered "
                                     "netlist, for interface signal "
                                     f"{sig['name']}")
                found = True
                conns[inst][pin] = ""
                bits = [pin] if pin in ports else _macro_pin_bits(pin, ports)
                held += [f"{inst}{SEP}{b}" for b in bits]
        if not found:
            raise CheckError(f"interface signal {sig['name']} (d2a) is bound "
                             "to no macro pin in top/spec/spec.yaml - the "
                             "held run cannot hold it")
    instances = [(cell, name, conns[name])
                 for cell, name, _c in design["instances"]]
    return {**design, "instances": instances}, sorted(held)


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="msde block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    topdir = ws / check_top_harden.TOP_DIR
    spec_path = topdir / "spec" / "spec.yaml"
    if not spec_path.is_file():
        raise CheckError(f"no assembled top at {topdir} - has the top_harden "
                         "gate run?")
    spec = speclib.load_spec(spec_path)
    top = ttlib.wrapper_name(spec)
    final_dir = topdir / "harden" / "runs" / "run" / "final"
    pnl = final_dir / "pnl" / f"{top}.pnl.v"
    macros = spec.get("macros") or []
    macro_spice = [topdir / m["files"]["spice"] for m in macros]
    analog_spec = ws / "analog" / "spec" / "spec.yaml"
    interface_path = ws / "interface.yaml"
    for label, p in (("powered netlist", pnl), ("analog spec", analog_spec),
                     ("interface.yaml", interface_path),
                     *(("macro .subckt", s) for s in macro_spice)):
        if not p.is_file():
            raise CheckError(f"no {label} at {p} - has top_harden run?")
    vdd = (speclib.load_spec(analog_spec).get("supply") or {}).get("vdd")
    if isinstance(vdd, bool) or not isinstance(vdd, (int, float)) or vdd <= 0:
        raise CheckError(f"{analog_spec} has no positive supply.vdd")
    vdd = float(vdd)
    interface = yaml.safe_load(interface_path.read_text(encoding="utf-8")) or {}

    pdk_root = check_lvs._pdk_root()
    library = parse_subckts((pdk_root / ttlib.PDK_NAME /
                             check_lvs.PDK_SPICE_MODELS[0]).read_text(
                                 errors="replace"))
    for path in macro_spice:
        library.update(parse_subckts(path.read_text(errors="replace")))

    design = parse_powered_netlist(pnl.read_text(errors="replace"))
    ground = GROUND_PORTS[0]
    flat, paths = flatten(design, library, ground)
    where = str(pnl.relative_to(ws))

    violations, modes = [], {}
    pins = rail_voltages(design["ports"], vdd)
    v, modes["operating"] = run_mode(ws, "operating", design["top"], flat,
                                     paths, pins, where)
    violations += v

    held_design, held_nets = hold_crossings(design, spec, interface, library)
    held_flat, held_paths = flatten(held_design, library, ground)
    held = dict(pins)
    for port in RESET_PORTS:
        if port in held:
            held[port] = {"power": 0.0}
    held.update({f"/{net}": {"power": 0.0} for net in held_nets})
    v, modes["held"] = run_mode(ws, "held", design["top"], held_flat,
                                 held_paths, held, where)
    violations += v

    for macro, path in zip(macros, macro_spice):
        cell = macro["cell"]
        ports = library[cell.lower()][0]
        power = macro.get("power") or {}
        if not (power.get("vdd") in ports and power.get("vss") in ports):
            raise CheckError(f"macro {cell} names no usable power pins: "
                             f"spec power {power}, ports {ports}")
        alone = {"top": cell, "ports": {p: "inout" for p in ports},
                 "widths": {}, "assigns": [],
                 "instances": [(cell, macro["instance"],
                                {p: p for p in ports})]}
        macro_flat, macro_paths = flatten(alone, library, power["vss"])
        macro_pins = {p: {"min": 0.0, "max": vdd} for p in ports}
        macro_pins[power["vdd"]] = {"power": vdd}
        macro_pins[power["vss"]] = {"power": 0.0}
        v, modes[f"analog:{cell}"] = run_mode(
            ws, f"analog_{cell}", cell, macro_flat, macro_paths, macro_pins,
            str(path.relative_to(ws)))
        violations += v

    payload = checklib.report(SCRIPT, pnl, violations, top=top, vdd=vdd,
                              modes=modes, report="log/top_erc")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""check_split.py - the split gate (docs/design.md 1.5 msde table, "### M10.").

    check_split.py --workspace DIR [--out FILE]

Reads <workspace>/interface.yaml (every crossing signal, with its direction,
level, domain, width and load - docs/design.md section 5: "The splitter
writes interface.yaml, every crossing signal with its direction, level,
domain and load, and two specs carrying the same entries, and split checks
they agree") plus the two side specs that must carry the same entries,
<workspace>/digital_spec.yaml and <workspace>/analog_spec.yaml, each an
`interface:` list of the same shape.

Typed interface (#110, engine/lib/boundary.py): interface.yaml is `version:
2`, each `level` is a name in engine/reference/levels.yaml and each `load` is
`{cap_ff: N}` or `{pin: <cell>/<pin>}`. Findings: `interface_untyped`,
`level_unknown`, `load_untyped`. The released blocks boundary.V1_BLOCKS names
may stay on version 1 until they are measured.

The nested digital spec: when digital/spec/spec.yaml exists, its `ports` must
carry every crossing signal, a2d as an input and d2a as an output, at the
interface's width (`nested_port_missing`, `nested_direction_mismatch`,
`nested_width_mismatch`; a spec with no `ports` is `nested_ports_missing`).
The nested analog spec has no port list; its `boundary:` section is read and
held to the crossing pins by the boundary gate.

Passes when every interface.yaml signal appears, by name, in both
digital_spec.yaml's and analog_spec.yaml's `interface:` lists, with matching
direction, level, domain, width and load - every field is required on an
interface.yaml entry (a name-only entry is refused, not compared None ==
None against an equally bare side entry). Fault this gate must catch
(gates.yaml): "a control word width that differs between the two".

Analog pads (Tiny Tapeout analog tile). An analog pin that leaves the chip
on a ua pad is not a crossing signal: top_harden and precheck route pads
only from interface.yaml's top-level `ua_pins: {<pin>: <k>}` map
(check_top_harden.py), so a pad listed under `signals:` is joined to the
digital side instead and nothing reaches its pad. The analog side declares
its pads as `pads: [<pin>, ...]` in analog_spec.yaml. The gate fails with
`pad_in_signals` when an interface.yaml signal is a ua_pins key, one of the
analog side's `pads`, or named like a pad (`ua`, `ua[k]`, `ua_*`, `uaK`),
and with `pad_missing_from_ua_pins` when a pad the analog side declares is
not in ua_pins. A malformed `ua_pins` (not a mapping) or `pads` (not a list
of names) is refused (exit 2).

Async crossings (#169). The digital side's cdc gate takes an input port
it is not told about as synchronous to its primary clock, so an
asynchronous analog output sampled by one flop would read clean there.
Every interface.yaml signal that is `a2d` with domain `clk_free` and is not
one of the digital spec's clock domains (a clock root, like a PLL's
vco_out) is an async port (`async_ports`), and the digital side's
spec/cdc.yaml (`digital/spec/cdc.yaml`, written from these by
split_cdc.py) must declare each one `async` in its `ports`: one missing or
given a clock domain fails as `cdc_async_undeclared`, and a `ports: async`
entry for an interface signal whose domain is a clock fails as
`cdc_async_contradicts_interface`. That is checked once the nested digital
workspace has its spec (`digital/spec/spec.yaml`); before it, the report's
`cdc.checked` is false with the reason, and the digital spec's arrival
stales this gate (invalidation.yaml), so release still needs it checked.

CLI/exit contract: checklib's (argparse, JSON to stdout or --out, exit 0
pass, 1 violations, 2 error with a remediation string).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(ENGINE / "lib"))
import boundary  # noqa: E402
import checklib  # noqa: E402
from checklib import CheckError  # noqa: E402

import yaml  # noqa: E402

SCRIPT = "check_split"
INTERFACE_REL = "interface.yaml"
SIDE_SPECS = {"digital": "digital_spec.yaml", "analog": "analog_spec.yaml"}
# The fields a crossing signal carries (docs/design.md section 5: "every
# crossing signal with its direction, level, domain and load"). `width` is
# not named in that sentence, but it is the field gates.yaml's own named
# fault for this gate turns on ("a control word width that differs between
# the two"), so it is checked and required with the same weight as the four
# the prose does name.
SIGNAL_FIELDS = ("direction", "level", "domain", "width", "load")
# The nested digital workspace's spec and cdc file (#169).
DIGITAL_NESTED_SPEC = "digital/spec/spec.yaml"
DIGITAL_CDC = "digital/spec/cdc.yaml"
# interface.yaml's domain for an analog output with no clock (splitter.md)
ASYNC_DOMAIN = "clk_free"
# A name that is itself a Tiny Tapeout analog pad: ua, ua[k], ua_*, uaK.
UA_PAD_RE = re.compile(r"^ua(\[\d+\]|_.*|\d+)?$", re.I)


def _load_yaml_mapping(path: Path, what: str) -> dict:
    if not path.is_file():
        raise CheckError(f"no {what} at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CheckError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckError(f"{path} must be a YAML mapping, got "
                         f"{type(data).__name__}")
    return data


def load_interface(path: Path) -> dict[str, dict]:
    """{signal_name: {direction, level, domain, width, load}} from
    interface.yaml. Duplicate, malformed, or incomplete entries are refused
    (CheckError) - interface.yaml is the splitter's own output and an entry
    missing one of its required fields (or carrying only a name) means the
    split step itself did not finish, not a finding this gate should report.
    Without this, an entry with only a name would compare None == None
    against a side spec missing the same fields and pass silently."""
    data = _load_yaml_mapping(path, "interface.yaml")
    signals = data.get("signals")
    if not isinstance(signals, list) or not signals:
        raise CheckError(f"{path}: no non-empty 'signals' list")
    out: dict[str, dict] = {}
    for i, sig in enumerate(signals):
        if not isinstance(sig, dict):
            raise CheckError(f"{path}: signals[{i}] is not a mapping")
        name = sig.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CheckError(f"{path}: signals[{i}] has no non-empty 'name'")
        if name in out:
            raise CheckError(f"{path}: signal {name!r} appears more than once")
        missing = [f for f in SIGNAL_FIELDS if sig.get(f) is None]
        if missing:
            raise CheckError(
                f"{path}: signal {name!r} is missing {', '.join(missing)}")
        out[name] = {k: sig.get(k) for k in SIGNAL_FIELDS}
    return out


def load_side_interface(path: Path, side: str) -> dict[str, dict]:
    """{signal_name: {direction, level, domain, width, load}} from a side
    spec's own `interface:` list - same shape as interface.yaml's, read
    leniently (a side spec missing the whole section, or an entry missing a
    field, is a finding - a mismatch against interface.yaml's now-required
    fields - not a refusal: the splitter may not have reached that side
    yet)."""
    if not path.is_file():
        return {}
    data = _load_yaml_mapping(path, f"{side} spec")
    entries = data.get("interface")
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict] = {}
    for sig in entries:
        if not isinstance(sig, dict):
            continue
        name = sig.get("name")
        if isinstance(name, str) and name.strip():
            out[name] = {k: sig.get(k) for k in SIGNAL_FIELDS}
    return out


def typed_findings(path: Path, interface: dict[str, dict], ws: Path,
                   levels: dict) -> list[tuple[str, str, str]]:
    """(kind, signal, msg) for each place interface.yaml is not the typed
    version 2 form (engine/lib/boundary.py). A released block named in
    boundary.V1_BLOCKS may stay on version 1."""
    version = boundary.interface_version(_load_yaml_mapping(path, "interface.yaml"))
    if version != boundary.TYPED_VERSION:
        if boundary.is_v1_block(ws):
            return []
        return [("interface_untyped", INTERFACE_REL,
                 f"interface.yaml is version {version!r}: it must be version "
                 f"{boundary.TYPED_VERSION}, with each level named in "
                 "levels.yaml and each load typed")]
    out = []
    for name, sig in sorted(interface.items()):
        if sig["level"] not in levels:
            out.append(("level_unknown", name,
                        f"{name!r} level {sig['level']!r} is not in "
                        f"levels.yaml ({sorted(levels)})"))
        problem = boundary.load_problem(sig["load"])
        if problem:
            out.append(("load_untyped", name, f"{name!r}: {problem}"))
    return out


def nested_port_findings(interface: dict[str, dict],
                         path: Path) -> list[tuple[str, str, str]]:
    """(kind, signal, msg) for each crossing signal the nested digital
    spec's `ports` disagrees with: a2d is one of its inputs, d2a an output."""
    ports = _load_yaml_mapping(path, "digital spec").get("ports")
    if not isinstance(ports, dict):
        return [("nested_ports_missing", DIGITAL_NESTED_SPEC,
                 f"{DIGITAL_NESTED_SPEC} has no 'ports' mapping to compare "
                 "the crossing signals with")]
    out = []
    for name, sig in sorted(interface.items()):
        port = ports.get(name)
        if not isinstance(port, dict):
            out.append(("nested_port_missing", name,
                        f"{name!r} is in interface.yaml but not in "
                        f"{DIGITAL_NESTED_SPEC}'s ports"))
            continue
        want = {"a2d": "input", "d2a": "output"}.get(sig["direction"])
        if port.get("dir") != want:
            out.append(("nested_direction_mismatch", name,
                        f"{name!r} is {sig['direction']} in interface.yaml, so "
                        f"{DIGITAL_NESTED_SPEC} must have it as {want}, not "
                        f"{port.get('dir')!r}"))
        if port.get("width") != sig["width"]:
            out.append(("nested_width_mismatch", name,
                        f"{name!r} width disagrees: interface.yaml says "
                        f"{sig['width']!r}, {DIGITAL_NESTED_SPEC} says "
                        f"{port.get('width')!r}"))
    return out


def load_ua_pins(path: Path) -> set[str]:
    """Lower-cased pin names of interface.yaml's optional `ua_pins` map -
    the analog pins that leave the chip on a ua pad (check_top_harden.py
    reads the same map). Anything but a mapping is refused."""
    ua = _load_yaml_mapping(path, "interface.yaml").get("ua_pins")
    if ua is None:
        return set()
    if not isinstance(ua, dict):
        raise CheckError(f"{path}: 'ua_pins' must be a mapping "
                         "{<analog pin>: <ua pad index>}")
    return {str(p).lower() for p in ua}


def load_analog_pads(path: Path) -> set[str]:
    """Lower-cased names in analog_spec.yaml's optional `pads:` list - the
    analog pins the analog side says go out on a ua pad."""
    if not path.is_file():
        return set()
    pads = _load_yaml_mapping(path, "analog spec").get("pads")
    if pads is None:
        return set()
    if not isinstance(pads, list) or not all(
            isinstance(p, str) and p.strip() for p in pads):
        raise CheckError(f"{path}: 'pads' must be a list of analog pin names")
    return {p.lower() for p in pads}


def digital_clock_domains(path: Path) -> set[str]:
    """The nested digital spec.yaml's clock.domains (its clock ports)."""
    clock = _load_yaml_mapping(path, "digital spec").get("clock")
    domains = clock.get("domains") if isinstance(clock, dict) else None
    return {d for d in domains or [] if isinstance(d, str)}


def async_ports(interface: dict[str, dict], clocks: set[str]) -> dict:
    """{port: 'async'}: each a2d clk_free signal that is not a digital clock
    domain - what the digital side's spec/cdc.yaml `ports` must declare."""
    return {name: "async" for name, sig in sorted(interface.items())
            if sig["direction"] == "a2d" and sig["domain"] == ASYNC_DOMAIN
            and name not in clocks}


def load_cdc_ports(path: Path) -> dict:
    """spec/cdc.yaml's `ports` ({} when the file or the key is absent);
    anything but a mapping of names is refused."""
    if not path.is_file():
        return {}
    ports = _load_yaml_mapping(path, "spec/cdc.yaml").get("ports")
    if ports is None:
        return {}
    if not isinstance(ports, dict):
        raise CheckError(f"{path}: 'ports' must be a mapping "
                         "{<port>: <domain>|async}")
    return ports


def cdc_findings(interface: dict[str, dict], clocks: set[str],
                 ports: dict) -> list[tuple[str, str, str]]:
    """(kind, signal, msg) for each place spec/cdc.yaml's `ports` and the
    interface's async a2d signals disagree (module docstring)."""
    out = []
    for name in async_ports(interface, clocks):
        if ports.get(name) != "async":
            got = ("does not declare it" if name not in ports
                   else f"says {ports[name]!r}")
            out.append(("cdc_async_undeclared", name,
                        f"{name!r} is an async a2d signal (interface.yaml "
                        f"domain {ASYNC_DOMAIN}) but {DIGITAL_CDC} {got}: "
                        f"its ports must say '{name}: async' (split_cdc.py "
                        "writes it), or the cdc gate reads the crossing as "
                        "synchronous"))
    for name, dom in sorted(ports.items()):
        sig = interface.get(name)
        if dom == "async" and sig and sig["domain"] != ASYNC_DOMAIN:
            out.append(("cdc_async_contradicts_interface", name,
                        f"{DIGITAL_CDC} declares {name!r} async but "
                        f"interface.yaml puts it in domain {sig['domain']!r}:"
                        " one of the two is wrong"))
    return out


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    interface_path = ws / INTERFACE_REL
    interface = load_interface(interface_path)

    sides = {side: load_side_interface(ws / rel, side)
             for side, rel in SIDE_SPECS.items()}

    violations = []

    def bad(kind: str, refs: list[str], msg: str):
        violations.append(checklib.violation(
            "split", "error", INTERFACE_REL, None, kind, refs, msg, "check_split"))

    for kind, name, msg in typed_findings(interface_path, interface, ws,
                                          boundary.load_levels()):
        bad(kind, [name], msg)

    for name, canon in sorted(interface.items()):
        for side, entries in sides.items():
            if name not in entries:
                bad("signal_missing_from_spec", [name],
                   f"{name!r} is in interface.yaml but not in the {side} "
                   f"spec's 'interface' list")
                continue
            side_entry = entries[name]
            for field in SIGNAL_FIELDS:
                want, got = canon.get(field), side_entry.get(field)
                if field == "load":
                    want = boundary.comparable_load(want)
                    got = boundary.comparable_load(got)
                if want != got:
                    kind = f"{field}_mismatch"
                    bad(kind, [name],
                       f"{name!r} {field} disagrees: interface.yaml says "
                       f"{want!r}, the {side} spec says {got!r}")

    interface_names = set(interface)
    for side, entries in sides.items():
        extra = sorted(set(entries) - interface_names)
        for name in extra:
            bad("signal_not_declared", [name],
               f"{name!r} is in the {side} spec's 'interface' list but not "
               "in interface.yaml")

    # A pad is routed from ua_pins only; as a crossing signal it would be
    # joined to the digital side and never reach its pad (task brief: "the
    # split gate fails ... when a pad or ua_* pin appears in interface.yaml's
    # signals, or when an analog pad the analog side declares is missing
    # from ua_pins").
    ua_pins = load_ua_pins(interface_path)
    pads = load_analog_pads(ws / SIDE_SPECS["analog"])
    for name in sorted(interface):
        low = name.lower()
        if low in ua_pins or low in pads or UA_PAD_RE.match(name):
            bad("pad_in_signals", [name],
               f"{name!r} is an analog pad, not a crossing signal: take it "
               "out of interface.yaml's signals (and both side specs' "
               "'interface' lists) and map it in interface.yaml's ua_pins "
               "{<pin>: <ua index>} - top_harden routes pads only from there")
    for name in sorted(pads - ua_pins):
        bad("pad_missing_from_ua_pins", [name],
           f"the analog spec declares pad {name!r} but interface.yaml's "
           f"ua_pins does not map it; add '{name}: <ua index>' under ua_pins "
           "or it never reaches a pad")

    if (ws / DIGITAL_NESTED_SPEC).is_file():
        for kind, name, msg in nested_port_findings(interface,
                                                    ws / DIGITAL_NESTED_SPEC):
            bad(kind, [name], msg)
        clocks = digital_clock_domains(ws / DIGITAL_NESTED_SPEC)
        for kind, name, msg in cdc_findings(
                interface, clocks, load_cdc_ports(ws / DIGITAL_CDC)):
            bad(kind, [name], msg)
        cdc = {"checked": True, "file": DIGITAL_CDC,
               "async_ports": sorted(async_ports(interface, clocks))}
    else:
        cdc = {"checked": False, "file": DIGITAL_CDC,
               "async_ports": sorted(async_ports(interface, set())),
               "reason": f"no {DIGITAL_NESTED_SPEC} yet: the digital side's "
                         "cdc ports are checked once it exists"}

    payload = checklib.report(SCRIPT, interface_path, violations,
                              signals=sorted(interface), cdc=cdc)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

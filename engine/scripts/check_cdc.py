#!/usr/bin/env python
"""check_cdc.py - the cdc gate: clock domains from the netlist, every crossing
and every async reset's release synchronised or waived, no glitchy clock gate.

    check_cdc.py --workspace DIR [--out FILE]

Runs yosys over rtl/ to a flat, bit-level netlist before abc (so a gate on a
clock is still the gate the RTL wrote) and hands it to engine/lib/cdclib.py,
whose docstring has the rules. Findings:

  cdc_unsynchronized        a flop samples another domain, no 2-flop chain
  cdc_comb_before_sync      a synchroniser with logic ahead of its first flop
  cdc_async_reset_crossing  a flop's async set/reset reads another domain's flop
  cdc_glitchy_clock_gate    logic on a clock with no glitch-free structure
  cdc_glitchy_clock_mux     a clock mux
  rdc_unsynced_release      a raw reset reaches a domain with no synchroniser
  rdc_comb_reset            a reset net driven by combinational logic
  rdc_nonreset_drives_reset a flop with no reset of its own on an async pin
  cdc_clock_unknown         a flop's clock pin has no driver
  cdc_clock_undeclared      a clock root spec.yaml's clock.domains leaves out
  cdc_port_no_domain        several domains, no primary, no declared domain
  cdc_waiver_unused         a waiver that matches nothing here
  cdc_crosscheck_mismatch   OpenSTA finds other flops on a clock than cdclib

spec/cdc.yaml, optional (a side file, like spec/mutant_rulings.yaml, so a
waiver re-runs this gate and not every gate that reads spec.yaml):

    ports:   {n_sel: async, mosi: sclk}     # default: clock.primary
    waivers:
      - {from: n_sel, to: "u_div.*", reason: "..."}   # a crossing
      - {clock: obs_int, reason: "..."}               # a clock gate or mux
      - {reset: rst_n, domain: clk, reason: "..."}    # a reset, one domain
      - {reset: u_pfd.pfd_clr, reason: "..."}         # a reset, every domain

Reset checks read `ports` too, but only as written: `ports: {rst_n: clk}` says
that port is already synchronous to clk. The clock.primary default a data input
gets is not assumed for a reset.

A Tiny Tapeout tile's pad reset is raw and asynchronous (it comes from the
RP2040), so a port spec.yaml's `tt_pins` maps to `rst_n` or `~rst_n` defaults
to `async` here, with no claim from anyone (#179): read synchronously it is a
crossing, and a one-flop sample of it fails like any other. A `ports` entry for
it still overrides that, as an explicit claim. A tt_pins port on `ui_in` or
`uio_in` keeps the clock.primary default, since what drives those pins is the
board's: the report lists each one under `assumptions`. The report's
`port_domains` says where every port's non-default domain came from, and
`assumed_ports` names every input a flop reads that took the default
(clock.primary, or the one domain) with nothing declaring it (#169): the
coverage gap a reviewer reads, since an async input left there reads clean.
On a /msde block the split gate holds spec/cdc.yaml's `ports` to the
interface's async a2d signals, so those are never among them.

Scope (the report's `scope`). That default is for a block read on its own.
A tile that ships through the generated TT wrapper with the pad reset
synchroniser on (ttlib.pad_reset_latency() > 0) is analysed as it ships:
the wrapper (ttlib.generate_tt_wrapper) and the block together, top the
wrapper, with the wrapper's `rst_n` pin `async` and `clk` the one domain,
so the block's reset is checked as the synchroniser's output
(rst_n_sync_q1/q2) and a block that samples it once more is not flagged
for a crossing the wrapper already closed. That needs the case the
wrapper's names can carry: one clock domain, no waivers in spec/cdc.yaml
(they name block nets) and no macros. A spec/cdc.yaml `ports` entry is
carried onto the wrapper pin bits its tt_pins entry names (`osc_out:
async` on `ui_in[7]` makes `ui_in[7]` async), when it is `async` or the
one domain on a plain ui_in/uio_in pin; one the wrapper can't carry
(inverted, or another pin) keeps the block-alone scope. Any other
tile, and a tile that opts out (`pad_reset_sync: off`, whose wrapper
passes rst_n straight through), is analysed alone with the async default
above, so its rst_n is still checked, never skipped; `scope.why` says
which case it is.

A waiver's names are globs over any alias of the net (buses without their
index); a waived finding stays in the report under `waived`.

NOT CHECKED is its own answer (`applicable: false`, status not_applicable),
never clean. No sequential cell: nothing to check. Sequential cells but no
`clock.domains` in spec.yaml: domains can't be anchored, and release still owes this
gate (attest.py), since only the first is a declared exception. Out of scope,
named in the report's `not_checked`: bus coherence (gray, handshake, MCP),
reconvergence of separately synchronised bits, and the reset items in
NOT_CHECKED below.

Second view: the same netlist mapped to the liberty goes to OpenSTA with a
clock on each root port, and `all_registers -clock` must count the flops
cdclib says that port reaches without passing through a flop.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import cdclib  # noqa: E402
import check_synth  # noqa: E402
import checklib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
import yaml  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_cdc"
EDA_BIN = REPO / "bin" / "eda"
TIMEOUT_S = 600.0
CDC_REL = "spec/cdc.yaml"
NOT_CHECKED = ["bus coherence (gray, handshake, multi-cycle)",
               "reconvergence of separately synchronised bits",
               "reset release order between domains",
               "recovery/removal timing of a synchronised release (STA's)",
               "a data path from a flop with no reset into a flop with one",
               "a raw reset read synchronously, other than tt_pins's pad "
               "reset (a data input: declare it `async` in ports to check it)"]
FLOP_CELL_RE = re.compile(r"__s?(?:dff|lat)")
XCHECK_RE = re.compile(r"^CDC_XCHECK (\S+) (\d+)$", re.M)

CROSSING_KINDS = {
    "unsynchronized": "cdc_unsynchronized",
    "comb_before_sync": "cdc_comb_before_sync",
    "async_reset": "cdc_async_reset_crossing",
}
GATE_KINDS = {"mux": "cdc_glitchy_clock_mux"}
RESET_KINDS = {"unsynced": "rdc_unsynced_release", "comb": "rdc_comb_reset",
               "nonreset": "rdc_nonreset_drives_reset"}


def load_cdc_yaml(ws: Path) -> dict:
    path = ws / CDC_REL
    if not path.is_file():
        return {"ports": {}, "waivers": []}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CheckError(f"{CDC_REL} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"ports", "waivers"}:
        raise CheckError(f"{CDC_REL} must be a mapping of 'ports' and "
                         "'waivers' only")
    ports = data.get("ports") or {}
    waivers = data.get("waivers") or []
    if not isinstance(ports, dict) or not isinstance(waivers, list):
        raise CheckError(f"{CDC_REL}: 'ports' is a mapping, 'waivers' a list")
    for w in waivers:
        if not isinstance(w, dict) or not str(w.get("reason") or "").strip():
            raise CheckError(f"{CDC_REL}: every waiver needs a reason: {w!r}")
        if set(w) == {"clock", "reason"}:
            names = [w["clock"]]
        elif set(w) in ({"reset", "reason"}, {"reset", "domain", "reason"}):
            names = [w["reset"]]
            if not isinstance(w.get("domain", ""), str):
                raise CheckError(f"{CDC_REL}: a waiver's domain is a name: "
                                 f"{w!r}")
        elif set(w) == {"from", "to", "reason"}:
            names = [w["from"], w["to"]]
        else:
            raise CheckError(f"{CDC_REL}: a waiver is {{from, to, reason}}, "
                             f"{{clock, reason}} or {{reset, domain?, reason}}"
                             f": {w!r}")
        if not all(isinstance(n, str) and n.strip("*?") for n in names):
            raise CheckError(f"{CDC_REL}: a waiver must name its nets, not "
                             f"a bare wildcard: {w!r}")
    return {"ports": ports, "waivers": waivers}


def run_yosys(ws: Path, top: str, liberty: Path, work: Path,
              extra: tuple = ()) -> dict:
    """The bit-level netlist as JSON; also writes the mapped work/mapped.v.
    extra: more sources (the generated wrapper), read after rtl/."""
    work.mkdir(parents=True, exist_ok=True)
    reads = []
    for f in [*check_synth.collect_sources(ws), *extra]:
        flag = "-sv " if f.suffix == ".sv" else ""
        reads.append(f"read_verilog {flag}{f.relative_to(ws)}")
    rel = work.relative_to(ws)
    script = work / "cdc.ys"
    script.write_text(f"""\
read_liberty -lib {liberty}
{chr(10).join(reads)}
hierarchy -check -top {top}
proc
flatten
delete t:$scopeinfo
memory
techmap
opt_expr
opt_clean
write_json {rel}/netlist.json
dfflibmap -liberty {liberty}
abc -liberty {liberty}
opt_clean
write_verilog -noattr {rel}/mapped.v
""", encoding="utf-8")
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), "yosys", "-q", "-s", str(script.relative_to(ws))],
            cwd=str(ws), capture_output=True, text=True, encoding="utf-8",
            errors="replace", wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"yosys {checklib.timeout_why(exc)}: {exc}") from exc
    if proc.returncode != 0:
        raise CheckError(f"yosys exited {proc.returncode}: "
                         f"{((proc.stdout or '') + (proc.stderr or ''))[-2000:]}")
    try:
        nl = json.loads((work / "netlist.json").read_text(encoding="utf-8"))
        return nl["modules"][top]
    except (OSError, ValueError, KeyError) as exc:
        raise CheckError(f"yosys wrote no netlist for {top}: {exc}") from exc


def crosscheck(top: str, liberty: Path, work: Path, want: dict,
               roots: list[str]) -> dict:
    """OpenSTA's count of the flops each root port clocks against cdclib's.
    A tool that can't read the netlist says so; it decides nothing."""
    tcl = work / "xcheck.tcl"
    lines = [f"read_liberty {liberty}", f"read_verilog {work / 'mapped.v'}",
             f"link_design {top}"]
    for r in roots:
        lines.append(f"create_clock -name {r} -period 10 [get_ports {{{r}}}]")
        lines.append(f'puts "CDC_XCHECK {r} '
                     f'[llength [all_registers -clock {r}]]"')
    lines.append("exit")
    tcl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        proc = checklib.run_tool([str(EDA_BIN), "sta", str(tcl)],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        return {"tool": "opensta", "ran": False,
                "reason": checklib.timeout_why(exc)}
    got = {k: int(v) for k, v in XCHECK_RE.findall(proc.stdout or "")}
    if set(got) != set(roots):
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-400:]
        return {"tool": "opensta", "ran": False,
                "reason": f"no count for every root: {tail}"}
    expect = {r: want.get(r, 0) for r in roots}
    return {"tool": "opensta", "ran": True, "sta": got, "cdclib": expect,
            "match": got == expect}


def has_sequential_cells(module: dict) -> bool:
    return any(cdclib.YOSYS_FF.match(c["type"])
               or cdclib.YOSYS_LATCH.match(c["type"])
               or FLOP_CELL_RE.search(c["type"])
               for c in (module.get("cells") or {}).values())


def not_applicable_at_release(ws: Path, spec: dict) -> str | None:
    """attest's question: does this block declare that it owes no cdc? Only
    a mapped netlist with no flop in it does; doubt means owed."""
    top = spec.get("top")
    try:
        nl = json.loads((ws / "synth" / f"{top}.json")
                        .read_text(encoding="utf-8"))
        module = nl["modules"][top]
    except (OSError, ValueError, KeyError):
        return None
    if not module.get("cells") or has_sequential_cells(module):
        return None
    return ("not applicable: the synthesized netlist has no sequential cell, "
            "re-read at this release")


def apply_waivers(findings: list[dict], waivers: list[dict]) -> tuple:
    """(open, waived, unused waivers)."""
    used = set()
    still, waived = [], []
    for f in findings:
        hit = None
        for i, w in enumerate(waivers):
            if "clock" in w:
                ok = "gate" in f and cdclib.waiver_matches(
                    w, f["gate"]["aliases"], "clock")
            elif "reset" in w:
                ok = "reset" in f and cdclib.waiver_matches(
                    w, f["reset"]["aliases"], "reset") and (
                    "domain" not in w or all(
                        cdclib.waiver_matches(w, [root], "domain")
                        for root in f["reset"]["domain"]))
            else:
                ok = "crossing" in f and cdclib.waiver_matches(
                    w, f["crossing"]["from_aliases"], "from"
                ) and cdclib.waiver_matches(
                    w, f["crossing"]["to_aliases"], "to")
            if ok:
                used.add(i)
                hit = hit or w
        if hit:
            waived.append({**f, "reason": hit["reason"]})
        else:
            still.append(f)
    return still, waived, [w for i, w in enumerate(waivers) if i not in used]


def _tt_pin(expr) -> str:
    """A tt_pins expr's pad, without its `~` and bit range: rst_n, ui_in..."""
    return str(expr).strip().lstrip("~").strip().split("[")[0].strip()


def port_defaults(spec: dict, cfg_ports: dict, module_ports: dict) -> tuple:
    """(port_domains, rows, board_inputs) from tt_pins and spec/cdc.yaml.

    port_domains is cfg_ports plus `async` for each tt_pins pad reset it does
    not name; rows says where each entry came from; board_inputs maps each
    tt_pins input on ui_in/uio_in left on the default to its pin."""
    tt_pins = spec.get("tt_pins") if isinstance(spec.get("tt_pins"), dict) \
        else {}
    domains = dict(cfg_ports)
    rows = [{"port": p, "domain": d, "source": CDC_REL}
            for p, d in sorted(cfg_ports.items())]
    board_inputs = {}
    for port, expr in sorted(tt_pins.items()):
        info = module_ports.get(port)
        if not info or info.get("direction") != "input" or port in cfg_ports:
            continue
        pad = _tt_pin(expr)
        if pad == "rst_n":
            domains[port] = "async"
            rows.append({"port": port, "domain": "async",
                         "source": f"tt_pins: {port} -> {str(expr).strip()} "
                                   "(the tile's pad reset)"})
        elif pad in ("ui_in", "uio_in"):
            board_inputs[port] = str(expr).strip()
    return domains, rows, board_inputs


def _pin_bits(expr) -> set[str]:
    """The wrapper pin bits a tt_pins expr names: {`ui_in[7]`, ...}."""
    pin = ttlib.parse_pin_expr(expr)
    if pin["hi"] is None:
        return {pin["base"]}
    return {f"{pin['base']}[{b}]" for b in range(pin["lo"], pin["hi"] + 1)}


def wrapper_port_domains(spec: dict, cfg_ports: dict,
                         declared: list) -> tuple[dict, list]:
    """(pin_domains, blocked): spec/cdc.yaml's `ports` as the generated
    wrapper's pins carry them, `async` or the block's one domain (the
    wrapper's clk) per wrapper pin bit (`ui_in[7]`) its tt_pins entry names;
    blocked lists each entry the wrapper's names can't carry (not on a
    tt_pins input pad, inverted, or another domain)."""
    tt_pins = spec.get("tt_pins") if isinstance(spec.get("tt_pins"), dict) \
        else {}
    pins, blocked = {}, []
    for port, dom in sorted(cfg_ports.items()):
        try:
            pin = ttlib.parse_pin_expr(tt_pins[port]) if port in tt_pins \
                else None
        except ttlib.TTError:
            pin = None
        if dom == "async":
            want = "async"
        elif len(declared) == 1 and dom == declared[0]:
            want = "clk"
        else:
            want = None
        if pin is None or pin["invert"] or want is None \
                or pin["base"] not in ("ui_in", "uio_in"):
            blocked.append(f"{CDC_REL} ports {port}: {dom} is not on a "
                           "ui_in/uio_in pin")
            continue
        for bit in range(pin["lo"], pin["hi"] + 1):
            pins[f"{pin['base']}[{bit}]"] = want
    return pins, blocked


def analysis_scope(spec: dict, cfg: dict, declared: list) -> tuple:
    """(wrapper, why): wrapper True when the tile is analysed through the
    generated TT wrapper with its pad reset synchroniser (see the module
    docstring's Scope), else False and the block alone."""
    try:
        latency = ttlib.pad_reset_latency(spec)
        opt_out = ttlib.pad_reset_opt_out(spec)
    except ttlib.TTError as exc:
        raise CheckError(f"spec.yaml: {exc}") from exc
    if opt_out is not None:
        return False, ("the tile opts out of the pad reset synchroniser "
                       f"(pad_reset_sync off: {opt_out}): its rst_n reaches "
                       "the block raw, checked as an async input")
    if not latency:
        return False, ("no pad reset synchroniser in a generated wrapper (no "
                       "input on rst_n, or no clk to sample it): the block "
                       "alone")
    blocked = [why for bad, why in (
        (len(declared) != 1, "more than one clock domain"),
        (bool(cfg["waivers"]), f"{CDC_REL} has waivers"),
        (bool(spec.get("macros")), "the tile has macros")) if bad]
    blocked += wrapper_port_domains(spec, cfg["ports"], declared)[1]
    if blocked:
        return False, ("the block alone, rst_n async (the wrapper scope takes "
                       "one domain and block-free names; here: "
                       f"{', '.join(blocked)})")
    return True, (f"the generated wrapper {ttlib.wrapper_name(spec)} and the "
                  "block: rst_n through the wrapper's "
                  f"{ttlib.pad_reset_latency(spec)}-flop pad reset "
                  "synchroniser on clk")


def wrapper_ports(spec: dict) -> dict:
    """spec.yaml's ports as port_defaults reads a netlist's: name ->
    {direction}, for the ports tt_pins wires to a pin."""
    return {n: {"direction": "input" if p.get("dir") == "input" else "output"}
            for n, p in (spec.get("ports") or {}).items()
            if isinstance(p, dict)}


def board_assumptions(board_inputs: dict, an) -> list[str]:
    """The report's line for each ui_in/uio_in data input taken as
    synchronous to the default domain (a clock root is not a data input)."""
    if an.primary:
        where = f"clock.primary ({an.primary})"
    elif len(an.all_roots) == 1:
        where = f"the only clock domain ({an.all_roots[0]})"
    else:
        return []   # no default: cdc_port_no_domain reports a read one
    return [f"{port} (tt_pins {pin}) is taken as synchronous to {where}: "
            f"what drives it is the board's; declare it in {CDC_REL} ports "
            "if it is not"
            for port, pin in sorted(board_inputs.items())
            if port not in an.all_roots]


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    cfg = load_cdc_yaml(ws)
    clock = spec.get("clock") if isinstance(spec.get("clock"), dict) else None
    if clock is not None and not clock.get("domains"):
        clock = None
    declared = [d for d in (clock or {}).get("domains") or []
                if isinstance(d, str)]
    wrapped, scope_why = analysis_scope(spec, cfg, declared)
    liberty = check_synth.liberty_path(check_synth.toolchain_root(),
                                       check_synth.choose_std_cell(spec))
    if not liberty.is_file():
        raise CheckError(f"liberty file not found: {liberty}")
    work = ws / "log" / "cdc"
    block_top = top
    scope = {"scope": "wrapper+block" if wrapped else "block",
             "why": scope_why}
    extra = ()
    if wrapped:
        top = ttlib.wrapper_name(spec)
        work.mkdir(parents=True, exist_ok=True)
        wrapper = work / f"{top}.v"
        try:
            wrapper.write_text(ttlib.generate_tt_wrapper(spec),
                               encoding="utf-8")
        except ttlib.TTError as exc:
            raise CheckError(f"spec.yaml: {exc}") from exc
        extra = (wrapper,)
    module = run_yosys(ws, top, liberty, work, extra)
    if not has_sequential_cells(module):
        reason = "no sequential cell in the netlist: nothing to check"
    elif clock is None:
        reason = ("sequential cells but no clock.domains in spec.yaml: domains and "
                  "input ports can't be anchored. NOT CHECKED, not clean - "
                  "add clock: {period_ns, domains, primary?}")
    else:
        reason = None
    if reason:
        payload = checklib.report(SCRIPT, ws / "rtl", [], top=top,
                                  applicable=False, checked=False,
                                  reason=reason, scope=scope)
        return payload, args.out

    primary = clock.get("primary")
    if wrapped:
        # the wrapper's pins: clk is the block's one domain (pad_reset_latency
        # says the primary clock enters there), rst_n the raw pad
        _, _, board_inputs = port_defaults(spec, {}, wrapper_ports(spec))
        pins, _ = wrapper_port_domains(spec, cfg["ports"], declared)
        declared, primary = ["clk"], "clk"
        port_domains = {"rst_n": "async", **pins}
        port_rows = [{"port": "rst_n", "domain": "async",
                      "source": f"the tile's pad reset, into {top}'s "
                                "synchroniser"}]
        port_rows += [{"port": pin, "domain": pins[pin],
                       "source": f"{CDC_REL} ports {port} -> tt_pins "
                                 f"{spec['tt_pins'][port]}"}
                      for port in sorted(cfg["ports"])
                      for pin in sorted(_pin_bits(spec["tt_pins"][port]))]
    else:
        port_domains, port_rows, board_inputs = port_defaults(
            spec, cfg["ports"], module["ports"])
    an = cdclib.Analysis(module, primary=primary,
                         port_domains=port_domains, clock_ports=declared)
    # wrapped: wrapper_port_domains already held each entry to a tt_pins
    # input pin and `async` or the one domain
    for port, dom in ({} if wrapped else cfg["ports"]).items():
        if port not in module["ports"]:
            raise CheckError(f"{CDC_REL}: ports names {port!r}, not a port "
                             f"of {top}")
        if dom != "async" and dom not in an.all_roots:
            raise CheckError(f"{CDC_REL}: port {port!r} is in domain {dom!r}, "
                             f"not one of {an.all_roots} or 'async'")

    assumptions = board_assumptions(board_inputs, an)
    crossings, no_domain = an.crossings()
    gates = an.gate_rows()
    violations = []

    def finding(kind, msg, **extra):
        return {"kind": kind, "msg": msg, **extra}

    open_items = []
    for c in crossings:
        if c["status"] == "synchronized":
            continue
        open_items.append(finding(
            CROSSING_KINDS[c["status"]],
            f"{c['from']} ({'/'.join(c['from_domain'])}) -> {c['to']} "
            f"({'/'.join(c['to_domain'])}): {c['status'].replace('_', ' ')}, "
            f"{c['bits']} bit path(s)", crossing=c))
    for g in gates:
        if not g["safe"]:
            open_items.append(finding(
                GATE_KINDS.get(g["kind"], "cdc_glitchy_clock_gate"),
                f"clock net {g['net']}: {g['why']}", gate=g))
    reset_trees, reset_findings = an.reset_report()
    for r in reset_findings:
        where = "/".join(r["domain"])
        clocks = ", ".join(r["clock_nets"])
        text = {
            "unsynced": f"reset {r['net']} releases {r['flops']} flop(s) in "
                        f"domain {where} (clock nets {clocks}) with no reset "
                        "synchroniser in that domain",
            "comb": f"reset net {r['net']} is driven by combinational logic "
                    f"and resets {r['flops']} flop(s) in domain {where}: a "
                    "glitch on it resets them",
            "nonreset": f"flop {r['net']} has no async reset of its own and "
                        f"drives the async pin of {r['flops']} flop(s) in "
                        f"domain {where}: unknown until its first clock",
        }[r["kind"]]
        open_items.append(finding(RESET_KINDS[r["kind"]], text, reset=r))
    still, waived, unused = apply_waivers(open_items, cfg["waivers"])
    for f in still:
        extra = {k: f[k] for k in ("crossing", "gate", "reset") if k in f}
        violations.append(checklib.violation(
            "cdc", "error", None, top, f["kind"], [], f["msg"], "cdclib",
            **extra))
    for w in unused:
        violations.append(checklib.violation(
            "cdc", "error", CDC_REL, top, "cdc_waiver_unused", [],
            f"waiver {json.dumps(w)} matches no finding: it would hide "
            "whatever matches it next - remove it or fix its names",
            "cdclib"))
    for port in no_domain:
        violations.append(checklib.violation(
            "cdc", "error", None, top, "cdc_port_no_domain", [],
            f"input {port} is read by a flop, the design has domains "
            f"{an.all_roots} and spec.yaml names no clock.primary - declare "
            f"it in {CDC_REL} ports", "cdclib"))
    for root in an.all_roots:
        if root == cdclib.UNKNOWN_ROOT:
            violations.append(checklib.violation(
                "cdc", "error", None, top, "cdc_clock_unknown", [],
                "a flop's clock pin traces back to no port, flop or macro",
                "cdclib"))
        elif root in module["ports"] and root not in declared:
            violations.append(checklib.violation(
                "cdc", "error", None, top, "cdc_clock_undeclared", [],
                f"port {root} clocks flops but spec.yaml's clock.domains "
                f"{declared} leaves it out", "cdclib"))

    port_roots = [r for r in an.all_roots if r in module["ports"]]
    xcheck = crosscheck(top, liberty, work, an.direct_flop_counts(),
                        port_roots)
    if xcheck.get("ran") and not xcheck["match"]:
        violations.append(checklib.violation(
            "cdc", "error", None, top, "cdc_crosscheck_mismatch", [],
            f"flops per root port: OpenSTA {xcheck['sta']}, cdclib "
            f"{xcheck['cdclib']} - one of them misread the clock network",
            "opensta"))

    payload = checklib.report(
        SCRIPT, ws / "rtl", violations, top=top, applicable=True, checked=True,
        domains=an.all_roots, clocks=an.clocks(), crossings=crossings,
        clock_gates=gates, reset_trees=reset_trees,
        reset_syncs=an.reset_syncs(), waived=waived, crosscheck=xcheck,
        port_domains=port_rows, assumed_ports=sorted(an.assumed),
        assumptions=assumptions, scope=scope,
        block=block_top,
        not_checked=NOT_CHECKED)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

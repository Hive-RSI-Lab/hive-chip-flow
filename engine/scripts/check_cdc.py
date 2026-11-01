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
               "a raw reset read synchronously (a data input: declare it "
               "`async` in ports to check it)"]
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


def run_yosys(ws: Path, top: str, liberty: Path, work: Path) -> dict:
    """The bit-level netlist as JSON; also writes the mapped work/mapped.v."""
    work.mkdir(parents=True, exist_ok=True)
    reads = []
    for f in check_synth.collect_sources(ws):
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
    liberty = check_synth.liberty_path(check_synth.toolchain_root(),
                                       check_synth.choose_std_cell(spec))
    if not liberty.is_file():
        raise CheckError(f"liberty file not found: {liberty}")
    work = ws / "log" / "cdc"
    module = run_yosys(ws, top, liberty, work)

    clock = spec.get("clock") if isinstance(spec.get("clock"), dict) else None
    if clock is not None and not clock.get("domains"):
        clock = None
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
                                  reason=reason)
        return payload, args.out

    declared = [d for d in clock.get("domains") or [] if isinstance(d, str)]
    an = cdclib.Analysis(module, primary=clock.get("primary"),
                         port_domains=cfg["ports"], clock_ports=declared)
    for port, dom in cfg["ports"].items():
        if port not in module["ports"]:
            raise CheckError(f"{CDC_REL}: ports names {port!r}, not a port "
                             f"of {top}")
        if dom != "async" and dom not in an.all_roots:
            raise CheckError(f"{CDC_REL}: port {port!r} is in domain {dom!r}, "
                             f"not one of {an.all_roots} or 'async'")

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
        not_checked=NOT_CHECKED)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

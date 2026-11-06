#!/usr/bin/env python
"""check_harden.py - the harden gate (docs/design.md 1.5, "### M4."). Runs
as a job (`jobs.py start --gate harden`, 1.7); this script IS the work a
detached `gate.py` invocation does when it runs.

    check_harden.py --workspace DIR [--out FILE]

Generates `harden/tt_um_<top>.v` and `harden/config.json`/`harden/info.yaml`
from spec.yaml's `tt_pins` (engine/lib/ttlib.py), then runs LibreLane 3
against the vendored TT GF180 template through `bin/eda librelane`
(never a bare `librelane` - docs/design.md 1.2). Passes when the flow
finishes with no failing step and `runs/<tag>/final/` carries GDS, LEF,
netlist, SDF and metrics (gates.yaml's `harden` row). config.json names
no machine's path (ttlib.portable_config): the workspace's files are
LibreLane `dir::` paths relative to harden/, the engine files it needs (the
tile's DEF template, macro_pdn.tcl) are copied beside it, and PDK_ROOT is
`--pdk-root` on the command line, so a committed block reads the same on
every machine.

Per-design config: `harden/config.json` is REGENERATED on every run, so a
hand edit to it is discarded. A design's own LibreLane keys go in
`harden/config.override.json` - a JSON object merged last (e.g.
`{"RUN_POST_GRT_RESIZER_TIMING": 1}` for a small setup miss). It may not
set a key the engine or the TT template owns (tile/PDK keys, VERILOG_FILES,
the template's DO-NOT-CHANGE block, CLOCK_PERIOD - the clock is spec.yaml's):
such a key is a CheckError, exit 2. The file is its own `harden_override`
input in engine/reference/invalidation.yaml, so editing it stales harden.

Clocks: CLOCK_PORT is spec.yaml's primary clock domain (`clock.primary`, or
the one entry of `clock.domains`) mapped through tt_pins, at
clock.period_ns. Anything else - the other domains, generated clocks, false
paths - goes in `harden/constraints.sdc`, written against the TT wrapper's
ports. When it exists, `harden/flow.sdc` is regenerated each run (LibreLane's
base SDC, then that file) and becomes PNR_SDC_FILE and SIGNOFF_SDC_FILE.
CheckError, exit 2, when several domains are named without `clock.primary`
or without that file, or when the file redefines the primary clock or
leaves a domain without a clock (ttlib.prepare_harden_sdc). The file is the
`harden_sdc` input, so editing it stales harden onward (harden_config_edit).

Restart: the run tag is fixed ("run") and every invocation passes
`--overwrite`. LibreLane's own implicit resume (omit `--overwrite`, let it
load `runs/<tag>`'s latest `state_out.json`) was tried here first and is not
safe on the installed LibreLane 3 build - replayed even against a run
directory an EARLIER, fully successful attempt had left in `final/` with
nothing left to do, it still crashed (FileNotFoundError inside
`Step._reroute_env`, a step directory the flow never created, after
climbing well past the original run's own highest step number instead of
recognizing completion). A job killed halfway therefore finishes by redoing
the whole flow, not by continuing from its last completed step - correct
over clever, and on these tiny corpus designs a full run is a few minutes.

Failure classification (docs/design.md 1.9's "a gate that didn't run is a
refusal, never a pass", enforced here three ways):
  - the launcher never reached LibreLane at all (bad tt_pins so no wrapper
    could be generated, `eda` itself missing/misconfigured, a timeout
    before `runs/<tag>/flow.log` even appears) -> CheckError, exit 2.
  - LibreLane ran and a real step failed (nonzero exit, `flow.log` exists)
    -> a `violations` finding, exit 1 - this is the fault gates.yaml names
    ("a design too large for the tile": global placement/legalization
    fails outright).
  - LibreLane exits 0 but `final/` is missing an expected format -> a
    `violations` finding (a launcher/flow that silently drops output is
    exactly the "missing report" case the review calls out), never a
    silent pass.

Signoff metrics: LibreLane only reports antenna violations left after repair,
floating nets, unannotated parasitics, PDN violations and IR drop, and exits 0
whatever they say. `signoff_metrics()` judges them against gates.yaml's
`harden_metrics` limits; the report carries the whole table as
`signoff_metrics`. A metric that is absent or not a finite number is a
`signoff_metric_missing` finding, never read as zero. spec.yaml's optional
`harden_signoff` sets `ir_budget_pct` (default gates.yaml's) and
`floating_nets_waived: {net: reason}`.

Max slew and max cap (top_harden only): `corner_drv_metrics()` judges
gates.yaml's `top_harden_metrics` at every corner the signoff STA wrote, and
names the pins from that corner's checks.rpt. A pin in spec.yaml's
`harden_signoff.drv_pins_waived: {pin: reason}` does not count; a count the
report does not name in full cannot be excused.

Floating nets carry their names in the log of the step that counted them.
TT's repair step counts the power nets, so every TT harden records at least
VPWR and VGND there: a named power net does not count. A count the log does
not name in full (a trimmed run dir) fails, since nothing shows what floats.

Connectivity (#156): that metric is counted once, at global placement, and only
for nets with a load and no driver. So after a finished flow
`read_connectivity()` reads the final ODB itself and `judge_connectivity()`
rules on what it lists: a net with no driver is `net_undriven`, and nothing
excuses it; a net with no load is `net_unloaded`, unless it carries a TT wrapper
input port or `floating_nets_waived` names it. An antenna diode is no load. The
report carries the names as `connectivity`, and the metric row stays as the
cross-check. A flow that exits 0 with no final ODB is
`harden_missing_artifact`; an ODB that OpenROAD can't read is a refusal, never
a pass.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import qorlib  # noqa: E402
import speclib  # noqa: E402
import statelib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_harden"
EDA_BIN = REPO / "bin" / "eda"
RUN_TAG = "run"
# LibreLane genuinely can run for the better part of an hour on a real
# design (docs/design.md section 1); this default is a ceiling against a
# truly hung tool, not a budget - CHIP_FLOW_HARDEN_TIMEOUT_S overrides it
# (tests use a small value to prove a timeout fails the gate, not a real
# one).
DEFAULT_TIMEOUT_S = 3600.0
EXPECTED_FORMATS = ("gds", "lef", "nl", "sdf", "spef", "metrics.json")
GATES_YAML = ENGINE / "reference" / "gates.yaml"
POWER_NETS = {"VPWR", "VGND"}  # ttlib.harden_config's VDD_PIN/GND_PIN
_FLOATING_RE = re.compile(r"found (\d+) floating nets")
_ANTENNA_NET_RE = re.compile(r"^\s*Net:?\s+(\S+)", re.MULTILINE)
CONNECTIVITY_TCL = ENGINE / "lib" / "net_connectivity.tcl"
CONNECTIVITY_TIMEOUT_S = 600.0


def _step_number(path: Path) -> int:
    m = re.match(r"(\d+)-", path.name)
    return int(m.group(1)) if m else -1


def _step_dirs(run_dir: Path, pattern: str) -> list[Path]:
    """A run's top-level step dirs matching pattern, latest last."""
    return sorted((d for d in run_dir.glob(pattern) if d.is_dir()),
                  key=_step_number)


def floating_net_names(run_dir: Path) -> list[str] | None:
    """The nets the latest step to count floating nets named in its log
    (`found N floating nets.` then one indented name per line), or None when
    no log names all N."""
    for step in reversed(_step_dirs(run_dir, "[0-9]*-*")):
        for log in sorted(step.glob("*.log")):
            lines = log.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
            for i, line in reversed(list(enumerate(lines))):
                m = _FLOATING_RE.search(line)
                if not m:
                    continue
                count = int(m.group(1))
                names = [ln.strip() for ln in lines[i + 1:i + 1 + count]]
                if len(names) == count and all(
                        n and ln.startswith(" ") and len(n.split()) == 1
                        for n, ln in zip(names, lines[i + 1:i + 1 + count])):
                    return names
                return None
    return None


def antenna_net_names(run_dir: Path) -> tuple[list[str], str | None]:
    """(nets, report path) from the latest check-antennas step's report."""
    for step in reversed(_step_dirs(run_dir, "[0-9]*-openroad-checkantennas*")):
        rpt = step / "reports" / "antenna.rpt"
        if rpt.is_file():
            text = rpt.read_text(encoding="utf-8", errors="replace")
            return (sorted(set(_ANTENNA_NET_RE.findall(text))),
                    str(rpt.relative_to(run_dir)))
    return [], None


def _number(metrics: dict, name: str) -> float | None:
    v = metrics.get(name)
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v):
        return None
    return v


def signoff_metrics(metrics: dict, signoff: dict, run_dir: Path, top: str,
                    limits: dict | None = None):
    """(table, violations): each of gates.yaml's harden_metrics against its
    limit. signoff is speclib.harden_signoff(spec)."""
    if limits is None:
        limits = yaml.safe_load(
            GATES_YAML.read_text(encoding="utf-8"))["harden_metrics"]
    table, violations = {}, []

    def finding(kind, msg, refs=(), **extras):
        violations.append(checklib.violation(
            "harden", "error", None, top, kind, list(refs), msg,
            "check_harden", **extras))

    for name, rule in limits.items():
        value = _number(metrics, name)
        kind = rule["kind"]
        row = {"value": value}
        table[name] = row
        if value is None:
            row["status"] = "missing"
            finding("signoff_metric_missing",
                    f"metrics.json has no number for {name} "
                    f"({metrics.get(name)!r}): {kind} was not judged",
                    refs=[name], metric=name)
            continue
        if "max_pct_vdd" in rule:
            pct = signoff.get("ir_budget_pct", rule["max_pct_vdd"])
            worst = _number(metrics, rule["vdd_metric"])
            if worst is None:
                row["status"] = "missing"
                finding("signoff_metric_missing",
                        f"metrics.json has no number for {rule['vdd_metric']}"
                        f": cannot size the {pct}% budget for {name}",
                        refs=[rule["vdd_metric"]], metric=rule["vdd_metric"])
                continue
            # the worst rail voltage plus the drop is the rail's nominal
            limit = (worst + value) * pct / 100.0
            msg = (f"{name} {value:g} V is over {pct:g}% of "
                   f"{worst + value:g} V ({limit:g} V)")
        else:
            limit = rule["max"]
            msg = f"{name} is {value:g}, limit {limit:g}"
        row["limit"] = limit
        refs: list[str] = []
        extras: dict = {}
        over = value > limit
        if kind == "floating_net":
            names = floating_net_names(run_dir) if over else []
            row["nets"] = names
            if over and names is None:
                msg += "; no step log names them all, so none can be excused"
            elif over:
                waived = signoff.get("floating_nets_waived", {})
                row["power_nets"] = sorted(POWER_NETS & set(names))
                row["waived"] = sorted(set(names) & set(waived))
                real = sorted(set(names) - POWER_NETS - set(waived))
                row["unused_waivers"] = sorted(set(waived) - set(names))
                over = bool(real)
                refs = real
                msg = f"floating nets: {', '.join(real)}"
        elif kind == "antenna_violation" and over:
            refs, report = antenna_net_names(run_dir)
            extras = {"report": report}
            if refs:
                msg += f": {', '.join(refs)}"
            elif report:
                msg += f"; {report} names none"
            else:
                msg += "; no check-antennas report in the run"
        row["status"] = "over" if over else "ok"
        if over:
            finding(kind, msg, refs=refs or [name], metric=name, value=value,
                    limit=limit, **extras)
    return table, violations


def parse_connectivity(text: str) -> tuple[int, list[dict]] | None:
    """(signal nets read, the rows net_connectivity.tcl listed), or None when
    its end row is missing: a list cut short says nothing about the nets it
    did not reach."""
    rows, seen = [], None
    for line in text.splitlines():
        field = line.split("\t")
        if field[0] == "CONN_NET" and len(field) == 5:
            rows.append({"net": field[1].replace("\\", ""),
                         "drivers": int(field[2]), "loads": int(field[3]),
                         "inputs": [p.replace("\\", "")
                                    for p in field[4].split(",") if p]})
        elif field[0] == "CONN_END" and len(field) == 2:
            seen = int(field[1])
    return None if seen is None else (seen, rows)


def read_connectivity(odb: Path, cwd: Path) -> tuple[int, list[dict]]:
    """net_connectivity.tcl's answer on a final ODB. An answer that can't be
    had raises: the check did not run, and that is never a pass."""
    cmd = [str(EDA_BIN), "openroad", "-no_splash", "-exit",
           str(CONNECTIVITY_TCL)]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=CONNECTIVITY_TIMEOUT_S,
                              env={**os.environ,
                                   "CONNECTIVITY_ODB": str(odb)})
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"the connectivity read of {odb} timed out after "
                         f"{CONNECTIVITY_TIMEOUT_S:g}s: {exc}") from exc
    parsed = parse_connectivity(proc.stdout or "")
    if proc.returncode != 0 or parsed is None or parsed[0] == 0:
        raise CheckError(
            f"could not read the nets of {odb} (openroad exit "
            f"{proc.returncode}), so its connectivity is unchecked: "
            f"{((proc.stdout or '') + (proc.stderr or ''))[-2000:]}")
    return parsed


def judge_connectivity(seen: int, rows: list[dict], signoff: dict,
                       top: str) -> tuple[dict, list[dict]]:
    """(the report's `connectivity` table, the findings) for
    read_connectivity's answer. A waiver excuses no load, never no driver."""
    waivers = signoff.get("floating_nets_waived", {})
    undriven, unloaded, tt_inputs, waived = [], [], [], []
    for row in rows:
        net = row["net"]
        if row["drivers"] == 0:
            undriven.append(net)
        elif any(ttlib.is_wrapper_input(p) for p in row["inputs"]):
            tt_inputs.append(net)
        elif net in waivers:
            waived.append(net)
        else:
            unloaded.append(net)
    table = {"nets": seen, "undriven": sorted(undriven),
             "unloaded": sorted(unloaded),
             "tt_inputs_unloaded": sorted(tt_inputs), "waived": sorted(waived),
             "unused_waivers": sorted(set(waivers) - set(waived))}
    violations = [
        checklib.violation("harden", "error", None, top, kind, names,
                           f"{what} in the final ODB: {', '.join(names)}",
                           "check_harden")
        for kind, what, names in (
            ("net_undriven", "nets with no driver", table["undriven"]),
            ("net_unloaded", "nets with no load", table["unloaded"]))
        if names]
    return table, violations


# OpenSTA's `report_check_types -violators` in a corner's checks.rpt: a bare
# section header ("max slew", "max capacitance", "max fanout"), a column
# header and a rule, then a "(VIOLATED)" row per pin (max capacitance puts a
# blank line between rows). A "=" banner ends the section.
DRV_SECTIONS = ("max slew", "max capacitance", "max fanout")
DRV_ROW_RE = re.compile(r"^(\S+)\s.*\(VIOLATED\)\s*$")
STA_SIGNOFF_RE = re.compile(r"^(\d+)-openroad-stapostpnr$")


def drv_pins(run_dir: Path, corner: str, check: str):
    """(pins, report): the pins listed under `check` in the corner's
    checks.rpt of the run's last post-PnR STA step, or (None, None) when
    the run has no such report."""
    steps = sorted((int(m.group(1)), d) for d in run_dir.glob("*-openroad-stapostpnr")
                   if (m := STA_SIGNOFF_RE.match(d.name)) and d.is_dir())
    if not steps:
        return None, None
    rpt = steps[-1][1] / corner / "checks.rpt"
    if not rpt.is_file():
        return None, None
    pins, current = [], None
    for line in rpt.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.lower() in DRV_SECTIONS:
            current = stripped.lower()
            continue
        if stripped.startswith("="):
            current = None
            continue
        row = DRV_ROW_RE.match(stripped)
        if row and current == check and row.group(1) not in pins:
            pins.append(row.group(1))
    return pins, str(rpt.relative_to(run_dir))


def corner_drv_metrics(metrics: dict, signoff: dict, run_dir: Path, top: str,
                       limits: dict | None = None,
                       analog_pads: frozenset = frozenset()):
    """(table, violations): each of gates.yaml's top_harden_metrics at every
    corner metrics.json carries it for, one finding per corner over its
    limit naming the pins. table is {metric: {corner: row}}.

    analog_pads names pins on an analog pad's net (the tile's ua[k] ports
    and the macro pins routed to them). STA has no digital driver or
    receiver there: their slew and load are LibreLane's defaults for every
    port (SYNTH_DRIVING_CELL, OUTPUT_CAP_LOAD), so they are listed in the
    row's analog_pad and not counted. Like a waiver, they excuse only pins
    the report names."""
    if limits is None:
        limits = yaml.safe_load(
            GATES_YAML.read_text(encoding="utf-8"))["top_harden_metrics"]
    waived = signoff.get("drv_pins_waived", {})
    table, violations = {}, []

    def finding(kind, msg, refs, **extras):
        violations.append(checklib.violation(
            "top_harden", "error", None, top, kind, list(refs), msg,
            "check_harden", **extras))

    for name, rule in limits.items():
        prefix = f"{name}__corner:"
        corners = sorted(k[len(prefix):] for k in metrics if k.startswith(prefix))
        table[name] = rows = {}
        if not corners:
            finding("signoff_metric_missing",
                    f"metrics.json has no {prefix}<corner> number: "
                    f"{rule['kind']} was not judged at any corner",
                    [name], metric=name)
            continue
        for corner in corners:
            key = prefix + corner
            value = _number(metrics, key)
            row = rows[corner] = {"value": value}
            if value is None:
                row["status"] = "missing"
                finding("signoff_metric_missing",
                        f"metrics.json has no number for {key} "
                        f"({metrics.get(key)!r}): {rule['kind']} was not "
                        f"judged at {corner}", [key], metric=key)
                continue
            limit = row["limit"] = rule["max"]
            if value <= limit:
                row["status"] = "ok"
                continue
            pins, report = drv_pins(run_dir, corner, rule["check"])
            row["pins"] = pins
            msg = f"{corner}: {name} is {value:g}, limit {limit:g}"
            if pins is None:
                real = []
                msg += (f"; no {corner}/checks.rpt from the signoff STA "
                        "names them, so none can be excused")
            else:
                row["waived"] = sorted(set(pins) & set(waived))
                row["analog_pad"] = sorted(set(pins) & analog_pads)
                real = [p for p in pins
                        if p not in waived and p not in analog_pads]
                if len(pins) < value:
                    msg += (f"; {report} names only {len(pins)}, so none can "
                            "be excused")
                    real = real or pins
                elif not real:
                    row["status"] = "ok"
                    continue
                msg += f": {', '.join(real)}"
            row["status"] = "over"
            finding(rule["kind"], msg, real or [key], metric=key,
                    corner=corner, value=value, limit=limit, report=report)
    return table, violations


def _pdk_root() -> Path:
    proc = subprocess.run([str(EDA_BIN), "--print-toolchain-root"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CheckError(f"eda --print-toolchain-root failed: "
                         f"{proc.stderr.strip()}")
    return Path(proc.stdout.strip()) / "foss" / "pdks"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--timeout-s", type=float,
                    default=float(os.environ.get("CHIP_FLOW_HARDEN_TIMEOUT_S",
                                                  DEFAULT_TIMEOUT_S)))
    # a partial run (check_top_harden's synthesis probe, #191): the flow
    # stops after this step under its own run tag, and no final artifact,
    # metric or signoff is judged
    ap.add_argument("--to", help="stop after this LibreLane step id")
    ap.add_argument("--run-tag", default=RUN_TAG, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    # a macro's files are named relative to the workspace (top_harden writes
    # them so); LibreLane gets them as paths
    for m in spec.get("macros") or []:
        if isinstance(m, dict) and isinstance(m.get("files"), dict):
            m["files"] = {k: str(ws / v) for k, v in m["files"].items()}
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint first")
    if not spec.get("tt_pins"):
        raise CheckError("spec.yaml has no 'tt_pins' mapping - harden needs "
                         "one to generate the TT wrapper (engine/lib/ttlib.py)")
    problems = ttlib.validate_tt_pins(spec)
    if problems:
        raise CheckError("tt_pins does not fit the tile: " + "; ".join(problems))
    signoff = speclib.harden_signoff(spec)
    if signoff is None:
        raise CheckError("spec.yaml 'harden_signoff' is malformed: it takes "
                         "ir_budget_pct (0-100), floating_nets_waived "
                         "({net: reason}) and drv_pins_waived ({pin: reason})"
                         " - run spec_lint and fix it")

    rtl_dir = ws / "rtl"
    rtl_files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not rtl_files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")

    harden_dir = ws / "harden"
    harden_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = harden_dir / f"{ttlib.wrapper_name(spec)}.v"
    try:
        wrapper_path.write_text(ttlib.generate_tt_wrapper(spec), encoding="utf-8")
        # the tile's rst_n path, opt-out included, in the gate record
        pad_reset = ttlib.pad_reset_facts(spec)
    except ttlib.TTError as exc:
        raise CheckError(str(exc)) from exc

    # The override is read and refused before config.json is rewritten or
    # the toolchain is asked for: a forbidden key is a CheckError (exit 2,
    # the message its remediation), never a silently dropped edit.
    try:
        override = ttlib.load_harden_override(
            harden_dir / ttlib.HARDEN_OVERRIDE_NAME)
    except ttlib.TTError as exc:
        raise CheckError(str(exc)) from exc
    # The clocks are refused here too, before the toolchain: a spec with
    # several clock domains and no design SDC is never hardened against its
    # primary clock alone.
    try:
        flow_sdc = ttlib.prepare_harden_sdc(spec, harden_dir)
        pdk_root = _pdk_root()
        config = ttlib.harden_config(spec, rtl_files, wrapper_path, pdk_root,
                                     override=override, flow_sdc=flow_sdc)
        config = ttlib.portable_config(config, harden_dir)
    except ttlib.TTError as exc:
        raise CheckError(str(exc)) from exc
    config_path = harden_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    ttlib.write_info_yaml(spec, harden_dir / "info.yaml")

    run_dir = harden_dir / "runs" / args.run_tag
    resumed = run_dir.is_dir()
    # `--overwrite` always: LibreLane's OWN implicit resume (omit
    # --overwrite, let it pick up runs/<tag>'s latest state_out.json) was
    # tried here first and is not safe on this LibreLane build - re-running
    # it against a run directory left by an EARLIER, fully successful run
    # (nothing left to do) still crashed (FileNotFoundError inside
    # Step._reroute_env, a step directory it never created, after climbing
    # past the original run's own highest step number instead of recognizing
    # completion). A killed-halfway job therefore resumes by redoing the
    # whole flow, not by continuing from its last completed step; on these
    # tiny corpus designs a full run is a few minutes, and correctness (never
    # feeding a half-written run directory back into a fresh flow object)
    # matters more than the minutes saved. `resumed` is still recorded so a
    # caller can tell a restart from a first run.
    tech = ttlib.gf180_tech()
    cmd = [str(EDA_BIN), "librelane", "--pdk-root", str(pdk_root),
          *tech.librelane_pdk_args.split(), "--run-tag", args.run_tag,
          "--overwrite", *(["--to", args.to] if args.to else []),
          str(config_path)]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(harden_dir), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(
            f"eda librelane timed out after {args.timeout_s:g}s: {exc}") from exc
    wall_s = round(time.monotonic() - t0, 2)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")

    flow_log = run_dir / "flow.log"
    if not flow_log.is_file():
        # never reached LibreLane's own flow object at all - a launcher
        # failure (bad args, missing binary, a config that failed to even
        # load), not a real flow attempt: refuse, never record a violation
        # that looks like an evaluated design.
        raise CheckError(
            f"librelane never started (no {flow_log}, exit {proc.returncode}): "
            f"{output[-2000:]}")

    violations = []
    if proc.returncode != 0:
        error_log = run_dir / "error.log"
        detail = (error_log.read_text(encoding="utf-8", errors="replace")
                  if error_log.is_file() else output[-2000:])
        violations.append(checklib.violation(
            "harden", "error", None, top, "flow_step_failed", [],
            f"librelane exited {proc.returncode}: {detail[-2000:]}",
            "librelane", resumed=resumed, wall_s=wall_s))
    elif args.to:
        payload = checklib.report(SCRIPT, ws / "rtl", violations, top=top,
                                  run_tag=args.run_tag, resumed=resumed,
                                  wall_s=wall_s, run_dir=str(run_dir))
        return payload, args.out
    else:
        final_dir = run_dir / "final"
        missing = [fmt for fmt in EXPECTED_FORMATS
                  if not any((final_dir / fmt).glob("*"))
                  and not (final_dir / fmt).is_file()]
        if not final_dir.is_dir():
            missing = list(EXPECTED_FORMATS)
        if missing:
            violations.append(checklib.violation(
                "harden", "error", None, top, "harden_missing_artifact",
                [], f"librelane exited 0 but {final_dir} is missing: "
                f"{missing}", "check_harden", resumed=resumed, wall_s=wall_s))

    metrics = {}
    metrics_path = run_dir / "final" / "metrics.json"
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metrics = {}
    metric_subset = {k: v for k, v in metrics.items()
                     if k.startswith(("design__instance", "design__die",
                                      "design__core", "timing__setup",
                                      "timing__hold"))
                     and ":" not in k}

    # The full QoR table, the versions the run ran on and its config
    # hashes (#65): what `qor diff` compares and `qor repro` replays. Only
    # for a run that wrote metrics; a metric it did not write stays None
    # and is listed in qor.missing, never read as 0.
    qor = None
    if metrics:
        qor = qorlib.record(metrics, run_dir, config_path,
                            env=qorlib.env_versions(EDA_BIN),
                            pdk=statelib.pdk_version())
        qor["wall_s"] = wall_s

    # Judged only when the flow finished: a failed flow already has its
    # finding, and an unfinished run's metrics say nothing about a design.
    signoff_table = None
    if proc.returncode == 0 and metrics_path.is_file():
        signoff_table, signoff_violations = signoff_metrics(
            metrics, signoff, run_dir, top)
        violations += signoff_violations

    connectivity = None
    if proc.returncode == 0:
        odb = (run_dir / "final" / "odb"
               / f"{ttlib.wrapper_name(spec)}.odb").resolve()
        if odb.is_file():
            connectivity, net_violations = judge_connectivity(
                *read_connectivity(odb, harden_dir), signoff, top)
            violations += net_violations
        else:
            violations.append(checklib.violation(
                "harden", "error", None, top, "harden_missing_artifact", [],
                f"librelane exited 0 but there is no {odb}, so no net of the "
                "final design was checked", "check_harden",
                resumed=resumed, wall_s=wall_s))

    payload = checklib.report(SCRIPT, ws / "rtl", violations, top=top,
                              run_tag=args.run_tag, resumed=resumed,
                              wall_s=wall_s,
                              run_dir=str(run_dir), metrics=metric_subset,
                              qor=qor, signoff_metrics=signoff_table,
                              connectivity=connectivity,
                              pad_reset=pad_reset)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

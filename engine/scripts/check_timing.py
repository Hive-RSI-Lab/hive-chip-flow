#!/usr/bin/env python
"""check_timing.py - the timing gate (docs/design.md 1.5, "### M4.").

    check_timing.py --workspace DIR [--out FILE]

Independently re-runs OpenSTA (`eda sta`, never LibreLane's own built-in
timing checker - the design's whole point in separating this from `harden`
is a second, independent signoff on what the hardening tool claims) on the
hardened netlist, once per corner LibreLane actually produced
(`harden/runs/run/final/lib/<corner>/`), linked against the PDK's own
standard-cell liberty for that corner (engine/lib/ttlib.py's
`stdcell_liberty_path` - NOT the per-corner file under that same `lib/`
directory, which is the hardened macro's own abstracted timing view, not
the cell library) with the matching SPEF for parasitics and the flow's own
signoff SDC for constraints.

The SDC is evaluated per corner, as LibreLane's own signoff STA
(OpenROAD.STAPostPNR) evaluates it: the file its config names
(SIGNOFF_SDC_FILE, else PNR_SDC_FILE, else FALLBACK_SDC - LibreLane's
base.sdc), read with every key of the run's resolved config
(`harden/runs/run/resolved.json`) set in `::env` the way LibreLane's
TclStep exports it, plus SCRIPTS_DIR, OPENLANE_SDC_IDEAL_CLOCKS=0 and
read_current_sdc's compatibility names. Never `final/sdc`: that is one
step's write_sdc of the constraints as they came out for whichever corner
that step loaded, so an SDC that picks a value per corner (a clock period
from the loaded liberty's name) would be frozen at one corner's value and
timed wrong at the others. A path under the run's DESIGN_DIR is read from
this workspace's harden/ (the workspace may have moved since harden ran).

Passes when every corner's worst setup AND hold slack is >= 0 and
`report_check_types` finds no max-slew/max-cap/max-fanout violator
(gates.yaml's `timing` row). Fault this gate must catch: "a chain that
misses the spec's clock" (docs/design.md 1.5) - a design whose slowest
corner runs the clock period into negative setup slack. A DIFFERENT fault
this gate catches: a corner whose worst slack is INF, not negative - the
SDC's clock reaches no path at all (a block clocked from a tt_pins bit the
template's default CLOCK_PORT does not name), which run() reports as a
`clock_unconstrained` violation rather than letting `inf >= 0` pass it
silently.

Signoff checks beyond slack (issues #70, #72), each a finding of its own
because a design can meet setup and hold everywhere and still fail them:
  - OpenSTA `check_setup` (PrimeTime's check_timing): an unclocked
    register/latch pin (`unclocked_register` - one register bank clocked
    from a divider flop no create_generated_clock names, while the rest of
    the design has finite slack), an unconstrained endpoint
    (`unconstrained_endpoint`), a generated clock with no path to its
    master (`generated_clock_unreachable` - the SDC keyed it on a pin a
    rename moved) and a combinational loop (`combinational_loop`). A
    register pin reached by several clocks is recorded as a fact
    (`multiple_clock` in the corner payload), not a failure: a clock mux
    does exactly that on purpose. spec.yaml `timing.unconstrained_ok` (a
    list of pin/port names, glob patterns allowed) waives endpoints
    that are meant to be unconstrained.
  - `report_check_types -min_pulse_width -min_period
    -clock_gating_setup -clock_gating_hold`: a clock phase shorter than a
    flop's liberty min_pulse_width (`min_pulse_width_violation`), a clock
    period under a cell's min_period (`min_period_violation`), a clock
    gate whose enable misses its gating check (`clock_gating_violation`).
    OpenSTA 3.1.0 has no counters for these, so each runs in its own
    marked section and the gate insists on seeing the closing marker.

Failure classification (the same three-way split check_harden.py uses):
  - no `harden/runs/run/final/` at all -> CheckError (harden has not run;
    never a pass masquerading as "nothing to check").
  - `eda sta` for a corner times out, crashes, or exits with no parseable
    "worst slack" line, or with no slew/cap/fanout counts line ->
    CheckError (a launcher/tool failure is a refusal, never recorded as a
    plain slack violation the fix loop could waive as "just timing", and
    never a pass on a check that did not run).
  - every corner's STA completes and reports a negative slack, an infinite
    (unconstrained) slack, or a reported violator -> a `violations`
    finding, exit 1. An infinite slack is never recorded in the `corners`
    payload as `Infinity` (not valid JSON) - `None` instead.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_timing"
EDA_BIN = REPO / "bin" / "eda"
# A wall ceiling only, ten times the old fixed limit: the tool is ended
# sooner when it stops making progress (checklib.STALL_S).
TIMEOUT_S = 1200.0
# OpenSTA prints "worst slack max INF" when no clock reaches any path (the
# SDC's clock is on a port the design never uses as one): a parseable
# result, but an unconstrained one - run() turns it into a finding.
SLACK_RE = re.compile(
    r"worst slack (max|min)\s+(-?(?:[0-9.]+(?:e-?[0-9]+)?|INF))\b")
# OpenSTA 3.1.0's `report_check_types -violators` prints a bare section
# header ("max slew", "max capacitance", "max fanout"), a column header and
# a dashed rule, then one row per violating pin ending "(VIOLATED)" - never
# the word "violation" (tests/fixtures/opensta/check_types_violators.txt is
# its real output). The rows are only the finding's detail: the pass/fail
# decision rests on sta's own per-check counters (the same ones LibreLane's
# corner.tcl reads), printed on one marker line the gate insists on seeing.
CHECK_KINDS = {"max slew": "slew", "max capacitance": "cap",
               "max fanout": "fanout"}
COUNTS_MARK = "chipflow_violation_counts"
COUNTS_RE = re.compile(
    rf"^{COUNTS_MARK} slew (\d+) cap (\d+) fanout (\d+)\s*$", re.MULTILINE)
VIOLATED_ROW_RE = re.compile(r"^(\S+)\s.*\(VIOLATED\)\s*$")
COUNTS_TCL = (f'puts "{COUNTS_MARK} slew [sta::max_slew_violation_count] '
              "cap [sta::max_capacitance_violation_count] "
              'fanout [sta::max_fanout_violation_count]"\n')


# check_setup and the signoff check types, each between marker lines so a
# run that died half-way is a refusal, never a clean section.
SETUP_BEGIN = "chipflow_check_setup_begin"
SECTION_MARK = "chipflow_section"
SIGNOFF_SECTIONS = ("min_pulse_width", "min_period", "clock_gating_setup",
                    "clock_gating_hold")
SIGNOFF_END = f"{SECTION_MARK} end"
SIGNOFF_TCL = (
    f'puts "{SETUP_BEGIN}"\n'
    "check_setup -verbose -unconstrained_endpoints -multiple_clock "
    "-no_clock -loops -generated_clocks\n"
    + "".join(f'puts "{SECTION_MARK} {name}"\n'
              f"report_check_types -{name} -violators\n"
              for name in SIGNOFF_SECTIONS)
    + f'puts "{SIGNOFF_END}"\n')
# OpenSTA's CheckTiming.cc: "Warning: There %is %d <what>%s." then one
# indented line per item (a loop's listing also carries "| loop cut point"
# and a dashed rule, which are not items).
SETUP_HEAD_RE = re.compile(r"^Warning: There (?:is|are) (\d+) (.+?)\.?\s*$")
SETUP_KINDS = (("unclocked", "unclocked_register"),
               ("unconstrained endpoint", "unconstrained_endpoint"),
               ("multiple clock", "multiple_clock"),
               ("combinational loop", "combinational_loop"),
               ("generated clock", "generated_clock_unreachable"))
# min pulse width / min period rows: "f0/CLK (high)  0.71 0.30 -0.41 (VIOLATED)"
CHECK_ROW_RE = re.compile(r"^(\S+(?: \((?:high|low)\))?)\s.*\(VIOLATED\)\s*$")
# clock gating rows are per path group: "gated clock   -0.50"
GROUP_ROW_RE = re.compile(r"^(.*\S)\s+(-?[0-9.]+(?:e-?[0-9]+)?)\s*$")


def parse_check_setup(output: str) -> dict:
    """{kind: [items]} from check_setup's warnings between SETUP_BEGIN and
    the first signoff section marker. A run that never printed SETUP_BEGIN
    is a CheckError. An unknown "There is N ..." heading is kept under its
    own text so a new OpenSTA message is never silently dropped."""
    lines = output.splitlines()
    try:
        start = lines.index(SETUP_BEGIN)
    except ValueError:
        raise CheckError(f"eda sta printed no {SETUP_BEGIN} line - "
                         "check_setup never ran") from None
    found: dict[str, list[str]] = {}
    current = None
    for line in lines[start + 1:]:
        if line.startswith(SECTION_MARK):
            break
        head = SETUP_HEAD_RE.match(line)
        if head:
            what = head.group(2)
            current = next((k for key, k in SETUP_KINDS if key in what),
                           what)
            found.setdefault(current, [])
            continue
        if current and line[:1].isspace():
            item = line.strip()
            if item and item[0] not in "|-" and item not in found[current]:
                found[current].append(item)
            continue
        current = None
    return found


def parse_signoff_checks(output: str) -> dict:
    """{section: [violating pin or group, ...]} for SIGNOFF_SECTIONS. No
    closing SIGNOFF_END marker (or a section's own marker missing) is a
    CheckError: a run that died part-way proves nothing clean."""
    lines = output.splitlines()
    if SIGNOFF_END not in lines:
        raise CheckError(f"eda sta printed no {SIGNOFF_END!r} line - the "
                         "min pulse width/min period/clock gating checks "
                         "never finished")
    result: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith(SECTION_MARK + " "):
            current = line[len(SECTION_MARK) + 1:].strip()
            if current in SIGNOFF_SECTIONS:
                result[current] = []
            else:
                current = None
            continue
        if current is None:
            continue
        stripped = line.strip()
        if current.startswith("clock_gating"):
            row = GROUP_ROW_RE.match(stripped)
            if row and not stripped.startswith(("Group", "-")):
                if float(row.group(2)) < 0:
                    result[current].append(
                        f"{row.group(1)} (slack {row.group(2)})")
        else:
            row = CHECK_ROW_RE.match(stripped)
            if row:
                result[current].append(row.group(1))
    missing = [s for s in SIGNOFF_SECTIONS if s not in result]
    if missing:
        raise CheckError(f"eda sta printed no {SECTION_MARK} marker for "
                         f"{missing} - those checks never ran")
    return result


def parse_check_types(output: str) -> dict:
    """{"slew"|"cap"|"fanout": {"count": n, "pins": [...]}} from one sta
    run's output. `count` is sta's own counter; `pins` the "(VIOLATED)"
    rows listed under that check's section header. No counts line at all
    is a CheckError: a run that never reached it proves nothing clean."""
    m = COUNTS_RE.search(output)
    if not m:
        raise CheckError(f"eda sta printed no {COUNTS_MARK} line - the "
                         "slew/cap/fanout check never ran")
    result = {kind: {"count": int(n), "pins": []}
              for kind, n in zip(("slew", "cap", "fanout"), m.groups())}
    current = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower() in CHECK_KINDS:
            current = CHECK_KINDS[stripped.lower()]
            continue
        row = VIOLATED_ROW_RE.match(stripped)
        if row and current:
            result[current]["pins"].append(row.group(1))
    return result


def _pdk_root() -> Path:
    proc = subprocess.run([str(EDA_BIN), "--print-toolchain-root"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CheckError(f"eda --print-toolchain-root failed: "
                         f"{proc.stderr.strip()}")
    return Path(proc.stdout.strip()) / "foss" / "pdks"


# LibreLane's common/tcl.py: what Tcl reads as one word unquoted.
_TCL_UNSAFE = re.compile(r"[^\w@%+=:,./-]", re.ASCII).search
_TCL_IN_QUOTES = re.compile(r"([\\$\"\[])")
_ENV_KEY = re.compile(r"^\w+$", re.ASCII)
# read_current_sdc's (scripts/openroad/common/io.tcl) compatibility layer
# for deprecated names a user SDC may still read.
COMPAT_TCL = """\
set ::env(IO_PCT) [expr $::env(IO_DELAY_CONSTRAINT) / 100.0]
set ::env(SYNTH_TIMING_DERATE) [expr $::env(TIME_DERATING_CONSTRAINT) / 100.0]
set ::env(SYNTH_MAX_FANOUT) $::env(MAX_FANOUT_CONSTRAINT)
set ::env(SYNTH_CLOCK_UNCERTAINTY) $::env(CLOCK_UNCERTAINTY_CONSTRAINT)
set ::env(SYNTH_CLOCK_TRANSITION) $::env(CLOCK_TRANSITION_CONSTRAINT)
set ::env(SYNTH_CAP_LOAD) $::env(OUTPUT_CAP_LOAD)
if { [info exists ::env(MAX_TRANSITION_CONSTRAINT)] } {
    set ::env(SYNTH_MAX_TRAN) $::env(MAX_TRANSITION_CONSTRAINT)
}
"""


def tcl_escape(s: str) -> str:
    """LibreLane's TclUtils.escape: `s` verbatim when Tcl reads it as one
    word, else double-quoted with $ [ " \\ escaped."""
    if s == "":
        return '""'
    if not _TCL_UNSAFE(s):
        return s
    return '"' + _TCL_IN_QUOTES.sub(r"\\\1", s).replace("\n", r"\n") + '"'


def value_to_tcl(value) -> str:
    """LibreLane's TclStep.value_to_tcl on a value as resolved.json holds
    it: a dict or list joined as a Tcl list, a bool 1/0, else str()."""
    if isinstance(value, dict):
        return " ".join(tcl_escape(value_to_tcl(x))
                        for kv in value.items() for x in kv)
    if isinstance(value, list):
        return " ".join(tcl_escape(value_to_tcl(x)) for x in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _scripts_dir(pdk_root: Path) -> Path:
    """LibreLane's scripts/ in the toolchain tree (pdk_root is
    <tree>/foss/pdks): what SCRIPTS_DIR names in its own steps."""
    tree = pdk_root.parent.parent
    found = sorted(tree.glob(
        "usr/local/lib/python3*/dist-packages/librelane/scripts"))
    if not found:
        raise CheckError(f"no librelane/scripts under {tree} - the toolchain "
                         "tree has no LibreLane to read base.sdc from")
    return found[-1]


def signoff_sdc(ws: Path, run_dir: Path, pdk_root: Path) -> tuple[Path, str]:
    """The SDC LibreLane's signoff STA read and the Tcl that sets up the
    ::env it read it under (everything but the corner's own name)."""
    resolved = run_dir / "resolved.json"
    try:
        config = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"cannot read the harden run's config {resolved} "
                         f"({exc}) - re-run the harden gate") from exc
    config.pop("meta", None)
    harden_dir = ws / "harden"
    design_dir = str(config.get("DESIGN_DIR") or "").rstrip("/")

    def rebase(v):
        if isinstance(v, list):
            return [rebase(x) for x in v]
        if isinstance(v, dict):
            return {k: rebase(x) for k, x in v.items()}
        if (isinstance(v, str) and design_dir
                and (v == design_dir or v.startswith(design_dir + "/"))):
            return str(harden_dir) + v[len(design_dir):]
        return v

    config = {k: rebase(v) for k, v in config.items()}
    config["DESIGN_DIR"] = str(harden_dir)
    scripts = _scripts_dir(pdk_root)
    sdc = (config.get("SIGNOFF_SDC_FILE") or config.get("PNR_SDC_FILE")
           or config.get("FALLBACK_SDC") or str(scripts / "base.sdc"))
    sdc = Path(sdc)
    if not sdc.is_file():
        # a FALLBACK_SDC recorded from another toolchain tree: the same
        # base.sdc in this one.
        if sdc.name == "base.sdc" and (scripts / "base.sdc").is_file():
            sdc = scripts / "base.sdc"
        else:
            raise CheckError(f"the harden run's signoff SDC {sdc} does not "
                             "exist - re-run the harden gate")
    lines = [f"set ::env({k}) {tcl_escape(value_to_tcl(v))}"
             for k, v in config.items()
             if v is not None and _ENV_KEY.match(k)]
    lines += [f"set ::env(SCRIPTS_DIR) {tcl_escape(str(scripts))}",
              f"set ::env(_SDC_IN) {tcl_escape(str(sdc))}",
              "set ::env(OPENLANE_SDC_IDEAL_CLOCKS) 0"]
    return sdc, "\n".join(lines) + "\n" + COMPAT_TCL


def run_corner(final_dir: Path, top: str, corner: str, pdk_root: Path,
              work_dir: Path, sdc: Path, env_tcl: str) -> dict:
    bucket = corner.split("_", 1)[0]
    lib = ttlib.stdcell_liberty_path(corner, pdk_root)
    netlist = final_dir / "nl" / f"{top}.nl.v"
    spef = final_dir / "spef" / bucket / f"{top}.{bucket}.spef"
    for label, p in (("liberty", lib), ("netlist", netlist), ("sdc", sdc),
                     ("spef", spef)):
        if not p.is_file():
            raise CheckError(f"corner {corner!r}: no {label} at {p}")

    # written under work_dir (log/timing_work/), never final_dir: final_dir
    # sits inside harden/, the exact directory tree the "harden" artifact
    # kind hashes for freshness (invalidation.yaml) - a scratch file dropped
    # there would change that hash on every timing run and falsely stale
    # every OTHER gate that also reads "harden" (drc, lvs, glsim, precheck,
    # release), including timing's own last-recorded pass.
    tcl = work_dir / f".sta_{corner}.tcl"
    tcl.write_text(
        f"read_liberty {lib}\n"
        f"read_verilog {netlist}\n"
        f"link_design {top}\n"
        + env_tcl
        + f"set ::env(_CURRENT_CORNER_NAME) {tcl_escape(corner)}\n"
        f"read_sdc {tcl_escape(str(sdc))}\n"
        f"read_spef {spef}\n"
        "report_worst_slack -max\n"
        "report_worst_slack -min\n"
        "report_check_types -max_slew -max_capacitance -max_fanout -violators\n"
        + COUNTS_TCL + SIGNOFF_TCL + "exit\n", encoding="utf-8")
    try:
        proc = checklib.run_tool([str(EDA_BIN), "sta", str(tcl)],
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"corner {corner!r}: eda sta "
                         f"{checklib.timeout_why(exc)}: {exc}") from exc
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    slacks = {kind: float(val) for kind, val in SLACK_RE.findall(output)}
    if "max" not in slacks or "min" not in slacks:
        raise CheckError(
            f"corner {corner!r}: eda sta produced no parseable worst-slack "
            f"line (exit {proc.returncode}): {output[-2000:]}")
    try:
        checks = parse_check_types(output)
    except CheckError as exc:
        raise CheckError(f"corner {corner!r}: {exc} (exit {proc.returncode}): "
                         f"{output[-2000:]}") from exc
    try:
        setup_findings = parse_check_setup(output)
        signoff = parse_signoff_checks(output)
    except CheckError as exc:
        raise CheckError(f"corner {corner!r}: {exc} (exit {proc.returncode}): "
                         f"{output[-2000:]}") from exc
    violators = []
    for kind, found in checks.items():
        if found["count"] or found["pins"]:
            pins = found["pins"]
            shown = ", ".join(pins[:5]) + (" ..." if len(pins) > 5 else "")
            violators.append(f"max {kind}: {max(found['count'], len(pins))} "
                             f"violating pin(s){': ' + shown if shown else ''}")
    return {"corner": corner, "setup_ws": slacks["max"], "hold_ws": slacks["min"],
           "violators": violators, "check_setup": setup_findings,
           "signoff": signoff}


SIGNOFF_KINDS = {"min_pulse_width": "min_pulse_width_violation",
                 "min_period": "min_period_violation",
                 "clock_gating_setup": "clock_gating_violation",
                 "clock_gating_hold": "clock_gating_violation"}
SETUP_WHY = {
    "unclocked_register": "register/latch clock pin(s) no clock reaches - "
        "a divided or gated clock with no create_generated_clock",
    "unconstrained_endpoint": "endpoint(s) with no timing check or IO "
        "delay - list intended ones in spec.yaml timing.unconstrained_ok",
    "generated_clock_unreachable": "generated clock(s) with no path to "
        "their master clock - the SDC keys them on a pin that no longer "
        "connects to the source",
    "combinational_loop": "combinational loop(s); OpenSTA cuts them, so "
        "the paths through them are not timed",
}


def _shown(items: list[str]) -> str:
    return ", ".join(items[:5]) + (" ..." if len(items) > 5 else "")


def unconstrained_waivers(spec: dict) -> list[str]:
    """spec.yaml `timing.unconstrained_ok`: pin/port name globs whose
    unconstrained endpoints are intended (TT's `ena`, a test-only pin)."""
    timing = spec.get("timing") or {}
    waived = timing.get("unconstrained_ok") if isinstance(timing, dict) else None
    if waived is None:
        return []
    if (not isinstance(waived, list)
            or not all(isinstance(w, str) and w for w in waived)):
        raise CheckError("spec.yaml timing.unconstrained_ok must be a list "
                         "of pin/port names")
    return waived


def signoff_violations(r: dict, top: str, waived: list[str]) -> list[dict]:
    """The check_setup and min pulse width/min period/clock gating findings
    for one corner's run_corner() result."""
    out = []
    for kind, items in r["check_setup"].items():
        if kind == "multiple_clock":
            continue
        if kind == "unconstrained_endpoint":
            items = [i for i in items
                     if not any(fnmatch.fnmatchcase(i, w) for w in waived)]
            if not items:
                continue
        why = SETUP_WHY.get(kind, f"check_setup: {kind}")
        out.append(checklib.violation(
            "timing", "error", None, top,
            kind if kind in SETUP_WHY else "check_setup", items[:50],
            f"corner {r['corner']}: {len(items)} {why}"
            f"{': ' + _shown(items) if items else ''}",
            "opensta", corner=r["corner"]))
    for section, items in r["signoff"].items():
        if items:
            out.append(checklib.violation(
                "timing", "error", None, top, SIGNOFF_KINDS[section],
                items[:50], f"corner {r['corner']}: {section.replace('_', ' ')}: "
                f"{len(items)} violating pin(s)/group(s): {_shown(items)}",
                "opensta", corner=r["corner"]))
    return out


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = ttlib.wrapper_name(spec)
    final_dir = ws / "harden" / "runs" / "run" / "final"
    lib_dir = final_dir / "lib"
    if not lib_dir.is_dir():
        raise CheckError(f"no {lib_dir} - the harden gate has not produced "
                         "a hardened design to time yet")
    corners = sorted(p.name for p in lib_dir.iterdir() if p.is_dir())
    if not corners:
        raise CheckError(f"{lib_dir} has no corner subdirectories")

    waived = unconstrained_waivers(spec)
    pdk_root = _pdk_root()
    sdc, env_tcl = signoff_sdc(ws, final_dir.parent, pdk_root)
    work_dir = ws / "log" / "timing_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    results = [run_corner(final_dir, top, corner, pdk_root, work_dir,
                          sdc, env_tcl)
              for corner in corners]

    violations = []
    corners_out = {}
    for r in results:
        inf_sides = [side for side, key in (("setup", "setup_ws"),
                                            ("hold", "hold_ws"))
                    if math.isinf(r[key])]
        if inf_sides:
            violations.append(checklib.violation(
                "timing", "error", None, top, "clock_unconstrained", [],
                f"corner {r['corner']}: worst {' and '.join(inf_sides)} "
                "slack is unconstrained (INF) - no clock reaches any path "
                "in this corner; check the SDC's clock port against "
                "spec.yaml's clock.domains mapped through tt_pins",
                "opensta", corner=r["corner"]))
        else:
            if r["setup_ws"] < 0:
                violations.append(checklib.violation(
                    "timing", "error", None, top, "setup_violation", [],
                    f"corner {r['corner']}: worst setup slack {r['setup_ws']:.4f}ns",
                    "opensta", corner=r["corner"]))
            if r["hold_ws"] < 0:
                violations.append(checklib.violation(
                    "timing", "error", None, top, "hold_violation", [],
                    f"corner {r['corner']}: worst hold slack {r['hold_ws']:.4f}ns",
                    "opensta", corner=r["corner"]))
        for line in r["violators"]:
            violations.append(checklib.violation(
                "timing", "error", None, top, "slew_or_cap_or_fanout_violation",
                [], f"corner {r['corner']}: {line}", "opensta",
                corner=r["corner"]))
        violations.extend(signoff_violations(r, top, waived))
        corners_out[r["corner"]] = {
            "setup_ws": None if "setup" in inf_sides else r["setup_ws"],
            "hold_ws": None if "hold" in inf_sides else r["hold_ws"],
            "multiple_clock": r["check_setup"].get("multiple_clock", [])}

    payload = checklib.report(SCRIPT, ws / "harden", violations, top=top,
                              corners=corners_out)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

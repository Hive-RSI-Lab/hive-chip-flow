"""check_fpga.py - the optional FPGA prototype step (#96).

    check_fpga.py --workspace DIR [--out FILE]

Opt-in: a spec.yaml `fpga:` section turns it on, e.g.

    fpga:
      board: icebreaker      # the only board so far (BOARDS below)

Without one the gate is not applicable (attest.NOT_APPLICABLE), so no
block is held to it unless its spec asks.

What it does, through `bin/eda`:
  1. wraps the design for the board: the Tiny Tapeout wrapper
     `tt_um_<top>` (ttlib.generate_tt_wrapper, the same one harden builds
     from tt_pins), inside a generated `fpga_top` that ties `ena` high and
     puts clk, rst_n, ui_in, uo_out and the bidirectional uio pins on the
     board's pins (uio through SB_IO tristate buffers, driven by uio_oe);
  2. yosys `synth_ice40 -top fpga_top`;
  3. nextpnr-ice40 for the board's device and package, with the board's
     pin constraint file and the spec clock (`clock.period_ns`) as the
     target frequency, writing its timing and utilisation report;
  4. icepack, writing the bitstream to `log/fpga/<top>_<board>.bin`.

Between 2 and 3 it proves the synth_ice40 netlist equal to the RTL (#378;
prove_netlist()): the same reads elaborated as gold, the netlist read back
from its JSON as gate, both with yosys's own ice40 cell models
(`+/ice40/cells_sim.v`) mapped in, then `equiv_make` (signals matched by
name), `equiv_simple` and `equiv_induct`. lec proves the GF180 netlist equal
to the same RTL, so the two netlists agree by transitivity. Where the names
do not all match (an FSM yosys recoded, say) the induction cannot close, so
the outputs alone get one more proof: a miter of the two, both started from
zero, under sby `abc pdr`. The proof has its own wall budget, --proof-budget
(PROOF_BUDGET_S by default), shared by both steps, so the gate stays cheap;
fact `equiv` records how it was proved and how long it took.

Programming a board is the person's, never this gate's: nothing here
touches a USB device.

Findings (gates.yaml `fpga` row):
  fpga_comb_loop          yosys found a combinational logic loop - an FPGA
                          has no way to time it, even where the ASIC flow
                          (ABC breaking the loop) would have finished
  fpga_latch              a latch survived into the ice40 netlist: ice40
                          logic cells have no latch, so synth_ice40 builds
                          it from a LUT feeding itself
  fpga_place_route_failed nextpnr could not place or route the design (too
                          big for the device, an unplaceable pin, ...)
  fpga_fmax_below_clock   nextpnr's achieved Fmax for the clock is below
                          the spec clock, named with both figures
  fpga_netlist_not_equal  the ice40 netlist is not the RTL: the outputs
                          (and internal signals) that differ are named
  fpga_equiv_unproven     the proof ran out of budget or could not decide;
                          not a pass

The report carries the device, logic-cell (LUT) use, and per-clock Fmax
next to the spec clock, so a passing block's numbers are on record too.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(SCRIPTS))
import checklib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_fpga"
GATE = "fpga"
EDA_BIN = REPO / "bin" / "eda"
TIMEOUT_S = 1200.0
# The netlist proof's whole wall budget (#378): counter8 proves in under a
# second; the pdr fallback gets what the induction leaves.
PROOF_BUDGET_S = 120.0
PDR_MIN_S = 10.0

# One board so far. Pins are the iCEBreaker's (1BitSquared's published PCF):
# the 12 MHz clock, the user button as the active-low reset, ui_in on
# PMOD1A, uo_out on PMOD1B and uio on PMOD2.
BOARDS = {
    "icebreaker": {
        "device": "up5k",
        "package": "sg48",
        "pins": {
            "clk": 35,
            "rst_n": 10,
            "ui_in": [4, 2, 47, 45, 3, 48, 46, 44],
            "uo_out": [43, 38, 34, 31, 42, 36, 32, 28],
            "uio": [27, 25, 21, 19, 26, 23, 20, 18],
        },
    },
}

LOOP_RE = re.compile(r"found logic loop in module (\S+?):")
# yosys `proc_dlatch` names every latch it creates; on ice40 it never maps
# to a cell, so the log line is the evidence.
DLATCH_RE = re.compile(r"Latch inferred for signal `([^']+)'")


def inferred_latches(text: str) -> list[str]:
    """The signals yosys inferred a latch for, from its log: a "Warning:"
    on yosys 0.69, an "ERROR:" that stops synth_ice40 on 0.70."""
    return sorted({s.replace("\\", "") for s in DLATCH_RE.findall(text)})


def fpga_settings(spec: dict) -> dict | None:
    """spec.yaml's `fpga:` section checked, or None when there is none."""
    f = spec.get("fpga")
    if f is None:
        return None
    if not isinstance(f, dict) or set(f) - {"board"}:
        raise CheckError("spec.yaml 'fpga' must be a mapping with 'board' "
                         "only")
    board = f.get("board", "icebreaker")
    if board not in BOARDS:
        raise CheckError(f"spec.yaml fpga.board {board!r} is not one of "
                         f"{', '.join(sorted(BOARDS))}")
    return {"board": board}


def spec_clock_mhz(spec: dict) -> float:
    clock = spec.get("clock") or {}
    period = clock.get("period_ns") if isinstance(clock, dict) else None
    if not isinstance(period, (int, float)) or period <= 0:
        raise CheckError("spec.yaml has no positive clock.period_ns - the "
                         "fpga step times the design against it")
    return 1000.0 / float(period)


def board_top(wrapper: str) -> str:
    """`fpga_top`: the TT wrapper on board pins, uio through SB_IO."""
    return f"""\
// generated by check_fpga.py - the TT wrapper on board pins
module fpga_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    inout  wire [7:0] uio
);
  wire [7:0] uio_in, uio_out, uio_oe;
  {wrapper} dut (
      .ui_in(ui_in), .uo_out(uo_out),
      .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
      .ena(1'b1), .clk(clk), .rst_n(rst_n)
  );
  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_uio
      SB_IO #(.PIN_TYPE(6'b1010_01), .PULLUP(1'b0)) io (
          .PACKAGE_PIN(uio[i]), .OUTPUT_ENABLE(uio_oe[i]),
          .D_OUT_0(uio_out[i]), .D_IN_0(uio_in[i])
      );
    end
  endgenerate
endmodule
"""


def pcf(board: str) -> str:
    pins = BOARDS[board]["pins"]
    lines = [f"# generated by check_fpga.py for {board}",
             f"set_io -nowarn clk {pins['clk']}",
             f"set_io -nowarn rst_n {pins['rst_n']}"]
    for bus in ("ui_in", "uo_out", "uio"):
        for i, pin in enumerate(pins[bus]):
            lines.append(f"set_io -nowarn {bus}[{i}] {pin}")
    return "\n".join(lines) + "\n"


def wrapper_source(spec: dict, rtl_files: list[Path]) -> tuple[str, str | None]:
    """(wrapper module name, generated source or None when the RTL is
    already the TT wrapper)."""
    import ttlib
    top = spec["top"]
    if top.startswith("tt_um_"):
        return top, None
    if not spec.get("tt_pins"):
        raise CheckError("the fpga step puts the Tiny Tapeout wrapper on "
                         "board pins, so spec.yaml needs tt_pins (or a "
                         "tt_um_* top)")
    if ttlib.is_analog(spec) or ttlib.macro_bound_ports(spec):
        raise CheckError("the fpga step prototypes digital logic only; "
                         "this spec has analog pins or hard macros")
    try:
        return ttlib.wrapper_name(spec), ttlib.generate_tt_wrapper(spec)
    except ttlib.TTError as exc:
        raise CheckError(str(exc)) from exc


def run_cmd(cmd: list[str], ws: Path, log: Path) -> subprocess.CompletedProcess:
    try:
        proc = checklib.run_tool(cmd, wall=TIMEOUT_S, cwd=str(ws),
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"{cmd[1]} {checklib.timeout_why(exc)}") from exc
    log.write_text((proc.stdout or "") + (proc.stderr or ""),
                   encoding="utf-8")
    return proc


def parse_report(rpt: dict, clk_mhz: float) -> tuple[dict, dict]:
    """(utilisation, {clock: {achieved, constraint}}) from nextpnr's
    --report JSON."""
    util = {}
    for res, v in (rpt.get("utilization") or {}).items():
        if isinstance(v, dict) and v.get("used"):
            util[res] = {"used": int(v["used"]),
                         "available": int(v.get("available", 0))}
    fmax = {}
    for clk, v in (rpt.get("fmax") or {}).items():
        if isinstance(v, dict) and "achieved" in v:
            fmax[clk] = {"achieved_mhz": round(float(v["achieved"]), 2),
                         "constraint_mhz": round(float(
                             v.get("constraint", clk_mhz)), 2)}
    return util, fmax


UNPROVEN_RE = re.compile(r"Unproven \$equiv \S+: \\(\S+)_gold( \[\d+\])?")

EQUIV_SCRIPT = """\
{reads}
read_verilog -lib +/ice40/cells_sim.v
hierarchy -top fpga_top
proc
flatten
memory_map
opt_clean
rename -top gold
design -stash gold
read_json {netlist}
rename -top gate
design -copy-from gold -as gold gold
techmap -wb -D EQUIV -autoproc -map +/ice40/cells_sim.v
opt_clean
write_rtlil {work}/views.il
equiv_make gold gate equiv
hierarchy -top equiv
async2sync
equiv_simple -seq 5 equiv
equiv_induct -seq 5 equiv
equiv_status equiv
"""

PDR_SBY = """\
[options]
mode prove
multiclock off
timeout {timeout}

[engines]
abc pdr

[script]
read_rtlil views.il
miter -equiv -flatten -make_assert gold gate miter
hierarchy -top miter
async2sync
setundef -zero -init
select -assert-min 1 miter/t:$assert
prep -top miter

[files]
views.il
"""


def unproven(log: str) -> list[str]:
    """The signals equiv_status left unproven, outputs (no `.` in the name,
    so not inside the flattened design) first."""
    names = []
    for name, bit in UNPROVEN_RE.findall(log):
        n = name + (f"[{bit.strip(' []')}]" if bit else "")
        if n not in names:
            names.append(n)
    return sorted(names, key=lambda n: ("." in n, n))


def prove_netlist(ws: Path, out: Path, reads: str, netlist: Path,
                  budget: float = PROOF_BUDGET_S) -> tuple[dict, list]:
    """(fact `equiv`, violations): the synth_ice40 netlist against the RTL
    read by `reads` (paths relative to ws). Work files in out/equiv."""
    start = time.monotonic()
    work = out / "equiv"
    work.mkdir(parents=True, exist_ok=True)
    rel = lambda p: str(Path(p).resolve().relative_to(ws.resolve()))  # noqa: E731
    script = work / "equiv.ys"
    script.write_text(EQUIV_SCRIPT.format(
        reads=reads, netlist=rel(netlist), work=rel(work)), encoding="utf-8")
    log = work / "equiv.log"
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), "yosys", "-q", "-l", rel(log), "-s", rel(script)],
            wall=budget, cwd=str(ws), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return _unproven(f"the signal induction {checklib.timeout_why(exc)}",
                         budget, start)
    text = log.read_text(encoding="utf-8", errors="replace") \
        if log.is_file() else ""
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or text).strip().splitlines()[-5:]
        raise CheckError("yosys could not set up the ice40 netlist proof: "
                         + " | ".join(tail))
    differ = unproven(text)
    if not differ:
        return {"verdict": "equal", "by": "signal induction",
                "seconds": round(time.monotonic() - start, 2),
                "budget_s": budget}, []

    left = budget - (time.monotonic() - start)
    if left < PDR_MIN_S:
        return _unproven("the budget ended before the pdr proof could "
                         "start", budget, start, differ)
    (work / "miter.sby").write_text(
        PDR_SBY.format(timeout=max(1, int(left) - 5)), encoding="utf-8")
    shutil.rmtree(work / "miter", ignore_errors=True)
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "sby", "-f", "miter.sby"], cwd=str(work),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=left)
    except subprocess.TimeoutExpired:
        return _unproven(f"pdr ran out of the {budget:g} s budget", budget,
                         start, differ)
    sby = (proc.stdout or "") + (proc.stderr or "")
    (work / "sby.log").write_text(sby, encoding="utf-8")
    done = re.findall(r"DONE \((\w+), rc=(\d+)\)", sby)
    verdict = done[-1][0] if done else None
    if proc.returncode == 0 and verdict == "PASS":
        return {"verdict": "equal", "by": "pdr",
                "seconds": round(time.monotonic() - start, 2),
                "budget_s": budget}, []
    if verdict == "FAIL":
        outs = [n for n in differ if "." not in n] or differ
        return {"verdict": "different", "by": "pdr",
                "seconds": round(time.monotonic() - start, 2),
                "budget_s": budget, "unproven": differ}, [
            checklib.violation(
                GATE, "error", None, outs[0], "fpga_netlist_not_equal",
                outs[:8],
                "the synth_ice40 netlist is not the RTL: "
                + ", ".join(outs[:8]) + (" ..." if len(outs) > 8 else "")
                + " can differ from reset (pdr found a counterexample; see "
                "log/fpga/equiv/)", "yosys equiv + sby pdr",
                unproven=differ)]
    return _unproven(f"pdr ended {verdict or f'with exit {proc.returncode}'}",
                     budget, start, differ)


def _unproven(why: str, budget: float, start: float,
              differ: list | None = None) -> tuple[dict, list]:
    fact = {"verdict": "unproven", "why": why,
            "seconds": round(time.monotonic() - start, 2), "budget_s": budget}
    if differ:
        fact["unproven"] = differ
    return fact, [checklib.violation(
        GATE, "error", None, None, "fpga_equiv_unproven", differ or [],
        f"the ice40 netlist could not be proved equal to the RTL: {why}. "
        "Not a pass: rerun with a larger --proof-budget", "yosys equiv",
        unproven=differ or [])]


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--proof-budget", type=float, default=PROOF_BUDGET_S,
                    help="wall seconds for the netlist-vs-RTL proof "
                         "(default %(default)s)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    settings = fpga_settings(spec)
    if settings is None:
        # not applicable is its own answer, never a pass (gate.evaluate);
        # stamped with spec.yaml, so state.record_gate won't keep it as a
        # fresh rtl-keyed result, and release re-asks the spec
        # (attest._fpga_not_applicable), as check_mc does.
        return checklib.report(SCRIPT, ws / "spec" / "spec.yaml", [],
                               top=top, applicable=False,
                               reason="spec.yaml has no 'fpga' section, so "
                                      "the FPGA prototype was not asked "
                                      "for"), args.out
    board = settings["board"]
    dev = BOARDS[board]
    clk_mhz = spec_clock_mhz(spec)

    import check_synth
    rtl_files = check_synth.collect_sources(ws)
    frontend, slang_so = check_synth.pick_frontend(rtl_files)
    wname, wsrc = wrapper_source(spec, rtl_files)

    out = ws / "log" / "fpga"
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):
        if stale.is_file():
            stale.unlink()
    files = list(rtl_files)
    if wsrc is not None:
        (out / f"{wname}.v").write_text(wsrc, encoding="utf-8")
        files.append(out / f"{wname}.v")
    (out / "fpga_top.v").write_text(board_top(wname), encoding="utf-8")
    files.append(out / "fpga_top.v")
    (out / f"{board}.pcf").write_text(pcf(board), encoding="utf-8")

    json_nl = out / "fpga_top.json"
    rel = lambda p: str(Path(p).resolve().relative_to(ws.resolve()))  # noqa: E731
    if slang_so is None:
        reads = "\n".join(f"read_verilog {rel(f)}" for f in files)
    else:
        rtl_rel = " ".join(rel(f) for f in rtl_files)
        gen = [f for f in files if f not in rtl_files]
        reads = (f"plugin -i {slang_so}\nread_slang {rtl_rel}\n"
                 + "\n".join(f"read_verilog {rel(f)}" for f in gen))
    script = out / "synth_ice40.ys"
    script.write_text(f"""\
{reads}
synth_ice40 -top fpga_top -json {rel(json_nl)}
stat
""", encoding="utf-8")
    proc = run_cmd([str(EDA_BIN), "yosys", "-q", "-l", rel(out / "yosys.log"),
                    "-s", rel(script)], ws, out / "yosys.out")
    ylog = (out / "yosys.log").read_text(encoding="utf-8", errors="replace") \
        if (out / "yosys.log").is_file() else ""
    # yosys 0.70's synth_ice40 stops with "ERROR: Latch inferred for signal"
    # where 0.69 warned and went on, so latches are read before the exit code
    # decides: a latch is a named finding on either version, never a crash.
    latches = inferred_latches(ylog + (proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0 and not latches:
        tail = (proc.stderr or proc.stdout or ylog).strip().splitlines()[-5:]
        raise CheckError("yosys synth_ice40 failed: " + " | ".join(tail))

    violations = []
    for mod in sorted(set(LOOP_RE.findall(ylog))):
        violations.append(checklib.violation(
            GATE, "error", None, mod, "fpga_comb_loop", [],
            f"yosys found a combinational logic loop in module {mod}: an "
            "FPGA cannot time it", "yosys"))
    for sig in latches:
        violations.append(checklib.violation(
            GATE, "error", None, None, "fpga_latch", [],
            f"a latch is inferred for {sig}: ice40 logic cells have no "
            "latch, so it would be built from a LUT feeding itself",
            "yosys"))

    facts = {"top": top, "board": board, "device": dev["device"],
             "package": dev["package"], "frontend": frontend,
             "clock_mhz": round(clk_mhz, 3)}
    if violations:
        return checklib.report(SCRIPT, ws / "rtl", violations, **facts), \
            args.out

    facts["equiv"], found = prove_netlist(ws, out, reads, json_nl,
                                          args.proof_budget)
    violations.extend(found)
    facts["netlist_proof"] = facts["equiv"]["verdict"] + (
        f" by {facts['equiv']['by']}" if "by" in facts["equiv"] else "")

    rpt_path = out / "nextpnr_report.json"
    asc = out / "fpga_top.asc"
    proc = run_cmd([str(EDA_BIN), "nextpnr-ice40", f"--{dev['device']}",
                    "--package", dev["package"], "--json", rel(json_nl),
                    "--pcf", rel(out / f"{board}.pcf"),
                    "--freq", f"{clk_mhz:.3f}", "--seed", "1",
                    "--timing-allow-fail", "--report", rel(rpt_path),
                    "--asc", rel(asc)], ws, out / "nextpnr.log")
    if proc.returncode != 0 or not asc.is_file() or not rpt_path.is_file():
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if "ERROR" in ln][-3:] or \
            (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        violations.append(checklib.violation(
            GATE, "error", None, top, "fpga_place_route_failed", [],
            f"nextpnr-ice40 could not place and route {top} on the "
            f"{board} ({dev['device']}): " + " | ".join(tail), "nextpnr"))
        return checklib.report(SCRIPT, ws / "rtl", violations, **facts), \
            args.out

    try:
        rpt = json.loads(rpt_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CheckError(f"nextpnr's report is not JSON: {exc}") from exc
    util, fmax = parse_report(rpt, clk_mhz)
    if not fmax:
        raise CheckError("nextpnr's report times no clock - the spec clock "
                         "could not be checked")
    for clk, f in sorted(fmax.items()):
        if f["achieved_mhz"] < clk_mhz:
            violations.append(checklib.violation(
                GATE, "error", None, top, "fpga_fmax_below_clock", [clk],
                f"Fmax on the {board} is {f['achieved_mhz']} MHz for "
                f"{clk}, below the spec clock of {clk_mhz:.3f} MHz "
                f"(clock.period_ns {1000.0 / clk_mhz:g})", "nextpnr"))

    bitstream = out / f"{top}_{board}.bin"
    proc = run_cmd([str(EDA_BIN), "icepack", rel(asc), rel(bitstream)], ws,
                   out / "icepack.log")
    if proc.returncode != 0 or not bitstream.is_file():
        raise CheckError("icepack could not write the bitstream: "
                         + (proc.stderr or proc.stdout).strip()[-300:])

    lc = util.get("ICESTORM_LC", {})
    facts.update(utilisation=util, luts=lc.get("used"),
                 luts_available=lc.get("available"), fmax=fmax,
                 fmax_mhz=min(f["achieved_mhz"] for f in fmax.values()),
                 bitstream=rel(bitstream))
    return checklib.report(SCRIPT, ws / "rtl", violations, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

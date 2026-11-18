"""seqequivlib.py - is a rewritten RTL the same machine as the baseline (#85)?

optimise keeps a trial only when it passes the visible tests. Tests sample
behaviour; a rewrite can pass them all and still differ in a state they
never reach (a counter that wraps to 1 instead of 0 passes any bench that
stops before 255). This module proves, or refutes, sequential equivalence
at the spec's ports from reset:

    both designs share the clock and every input; the reset is held active
    for formal.reset_cycles clocks, then left free (any value, any time);
    after that, on every clock, every watched output port of the trial
    equals the baseline's.

The two RTL sets are elaborated separately (yosys: proc, flatten,
memory_map, the designs' own formal properties removed), renamed cf_ref and
cf_uut, and put under a miter module with one assert per output port. sby
proves it unbounded (`abc pdr`, prove mode, `multiclock off`): PASS is
equivalence over every reachable state; FAIL gives a counterexample trace
(a VCD) and the clock it fails on.

Assumptions, the same as check_mutate's reachable-state proof: every
register without a declared initial value starts at zero in both designs
(`setundef -zero -init`; the held reset is what has to make that not
matter), an async reset is sampled at clock granularity (`async2sync`), and
an undefined constant reads as zero. Outputs the spec lets read X
(`sim.x_ok`) are not compared: the spec says their value is not defined.

Verdicts:
    equivalent    sby PASS
    different     sby FAIL: the port, the clock after reset, the trace
    inconclusive  anything else (timeout, UNKNOWN, a tool error, a spec it
                  cannot model - no clock or reset, several clock domains,
                  an inout port). The caller decides what that means; it is
                  never an equivalence.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import checklib
import xmonlib

EDA_BIN = Path(__file__).resolve().parents[2] / "bin" / "eda"
MITER = "cf_seq_miter"
REF, UUT = "cf_ref", "cf_uut"
DEFAULT_RESET_CYCLES = 2
DEFAULT_TIMEOUT_S = 300.0
DONE_RE = re.compile(r"DONE \((\w+), rc=(\d+)\)")
FAILED_RE = re.compile(
    r"failed assertion \S+ at \S+?:(\d+)\.\d+-\S+ step (\d+)")
TRACE_RE = re.compile(r"counterexample trace: (\S+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")

SBY = """\
[options]
mode prove
multiclock off
timeout {timeout}

[engines]
abc pdr

[script]
read_rtlil {ref}.il
read_rtlil {uut}.il
read_verilog -formal miter.sv
prep -top {miter}
async2sync
setundef -zero -init
prep -top {miter}

[files]
{ref}.il
{uut}.il
miter.sv
"""


class Refused(Exception):
    """The spec or design is outside what the proof models."""


def port_plan(spec: dict) -> dict:
    """{clock, reset, active_high, inputs: {name: width},
    outputs: {name: width}} - outputs are the ones compared (every output
    but sim.x_ok's). Refused where the proof cannot model the spec."""
    try:
        plan = xmonlib.plan(spec)
    except xmonlib.XmonError as exc:
        raise Refused(str(exc)) from exc
    if not plan["on"]:
        raise Refused(plan["why"])
    clock = spec.get("clock") if isinstance(spec.get("clock"), dict) else {}
    domains = clock.get("domains") or []
    if len(domains) > 1:
        raise Refused(f"several clock domains ({', '.join(map(str, domains))})"
                      " - the proof runs one clock")
    inputs, outputs = {}, {}
    for name, p in (spec.get("ports") or {}).items():
        if not isinstance(p, dict):
            continue
        name = str(name)
        if p.get("dir") == "inout":
            raise Refused(f"inout port {name!r}")
        if not _IDENT.match(name):
            raise Refused(f"port name {name!r} is not a plain identifier")
        w = int(p.get("width") or 1)
        if p.get("dir") == "input":
            inputs[name] = w
        elif name in plan["watch"]:
            outputs[name] = w
    if not outputs:
        raise Refused("no output port to compare")
    return {"clock": plan["clock"], "reset": plan["reset"],
            "active_high": plan["active_high"], "inputs": inputs,
            "outputs": outputs}


def reset_cycles(spec: dict) -> int:
    formal = spec.get("formal") if isinstance(spec.get("formal"), dict) \
        else {}
    v = formal.get("reset_cycles", DEFAULT_RESET_CYCLES)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise Refused(f"spec formal.reset_cycles must be a positive integer, "
                      f"got {v!r}")
    return v


def _vec(w: int) -> str:
    return f"[{w - 1}:0] " if w > 1 else ""


def miter_verilog(plan: dict, cycles: int) -> tuple[str, dict]:
    """The miter's Verilog and {line number: output port} for its asserts.
    Clock is the miter's only input; every other input is free each cycle
    (anyseq), the reset held active for `cycles` clocks first."""
    clk, rst = plan["clock"], plan["reset"]
    active = "1'b1" if plan["active_high"] else "1'b0"
    lines = [f"module {MITER}(input {clk});",
             "  reg [15:0] cf_n = 16'd0;",
             f"  always @(posedge {clk}) if (cf_n < 16'd{cycles}) "
             "cf_n <= cf_n + 16'd1;",
             f"  wire cf_armed = cf_n >= 16'd{cycles};",
             f"  (* anyseq *) wire cf_{rst}_free;",
             f"  wire {rst} = cf_armed ? cf_{rst}_free : {active};"]
    conns = [f".{clk}({clk})", f".{rst}({rst})"]
    for name, w in plan["inputs"].items():
        if name in (clk, rst):
            continue
        lines.append(f"  (* anyseq *) wire {_vec(w)}{name};")
        conns.append(f".{name}({name})")
    for name, w in plan["outputs"].items():
        lines.append(f"  wire {_vec(w)}cf_ref_{name}, cf_uut_{name};")
    ref_conns = conns + [f".{n}(cf_ref_{n})" for n in plan["outputs"]]
    uut_conns = conns + [f".{n}(cf_uut_{n})" for n in plan["outputs"]]
    lines.append(f"  {REF} r({', '.join(ref_conns)});")
    lines.append(f"  {UUT} u({', '.join(uut_conns)});")
    where = {}
    for name in plan["outputs"]:
        lines.append(f"  always @* if (cf_armed) "
                     f"assert (cf_ref_{name} == cf_uut_{name});")
        where[len(lines)] = name
    lines.append("endmodule")
    return "\n".join(lines) + "\n", where


def _elaborate(work: Path, sources: list[Path], top: str, name: str,
               timeout_s: float) -> None:
    """sources -> work/<name>.il: top flattened and renamed `name`."""
    inc = sorted({str(p.resolve().parent) for p in sources})
    files = " ".join(f'"{p.resolve()}"' for p in sources)
    ys = work / f"{name}.ys"
    ys.write_text("\n".join([
        f"read_verilog -sv {' '.join('-I ' + d for d in inc)} {files}",
        f"hierarchy -check -top {top}",
        "proc", "flatten", "memory_map", "opt_clean",
        "chformal -remove",
        f"rename {top} {name}",
        f"hierarchy -top {name}",
        f"write_rtlil {name}.il"]) + "\n", encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "yosys", "-q", "-s", ys.name], cwd=str(work),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise Refused(f"yosys ({name}) timed out after {timeout_s:.0f}s") \
            from exc
    if proc.returncode != 0:
        err = [ln for ln in (proc.stdout + proc.stderr).splitlines()
               if "ERROR" in ln]
        raise Refused(f"yosys ({name}) exited {proc.returncode}"
                      + (f": {err[0].strip()}" if err else ""))


def prove(spec: dict, top: str, ref_sources: list[Path],
          uut_sources: list[Path], work: Path,
          timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """{"verdict": equivalent|different|inconclusive, "why": str,
    "port": str|None, "cycle": int|None (clocks after reset release),
    "step": int|None (sby's step), "trace": str|None (VCD path)}."""
    out = {"verdict": "inconclusive", "why": "", "port": None,
           "cycle": None, "step": None, "trace": None}
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        plan = port_plan(spec)
        cycles = reset_cycles(spec)
        text, where = miter_verilog(plan, cycles)
        (work / "miter.sv").write_text(text, encoding="utf-8")
        _elaborate(work, checklib.order_sources(list(ref_sources)), top,
                   REF, timeout_s)
        _elaborate(work, checklib.order_sources(list(uut_sources)), top,
                   UUT, timeout_s)
    except Refused as exc:
        out["why"] = f"not proved: {exc}"
        return out
    (work / "seq.sby").write_text(SBY.format(
        timeout=max(1, int(timeout_s) - 5), ref=REF, uut=UUT, miter=MITER),
        encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "sby", "-f", "seq.sby", "-d", "run"],
            cwd=str(work), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s + 60,
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out["why"] = f"not proved: sby timed out after {timeout_s:.0f}s"
        return out
    except OSError as exc:
        out["why"] = f"not proved: sby did not start: {exc}"
        return out
    log = proc.stdout + proc.stderr
    done = DONE_RE.findall(log)
    status = done[-1][0] if done else None
    if status == "PASS" and proc.returncode == 0:
        out.update(verdict="equivalent",
                   why=f"proved equivalent at {', '.join(plan['outputs'])} "
                   f"from reset (pdr)")
        return out
    fails = FAILED_RE.findall(log)
    if status == "FAIL" and fails:
        line, step = int(fails[0][0]), int(fails[0][1])
        port = where.get(line, "?")
        tm = TRACE_RE.search(log)
        trace = None
        if tm:
            # sby names the trace relative to the directory it ran in
            t = Path(tm.group(1))
            trace = str(work / t) if not t.is_absolute() else str(t)
        cycle = step - cycles
        out.update(verdict="different", port=port, step=step, cycle=cycle,
                   trace=trace,
                   why=f"differs from the baseline at output {port}, "
                   f"{cycle} clocks after reset release"
                   + (f"; counterexample {trace}" if trace else ""))
        return out
    if status:
        out["why"] = f"not proved: sby {status}"
    else:
        err = [ln for ln in log.splitlines() if "ERROR" in ln]
        out["why"] = ("not proved: " + err[0].strip() if err
                      else f"not proved: sby exit {proc.returncode}")
    return out

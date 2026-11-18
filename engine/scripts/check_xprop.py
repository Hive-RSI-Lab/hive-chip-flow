#!/usr/bin/env python
"""check_xprop.py - the xprop gate (docs/design.md 1.5, #84).

    check_xprop.py --workspace DIR [--out FILE] [--timeout S]

Proves that no watched output of the top can be X once the reset has been
held for `formal.reset_cycles` clocks (default 2), for every input sequence
after that, with the reset itself free to come and go again. It does not
need tb/ or formal/: the property is the same for every block.

How: yosys reads rtl/, flattens it and runs `xprop -split-outputs
-assume-def-inputs`, which re-encodes the design in 2-valued logic with an
explicit X mask beside every output (`<port>_x`). Every register with no
initial value starts as X, as on an X-pessimistic simulator, and the
encoding carries that X through each `if`, `case` and operator instead of
taking the else branch the way Icarus does. A generated wrapper drives every
input with any defined value, forces the reset active for the first
reset_cycles clocks, and asserts each watched output's X mask is zero on
every clock after that. sby proves it with k-induction (smtbmc, depth
`formal.depth`, default 20) and abc pdr side by side; whichever settles
first decides.

Power-up contents that are arbitrary but never X: spec
`formal.x_init_ok: {name: reason}` names a memory or register whose
power-up value the design is allowed to see (a RAM the core may run before
it is loaded, a register file read before it is written). Each named
memory's words and register's bits start as an arbitrary DEFINED value
(yosys `formalff -ff2anyinit`) instead of X, so every other register still
starts X; a name that matches nothing in the design is a refusal, so a
stale allowance cannot outlive its register. The report repeats each
reason.

Which ports and which reset: xmonlib.plan, the same as sim's X/Z monitor:
the clock, the reset (spec `sim.reset`, tt_pins, or a conventional name),
and every output port except those in `sim.x_ok` (each with a reason, which
this report repeats).

Findings:
  - `output_x_after_reset` (error): a counterexample in which the named
    port is X the named number of clocks after the reset window. The fix is
    to reset or initialise what feeds it, or, if that output really is
    don't-care after reset, list it in `sim.x_ok` with the reason.
  - `xprop_unproven` (error): neither engine found a counterexample or a
    proof in time (sby UNKNOWN). Raise `formal.depth` so induction closes.
A design with no clock, no reset or no output left to watch, or with more
than one clock domain, is a refusal (exit 2), never a pass.

Fault this gate must catch (gates.yaml): "a register with no reset whose x
only reaches the output through an if" (corpus counter8
xprop_unreset_mode, the same plant reset_indep catches).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import speclib  # noqa: E402
import xmonlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_xprop"
CHECK = "xprop"
EDA_BIN = ENGINE.parent / "bin" / "eda"
WORK_SUBDIR = "log/xprop"
WRAP = "cf_xprop_wrap"
DEFAULT_RESET_CYCLES = 2
DEFAULT_DEPTH = 20
DEFAULT_TIMEOUT_S = 600.0
DONE_RE = re.compile(r"DONE \((\w+), rc=(\d+)\)")
FAILED_RE = re.compile(r"failed assertion \S+ at \S+?:(\d+)\.\d+-\S+ step (\d+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
# a register or memory after flatten: inst.inst.name
_HIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*\Z")


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def _positive_int(formal: dict, key: str, default: int) -> int:
    v = formal.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise CheckError(f"spec formal.{key} must be a positive integer, "
                         f"got {v!r}")
    return v


def settings(spec: dict) -> dict:
    """reset_cycles, depth and timeout_s from spec `formal:`."""
    formal = spec.get("formal") if isinstance(spec.get("formal"), dict) \
        else {}
    rc = _positive_int(formal, "reset_cycles", DEFAULT_RESET_CYCLES)
    depth = _positive_int(formal, "depth", DEFAULT_DEPTH)
    t = formal.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        raise CheckError(f"spec formal.timeout_s must be a positive number, "
                         f"got {t!r}")
    init_ok = formal.get("x_init_ok") or {}
    if not isinstance(init_ok, dict):
        raise CheckError("spec formal.x_init_ok must map a memory or "
                         f"register name to a reason, got {init_ok!r}")
    for name, why in init_ok.items():
        if not _HIER.match(str(name)):
            raise CheckError(f"spec formal.x_init_ok name {name!r} is not a "
                             "plain (dotted) identifier")
        if not isinstance(why, str) or not why.strip():
            raise CheckError(f"spec formal.x_init_ok.{name} needs a reason")
    # induction has to reach past the reset window to see armed states
    return {"reset_cycles": rc, "depth": max(depth, rc + 2),
            "timeout_s": float(t),
            "x_init_ok": {str(k): v.strip() for k, v in init_ok.items()}}


def port_plan(spec: dict) -> dict:
    """xmonlib.plan plus the port widths; a CheckError where xprop cannot
    run (the refusal the gate reports)."""
    plan = xmonlib.plan(spec)
    if not plan["on"]:
        raise CheckError(f"xprop cannot run: {plan['why']}")
    clock = spec.get("clock") if isinstance(spec.get("clock"), dict) else {}
    domains = clock.get("domains") or []
    if len(domains) > 1:
        raise CheckError(
            f"xprop runs one clock domain, and spec clock.domains has "
            f"{len(domains)} ({', '.join(map(str, domains))})")
    inputs, outputs = {}, {}
    for name, p in (spec.get("ports") or {}).items():
        if not isinstance(p, dict):
            continue
        if p.get("dir") == "inout":
            raise CheckError(f"xprop does not handle inout port {name!r}")
        if not _IDENT.match(str(name)):
            raise CheckError(f"port name {name!r} is not a plain identifier")
        w = int(p.get("width") or 1)
        (inputs if p.get("dir") == "input" else outputs)[str(name)] = w
    return {**plan, "inputs": inputs, "outputs": outputs}


def _vec(w: int) -> str:
    return f"[{w - 1}:0] " if w > 1 else ""


def wrapper(top: str, plan: dict, reset_cycles: int) -> tuple[str, dict]:
    """The wrapper's Verilog and {line number: port} for its asserts."""
    clk, rst = plan["clock"], plan["reset"]
    active = "1'b1" if plan["active_high"] else "1'b0"
    lines = [f"module {WRAP}(input {clk});",
             f"  reg [15:0] cf_n = 16'd0;",
             f"  always @(posedge {clk}) if (cf_n < 16'd{reset_cycles}) "
             "cf_n <= cf_n + 16'd1;",
             f"  wire cf_armed = cf_n >= 16'd{reset_cycles};",
             f"  (* anyseq *) wire cf_{rst}_free;",
             f"  wire {rst} = cf_armed ? cf_{rst}_free : {active};"]
    conns = [f".{clk}({clk})"]
    for name, w in plan["inputs"].items():
        if name == clk:
            continue
        if name != rst:
            lines.append(f"  (* anyseq *) wire {_vec(w)}{name};")
        conns.append(f".{name}({name})")
    for name, w in plan["outputs"].items():
        lines.append(f"  wire {_vec(w)}{name}_d, {name}_x;")
        conns += [f".{name}_d({name}_d)", f".{name}_x({name}_x)"]
    lines.append(f"  {top} dut({', '.join(conns)});")
    asserts = {}
    for name in plan["watch"]:
        lines.append(f"  always @* if (cf_armed) assert ({name}_x == 0);")
        asserts[len(lines)] = name
    lines.append("endmodule")
    return "\n".join(lines) + "\n", asserts


def init_selection(name: str) -> str:
    """yosys selection of the FFs holding memory or register `name` once
    memory_map has split memories into one $dff per word and dffunmap has
    turned every enable and sync reset back into a plain $dff."""
    return f"c:$memory\\{name}[* w:{name} %ci1:+$dff[Q] t:$dff %i %u"


def init_script(names) -> str:
    """Script lines giving each x_init_ok FF an arbitrary defined power-up
    value; `select -assert-min 1` refuses a name that matches nothing."""
    if not names:
        return ""
    lines = [f"select -assert-min 1 {init_selection(n)}" for n in names]
    lines.append("formalff -clk2ff -ff2anyinit "
                 + " ".join(init_selection(n) for n in names))
    return "\n".join(lines) + "\n"


def sby_text(top: str, sources: list[Path], depth: int,
             timeout_s: float, init_ok=()) -> str:
    reads = "\n".join(f"read_verilog -sv {s.name}" for s in sources)
    files = "\n".join(str(s) for s in sources)
    return f"""[options]
mode prove
depth {depth}
timeout {int(timeout_s)}

[engines]
smtbmc
abc pdr

[script]
{reads}
prep -top {top}
flatten
memory_map
async2sync
dffunmap
{init_script(init_ok)}opt_clean
xprop -split-outputs -assume-def-inputs -formal -required
read_verilog -formal {WRAP}.sv
prep -top {WRAP}

[files]
{files}
{WRAP}.sv
"""


def run_sby(work: Path, timeout_s: float) -> str:
    try:
        proc = subprocess.run(
            [str(EDA_BIN), "sby", "-f", "xprop.sby", "-d", "run"],
            cwd=str(work), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s + 120)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"sby did not finish within {timeout_s + 120:g}s: "
                         f"{exc}") from exc
    out = (proc.stdout or "") + (proc.stderr or "")
    empty = re.search(r"Assertion failed: selection is empty: (.*)", out)
    if empty:
        raise CheckError(
            "spec formal.x_init_ok names something that is not a memory or "
            f"an un-reset register of the design ({empty.group(1)}); fix "
            "the name or drop the entry")
    if not DONE_RE.search(out):
        raise CheckError(f"sby exited {proc.returncode} with no DONE line "
                         f"(the design did not read or encode): "
                         f"{out[-2000:]}")
    return out


def findings(output: str, asserts: dict, reset_cycles: int,
             depth: int) -> tuple[str, list[dict]]:
    """(sby's DONE status, violations) from the sby log."""
    status = DONE_RE.search(output).group(1)
    violations: list[dict] = []
    if status == "PASS":
        return status, violations
    if status == "FAIL":
        seen = set()
        for m in FAILED_RE.finditer(output):
            port = asserts.get(int(m.group(1)))
            if port is None or port in seen:
                continue
            seen.add(port)
            step = int(m.group(2))
            after = step - reset_cycles
            violations.append(checklib.violation(
                CHECK, "error", None, port, "output_x_after_reset", set(),
                f"output {port} can be X {after} clock(s) after the reset "
                f"was held for {reset_cycles}: some register or memory the "
                "reset does not set reaches it (counterexample at step "
                f"{step}, trace under {WORK_SUBDIR}/run)", "sby",
                step=step, clocks_after_reset=after))
        if violations:
            return status, violations
        raise CheckError("sby reported FAIL but no failed assertion named a "
                         f"watched port: {output[-2000:]}")
    if status not in ("UNKNOWN", "TIMEOUT"):
        raise CheckError(f"sby ended {status}: the encoded design did not "
                         f"run, so nothing was proven: {output[-2000:]}")
    violations.append(checklib.violation(
        CHECK, "error", None, None, "xprop_unproven", set(),
        f"sby ended {status}: no counterexample, but no proof either at "
        f"induction depth {depth}; raise spec formal.depth (or "
        "formal.timeout_s) so the proof closes", "sby", depth=depth))
    return status, violations


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--timeout", type=float,
                    help="sby timeout in seconds, overriding formal.timeout_s")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    cfg = settings(spec)
    if args.timeout is not None:
        cfg["timeout_s"] = args.timeout
    plan = port_plan(spec)
    sources = collect_sources(ws)
    work = ws / WORK_SUBDIR
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    wrap, asserts = wrapper(top, plan, cfg["reset_cycles"])
    (work / f"{WRAP}.sv").write_text(wrap, encoding="utf-8")
    (work / "xprop.sby").write_text(
        sby_text(top, sources, cfg["depth"], cfg["timeout_s"],
                 list(cfg["x_init_ok"])),
        encoding="utf-8")
    output = run_sby(work, cfg["timeout_s"])
    (work / "sby.log").write_text(output, encoding="utf-8")
    status, violations = findings(output, asserts, cfg["reset_cycles"],
                                  cfg["depth"])
    payload = checklib.report(
        SCRIPT, ws / "rtl", violations, top=top, sby_status=status,
        proven=status == "PASS", watched=plan["watch"],
        x_ok=plan["x_ok"], x_init_ok=cfg["x_init_ok"], reset=plan["reset"],
        reset_cycles=cfg["reset_cycles"], depth=cfg["depth"])
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

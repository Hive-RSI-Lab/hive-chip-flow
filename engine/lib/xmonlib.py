"""xmonlib.py - the X/Z output monitor the sim and holdout gates compile
beside the design (#84).

Icarus is X-optimistic in RTL: `if (x)` takes the else branch, so an output
register a reset never reaches can read 0 in sim and pass every test, and
the missing reset only shows at gate-level sim after harden - or on
silicon. The monitor is one extra Verilog root module, compiled in with
`-s`, that reads the top's ports by hierarchical name. On every falling
edge of the clock (the outputs settled after the rising edge's update), once
the bench has held the reset active, let it go, and the block has seen it
gone - `latency` rising edges later, the tile pad's reset synchroniser
(ttlib.pad_reset_latency) when the run drives the block through the pad,
and at least one - it prints one line per output port the first time any
bit of that port is X or Z. A new reset re-arms it, so each test that
resets gets its own report.
check_sim / check_holdout read those lines back from the sim log and fail
the run naming the port, the sim time and the test running then.

What it needs from spec.yaml:
- the clock: clock.primary (or the only clock.domains entry) when it is a
  1-bit input port, else a 1-bit input named `clk`;
- the reset: `sim.reset: {port, active: high|low}` when given, else the
  input mapped to the tile's rst_n pin in tt_pins (`~rst_n`: active high),
  else a 1-bit input named by convention (rst/reset active high, rst_n/
  rstn/reset_n/resetn/arst_n active low);
- `sim.x_ok: {port: reason}`: outputs that may legitimately read X after
  reset (a FIFO's read data before anything was written), each with a
  non-empty reason. Every other output port is watched.

No clock or no reset found: the monitor is off and plan() says why; the
gate records that in its report instead of failing.
"""
from __future__ import annotations

import re
from pathlib import Path

from checklib import CheckError, violation

MODULE = "cf_xmon__"
TAG = "CF_XMON"
ACTIVE_HIGH = ("rst", "reset", "rst_i", "reset_i", "arst", "srst")
ACTIVE_LOW = ("rst_n", "rstn", "reset_n", "resetn", "rst_ni", "arst_n",
              "rst_b", "nrst", "nreset")

_LINE = re.compile(rf"{TAG} port=(\S+) t_ps=(\d+) value=(\S+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


class XmonError(CheckError):
    """A malformed sim.reset or sim.x_ok in spec.yaml."""


def _inputs(spec: dict) -> dict[str, int]:
    out = {}
    for name, p in (spec.get("ports") or {}).items():
        if isinstance(p, dict) and p.get("dir") == "input":
            out[str(name)] = int(p.get("width") or 1)
    return out


def _clock(spec: dict, inputs: dict[str, int]) -> str | None:
    clock = spec.get("clock") if isinstance(spec.get("clock"), dict) else {}
    domains = clock.get("domains") or []
    cand = clock.get("primary") or (domains[0] if len(domains) == 1 else None)
    if isinstance(cand, str) and inputs.get(cand) == 1:
        return cand
    return "clk" if inputs.get("clk") == 1 else None


def _reset(spec: dict, sim: dict, inputs: dict[str, int]):
    given = sim.get("reset")
    if given is not None:
        if not isinstance(given, dict) or given.get("active") not in (
                "high", "low") or inputs.get(given.get("port")) != 1:
            raise XmonError(
                "spec sim.reset must be {port: <a 1-bit input port>, "
                f"active: high|low}}, got {given!r}")
        return given["port"], given["active"] == "high"
    for name, expr in (spec.get("tt_pins") or {}).items():
        if inputs.get(name) != 1 or not isinstance(expr, str):
            continue
        e = expr.replace(" ", "")
        if e in ("rst_n", "~rst_n"):
            return name, e.startswith("~")
    for name in ACTIVE_HIGH:
        if inputs.get(name) == 1:
            return name, True
    for name in ACTIVE_LOW:
        if inputs.get(name) == 1:
            return name, False
    return None


def plan(spec: dict) -> dict:
    """{"on": True, clock, reset, active_high, watch: [ports], x_ok: {..}}
    or {"on": False, "why": ...}. XmonError on a malformed sim.reset or
    sim.x_ok."""
    sim = spec.get("sim") if isinstance(spec.get("sim"), dict) else {}
    outputs = [str(n) for n, p in (spec.get("ports") or {}).items()
               if isinstance(p, dict) and p.get("dir") == "output"]
    x_ok = sim.get("x_ok") or {}
    if not isinstance(x_ok, dict):
        raise XmonError(f"spec sim.x_ok must map output port -> reason, "
                        f"got {x_ok!r}")
    for port, why in x_ok.items():
        if port not in outputs:
            raise XmonError(f"spec sim.x_ok names {port!r}, which is not an "
                            "output port in spec ports")
        if not isinstance(why, str) or not why.strip():
            raise XmonError(f"spec sim.x_ok[{port!r}] needs a reason: why "
                            "this output may read X or Z after reset")
    inputs = _inputs(spec)
    clk = _clock(spec, inputs)
    rst = _reset(spec, sim, inputs)
    if clk is None:
        return {"on": False, "why": "no 1-bit clock input found (clock."
                "domains / a port named clk)"}
    if rst is None:
        return {"on": False, "why": "no reset input found; declare spec "
                "sim.reset: {port, active: high|low}"}
    watch = [p for p in outputs if p not in x_ok and _IDENT.match(p)]
    if not watch:
        return {"on": False, "why": "no output port left to watch"}
    return {"on": True, "clock": clk, "reset": rst[0],
            "active_high": rst[1], "watch": watch, "x_ok": dict(x_ok)}


def monitor_verilog(p: dict, hdl_top: str, latency: int = 0) -> str:
    """The monitor module for plan p over toplevel hdl_top; latency is the
    rising edges between the bench releasing reset and the block seeing it
    released (the pad synchroniser's, 0 with no pad in front)."""
    act, idle = ("1'b1", "1'b0") if p["active_high"] else ("1'b0", "1'b1")
    rst = f"{hdl_top}.{p['reset']}"
    clk = f"{hdl_top}.{p['clock']}"
    need = max(int(latency), 1)
    n = len(p["watch"])
    checks = "\n".join(
        f"      if (!told[{i}] && ^({hdl_top}.{port}) === 1'bx) begin\n"
        f"        told[{i}] = 1'b1;\n"
        f"        $display(\"{TAG} port={port} t_ps=%0d value=%b\", "
        f"$realtime * 1000.0, {hdl_top}.{port});\n"
        f"        $fflush;\n"
        f"      end"
        for i, port in enumerate(p["watch"]))
    # $realtime is in this module's timescale (the run's 1ns), so *1000
    # gives integer ps for the log. $fflush keeps the line ahead of
    # cocotb's next "running" line in sim.log.
    return f"""\
// generated by engine/lib/xmonlib.py (#84): fails a run whose output port
// reads X or Z on a clock edge after the bench has applied reset.
module {MODULE};
  reg seen_reset = 1'b0;
  integer quiet = 0;
  reg [{n - 1}:0] told = {n}'b0;
  always @(posedge {clk}) begin
    if ({rst} === {act}) begin
      seen_reset = 1'b1;
      quiet = 0;
      told = {n}'b0;
    end else if (seen_reset && {rst} === {idle} && quiet < {need}) begin
      quiet = quiet + 1;
    end
  end
  always @(negedge {clk}) begin
    if (seen_reset && quiet >= {need} && {rst} === {idle}) begin
{checks}
    end
  end
endmodule
"""


def write_monitor(spec: dict, hdl_top: str, out_dir: Path,
                  latency: int = 0):
    """(plan, extra sources, extra build args): the monitor written under
    out_dir when plan(spec) is on, else no sources and no args. latency:
    the pad reset synchroniser's edges when the run goes through the pad."""
    p = plan(spec)
    if not p["on"]:
        return p, [], []
    p["latency"] = int(latency)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{MODULE}.v"
    path.write_text(monitor_verilog(p, hdl_top, latency), encoding="utf-8")
    return p, [path], ["-s", MODULE]


def read_hits(sim_log: Path) -> list[dict]:
    """[{port, t_ps, value, test}] from the monitor's lines in sim.log, in
    order; test is the cocotb test running at the hit's sim time (cocotb
    logs "<t>ns ... running <module>.<test> (i/n)" ahead of each test in
    the same log; matched by time, since the two writers buffer apart),
    or None."""
    hits, starts = [], []
    run_re = re.compile(r"([\d.]+)\s*ns\b.*?running (?:\S+\.)?(\w+) "
                        r"\(\d+/\d+\)")
    try:
        text = Path(sim_log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for line in text.splitlines():
        m = run_re.search(line)
        if m:
            starts.append((float(m.group(1)) * 1000.0, m.group(2)))
            continue
        m = _LINE.search(line)
        if m:
            hits.append({"port": m.group(1), "t_ps": int(m.group(2)),
                         "value": m.group(3), "test": None})
    for hit in hits:
        for t0, name in starts:
            if t0 <= hit["t_ps"]:
                hit["test"] = name
    return hits


def describe(hit: dict, name_test: bool = True) -> str:
    t_ns = hit["t_ps"] / 1000.0
    during = f" during {hit['test']}" if hit["test"] and name_test else ""
    return (f"output {hit['port']} reads {hit['value']} (X/Z) at "
            f"{t_ns:g} ns{during}, after reset: a register on it is never "
            "reset or an input reaches it undriven. Icarus' optimistic X "
            "hides this in RTL; gate-level sim and silicon will not. Reset "
            "it, or if X there is truly harmless, list the port under spec "
            "sim.x_ok with the reason")


def findings(sim_log: Path, check: str, name_test: bool = True) -> list[dict]:
    """One `output_x` violation per (port, test) the monitor reported in
    sim_log. name_test=False (holdout) keeps the held-out test's name out
    of the message and the module field."""
    out, seen = [], set()
    for hit in read_hits(sim_log):
        key = (hit["port"], hit["test"])
        if key in seen:
            continue
        seen.add(key)
        out.append(violation(
            check, "error", None, hit["test"] if name_test else None,
            "output_x", [], describe(hit, name_test), "xmonlib",
            port=hit["port"], t_ns=hit["t_ps"] / 1000.0))
    return out


def facts(p: dict) -> dict:
    """The report's x_monitor entry: what was watched, or why nothing was."""
    if not p["on"]:
        return {"on": False, "why": p["why"]}
    return {"on": True, "clock": p["clock"], "reset": p["reset"],
            "reset_active": "high" if p["active_high"] else "low",
            "latency": p.get("latency", 0),
            "watched": p["watch"], "x_ok": p["x_ok"]}

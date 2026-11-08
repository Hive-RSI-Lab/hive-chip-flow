#!/usr/bin/env python
"""xsim.py - a bench's measures on ngspice and on Xyce, side by side.

    xsim.py --workspace DIR [--bench NAME ...] [--tol-dc 0.02]
            [--tol-tran 0.05] [--log-dir DIR] [--timeout S] [--out FILE]

The second-simulator cross-check (issue #105). Run on demand, never a gate:
it records nothing and no gate reads it. Every bench marked with a
`* xsim: true` comment line, in tb/*.cir or xsim/*.cir (a bench written for
the cross-check alone, kept out of tb/ so the gates never see it), is
materialised at tt the way sim_run.py does it, run once on ngspice as
written and once on Xyce after the translation below, and each `.measure`
is compared: within --tol-dc (default 2%) for a DC, AC, noise or
transient value, within --tol-tran (default 5%) for a transient timing
(TRIG/TARG, WHEN). A measure one engine gives and the other fails or
leaves out is a difference too.

A difference is a question about the bench, not a verdict on either
engine: a bench that leans on an ngspice-only default (a cap `ic=` with no
`.ic` or `uic`, which ngspice drops and Xyce honours) differs; so can the
PDK's Xyce port of the model cards, which is not the ngspice deck word for
word (docs/xsim.md lists what it changes).

A bench must be plain SPICE for this: its `.control` block, if any, holds
nothing but `run` (and `set noaskquit`), and no `.measure noise` (ngspice
has none; xsim adds `onoise_rms`, the output noise in V rms over the
`.noise` sweep, on both engines). Anything else is refused, not guessed at.

Translation for Xyce: the PDK's libs.tech/ngspice/design.spice and
sm141064.spice become its libs.tech/xyce/design.xyce and sm141064.xyce
(same .lib section names); the `.control` block is dropped; `TRIG v(x)
VAL=v` becomes `TRIG v(x)=v` (TARG too); the {{SIZING}} line moves to the
top of the deck, because Xyce keeps the FIRST definition of a `.param`
where ngspice keeps the last; vp() is read in degrees on both (ngspice
gives radians, xsim converts). Any other top-level `.param` defined twice
is refused: the two engines would run different circuits.

Exit 0 when every marked bench agrees, 1 on a difference, 2 on an error
(checklib's contract); no marked bench at all is an error, not a pass.
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import sim_run  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "xsim"
DEFAULT_TIMEOUT = 300.0
TOL_DC = 0.02
TOL_TRAN = 0.05

MARK_RE = re.compile(r"^\*\s*xsim:\s*true\b", re.I | re.M)
CONTROL_RE = re.compile(r"^[ \t]*\.control\b.*?^[ \t]*\.endc\b[^\n]*\n?",
                        re.I | re.M | re.S)
CONTROL_OK = re.compile(r"^\s*(run|set\s+noaskquit)\s*$", re.I)
MEASURE_RE = re.compile(r"^\s*\.meas(?:ure)?\s+(\w+)\s+(\w+)\s+(.*)$", re.I)
NOISE_RE = re.compile(
    r"^\s*\.noise\s+\S+\s+\S+\s+(?:dec|oct|lin)\s+\S+\s+(\S+)\s+(\S+)",
    re.I | re.M)
ANALYSIS_RE = re.compile(r"^\s*\.(tran|ac|dc|noise|op|tf|pz|sens)\b",
                         re.I | re.M)
PARAM_RE = re.compile(r"^\s*\.param\s+(.*)$", re.I)
PARAM_NAME_RE = re.compile(r"(\w+)\s*=")
SUBCKT_RE = re.compile(r"^\s*\.subckt\b", re.I)
ENDS_RE = re.compile(r"^\s*\.ends\b", re.I)
NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
XYCE_MEAS_RE = re.compile(rf"^\s*(\w+)\s*=\s*({NUM}|FAILED)\s*$", re.I)
NG_NOISE_RE = re.compile(rf"^\s*onoise_rms\s*=\s*({NUM})\s*$", re.I | re.M)
LIB_MAP = (("/libs.tech/ngspice/design.spice", "/libs.tech/xyce/design.xyce"),
           ("/libs.tech/ngspice/sm141064.spice",
            "/libs.tech/xyce/sm141064.xyce"))
TIMING_KINDS = {"trig", "when"}
NOISE_NAME = "onoise_rms"


class Refused(CheckError):
    """A bench xsim cannot translate faithfully."""


def joined_lines(text: str) -> list[str]:
    """The deck's lines with `+` continuations folded in."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("+") and out:
            out[-1] += " " + line[1:].strip()
        else:
            out.append(line)
    return out


def measure_table(text: str) -> dict[str, dict]:
    """{name.lower(): {analysis, kind, phase, timing}} for every .measure:
    `phase` when it reads vp() (ngspice radians, Xyce degrees), `timing`
    when it is a transient TRIG/TARG or WHEN (a time, not a value)."""
    table = {}
    for line in joined_lines(text):
        m = MEASURE_RE.match(line)
        if not m:
            continue
        an, name, rest = m.group(1).lower(), m.group(2).lower(), m.group(3)
        kind = rest.split()[0].lower() if rest.split() else ""
        table[name] = {
            "analysis": an, "kind": kind,
            "phase": bool(re.search(r"\bvp\s*\(", rest, re.I)),
            "timing": an == "tran" and kind in TIMING_KINDS,
        }
    return table


def refusals(text: str) -> list[str]:
    """Why this bench is not plain enough to translate, [] when it is."""
    why = []
    for block in CONTROL_RE.findall(text):
        body = block.splitlines()[1:-1]
        bad = [ln.strip() for ln in body
               if ln.strip() and not ln.lstrip().startswith("*")
               and not CONTROL_OK.match(ln)]
        if bad:
            why.append(f".control block runs more than `run`: {bad[0]!r}")
    for name, m in measure_table(text).items():
        if m["analysis"] == "noise":
            why.append(f".measure noise {name}: ngspice has no noise "
                       f"measures (xsim adds {NOISE_NAME} itself)")
    plain = CONTROL_RE.sub("", text)
    analyses = ANALYSIS_RE.findall(plain)
    if len(analyses) > 1:
        why.append("more than one analysis ("
                   + ", ".join("." + a.lower() for a in analyses)
                   + "): Xyce runs one per deck, split the bench")
    if not analyses:
        why.append("no .tran/.ac/.dc/.noise/.op line")
    if "$&" in plain:
        why.append("a `$&` vector reference outside .control")
    return why


def top_params(text: str) -> dict[str, int]:
    """{name: times defined} for `.param` lines outside any .subckt."""
    seen: dict[str, int] = {}
    depth = 0
    for line in joined_lines(text):
        if SUBCKT_RE.match(line):
            depth += 1
        elif ENDS_RE.match(line):
            depth = max(0, depth - 1)
        elif depth == 0:
            m = PARAM_RE.match(line)
            if m:
                for name in PARAM_NAME_RE.findall(m.group(1)):
                    seen[name.lower()] = seen.get(name.lower(), 0) + 1
    return seen


def redefined_params(bench_text: str, netlist_text: str,
                     sizing: dict) -> list[str]:
    """Top-level params defined more than once across the netlist and the
    bench, sizing's own names aside (xsim hoists that line on Xyce)."""
    counts = top_params(netlist_text)
    for k, v in top_params(bench_text).items():
        counts[k] = counts.get(k, 0) + v
    skip = {str(k).lower() for k in sizing}
    return sorted(k for k, v in counts.items() if v > 1 and k not in skip)


def noise_band(text: str) -> tuple[str, str] | None:
    m = NOISE_RE.search(CONTROL_RE.sub("", text))
    return (m.group(1), m.group(2)) if m else None


def _before_end(text: str, extra: str) -> str:
    """`extra` inserted before the deck's final `.end` (appended if none)."""
    lines = text.rstrip("\n").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^\s*\.end\s*$", lines[i], re.I):
            return "\n".join(lines[:i] + [extra.rstrip("\n"), lines[i]]) + "\n"
    return text.rstrip("\n") + "\n" + extra.rstrip("\n") + "\n.end\n"


def ngspice_deck(text: str) -> str:
    """The deck ngspice runs: its own .control dropped for one that issues
    the deck's analysis as a command (a dot `.ac` under a bare `run` in
    batch mode prints its measures and then fails a second, empty AC pass:
    "no data saved for A.C. Small signal analysis"), plus the noise
    integral when the analysis is a `.noise`."""
    lines, cmd = [], None
    for ln in joined_lines(CONTROL_RE.sub("", text)):
        if ANALYSIS_RE.match(ln) and cmd is None:
            cmd = ln.strip()[1:]
        else:
            lines.append(ln)
    ctl = [".control", "set noaskquit", cmd or "run"]
    if noise_band(text):
        ctl += ["setplot noise2", "echo",
                'echo "  Measurements for Noise Analysis (xsim)"',
                f'echo "{NOISE_NAME} = $&onoise_total"']
    ctl.append(".endc")
    return _before_end("\n".join(lines) + "\n", "\n".join(ctl))


def _xyce_measure(line: str) -> str:
    return re.sub(r"\b(TRIG|TARG)\s+(\w+\([^)]*\))\s+VAL\s*=", r"\1 \2=",
                  line, flags=re.I)


def xyce_deck(text: str, sizing_line: str) -> str:
    """The deck Xyce runs (module docstring, Translation)."""
    body = CONTROL_RE.sub("", text)
    for ng, xy in LIB_MAP:
        body = body.replace(ng, xy)
    lines = [(_xyce_measure(ln) if MEASURE_RE.match(ln) else ln)
             for ln in joined_lines(body)]
    if sizing_line and not sizing_line.startswith("*"):
        lines[1:1] = ["* xsim: sizing hoisted, Xyce keeps the first .param",
                      sizing_line]
    out = "\n".join(lines) + "\n"
    band = noise_band(text)
    if band:
        out = _before_end(out, f".measure noise {NOISE_NAME}_sq INTEG onoise "
                               f"FROM={band[0]} TO={band[1]}")
    return out


def parse_xyce(deck: Path) -> tuple[dict[str, float], set[str]]:
    """(values, failed) from every measure file Xyce wrote for `deck`
    (<deck>.mt0 / .ma0 / .ms0 / ...)."""
    vals: dict[str, float] = {}
    failed: set[str] = set()
    for f in sorted(deck.parent.glob(deck.name + ".m*")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = XYCE_MEAS_RE.match(line)
            if not m:
                continue
            name = m.group(1).lower()
            if m.group(2).upper() == "FAILED":
                failed.add(name)
            else:
                vals[name] = float(m.group(2))
    if f"{NOISE_NAME}_sq" in vals:
        v = vals.pop(f"{NOISE_NAME}_sq")
        vals[NOISE_NAME] = math.sqrt(v) if v >= 0 else float("nan")
    if f"{NOISE_NAME}_sq" in failed:
        failed.discard(f"{NOISE_NAME}_sq")
        failed.add(NOISE_NAME)
    return vals, failed


def run_xyce(eda_bin: Path, deck: Path, cwd: Path,
             timeout: float) -> tuple[str, int]:
    try:
        proc = subprocess.run([str(eda_bin), "Xyce", str(deck.resolve())],
                              cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired:
        return f"[xsim] Xyce timed out after {timeout:g}s", -1


def compare(table: dict[str, dict], ng: dict[str, float], ng_failed: set,
            xy: dict[str, float], xy_failed: set, tol_dc: float,
            tol_tran: float) -> list[dict]:
    """One row per measure: values, relative difference, tolerance, ok.
    Pure."""
    rows = []
    for name in sorted(set(table) | set(ng) | set(xy)):
        meta = table.get(name, {"analysis": "noise" if name == NOISE_NAME
                                else "?", "phase": False, "timing": False})
        tol = tol_tran if meta["timing"] else tol_dc
        a, b = ng.get(name), xy.get(name)
        if a is not None and meta["phase"]:
            a = math.degrees(a)
        row = {"measure": name, "analysis": meta["analysis"],
               "ngspice": a, "xyce": b, "tol": tol,
               "kind": "timing" if meta["timing"] else "value"}
        if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
            row.update(ok=False, rel=None, why=(
                "both engines failed it" if name in ng_failed and name in xy_failed
                else f"ngspice {'failed' if name in ng_failed else 'gave nothing'}"
                if a is None else
                f"Xyce {'failed' if name in xy_failed else 'gave nothing'}"))
            if name in ng_failed and name in xy_failed:
                row["ok"] = True  # the same trigger never met on both
        else:
            d = a - b
            if meta["phase"]:
                d = (d + 180.0) % 360.0 - 180.0
            scale = max(abs(a), abs(b))
            rel = 0.0 if scale == 0 else abs(d) / scale
            row.update(rel=checklib.rnd(rel, 6), ok=rel <= tol)
        rows.append(row)
    return rows


def find_marked(ws: Path, only: list[str] | None) -> list[Path]:
    benches = [p for sub in ("tb", "xsim") for p in sorted((ws / sub).glob("*.cir"))
               if MARK_RE.search(p.read_text(encoding="utf-8", errors="replace"))]
    if only:
        want = {Path(n).name for n in only}
        missing = want - {p.name for p in benches}
        if missing:
            raise CheckError(f"not a `* xsim: true` bench here: {sorted(missing)}")
        benches = [p for p in benches if p.name in want]
    if not benches:
        raise CheckError(f"no tb/*.cir or xsim/*.cir under {ws} carries a "
                         "`* xsim: true` line")
    return benches


def nominal_vdd(ws: Path) -> float:
    for p in (ws / "spec" / "spec.yaml", ws / "spec.yaml"):
        if p.is_file():
            vdd = (speclib.load_spec(p).get("supply") or {}).get("vdd")
            if isinstance(vdd, bool) or not isinstance(vdd, (int, float)):
                raise CheckError(f"{p} has no numeric supply.vdd")
            return float(vdd)
    raise CheckError(f"no spec/spec.yaml (or spec.yaml) under {ws}")


def run_bench(eda_bin: Path, bench: Path, subs: dict, sizing: dict,
              netlist_text: str, log_dir: Path, timeout: float,
              tol_dc: float, tol_tran: float) -> tuple[dict, list[dict]]:
    name = f"{bench.parent.name}/{bench.name}"
    text = bench.read_text(encoding="utf-8")
    why = refusals(text)
    dup = redefined_params(text, netlist_text, sizing)
    if dup:
        why.append("top-level .param defined twice (ngspice keeps the last, "
                   f"Xyce the first): {', '.join(dup)}")
    if why:
        return ({"bench": name, "refused": why}, [checklib.violation(
            SCRIPT, "error", name, None, "xsim_refused", [], f"{name}: "
            + "; ".join(why), "xsim")])
    timeout = sim_run.bench_timeout(name, text, timeout)
    table = measure_table(text)
    stem = bench.stem
    ng_path = log_dir / f"{stem}__ngspice.cir"
    xy_path = log_dir / f"{stem}__xyce.cir"
    ng_path.write_text(simlib.materialize(ngspice_deck(text), subs), "utf-8")
    xy_subs = {**subs, "SIZING": "* sizing: hoisted to the top (xsim)"}
    xy_path.write_text(simlib.materialize(xyce_deck(text, subs["SIZING"]),
                                          xy_subs), "utf-8")
    for f in log_dir.glob(xy_path.name + ".m*"):
        f.unlink()

    out, err, rc = simlib.run_ngspice(eda_bin, ng_path, log_dir, timeout)
    ng = simlib.parse_measures(out)
    ng_failed = simlib.parse_failed_measures(err)
    ng_err = simlib.detect_engine_errors(out + "\n" + err)
    xy_text, xy_rc = run_xyce(eda_bin, xy_path, log_dir, timeout)
    (log_dir / f"{stem}__ngspice.log").write_text(out + "\n" + err, "utf-8")
    (log_dir / f"{stem}__xyce.log").write_text(xy_text, "utf-8")
    xy, xy_failed = parse_xyce(xy_path)
    rows = compare(table, ng, ng_failed, xy, xy_failed, tol_dc, tol_tran)

    violations = []
    if ng_err:
        violations.append(checklib.violation(
            SCRIPT, "error", name, None, "xsim_engine_error", ng_err,
            f"{name}: ngspice: {', '.join(ng_err)}", "ngspice"))
    if xy_rc != 0:
        tail = [ln.strip() for ln in xy_text.splitlines()
                if re.search(r"error|failed|timed out", ln, re.I)
                and "MPI_ABORT" not in ln][:3] or ["(no error line)"]
        violations.append(checklib.violation(
            SCRIPT, "error", name, None, "xsim_engine_error", [],
            f"{name}: Xyce exited {xy_rc}: {' | '.join(tail)[:400]}", "xyce"))
    if not table and not noise_band(text):
        violations.append(checklib.violation(
            SCRIPT, "error", name, None, "xsim_no_measures", [],
            f"{name}: no .measure and no .noise: nothing to compare", "xsim"))
    for r in rows:
        if not r["ok"]:
            msg = (f"{name} {r['measure']}: ngspice {r['ngspice']!r} vs Xyce "
                   f"{r['xyce']!r}" + (f" ({r['rel']:.2%} > {r['tol']:.0%})"
                                       if r["rel"] is not None
                                       else f" ({r['why']})"))
            violations.append(checklib.violation(
                SCRIPT, "error", name, None, "xsim_differs", [r["measure"]],
                msg, "xsim"))
    return ({"bench": name, "measures": rows, "ngspice_rc": rc,
             "xyce_rc": xy_rc}, violations)


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--bench", action="append",
                    help="only this marked bench (file name; repeatable)")
    ap.add_argument("--tol-dc", type=float, default=TOL_DC,
                    help="relative tolerance, DC/AC/noise/transient values")
    ap.add_argument("--tol-tran", type=float, default=TOL_TRAN,
                    help="relative tolerance, transient timing")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--log-dir", help="keep the decks and logs here "
                    "(default: a temporary directory, removed after)")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    benches = find_marked(ws, args.bench)
    eda_bin = sim_run.EDA_BIN
    t_root = sim_run.toolchain_root(eda_bin)
    corner = sim_run.corners_mod.corners_by_name(
        sim_run.corners_mod.load(), ["tt"])[0]
    netlist = sim_run.find_netlist(ws)
    sizing = sim_run.load_sizing(ws)
    subs = sim_run.build_subs(t_root, netlist, corner, nominal_vdd(ws), sizing)
    netlist_text = netlist.read_text(encoding="utf-8", errors="replace")

    keep = args.log_dir is not None
    log_dir = Path(args.log_dir) if keep else Path(tempfile.mkdtemp(prefix="xsim-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    results, violations = [], []
    try:
        for bench in benches:
            res, v = run_bench(eda_bin, bench, subs, sizing, netlist_text,
                               log_dir, args.timeout, args.tol_dc,
                               args.tol_tran)
            results.append(res)
            violations += v
    finally:
        if not keep:
            shutil.rmtree(log_dir, ignore_errors=True)
    return checklib.report(SCRIPT, ws, violations, corner="tt",
                           tol_dc=args.tol_dc, tol_tran=args.tol_tran,
                           results=results), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

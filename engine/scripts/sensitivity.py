#!/usr/bin/env python
"""sensitivity.py - rank the sizing parameters by how hard each one moves
one failing measure (#104). The analog fix loop's sizing fixer otherwise
guesses which knob to turn; this hands it d(measure)/d(param) at the
failing corner, so a `sim_bound_fail` work order (fix_dispatch.py) says
"l_out moves iout_raw 4x more than anything else" instead of nothing.

    sensitivity.py --workspace DIR --measure NAME [--corner NAME]
                   [--condition pad|no_pad] [--step 0.05] [--top 3]
                   [--timeout SEC] [--workers 1|2] [--out FILE]

For every numeric parameter of sizing/sizing.yaml, the bench that declares
`--measure` runs twice at the corner, with that one parameter at
value*(1+h) and value*(1-h) (h = --step) and every other at its value: a
central difference. The steps are neither snapped to a grid nor clipped to
the parameter's min/max - a derivative at the edge of the box is still the
derivative, and the report says how far the box lets the parameter go.

Each ranked entry carries:
- `derivative`: d(measure)/d(param), in the measure's units per the
  parameter's units;
- `normalised`: derivative * value / measure - the % change in the measure
  per % change in the parameter, so parameters in metres, ohms and amps rank
  on one scale (the ranking is by its magnitude);
- `m_up`, `m_down`: the two measured values;
- `to_bound`: the relative parameter step (+0.12 = raise it 12%) that the
  linear model says brings the measure to the bound edge it violates (the
  nearest edge when it passes) - an estimate only: the fixer re-simulates;
- `room`: how far the parameter's own sizing.yaml min/max lets it move in
  that direction, as a relative step (null without a limit).
When one of the two steps fails to print the measure (a length stepped
below the PDK's minimum), the derivative is the other step's one-sided
difference against the baseline, with `one_sided` naming the side that
ran and `error` the one that did not. A parameter whose bench failed at
both steps has a `derivative` of null and an `error`, and ranks after
every parameter with a number - it is never reported as 0 (insensitive),
which it is not known to be. A non-numeric or zero value is listed in `skipped`: it has no
relative step.

Without --corner, the measure runs once at every corner of the spec's
sweep (sim_run.spec_sweep) it is scored at, and the corner with the least
margin (optimise.measure_margin) is the one differentiated. The decks go
to log/sensitivity/<measure>__<corner>/, one subdirectory per run.
Report status is "pass" whether or not the measure itself passes: the
report is information for a fixer, not a gate.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import padlib  # noqa: E402
import sim_run  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "sensitivity"
DEFAULT_STEP = 0.05
DEFAULT_TOP = 3
DEFAULT_TIMEOUT = 60.0
MAX_WORKERS = 2
OUT_SUBDIR = "log/sensitivity"


def measure_margin(value: float, bound: dict) -> float:
    """optimise.measure_margin - imported lazily so this script does not
    pull optimise's import chain into fix_dispatch."""
    import optimise
    return optimise.measure_margin(value, bound)


def numeric_value(spec) -> float | None:
    val = spec.get("value") if isinstance(spec, dict) else spec
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def with_value(sizing: dict, name: str, value: float) -> dict:
    out = dict(sizing)
    spec = sizing[name]
    out[name] = ({**spec, "value": value} if isinstance(spec, dict)
                 else value)
    return out


class Bench:
    """The one bench that declares a measure, ready to run at any sizing."""

    def __init__(self, ws: Path, measure: str, eda_bin: Path | None,
                 timeout: float, condition: str | None):
        self.ws = ws
        self.measure = measure.lower()
        self.eda_bin = eda_bin or sim_run.EDA_BIN
        self.timeout = timeout
        self.condition = condition
        spec = speclib.load_spec(ws / "spec" / "spec.yaml")
        vdd = (spec.get("supply") or {}).get("vdd")
        if isinstance(vdd, bool) or not isinstance(vdd, (int, float)):
            raise CheckError("spec.yaml has no numeric 'supply.vdd' - run "
                             "spec_lint first")
        self.nominal_vdd = vdd
        self.top = str(spec.get("top"))
        self.netlist = sim_run.find_netlist(ws)
        found = []
        for bench_path, bounds_path in sim_run.find_benches(ws):
            bounds = simlib.load_bounds(bounds_path)
            if any(b["measure"].lower() == self.measure for b in bounds):
                found.append((bench_path, bounds))
        if not found:
            raise CheckError(f"no tb/*.bounds.json declares a measure "
                             f"'{measure}'")
        if len(found) > 1:
            raise CheckError(
                f"'{measure}' is declared by more than one bench ("
                + ", ".join(p.name for p, _ in found) + ") - sensitivity "
                "needs one")
        self.bench_path, bounds = found[0]
        self.bounds = [b for b in bounds if b["measure"].lower() == self.measure]
        self.text = self.bench_path.read_text(encoding="utf-8")
        self.pad = None
        if condition == "pad":
            try:
                self.pad = sim_run.pad_setup(spec, self.netlist.read_text(
                    encoding="utf-8", errors="replace"))
            except padlib.PadRefused as exc:
                raise CheckError(f"the pad condition cannot run: {exc}") from exc
            if self.pad is None:
                raise CheckError("--condition pad, but spec.yaml lists no "
                                 "ua_pins")
        self.t_root = sim_run.toolchain_root(self.eda_bin)

    def bound_at(self, corner: dict) -> dict | None:
        names = corners_mod.names_of(corner)
        scored = [b for b in self.bounds if simlib.scored_at(b, names)
                  and padlib.scored_in(b, self.condition)]
        if not scored:
            return None
        return scored[0]

    def run(self, corner: dict, sizing: dict, out_dir: Path) -> dict:
        """{value, error}: the measure at one corner and sizing."""
        subs = sim_run.build_subs(self.t_root, self.netlist, corner,
                                  self.nominal_vdd, sizing)
        text = self.text
        if self.pad is not None:
            text = sim_run.pad_bench(text, self.top, self.pad, corner,
                                     subs["VDD"])
        res = sim_run.run_bench_at_corner(
            self.eda_bin, self.bench_path.name, text, self.bounds, subs,
            corner, out_dir, self.timeout, check=SCRIPT,
            condition=self.condition)
        value = res["measures"].get(self.measure)
        if value is None:
            why = (", ".join(res["engine_errors"]) if res["engine_errors"]
                   else "the bench did not print it")
            return {"value": None, "error": f"{self.measure}: {why}"}
        return {"value": float(value), "error": None}


def pick_corner(bench: Bench, sweep: list[dict], sizing: dict,
                out_root: Path, workers: int) -> tuple[dict, dict, dict]:
    """The scored corner of the sweep with the least margin."""
    corners = [c for c in sweep if bench.bound_at(c) is not None]
    if not corners:
        raise CheckError(f"'{bench.measure}' is scored at no corner of the "
                         "spec's sweep")
    with ThreadPoolExecutor(workers) as pool:
        runs = list(pool.map(
            lambda c: bench.run(c, sizing, out_root / f"baseline__{c['name']}"),
            corners))
    scored = [(measure_margin(r["value"], bench.bound_at(c)), c, r)
              for c, r in zip(corners, runs) if r["value"] is not None]
    if not scored:
        raise CheckError(f"'{bench.measure}' printed at no corner: "
                         + runs[0]["error"])
    margin, corner, run = min(scored, key=lambda t: t[0])
    return corner, run, bench.bound_at(corner)


def target_edge(value: float, bound: dict) -> float | None:
    """The bound edge the measure violates, or the nearest one."""
    lo, hi = bound.get("min"), bound.get("max")
    if hi is not None and value > hi:
        return hi
    if lo is not None and value < lo:
        return lo
    edges = [e for e in (lo, hi) if e is not None]
    return min(edges, key=lambda e: abs(e - value)) if edges else None


def room(spec, value: float, direction: float) -> float | None:
    """The relative step the param's own min/max allows in `direction`."""
    if not isinstance(spec, dict) or direction == 0:
        return None
    limit = spec.get("max") if direction > 0 else spec.get("min")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return None
    return (limit - value) / value


def rank(entries: list[dict]) -> list[dict]:
    """By |normalised|, descending; entries with no number last."""
    return sorted(entries, key=lambda e: (
        e["normalised"] is None,
        -abs(e["normalised"]) if e["normalised"] is not None else 0.0,
        e["param"]))


def analyse(ws: Path, measure: str, corner_name: str | None = None,
            condition: str | None = None, step: float = DEFAULT_STEP,
            top: int = DEFAULT_TOP, timeout: float = DEFAULT_TIMEOUT,
            workers: int = 1, eda_bin: Path | None = None) -> dict:
    if not 0 < step < 1:
        raise CheckError(f"--step must be in (0, 1), got {step}")
    workers = max(1, min(MAX_WORKERS, workers))
    sizing = sim_run.load_sizing(ws)
    params, skipped = [], []
    for name, spec in sizing.items():
        v = numeric_value(spec)
        if v is None or v == 0:
            skipped.append({"param": name, "reason": "non-numeric value"
                            if v is None else "value 0 has no relative step"})
        else:
            params.append((name, v))
    if not params:
        raise CheckError("sizing/sizing.yaml has no numeric parameter to "
                         "differentiate")
    bench = Bench(ws, measure, eda_bin, timeout, condition)
    sweep, _ = sim_run.spec_sweep(ws)
    tag = bench.measure
    if condition:
        tag += f"__{condition}"
    if corner_name:
        matches = [c for c in sweep if corner_name in corners_mod.names_of(c)]
        if not matches:
            raise CheckError(f"corner '{corner_name}' is not in the spec's "
                             "sweep: " + ", ".join(c["name"] for c in sweep))
        corner = matches[0]
        bound = bench.bound_at(corner)
        if bound is None:
            raise CheckError(f"'{measure}' is not scored at {corner_name}")
        out_root = ws / OUT_SUBDIR / f"{tag}__{corner['name']}"
        shutil.rmtree(out_root, ignore_errors=True)
        base = bench.run(corner, sizing, out_root / "baseline")
        if base["value"] is None:
            raise CheckError(f"baseline at {corner['name']}: {base['error']} - "
                                 f"see {out_root / 'baseline'}")
    else:
        out_root = ws / OUT_SUBDIR / f"{tag}__worst"
        shutil.rmtree(out_root, ignore_errors=True)
        corner, base, bound = pick_corner(bench, sweep, sizing, out_root,
                                          workers)
    m0 = base["value"]
    edge = target_edge(m0, bound)

    jobs = []
    for name, v in params:
        for sign in (+1, -1):
            jobs.append((name, v, sign))

    def one(job):
        name, v, sign = job
        d = "up" if sign > 0 else "down"
        return bench.run(corner, with_value(sizing, name, v * (1 + sign * step)),
                         out_root / f"{name}__{d}")

    with ThreadPoolExecutor(workers) as pool:
        runs = list(pool.map(one, jobs))
    by = {(j[0], j[2]): r for j, r in zip(jobs, runs)}

    entries = []
    for name, v in params:
        up, dn = by[(name, +1)], by[(name, -1)]
        e = {"param": name, "value": v, "m_up": up["value"],
             "m_down": dn["value"], "derivative": None, "normalised": None,
             "to_bound": None, "room": None}
        if up["value"] is None and dn["value"] is None:
            e["error"] = up["error"]
        else:
            if up["value"] is not None and dn["value"] is not None:
                deriv = (up["value"] - dn["value"]) / (2 * step * v)
            else:
                # one step left the models' range (an L below the PDK's
                # minimum): the other side's one-sided difference, flagged
                side, run_ = (("up", up) if up["value"] is not None
                              else ("down", dn))
                sign = 1 if side == "up" else -1
                deriv = (run_["value"] - m0) / (sign * step * v)
                e["one_sided"] = side
                e["error"] = (dn if side == "up" else up)["error"]
            e["derivative"] = deriv
            e["normalised"] = deriv * v / m0 if m0 else None
            if deriv and edge is not None:
                e["to_bound"] = (edge - m0) / (deriv * v)
                e["room"] = room(sizing[name], v, e["to_bound"])
        entries.append(e)
    ranked = rank(entries)
    lo, hi = bound.get("min"), bound.get("max")
    return {
        "script": SCRIPT, "status": "pass", "workspace": str(ws),
        "measure": bench.measure, "bench": bench.bench_path.name,
        "corner": corner["name"], "condition": condition,
        "value": m0, "bound": {"min": lo, "max": hi},
        "margin": measure_margin(m0, bound),
        "passes": (lo is None or m0 >= lo) and (hi is None or m0 <= hi),
        "step": step, "params": ranked,
        "top": [e["param"] for e in ranked[:top] if e["normalised"] is not None],
        "skipped": skipped, "decks": str(out_root),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--measure", required=True)
    ap.add_argument("--corner")
    ap.add_argument("--condition", choices=["pad", "no_pad"])
    ap.add_argument("--step", type=float, default=DEFAULT_STEP)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    return checklib.cli_wrap(SCRIPT, lambda: (analyse(
        Path(args.workspace), args.measure, args.corner, args.condition,
        args.step, args.top, args.timeout, args.workers), args.out))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""sensitivity.py - rank the sizing parameters by how hard each one moves
one failing measure (#104). The analog fix loop's sizing fixer otherwise
guesses which knob to turn; this hands it d(measure)/d(param) at the
failing corner, so a `sim_bound_fail` work order (fix_dispatch.py) says
"l_out moves iout_raw 4x more than anything else" instead of nothing.

    sensitivity.py --workspace DIR --measure NAME [--corner NAME]
                   [--condition pad|no_pad] [--step 0.05] [--top 3]
                   [--timeout SEC] [--workers 1|2] [--out FILE]
    sensitivity.py --workspace DIR --measure NAME --mismatch [--corner NAME]
                   [--top 3] [--timeout SEC] [--workers 1|2] [--out FILE]

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

--mismatch (#386, pnps #389) ranks devices instead of parameters: each
MOS, pnp and resistor's share
of the measure's random part-to-part (Pelgrom) variance at the corner, from
a netlist copy whose mismatch draws are parameters (see the mismatch
section below). It reports `sigma` (the linear estimate of the mc gate's
mismatch-only sigma), and per device `per_sigma` (the measure's move per
1-sigma of each draw), `variance`, `share` and `sigma_if_area_doubled`;
devices it cannot rank are in `unranked` with the reason.
"""
from __future__ import annotations

import argparse
import math
import re
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

    def run(self, corner: dict, sizing: dict, out_dir: Path,
            netlist: Path | None = None, text: str | None = None) -> dict:
        """{value, error}: the measure at one corner and sizing, on the
        block's netlist or on `netlist` (a rewritten copy), and on the
        bench's text or on `text`."""
        subs = sim_run.build_subs(self.t_root, netlist or self.netlist,
                                  corner, self.nominal_vdd, sizing)
        text = self.text if text is None else text
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


def _corner_and_base(bench, ws, sweep, sizing, corner_name, tag, workers,
                     measure):
    """(corner, baseline run, bound, out_root): the named corner, or the
    scored one with the least margin."""
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
        return corner, base, bound, out_root
    out_root = ws / OUT_SUBDIR / f"{tag}__worst"
    shutil.rmtree(out_root, ignore_errors=True)
    corner, base, bound = pick_corner(bench, sweep, sizing, out_root, workers)
    return corner, base, bound, out_root


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
    tag = bench.measure + (f"__{condition}" if condition else "")
    corner, base, bound, out_root = _corner_and_base(
        bench, ws, sweep, sizing, corner_name, tag, workers, measure)
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


# --- mismatch ranking (#386) ------------------------------------------------
# `--mismatch` ranks each device's share of the measure's random
# part-to-part (Pelgrom) variance, so a fix for an mc gate failure
# (yield_below_spec, sigma_above_spec) enlarges the device that matters
# instead of guessing. GF180's MOS subckts (sm141064.spice, `.lib fets_mm`)
# draw each device's mismatch as delvto='mis_vth*sw_stat_mismatch' and
# mulu0='1-mis_k*sw_stat_mismatch', mis_* = agauss(0, var_*, 1) with
# var_* = 0.7071*par_*/sqrt(area): a random draw per run, no knob to turn.
# The copy of the netlist this writes replaces each draw with a parameter:
# every MOS instance is retargeted to `<model>__mm`, a copy of the PDK's
# subckt whose two lines read delvto='mm_zv*var_vth' and
# mulu0='1-mm_zk*var_k', and the instance passes mm_zv/mm_zk from global
# `.param`s - so z=+1 is the device's own 1-sigma shift. A RES_MISMATCH
# resistor gets mis_r='mm_zr_K*(<check_mc's sigma>)', a pnp its is/bf
# draws (#389, below). Each device's terms run at z=+1 and z=-1 with every other z at 0, a central difference
# d(measure)/dz; the device's variance is the sum of its squared terms, its
# share that over the total, and sigma = sqrt(total) is the linear estimate
# of the mc gate's mismatch-only sigma at that corner.
MOS_MM_RE = re.compile(r"^[np]fet_0[356]v[03](?:_dss|_nvt)?$", re.IGNORECASE)
MM_SUFFIX = "__mm"
VTH_TERM = "delvto='mis_vth*sw_stat_mismatch'"
K_TERM = "mulu0='1-mis_k*sw_stat_mismatch'"
PDK_MOS_LIB = Path("libs.tech") / "ngspice" / "sm141064.spice"
GLOBAL_NOTE = ("mismatch only: global (die-to-die) variation moves every "
               "device together and is not in this ranking or its sigma")
# GF180's pnp_* subckts (`.lib bjt_mc`, #389) draw theirs as
# `.param mis_<t>_<model>='agauss(0,S,1)'` (t = is, bf) used in their own
# `.model` as mis_<t>_<model>*sw_stat_mismatch/sqrt(par); the copy reads
# mm_z<t>*S there instead. The npn_* subckts draw no mismatch.
PNP_MM_RE = re.compile(r"^pnp_\w+$", re.IGNORECASE)
# a model whose PDK subckt draws its own mismatch under sw_stat_mismatch=1
# when this does not parameterise it (a MOS outside MOS_MM_RE; a pnp whose
# subckt is not the shape above)
_LIVE_MISMATCH_RE = re.compile(r"^(?:pnp_|[np]fet_)", re.IGNORECASE)


def _bjt_draws(block: str, model: str) -> tuple[str, list] | None:
    """A pnp subckt with each `mis_<t>_<model>*sw_stat_mismatch` replaced
    by mm_z<t>*S, and [(t, mm_z<t>)]; None when a draw is left over."""
    m = re.escape(model)
    draws = re.findall(rf"^\.param\s+mis_(\w+?)_{m}\s*=\s*"
                       r"'agauss\(0,\s*([0-9.eE+-]+)\s*,\s*1\)'\s*$",
                       block, re.IGNORECASE | re.MULTILINE)
    out = []
    for term, sig in draws:
        z = f"mm_z{term.lower()}"
        block, n = re.subn(rf"\bmis_{term}_{m}\*sw_stat_mismatch\b",
                           f"{z}*{sig}", block, flags=re.IGNORECASE)
        if n:
            out.append((term.lower(), z))
    if not out or "sw_stat_mismatch" in block:
        return None
    return block, out


def mm_subckt(lib_text: str, model: str) -> tuple[str, list] | None:
    """The PDK's `.subckt <model>` renamed <model>__mm with its mismatch
    draws turned into parameters (header defaults 0), and the draws as
    [(term, z parameter)]: a MOS's two (vth -> mm_zv, k -> mm_zk), a pnp's
    (is -> mm_zis, bf -> mm_zbf). None when the library has no such subckt
    or its draws are not the expected shape."""
    name = re.escape(model.lower())
    m = re.search(rf"^\.subckt\s+{name}\s.*?^\.ends\s+{name}\b[^\n]*",
                  lib_text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not m:
        return None
    block = m.group(0)
    if PNP_MM_RE.match(model):
        got = _bjt_draws(block, model.lower())
        if got is None:
            return None
        block, draws = got
    else:
        if VTH_TERM not in block or K_TERM not in block:
            return None
        block = block.replace(VTH_TERM, "delvto='mm_zv*var_vth'")
        block = block.replace(K_TERM, "mulu0='1-mm_zk*var_k'")
        draws = [("vth", "mm_zv"), ("k", "mm_zk")]
    head, rest = block.split("\n", 1)
    words = head.split()
    k = next((i for i, w in enumerate(words) if "=" in w), len(words))
    words[1] = model.lower() + MM_SUFFIX
    words[k:k] = [f"{z}=0" for _, z in draws]
    rest = re.sub(rf"^(\.ends\s+){name}\b", rf"\g<1>{model.lower()}{MM_SUFFIX}",
                  rest, flags=re.IGNORECASE | re.MULTILINE)
    return " ".join(words) + "\n" + rest, draws


def instance_counts(texts: list[str]) -> dict:
    """{subckt: how many instance lines reach it from the top level}, over
    every text of the deck. A subckt reached more than once holds devices
    one parameter cannot move apart (an `m=N` instance is one line: its
    copies are one device, par=N)."""
    import check_mc
    calls: list[tuple] = []
    for text in texts:
        lines = text.split("\n")
        _, walk = check_mc._subckt_blocks(text)
        for inside, group in walk:
            inst = check_mc._instance(group, lines)
            if inst is not None:
                calls.append((inside, inst[1].lower()))
    memo: dict = {}

    def count(name, stack=()):
        if name is None:
            return 1
        if name in memo:
            return memo[name]
        if name in stack:
            return 0
        n = sum(count(enc, (*stack, name)) for enc, model in calls
                if model == name)
        memo[name] = n
        return n

    return {model: count(model) for _, model in calls}


def _retarget(lines: list[str], group: list[int], model: str) -> None:
    pat = re.compile(rf"(\s){re.escape(model)}(?=\s|$)")
    for i in group:
        new, n = pat.subn(rf"\g<1>{model}{MM_SUFFIX}", lines[i], count=1)
        if n:
            lines[i] = new
            return


def mismatch_netlist(text: str, bench_text: str, lib_text: str
                     ) -> tuple[str, list[dict], list[dict]]:
    """The netlist copy for `--mismatch` (see above). Returns (new text,
    [{device, instance, subckt, model, kind, params}] ranked devices with
    their z parameter names, [{device, model, reason}] unranked ones). Every
    MOS or pnp is retargeted, ranked or not: an unranked one passes z=0 and so
    carries no mismatch at all (listed with its reason)."""
    import check_mc
    text, _ = check_mc.add_par_for_multiplier(text, [bench_text])
    uses = instance_counts([text, bench_text])
    lines = text.split("\n")
    _, walk = check_mc._subckt_blocks(text)
    subckts: dict[str, tuple | None] = {}
    devices, unranked, resistors, live = [], [], [], []
    for inside, group in walk:
        inst = check_mc._instance(group, lines)
        if inst is None or lines[group[0]].split()[0].lower() == ".subckt":
            continue
        name, model, _ = inst
        dev = f"{inside}/{name}" if inside else name
        base = {"device": dev, "instance": name, "subckt": inside,
                "model": model}
        n = uses.get(inside, 0) if inside else 1
        many = (None if n == 1 else
                f"its subckt {inside} is instantiated {n} times: one "
                "parameter would move every copy together")
        if MOS_MM_RE.match(model) or PNP_MM_RE.match(model):
            key = model.lower()
            if key not in subckts:
                subckts[key] = mm_subckt(lib_text, key)
            if subckts[key] is None:
                live.append(dev)
                unranked.append({**base, "reason": "the PDK library has no "
                                 f"{key} subckt with the mismatch draws "
                                 "this rewrites"})
                continue
            _retarget(lines, group, model)
            if many:
                unranked.append({**base, "reason": many + "; it carries no "
                                 "mismatch in these runs"})
                continue
            k = len(devices)
            params = {t: f"{z}_{k}" for t, z in subckts[key][1]}
            check_mc._append_param(lines, group, " ".join(
                f"{z}={{{z}_{k}}}" for _, z in subckts[key][1]))
            devices.append({**base, "kind": ("pnp" if PNP_MM_RE.match(model)
                                             else "mos"), "params": params})
        elif model.lower() in check_mc.RES_MISMATCH:
            params = check_mc._all_params(group, lines)
            if many:
                unranked.append({**base, "reason": many})
            elif "mis_r" in params:
                unranked.append({**base, "reason": "writes its own mis_r"})
            elif not {"r_length", "r_width"} <= params.keys():
                unranked.append({**base, "reason": "no r_length/r_width on "
                                 "the instance"})
            else:
                resistors.append((group, base, params))
        elif _LIVE_MISMATCH_RE.match(model):
            live.append(dev)
            unranked.append({**base, "reason": "no parameterised mismatch "
                             "for this model"})
    for group, base, params in resistors:
        if live:
            unranked.append({**base, "reason": "a resistor's term needs "
                             "sw_stat_mismatch=1, which would also draw "
                             "random mismatch for " + live[0]})
            continue
        k = len(devices)
        zr = f"mm_zr_{k}"
        expr = check_mc.resistor_sigma_expr(base["model"], params)
        sig = expr[len("'agauss(0,"):-len(",1)'")]
        check_mc._append_param(lines, group, f"mis_r='{zr}*({sig})'")
        devices.append({**base, "kind": "resistor", "params": {"r": zr}})
    out = "\n".join(lines).rstrip("\n") + "\n"
    blocks = [b[0] for b in subckts.values() if b]
    if blocks:
        out += ("* --- sensitivity.py --mismatch: the PDK's MOS and pnp "
                "subckts with their mismatch draws as parameters\n" + "\n".join(blocks)
                + "\n")
    return out, devices, unranked


def _absolutize(text: str, base: Path) -> str:
    import check_mc

    def fix(m):
        path = m.group(3)
        if path.startswith(("/", "{{")):
            return m.group(0)
        return m.group(1) + m.group(2) + str(base / path) + m.group(4)

    return check_mc._REL_INCLUDE_RE.sub(fix, text)


def analyse_mismatch(ws: Path, measure: str, corner_name: str | None = None,
                     condition: str | None = None, top: int = DEFAULT_TOP,
                     timeout: float = DEFAULT_TIMEOUT, workers: int = 1,
                     eda_bin: Path | None = None) -> dict:
    import check_mc
    workers = max(1, min(MAX_WORKERS, workers))
    sizing = sim_run.load_sizing(ws)
    bench = Bench(ws, measure, eda_bin, timeout, condition)
    sweep, _ = sim_run.spec_sweep(ws)
    tag = bench.measure + (f"__{condition}" if condition else "") + "__mm"
    corner, base, bound, out_root = _corner_and_base(
        bench, ws, sweep, sizing, corner_name, tag, workers, measure)
    lib = sim_run.pdk_root(bench.t_root) / PDK_MOS_LIB
    try:
        lib_text = lib.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CheckError(f"cannot read the PDK's MOS library {lib}: "
                         f"{exc}") from exc
    text = bench.netlist.read_text(encoding="utf-8")
    new_text, devices, unranked = mismatch_netlist(text, bench.text, lib_text)
    if not devices:
        raise CheckError("no device of the netlist has a mismatch term this "
                         "can rank" + (": " + "; ".join(
                             f"{u['device']}: {u['reason']}"
                             for u in unranked) if unranked else ""))
    out_root.mkdir(parents=True, exist_ok=True)
    copy = out_root / f"{bench.netlist.stem}__mm{bench.netlist.suffix}"
    copy.write_text(_absolutize(new_text, bench.netlist.resolve().parent),
                    encoding="utf-8")
    bench_text = bench.text
    if any(d["kind"] == "resistor" for d in devices):
        # the PDK's resistor body reads (1+mis_r*sw_stat_mismatch); every
        # MOS is retargeted and nothing unranked draws (mismatch_netlist)
        bench_text = check_mc._INCLUDE_DESIGN_RE.sub(
            lambda m: m.group(1) + "\n.param sw_stat_mismatch=1",
            bench_text, count=1)
    zeros = {p: 0.0 for d in devices for p in d["params"].values()}

    def with_z(param=None, z=0.0):
        out = {**sizing, **zeros}
        if param:
            out[param] = z
        return out

    zero = bench.run(corner, with_z(), out_root / "mm_zero", copy, bench_text)
    if zero["value"] is None:
        raise CheckError(f"the mismatch copy at {corner['name']}: "
                         f"{zero['error']} - see {out_root / 'mm_zero'}")
    m0 = base["value"]
    if abs(zero["value"] - m0) > 1e-6 * max(abs(m0), 1e-30):
        raise CheckError(
            f"the mismatch copy's nominal {zero['value']:.6g} differs from "
            f"the block's {m0:.6g} at {corner['name']}: the rewrite moved "
            f"the operating point - see {copy}")
    jobs = [(d, term, p, s) for d in devices
            for term, p in d["params"].items() for s in (+1, -1)]

    def one(job):
        d, term, p, s = job
        return bench.run(corner, with_z(p, float(s)),
                         out_root / f"{p}__{'up' if s > 0 else 'down'}",
                         copy, bench_text)

    with ThreadPoolExecutor(workers) as pool:
        runs = list(pool.map(one, jobs))
    by = {(j[2], j[3]): r for j, r in zip(jobs, runs)}
    entries = []
    for d in devices:
        e = {k: d[k] for k in ("device", "instance", "subckt", "model",
                               "kind")}
        terms, errors = {}, []
        for term, p in d["params"].items():
            up, dn = by[(p, +1)], by[(p, -1)]
            if up["value"] is None or dn["value"] is None:
                errors.append((up if up["value"] is None else dn)["error"])
                continue
            terms[term] = (up["value"] - dn["value"]) / 2
        e["per_sigma"] = terms
        if errors:
            e["error"] = "; ".join(errors)
            e["variance"] = None
        else:
            e["variance"] = sum(v * v for v in terms.values())
        entries.append(e)
    total = sum(e["variance"] for e in entries if e["variance"] is not None)
    for e in entries:
        var = e["variance"]
        e["share"] = var / total if var is not None and total else None
        e["sigma_if_area_doubled"] = (
            math.sqrt(max(total - var / 2, 0.0)) if var is not None else None)
    ranked = sorted(entries, key=lambda e: (
        e["variance"] is None, -(e["variance"] or 0.0), e["device"]))
    sigma = math.sqrt(total)
    lo, hi = bound.get("min"), bound.get("max")
    k_sigma = {"min": (m0 - lo) / sigma if lo is not None and sigma else None,
               "max": (hi - m0) / sigma if hi is not None and sigma else None}
    return {
        "script": SCRIPT, "status": "pass", "mode": "mismatch",
        "workspace": str(ws), "measure": bench.measure,
        "bench": bench.bench_path.name, "corner": corner["name"],
        "condition": condition, "value": m0, "bound": {"min": lo, "max": hi},
        "margin": measure_margin(m0, bound),
        "passes": (lo is None or m0 >= lo) and (hi is None or m0 <= hi),
        "sigma": sigma, "rel_sigma": sigma / abs(m0) if m0 else None,
        "k_sigma": k_sigma, "devices": ranked,
        "top": [e["device"] for e in ranked[:top]
                if e["variance"] is not None],
        "unranked": unranked, "global": GLOBAL_NOTE, "netlist": str(copy),
        "decks": str(out_root),
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
    ap.add_argument("--mismatch", action="store_true",
                    help="rank the devices' shares of the measure's random "
                         "mismatch variance instead of the sizing params")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if args.mismatch:
        return checklib.cli_wrap(SCRIPT, lambda: (analyse_mismatch(
            Path(args.workspace), args.measure, args.corner, args.condition,
            args.top, args.timeout, args.workers), args.out))
    return checklib.cli_wrap(SCRIPT, lambda: (analyse(
        Path(args.workspace), args.measure, args.corner, args.condition,
        args.step, args.top, args.timeout, args.workers), args.out))


if __name__ == "__main__":
    sys.exit(main())

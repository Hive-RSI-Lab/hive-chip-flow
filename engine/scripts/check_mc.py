#!/usr/bin/env python
"""check_mc.py - the mc gate (docs/design.md 1.5, "### M8."): ngspice Monte
Carlo, only when spec.yaml asks for it.

    check_mc.py --workspace DIR [--jobs N|auto] [--out FILE]

Pass criteria (gates.yaml): the yield's Clopper-Pearson lower confidence
bound at or above mc.yield_min, at tt and at the worst PVT corner of the
sim_pvt sweep, and every MC sigma a mutant ruling claims met. Fault: a
block whose typical-corner MC passes but whose worst-corner MC misses.
A spec with no `mc` block, or `mc.enabled: false`, is NOT a stub: this
script evaluates the spec and reports `applicable: false` with zero
violations - unless a below_spread ruling in spec/mutant_rulings.yaml
claims a non-zero MC sigma, which with MC off nothing would check, so that
is refused (exit 2).

When `mc.enabled: true`:

1. Mismatch on. This box's gf180mcuD design.spice sets `.param
   sw_stat_global=0` / `sw_stat_mismatch=0` (whatever its comment block
   says) and pins no `.option seed`, so a plain re-run is the same operating
   point repeated. The gate injects `.param sw_stat_mismatch=1` (and
   `sw_stat_global=1` when `mc.global` asks, at tt only - at ss/ff the
   corner IS the process shift) plus a per-run `.option seed=<mc.seed + i>`
   into the GENERATED deck, right after the bench's own `.include
   '.../design.spice'` line - never by editing the PDK. The PDK's MOS and
   pnp mismatch terms read `par`, not `m`, so the MC decks use a copy of the
   netlist where every multiplied device also carries `par=N`, carried down
   through `m=N` subckts (add_par_for_multiplier; listed in `par_added`; a
   subckt defined outside the netlist and bench is listed `unhandled`).
   The ngspice deck sets every resistor's `mis_r=0`; the copy writes the
   PDK's Xyce Pelgrom term on each nplus_u/pplus_u/npolyf_u/ppolyf_u
   instance (add_resistor_mismatch, RES_MISMATCH; listed in
   `res_mismatch`), and lists each resistor with no documented term
   (rm1-rm4, tm*, nwell, *_s, ppolyf_u_1k/2k/3k) as `no_term`.

2. Where it runs. Every bench is run nominal (mismatch off) over sim_pvt's
   own sweep (sim_run.spec_sweep), then MC at tt and MC at each bench's
   worst corner of that sweep: the corner whose nominal run sits fewest tt
   MC sigmas inside a bound scored there (worst_corner) - the corner
   mismatch is likeliest to push out. The pick is this run's, never a
   record's.

3. How it judges. A sample passes when it has no bound violation. Each
   (bench, corner) group's yield gets a one-sided Clopper-Pearson lower
   bound (binomial on the pass count; assumes independent samples and
   nothing about any measure's distribution) at confidence 1 - alpha/G,
   Bonferroni over the G groups, and the block's bound at each role (tt,
   worst) is 1 - the sum of its benches' loss bounds. The point yield below
   yield_min is `yield_below_spec` (the design misses); a point yield that
   meets it with a bound that does not is `yield_unproven` with the
   `runs_needed`; a group with every sample failed is `yield_all_failed`.
   `mc.runs` defaults to RUNS_MARGIN x the zero-failure run count that
   proves yield_min at that confidence (default_runs).

   A measure spec.yaml marks `statistical: none` (it needs an mc_waiver
   reason) is left out of every sample's judgement and listed in
   `nominal_only`: sim_tt and sim_pvt score it at zero mismatch, and a
   bench left with no bound MC judges is listed in `not_scored`.

4. What it reports. Per group and measure: n, mean, sigma, rel_sigma,
   min, max, and against the bounds scored there k_sigma, cpk and a
   normal-model yield - labelled as assuming normality, and never used for
   the verdict.

5. Spec sigmas. A measure with a `sigma_max` (a comparator's input-offset
   sigma) has each group's sample 1-sigma held to it: above it is
   `sigma_above_spec`; at or under it with a chi-square upper bound (at the
   yields' per-group confidence, normal assumed) above it is
   `sigma_unproven`; no judged bench measuring it is `sigma_unmeasured`.

6. Ruling sigmas. Every non-zero relative 1-sigma a below_spread ruling
   claims (bench_strength widens a mutant's allowance by 3x it) is checked
   against the tt MC rel_sigma of that measure (the largest across
   benches): a claim above the run's upper confidence bound on it
   (chi-square, normal assumed) is `ruling_sigma_unmet`; a measure no tt
   bench gives is `ruling_sigma_unverified`.

Refusals a caller cannot talk this gate out of (CheckError, exit 2):
`mc.runs` 0 or negative; yield_min outside (0, 1) - no finite run proves
1.0; and every sample of a group producing BIT-IDENTICAL measures (the
mismatch/seed injection did nothing), rather than a suspiciously perfect
yield.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import statistics
import sys
from collections.abc import Iterable
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import check_bench_strength as bench_strength  # noqa: E402
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import sim_run  # noqa: E402
import rulingslib  # noqa: E402
import padlib  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_mc"
DEFAULT_TIMEOUT = 60.0
DEFAULT_SEED_BASE = 1
DEFAULT_CONFIDENCE = 0.95
# default runs: this many times the zero-failure count (default_runs)
RUNS_MARGIN = 2
OUT_SUBDIR = "log/mc"
JOBS_ENV = "CHIP_FLOW_MC_JOBS"

# The bench's own `.include '.../design.spice'` line - matched BEFORE
# simlib.materialize() runs (so `{{PDK}}` is still a literal placeholder
# here; the regex only needs ".include" and "design.spice" on the same
# line, not a resolved path). The PDK ships the same file twice, as
# design.spice and design.ngspice (byte-identical, both setting the two
# sw_stat switches to 0), and a bench may include either.
_INCLUDE_DESIGN_RE = re.compile(
    r"^(\.include\s+.*design\.(?:ng)?spice.*)$", re.IGNORECASE | re.MULTILINE)


def inject_mc_params(template_text: str, seed: int, do_global: bool) -> str:
    """Insert `.param sw_stat_mismatch=1` (+ `sw_stat_global=1` when
    `do_global`) and `.option seed=<seed>` right after the bench's own
    design.spice include line - overriding that file's own
    sw_stat_global=0/sw_stat_mismatch=0 defaults in the MATERIALIZED DECK
    only; the PDK's own design.spice is never written to."""
    lines = []
    if do_global:
        lines.append(".param sw_stat_global=1")
    lines.append(".param sw_stat_mismatch=1")
    lines.append(f".option seed={seed}")
    injected = "\n".join(lines)
    new_text, n = _INCLUDE_DESIGN_RE.subn(
        lambda m: m.group(1) + "\n" + injected, template_text, count=1)
    if n == 0:
        raise CheckError(
            "mc: bench template has no '.include ...design.spice' (or "
            "design.ngspice) line to "
            "inject sw_stat_mismatch/seed after - cannot run a real Monte "
            "Carlo sample")
    return new_text


# The PDK subckts whose Pelgrom term reads `par` but not `m`: each
# computes p_sqrtarea = sqrt((l-par_l)*par*(w-par_w)) and passes `m` only to
# its inner MOSFET, so an `m=N` instance gets ONE unit's mismatch sigma,
# sqrt(N) too large. The pnp_* subckts divide theirs (mis_is_*, mis_bf_*) by
# sqrt(par) the same way; the npn_* take `par` and carry no mismatch term, so
# par=N changes nothing. The four resistors in RES_MISMATCH join them: the
# term add_resistor_mismatch gives them reads `par` the same way (the MIM
# caps carry none).
_PAR_MODEL_RE = re.compile(r"^(?:[np]fet_0[356]v[03](?:_dss|_nvt)?"
                           r"|(?:pnp|npn)_.+|[np](?:plus|polyf)_u)$",
                           re.IGNORECASE)
_PARAM_RE = re.compile(
    r"(?<![\w.])(m|par)\s*=\s*('[^']*'|\{\{[^}]*\}\}|\{[^}]*\}|[^\s]+)",
    re.IGNORECASE)
_FIRST_PARAM_RE = re.compile(r"\s[\w.]+\s*=")
# PDK devices whose `m` needs no `par`: their mismatch term is zero or absent
# (the resistors outside RES_MISMATCH set mis_r=0 or have none, MIM caps
# carry none).
_NO_PAR_MODEL_RE = re.compile(
    r"^(?:cap_|nplus_|pplus_|npolyf_|ppolyf_|nwell|rm\d|tm\d|efuse|diode_)",
    re.IGNORECASE)
# The parameter that carries an `m=N` down a user subckt in the MC copy.
MULT_PARAM = "mc_mult"


def _logical_lines(text: str) -> list[list[int]]:
    """Group physical line indexes into SPICE logical lines (a line plus
    its `+` continuations)."""
    groups: list[list[int]] = []
    for i, ln in enumerate(text.split("\n")):
        if ln.lstrip().startswith("+") and groups:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _strip_comment(ln: str) -> str:
    for mark in (" ;", " $", "\t;", "\t$"):
        k = ln.find(mark)
        if k >= 0:
            ln = ln[:k]
    return ln


def _instance(group: list[int], lines: list[str]) -> tuple | None:
    """(name, model, {m,par: value}) of the `X...` instance on this logical
    line, or None when it isn't one."""
    if lines[group[0]].lstrip()[:1] not in ("x", "X"):
        return None
    body = " ".join(_strip_comment(lines[i]).lstrip().lstrip("+")
                    for i in group)
    toks = _FIRST_PARAM_RE.split(body, maxsplit=1)[0].split()
    if toks and toks[-1].lower() == "params:":
        toks.pop()
    if len(toks) < 2:
        return None
    params = {k.lower(): v for k, v in _PARAM_RE.findall(body)}
    return toks[0], toks[-1], params


def _multiplier(params: dict) -> str | None:
    """The instance's `m` when it multiplies anything. An instance that
    already writes `par` has settled its own mismatch and is left alone."""
    mval = params.get("m")
    if mval is None or "par" in params or _bare(mval) == "1":
        return None
    return mval


def _bare(value: str) -> str:
    """A parameter value with its one layer of quotes or braces off, ready
    to sit inside an expression. A bench's `{{TOKEN}}` placeholder keeps its
    braces, since materialize() still has to find it."""
    if value.startswith("{{"):
        return value
    return value.strip("'{}")


def _append_param(lines: list[str], group: list[int], param: str) -> None:
    last = group[-1]
    code = _strip_comment(lines[last])
    lines[last] = code.rstrip() + f" {param}" + lines[last][len(code):]


def _set_par(lines: list[str], group: list[int], value: str) -> None:
    for i in group:
        code = _strip_comment(lines[i])
        for found in _PARAM_RE.finditer(code):
            if found.group(1).lower() == "par":
                lines[i] = (lines[i][:found.start(2)] + value
                            + lines[i][found.end(2):])
                return
    _append_param(lines, group, f"par={value}")


def _subckt_blocks(text: str) -> tuple[dict, list]:
    """Walk one netlist or bench: ({subckt name: {mos, calls}}, [(enclosing
    subckt or None, line group)] for its `X` instances and .subckt lines)."""
    lines = text.split("\n")
    defs: dict[str, dict] = {}
    walk: list = []
    stack: list[str] = []
    for group in _logical_lines(text):
        head = lines[group[0]].split()
        word = head[0].lower() if head else ""
        if word == ".subckt" and len(head) > 1:
            stack.append(head[1].lower())
            defs.setdefault(stack[-1], {"mos": False, "calls": set()})
            walk.append((None, group))
        elif word == ".ends":
            if stack:
                stack.pop()
        else:
            found = _instance(group, lines)
            if found is None:
                continue
            walk.append((stack[-1] if stack else None, group))
            if stack:
                if _PAR_MODEL_RE.match(found[1]):
                    defs[stack[-1]]["mos"] = True
                else:
                    defs[stack[-1]]["calls"].add(found[1].lower())
    return defs, walk


def _carriers(texts: list[str]) -> tuple[set[str], dict]:
    """The subckts that must carry a multiplier down to their devices: those
    holding a par-reading device at any depth and sitting under an `m=N` instance at any
    depth, over every text the deck includes (a bench may multiply a subckt
    its netlist defines). Also returns the joined definitions."""
    defs: dict[str, dict] = {}
    roots: set[str] = set()
    for text in texts:
        found, walk = _subckt_blocks(text)
        for name, d in found.items():
            have = defs.setdefault(name, {"mos": False, "calls": set()})
            have["mos"] = have["mos"] or d["mos"]
            have["calls"] |= d["calls"]
        lines = text.split("\n")
        for _, group in walk:
            inst = _instance(group, lines)
            if inst and _multiplier(inst[2]) is not None:
                roots.add(inst[1].lower())

    def closure(start: set[str], edges) -> set[str]:
        seen, todo = set(), list(start)
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo.extend(edges(name))
        return seen

    reached = closure(roots & set(defs),
                      lambda n: defs[n]["calls"] & set(defs))
    holds_mos = {n for n in defs if any(
        defs[c]["mos"] for c in closure(
            {n}, lambda k: defs[k]["calls"] & set(defs)))}
    return reached & holds_mos, defs


def add_par_for_multiplier(text: str, others: Iterable[str] = ()
                           ) -> tuple[str, list[dict]]:
    """Make the PDK's mismatch term see each device's total area, keeping the
    DC point (m=N par=N: the right sigma as well; m=4 alone measured sd
    3.9 mV against 1.8 mV for the same total width).

    An `X... <nfet_*/pfet_*/pnp_*/npn_*> ... m=N` instance (N not 1) gets a
    matching `par=N`. For an `X... <user subckt> ... m=N`, ngspice multiplies
    the inner devices' `m` but `par` stays 1, so a subckt holding such a
    device (a carrier, see _carriers) takes a MULT_PARAM parameter, the
    instance sets it to N, nested instances multiply it, and each inner one gets
    par={MULT_PARAM*<own par or m>}. An instance that already writes `par`
    is left as it is.

    `others` are the other texts of the same deck, read only to find what
    is multiplied and defined across them. Returns (new text, [{instance,
    model, m}]): each one rewritten, with `subckt` when it sits in a
    carrier, and with `unhandled` for a multiplied subckt this text set can't
    see into, which keeps one unit's sigma (pessimistic). Used on the MC
    deck's copies only."""
    carriers, defs = _carriers([text, *others])
    _, walk = _subckt_blocks(text)
    lines = text.split("\n")
    done: list[dict] = []
    for inside, group in walk:
        head = lines[group[0]].split()
        if head[0].lower() == ".subckt":
            if head[1].lower() in carriers:
                _append_param(lines, group, f"{MULT_PARAM}=1")
            continue
        name, model, params = _instance(group, lines)
        mval = _multiplier(params)
        carried = inside in carriers
        where = {"subckt": inside} if carried else {}
        if _PAR_MODEL_RE.match(model):
            if carried:
                own = params.get("par") or params.get("m")
                par = (f"{{{MULT_PARAM}*({_bare(own)})}}" if own
                       else f"{{{MULT_PARAM}}}")
                _set_par(lines, group, par)
                done.append({"instance": name, "model": model,
                             "m": own or "1", **where})
            elif mval is not None:
                _set_par(lines, group, mval)
                done.append({"instance": name, "model": model, "m": mval})
        elif model.lower() in carriers:
            if carried:
                mult = (f"{{{MULT_PARAM}*({_bare(mval)})}}" if mval
                        else f"{{{MULT_PARAM}}}")
            elif mval is not None:
                mult = mval
            else:
                continue
            _append_param(lines, group, f"{MULT_PARAM}={mult}")
            done.append({"instance": name, "model": model,
                         "m": mval or "1", **where})
        elif (mval is not None or carried) and model.lower() not in defs \
                and not _NO_PAR_MODEL_RE.match(model):
            done.append({
                "instance": name, "model": model, "m": mval or "1", **where,
                "unhandled": "defined outside the netlist and bench, so "
                             "what it holds keeps one unit's mismatch: "
                             "sqrt(N) pessimistic"})
    return "\n".join(lines), done


# Resistor mismatch. GF180's ngspice deck (sm141064.spice, `.lib res`)
# sets `mis_r=0` on every resistor subckt, so a resistor never moves under
# Monte Carlo there. Its Xyce deck (libs.tech/xyce/sm141064.xyce, gf180mcuD)
# documents a Pelgrom term for four of them, on the subckt lines of nplus_u
# (38280), pplus_u (38295), npolyf_u (38355) and ppolyf_u (38370):
#   par_sqrtarea='sqrt(par*(r_l)*(r_w))'
#   var_r='0.7071*par_r*1e-06/par_sqrtarea'   mis_r='agauss(0,var_r,1)'
# with r_l='s*(r_length-2*r_dl)', r_w='r_width-2*r_dw', and the rb body
# scaled by (1+mis_r*sw_stat_mismatch) in both decks. par_r is A_R in um
# (0.021 um = 2.1 %*um for ppolyf_u): one resistor's sigma(dR/R) is
# 0.7071*A_R/sqrt(W*L), a pair's difference A_R/sqrt(W*L). The MC copy
# carries that term into ngspice by writing it on each instance: an
# instance parameter is evaluated in the caller's scope, where r_l and r_w
# don't exist, so the expression is built from the instance's own r_length,
# r_width, s and par and the model's nominal r_dl/r_dw (the same in both
# decks; with mc.global the PDK moves r_dw by a few nm, which this sigma
# ignores). Every other resistor (rm1-rm4, tm*, nwell, the *_s and the high-R
# ppolyf_u_1k/2k/3k variants) has no mismatch term in either deck and is
# listed `no_term`: the PDK documents none to carry.
# model: (par_r = A_R in um, r_dl, nominal r_dw), from those subckt lines.
RES_MISMATCH = {
    "nplus_u": (0.012608, 2e-11, -5e-8),
    "pplus_u": (0.0126, 5.0e-11, 2.75e-8),
    "npolyf_u": (0.05808, 8.48e-11, 0.0265e-6),
    "ppolyf_u": (0.021, 2e-11, 2.55e-8),
}
_NO_TERM_RES_RE = re.compile(
    r"^(?:rm\d|tm\d+k|nwell|[np](?:plus|polyf)_s|ppolyf_u_\dk(?:_6p0)?)$",
    re.IGNORECASE)
_ANY_PARAM_RE = re.compile(
    r"(?<![\w.])([\w.]+)\s*=\s*('[^']*'|\{\{[^}]*\}\}|\{[^}]*\}|[^\s]+)")


def _all_params(group: list[int], lines: list[str]) -> dict:
    body = " ".join(_strip_comment(lines[i]).lstrip().lstrip("+")
                    for i in group)
    return {k.lower(): v for k, v in _ANY_PARAM_RE.findall(body)}


def resistor_sigma_expr(model: str, params: dict) -> str:
    """The Xyce mis_r for one instance of a RES_MISMATCH model, written in
    the instance's own terms (see RES_MISMATCH)."""
    a_r, dl, dw = RES_MISMATCH[model.lower()]

    def term(key, default="1"):
        return f"({_bare(params[key])})" if key in params else default

    area = (f"{term('par')}*{term('s')}*({term('r_length')}-2*({dl:g}))"
            f"*({term('r_width')}-2*({dw:g}))")
    return f"'agauss(0,0.7071*{a_r:g}*1e-06/sqrt({area}),1)'"


def add_resistor_mismatch(text: str) -> tuple[str, list[dict]]:
    """Give each `X... <nplus_u|pplus_u|npolyf_u|ppolyf_u>` instance the
    PDK's Xyce mismatch term as `mis_r=` (see RES_MISMATCH). Run after
    add_par_for_multiplier, so an `m=N` resistor's `par=N` divides its sigma
    by sqrt(N) as N parallel units would. Returns (new text, [{instance,
    model, a_r_pct_um}]), plus one {instance, model, no_term} for each PDK
    resistor that has no documented term and keeps none, and `unhandled`
    for an instance that already writes mis_r or lacks r_length/r_width (the
    PDK's l/w defaults don't resolve, so ngspice would refuse it anyway).
    Used on the MC deck's copies only."""
    lines = text.split("\n")
    done: list[dict] = []
    for group in _logical_lines(text):
        found = _instance(group, lines)
        if found is None:
            continue
        name, model, _ = found
        if _NO_TERM_RES_RE.match(model):
            done.append({"instance": name, "model": model,
                         "no_term": "the PDK documents no mismatch term "
                                    "for this resistor; MC cannot move it"})
            continue
        if model.lower() not in RES_MISMATCH:
            continue
        params = _all_params(group, lines)
        entry = {"instance": name, "model": model,
                 "a_r_pct_um": round(RES_MISMATCH[model.lower()][0] * 100, 4)}
        if "mis_r" in params:
            entry["unhandled"] = "writes its own mis_r, kept as written"
        elif not {"r_length", "r_width"} <= params.keys():
            entry["unhandled"] = ("no r_length/r_width on the instance, so "
                                  "no mismatch term was added")
        else:
            _append_param(lines, group,
                          f"mis_r={resistor_sigma_expr(model, params)}")
        done.append(entry)
    return "\n".join(lines), done


def mc_rewrite(text: str, others: Iterable[str] = ()
               ) -> tuple[str, list[dict], list[dict]]:
    """One text's MC copy: add_par_for_multiplier, then
    add_resistor_mismatch. Returns (new text, par_added, res_mismatch)."""
    text, par_done = add_par_for_multiplier(text, others)
    text, res_done = add_resistor_mismatch(text)
    return text, par_done, res_done


_REL_INCLUDE_RE = re.compile(
    r"^(\s*\.(?:include|inc|lib)\s+)(['\"]?)([^'\"\s]+)(['\"]?)",
    re.IGNORECASE | re.MULTILINE)


def mc_netlist(netlist_path: Path, out_dir: Path, others: Iterable[str] = ()
               ) -> tuple[Path, list[dict], list[dict]]:
    """The netlist the MC decks include: the block's own when no instance
    needs `par` or a resistor mismatch term (mc_rewrite; `others` are the
    bench texts), else a rewritten copy under out_dir (relative .include/.lib
    paths made absolute, so they still resolve from there). The block's
    netlist file is never written. Returns (path, par_added, res_mismatch)."""
    text = netlist_path.read_text(encoding="utf-8")
    new_text, done, res = mc_rewrite(text, others)
    if new_text == text:
        return netlist_path, done, res
    base = netlist_path.resolve().parent

    def absolutize(m):
        path = m.group(3)
        if path.startswith(("/", "{{")):
            return m.group(0)
        return m.group(1) + m.group(2) + str(base / path) + m.group(4)

    new_text = _REL_INCLUDE_RE.sub(absolutize, new_text)
    out_dir.mkdir(parents=True, exist_ok=True)
    copy = out_dir / f"{netlist_path.stem}__mc_par{netlist_path.suffix}"
    copy.write_text(new_text, encoding="utf-8")
    return copy, done, res


# --- statistics ------------------------------------------------------------
# No scipy on the box's eda python is assumed: the binomial tail and the
# chi-square quantile are written out here, each checked against known
# answers in tests/test_check_mc.py.

def _log_binom_pmf(i: int, n: int, p: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p))


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return min(1.0, sum(math.exp(_log_binom_pmf(i, n, p))
                        for i in range(k, n + 1)))


def cp_lower(k: int, n: int, confidence: float) -> float:
    """The one-sided Clopper-Pearson lower bound on a pass probability from
    k passes in n trials: the p at which P(X >= k) = 1 - confidence. It
    assumes only that the samples are independent draws - nothing about
    how any measure is distributed. k = n gives (1 - confidence)^(1/n)."""
    if n <= 0 or k <= 0:
        return 0.0
    alpha = 1.0 - confidence
    if k >= n:
        return alpha ** (1.0 / n)
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if binom_sf(k, n, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def runs_for(target: float, confidence: float, fail_frac: float = 0.0
             ) -> int | None:
    """The fewest runs whose Clopper-Pearson lower bound reaches `target`
    when a `fail_frac` share of them fail - None when that share alone
    already sits at or below the target (no run count can prove it)."""
    if fail_frac >= 1.0 - target:
        return None
    if fail_frac <= 0.0:
        return max(1, math.ceil(math.log(1.0 - confidence) / math.log(target)))

    def ok(n: int) -> bool:
        return cp_lower(n - math.ceil(fail_frac * n), n, confidence) >= target

    hi = runs_for(target, confidence)
    while not ok(hi):
        hi *= 2
        if hi > 10 ** 7:
            return None
    lo = hi // 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def block_lower(n: int, fail_fracs: list[float], confidence: float
                ) -> float:
    """The union bound 1 - sum of each group's Clopper-Pearson loss bound,
    every group run n times with its `fail_fracs` share failing."""
    return 1.0 - sum(1.0 - cp_lower(n - math.ceil(f * n), n, confidence)
                     for f in fail_fracs)


def runs_needed(target: float, confidence: float, fail_fracs: list[float]
                ) -> int | None:
    """The fewest runs per group at which block_lower reaches `target` if
    each group keeps failing at its observed rate - None when those rates
    alone already reach the allowed loss (no run count proves it)."""
    if sum(fail_fracs) >= 1.0 - target:
        return None

    def ok(n: int) -> bool:
        return block_lower(n, fail_fracs, confidence) >= target

    hi = 1
    while not ok(hi):
        hi *= 2
        if hi > 10 ** 7:
            return None
    lo = hi // 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def chi2_quantile(p: float, dof: int) -> float:
    """The chi-square p-quantile (Wilson-Hilferty; within 1% from 3 degrees
    of freedom up, which is all an MC sigma needs)."""
    z = statistics.NormalDist().inv_cdf(p)
    h = 2.0 / (9.0 * dof)
    return dof * max(1.0 - h + z * math.sqrt(h), 0.0) ** 3


def sigma_upper(sigma: float, n: int, confidence: float) -> float:
    """The one-sided upper confidence bound on a 1-sigma from n samples
    (sample sd `sigma`), from (n-1) s^2 / sigma^2 ~ chi-square(n-1): a
    normal measure is assumed here (and stated in the report)."""
    if n < 2:
        return math.inf
    q = chi2_quantile(1.0 - confidence, n - 1)
    return math.inf if q <= 0 else sigma * math.sqrt((n - 1) / q)


NORMAL_ASSUMPTION = (
    "cpk, k_sigma and normal_yield assume the measure is normally "
    "distributed around its sample mean; the gate's verdict does not - it "
    "rests on the Clopper-Pearson bound on the pass count alone")


def measure_stats(samples: list[dict], bounds: list[dict], names) -> dict:
    """{measure: {n, mean, sigma, rel_sigma, min, max, bounds?, k_sigma?,
    cpk?, normal_yield?}} over one (bench, corner) group's samples. The
    bound terms appear for a measure with a bound scored at this corner."""
    out: dict[str, dict] = {}
    keys = sorted({k for s in samples for k in s})
    for key in keys:
        vals = [s[key] for s in samples
                if isinstance(s.get(key), (int, float))
                and math.isfinite(s[key])]
        if not vals:
            continue
        n = len(vals)
        mean = statistics.fmean(vals)
        sd = statistics.stdev(vals) if n > 1 else 0.0
        st = {"n": n, "mean": mean, "sigma": sd,
              "rel_sigma": sd / abs(mean) if mean else None,
              "min": min(vals), "max": max(vals)}
        lo = [b["min"] for b in bounds if b["measure"].lower() == key
              and "min" in b and simlib.scored_at(b, names)]
        hi = [b["max"] for b in bounds if b["measure"].lower() == key
              and "max" in b and simlib.scored_at(b, names)]
        if lo or hi:
            bmin = max(lo) if lo else None
            bmax = min(hi) if hi else None
            st["bounds"] = {"min": bmin, "max": bmax}
            if sd > 0:
                ks = {}
                if bmin is not None:
                    ks["min"] = (mean - bmin) / sd
                if bmax is not None:
                    ks["max"] = (bmax - mean) / sd
                st["k_sigma"] = ks
                st["cpk"] = min(ks.values()) / 3.0
                nd = statistics.NormalDist(mean, sd)
                st["normal_yield"] = (
                    (nd.cdf(bmax) if bmax is not None else 1.0)
                    - (nd.cdf(bmin) if bmin is not None else 0.0))
        out[key] = st
    return out


# --- applicability ---------------------------------------------------------

def ruling_sigma_claims(ws: Path) -> list[dict]:
    """[{ruling, measure, sigma}] for every non-zero relative MC 1-sigma a
    below_spread ruling in spec/mutant_rulings.yaml claims (its `sigma` for
    its `measure`, and its `sigmas` map) - the spreads bench_strength
    widens a mutant's allowance by, which only an MC run can back."""
    rulings = rulingslib.load(ws, "bench_strength")
    out = []
    for rid, entry in sorted(rulings.get("below_spread", {}).items()):
        claims = {k.lower(): v for k, v in (entry.get("sigmas") or {}).items()}
        claims[str(entry["measure"]).lower()] = entry["sigma"]
        for measure, sigma in sorted(claims.items()):
            if sigma:
                out.append({"ruling": rid, "measure": measure,
                            "sigma": float(sigma)})
    return out


def mc_applicable(spec: dict, ws: Path | None = None) -> bool:
    """Does this spec ask for Monte Carlo? The ONE predicate for that
    question - this gate's own not-applicable branch below and attest.py's
    release-time declaration (attest.not_applicable_reason) both call it,
    so the two can never disagree about what "not applicable" means. A
    non-mapping `mc` is refused (exit 2), never read as "no MC". With `ws`,
    a below_spread ruling that claims a non-zero MC sigma while the spec
    has no `mc.enabled: true` is refused too: that claim would go
    unchecked."""
    mc_cfg = spec.get("mc")
    if mc_cfg is not None and not isinstance(mc_cfg, dict):
        raise CheckError(
            f"spec.yaml's mc must be a mapping (enabled/runs/yield_min/...), "
            f"got {type(mc_cfg).__name__} - write 'mc: {{enabled: true}}'")
    enabled = bool((mc_cfg or {}).get("enabled"))
    if not enabled and ws is not None:
        claims = ruling_sigma_claims(Path(ws))
        if claims:
            c = claims[0]
            raise CheckError(
                f"spec/mutant_rulings.yaml ruling {c['ruling']!r} claims an "
                f"MC sigma of {c['sigma']:g} for {c['measure']!r} "
                f"({len(claims)} such claim(s)), but spec.yaml has no "
                "'mc: {enabled: true}', so no gate checks it - turn Monte "
                "Carlo on (with a yield_min), or set the ruling's sigma to 0")
    return enabled


def _config(mc_cfg: dict) -> dict:
    yield_min = mc_cfg.get("yield_min")
    if isinstance(yield_min, bool) or not isinstance(yield_min, (int, float)) \
            or not 0.0 < yield_min < 1.0:
        raise CheckError(
            f"mc.yield_min must be a number strictly between 0 and 1, got "
            f"{yield_min!r} - the gate proves a lower confidence bound on "
            "the yield, and no finite run proves 1.0; write the yield the "
            "block needs (for example 0.99)")
    confidence = mc_cfg.get("confidence", DEFAULT_CONFIDENCE)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
            or not 0.5 <= confidence < 1.0:
        raise CheckError(
            f"mc.confidence must be a number in [0.5, 1), got "
            f"{confidence!r} - for example 0.95")
    runs = mc_cfg.get("runs")
    if runs is not None and (isinstance(runs, bool) or not isinstance(runs, int)
                             or runs <= 0):
        raise CheckError(
            f"mc.runs must be a positive integer, got {runs!r} - 'runs: 0' "
            "(or negative) is refused outright, never silently widened to "
            "the default; leave it out to size the run from yield_min")
    return {"yield_min": float(yield_min), "confidence": float(confidence),
            "runs": runs, "global": bool(mc_cfg.get("global", False)),
            "seed": int(mc_cfg.get("seed", DEFAULT_SEED_BASE))}


def default_runs(yield_min: float, confidence: float, groups: int,
                 benches: int) -> int:
    """RUNS_MARGIN x the zero-failure run count that proves each bench's
    share of the allowed loss, 1 - (1 - yield_min)/benches, at confidence
    1 - (1 - confidence)/groups - room for a few failures."""
    target = 1.0 - (1.0 - yield_min) / max(benches, 1)
    conf = 1.0 - (1.0 - confidence) / max(groups, 1)
    return RUNS_MARGIN * runs_for(target, conf)


def _margin_sigma(value, bound: dict, sigma: float | None) -> float:
    """How far `value` sits inside `bound`, in units of `sigma` (the tt MC
    1-sigma of that measure; 1% of |value| when there is none) - negative
    outside it."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return -math.inf
    margins = []
    if "min" in bound:
        margins.append(value - bound["min"])
    if "max" in bound:
        margins.append(bound["max"] - value)
    scale = sigma if sigma else max(abs(value) * 0.01, 1e-30)
    return min(margins) / scale


def worst_corner(bench: str, bounds: list[dict], nominal: list[dict],
                 tt_stats: dict, tt_names,
                 condition: str | None = None) -> dict | None:
    """The corner of the sim_pvt sweep (bar tt) where this bench's nominal
    run sits closest to a bound - or past one - counted in the measure's tt
    MC sigmas: the corner mismatch is likeliest to push out. None when the
    sweep scores the bench nowhere but tt."""
    worst = None
    for r in nominal:
        if r["bench"] != bench or set(r["names"]) & set(tt_names) \
                or r.get("condition") != condition:
            continue
        names = r["names"]
        for b in bounds:
            if not simlib.scored_at(b, names) \
                    or not padlib.scored_in(b, r.get("condition")):
                continue
            key = b["measure"].lower()
            sd = (tt_stats.get(key) or {}).get("sigma")
            m = (-math.inf if r["engine_errors"]
                 else _margin_sigma(r["measures"].get(key), b, sd))
            if worst is None or m < worst["margin_sigma"]:
                worst = {"corner": r["corner"], "measure": key,
                         "margin_sigma": m,
                         "value": r["measures"].get(key),
                         "sigma_unit": "tt_mc" if sd else "1pct_of_value"}
    return worst


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--timeout", type=float, default=None,
                    help="ngspice limit per run, s (default: the spec's "
                    f"sim_timeout_s.mc, else {DEFAULT_TIMEOUT:g} s)")
    ap.add_argument("--jobs", default=os.environ.get(JOBS_ENV, "auto"),
                    help="runs simulated at once: 'auto' (default; cores "
                    "minus the 1-minute load, re-read as it goes) or a "
                    f"number; ${JOBS_ENV} sets the default")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)
    jobs = bench_strength.parse_jobs(args.jobs)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    timeout = speclib.sim_timeout(spec, "mc", args.timeout, DEFAULT_TIMEOUT)
    top = spec.get("top")

    # Not applicable: a real answer, but NOT a recordable pass in a
    # workspace. It is stamped with spec.yaml, while invalidation.yaml keys
    # mc on [netlist, tb, ...], so state.record_gate refuses it
    # (input_digest mismatch) - deliberately: a recorded "pass" keyed on
    # netlist/tb would stay fresh after spec.yaml later turned MC on, and
    # release would accept a Monte Carlo that never ran. Release instead
    # re-asks the current spec.yaml every time (attest.not_applicable_reason).
    if not mc_applicable(spec, ws):
        payload = checklib.report(SCRIPT, ws / "spec" / "spec.yaml", [],
                                  top=top, applicable=False,
                                  reason="spec.yaml has no 'mc.enabled: true'")
        return payload, args.out

    cfg = _config(spec["mc"])
    supply = spec.get("supply") or {}
    nominal_vdd = supply.get("vdd")

    eda_bin = sim_run.EDA_BIN
    t_root = sim_run.toolchain_root(eda_bin)
    netlist_path = sim_run.find_netlist(ws)
    tt_corner = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]
    tt_names = corners_mod.names_of(tt_corner)
    sizing = sim_run.load_sizing(ws)

    out_dir = ws / OUT_SUBDIR
    shutil.rmtree(out_dir, ignore_errors=True)
    # m=N MOS and bipolar instances get par=N in the MC copy of the netlist
    # (and of each bench), or the PDK gives them one unit's mismatch: sqrt(N)
    # too much. An m=N on a user subckt carries down the same way, and a
    # bench may multiply a subckt the netlist defines, so each sees the rest.
    bench_list = sim_run.find_benches(ws)
    bench_texts = {bench: bench.read_text(encoding="utf-8")
                   for bench, _ in bench_list}
    netlist_text = netlist_path.read_text(encoding="utf-8")
    mc_net, par_added, res_mismatch = mc_netlist(
        netlist_path, out_dir, bench_texts.values())
    # A measure the spec marks `statistical: none` (with its mc_waiver) is a
    # nominal check - sim_tt/sim_pvt score it at zero mismatch - and Monte
    # Carlo does not judge it: a comparator's 1 mV decision would flip on
    # the mismatch its offset measure already bounds.
    nominal_only = sorted(speclib.nominal_only_measures(spec))
    # ua pins (padlib): sim_pvt runs every bench bare and through the pad
    # model and scores each bound only in its `pad` condition, so Monte
    # Carlo samples each (bench, condition) the same way. Scoring a
    # pad-only bound (a glitch read at the pin, through 500 ohm + 5 pF) on
    # the bare deck failed every sample of a design sim_pvt passes. A pad
    # the netlist or a bench refuses is sim_pvt's error to report: here
    # that bench runs bare only.
    try:
        pad = sim_run.pad_setup(spec, netlist_text)
    except padlib.PadRefused:
        pad = None
    conditions = padlib.conditions_of(spec) if pad else [None]
    benches = []
    for bench_path, bounds_path in bench_list:
        template_text, bench_par, bench_res = mc_rewrite(
            bench_texts[bench_path],
            [netlist_text, *(t for b, t in bench_texts.items()
                             if b != bench_path)])
        par_added += [dict(d, bench=bench_path.name) for d in bench_par]
        res_mismatch += [dict(d, bench=bench_path.name) for d in bench_res]
        bounds = [x for x in simlib.load_bounds(bounds_path)
                  if x["measure"].lower() not in nominal_only]
        for condition in conditions:
            if condition == "pad":
                try:
                    sim_run.pad_bench(template_text, str(top), pad,
                                      tt_corner, "0")
                except padlib.PadRefused:
                    continue
            mine = [x for x in bounds if padlib.scored_in(x, condition)]
            if not mine and condition == "pad":
                continue    # the bare entry, bound-less, names it not_scored
            benches.append({
                "name": (f"{bench_path.name}[pad]" if condition == "pad"
                         else bench_path.name),
                "file": bench_path.name, "stem": bench_path.stem,
                "condition": condition, "bounds": mine,
                "text": template_text})

    # The worst PVT corner: sim_pvt's own sweep (spec_sweep, the same
    # corner set, scope-outs pinned), run nominal - mismatch off - here,
    # so the pick is this run's and never a stale record's.
    sweep, _ = sim_run.spec_sweep(ws)
    nominal = sim_run.run_workspace_benches(
        ws, eda_bin, corners=sweep, timeout=timeout, check="mc",
        out_subdir=f"{OUT_SUBDIR}/pvt")

    subs_cache: dict[str, dict] = {}

    def subs_for(corner: dict) -> dict:
        if corner["name"] not in subs_cache:
            subs_cache[corner["name"]] = sim_run.build_subs(
                t_root, mc_net, corner, nominal_vdd, sizing)
        return subs_cache[corner["name"]]

    def plan(groups_now: list[tuple[dict, dict]], n: int) -> list[tuple]:
        return [(b, c, i, cfg["seed"] + i) for b, c in groups_now
                for i in range(n)]

    def one(task) -> dict:
        b, corner, i, seed = task
        at_tt = corner["name"] == tt_corner["name"]
        # global (process) variation only at tt: at ss/ff the corner IS
        # the process shift, and drawing it again would count it twice
        text = inject_mc_params(b["text"], seed, cfg["global"] and at_tt)
        subs = subs_for(corner)
        if b["condition"] == "pad":
            text = sim_run.pad_bench(text, str(top), pad, corner,
                                     subs["VDD"])
        r = sim_run.run_bench_at_corner(
            eda_bin, f"{b['stem']}__mc{i}", text, b["bounds"],
            subs, corner, out_dir, timeout, check="mc",
            condition=b["condition"])
        r["bench"] = b["name"]
        return {"bench": b["name"], "corner": corner["name"], "run": i,
                "seed": seed, "passed": not r["violations"],
                "measures": r["measures"], "violations": r["violations"]}

    # Stage 1: tt, every bench scored there. Stage 2: each bench at its
    # worst sweep corner, picked with stage 1's sigmas.
    tt_groups = [(b, tt_corner) for b in benches
                 if any(simlib.scored_at(x, tt_names) for x in b["bounds"])]
    sweep_by_name = {c["name"]: c for c in sweep}
    # the run count is sized for the groups it will judge: tt plus one
    # worst corner per bench the sweep scores anywhere else
    other = [b for b in benches if any(
        not set(corners_mod.names_of(c)) & set(tt_names)
        and any(simlib.scored_at(x, corners_mod.names_of(c))
                for x in b["bounds"]) for c in sweep)]
    n_groups = len(tt_groups) + len(other)
    runs = cfg["runs"] or default_runs(cfg["yield_min"], cfg["confidence"],
                                       n_groups, len({b["name"] for b in
                                                      [*(x for x, _ in tt_groups),
                                                       *other]}))
    seeds = [cfg["seed"] + i for i in range(runs)]
    outcomes = bench_strength.run_pool(plan(tt_groups, runs), one, jobs)

    def group_stats(bname: str, corner: dict) -> dict:
        b = next(x for x in benches if x["name"] == bname)
        return measure_stats(
            [o["measures"] for o in outcomes
             if o["bench"] == bname and o["corner"] == corner["name"]],
            b["bounds"], corners_mod.names_of(corner))

    tt_stats = {b["name"]: group_stats(b["name"], tt_corner)
                for b, _ in tt_groups}
    worst = {}
    worst_groups = []
    for b in other:
        w = worst_corner(b["file"], b["bounds"], nominal["results"],
                         tt_stats.get(b["name"], {}), tt_names,
                         condition=b["condition"])
        if w is not None:
            worst[b["name"]] = w
            worst_groups.append((b, sweep_by_name[w["corner"]]))
    outcomes += bench_strength.run_pool(plan(worst_groups, runs), one, jobs)

    no_term = sorted({f"{d['instance']} ({d['model']})"
                      for d in res_mismatch if "no_term" in d})
    no_term_note = (f"This deck's resistors with no mismatch term: "
                    f"{', '.join(no_term)}. " if no_term else "")
    groups = []
    for role, glist in (("tt", tt_groups), ("worst", worst_groups)):
        for b, corner in glist:
            mine = [o for o in outcomes if o["bench"] == b["name"]
                    and o["corner"] == corner["name"]]
            ms = [o["measures"] for o in mine]
            if len(ms) > 1 and all(m == ms[0] for m in ms):
                raise CheckError(
                    f"mc: all {len(ms)} runs of {b['name']!r} at "
                    f"{corner['name']} produced bit-identical measures "
                    f"{ms[0]!r} across seeds {seeds[0]}..{seeds[-1]} - "
                    "sw_stat_mismatch/seed injection did not vary the "
                    "result; Monte Carlo must never be one sample repeated. "
                    f"{no_term_note}"
                    "If the measure depends only on resistors the PDK gives "
                    "no mismatch term (GF180 documents one only for "
                    "nplus_u, pplus_u, npolyf_u and ppolyf_u), MC cannot "
                    "move it: move the matched resistors onto one of those, "
                    "or turn mc off and mark the measure "
                    "'statistical: none' with an 'mc_waiver' saying so")
            hits = sum(1 for o in mine if o["passed"])
            groups.append({
                "bench": b["name"], "corner": corner["name"], "role": role,
                "global": cfg["global"] and role == "tt",
                "runs": len(mine), "hits": hits,
                "stats": (tt_stats[b["name"]] if role == "tt"
                          else group_stats(b["name"], corner))})

    if not groups:
        raise CheckError(
            "mc: no tb/ bench has a bound scored at tt or at any corner of "
            f"the sweep ({', '.join(c['name'] for c in sweep)}), so Monte "
            "Carlo has nothing to judge - add a bound the sweep scores, "
            "or turn mc off")

    # Each group's bound at 1 - alpha/G (Bonferroni: all G hold at once
    # with the stated confidence), and the block's at each role as the
    # union bound over its benches: 1 - sum of each bench's loss bound.
    alpha = 1.0 - cfg["confidence"]
    g_conf = 1.0 - alpha / max(len(groups), 1)
    for g in groups:
        g["yield_lower"] = cp_lower(g["hits"], g["runs"], g_conf)
        g["yield_frac"] = g["hits"] / g["runs"] if g["runs"] else 0.0
    violations: list[dict] = []
    yields = {}
    for role in ("tt", "worst"):
        mine = [g for g in groups if g["role"] == role]
        if not mine:
            continue
        lower = max(0.0, 1.0 - sum(1.0 - g["yield_lower"] for g in mine))
        point = max(0.0, 1.0 - sum(1.0 - g["yield_frac"] for g in mine))
        where = ", ".join(f"{g['bench']}@{g['corner']} {g['hits']}/"
                          f"{g['runs']}" for g in mine)
        yields[role] = {"yield_lower": lower, "yield_frac": point,
                        "groups": [f"{g['bench']}@{g['corner']}"
                                   for g in mine]}
        dead = [g for g in mine if g["hits"] == 0]
        if dead:
            violations.append(checklib.violation(
                "mc", "error", None, None, "yield_all_failed",
                [f"{g['bench']}@{g['corner']}" for g in dead],
                f"every Monte Carlo sample failed at the {role} corner "
                f"({where}) - regardless of yield_min", "sim_run"))
        elif point < cfg["yield_min"]:
            violations.append(checklib.violation(
                "mc", "error", None, None, "yield_below_spec",
                [f"{g['bench']}@{g['corner']}" for g in mine],
                f"Monte Carlo yield at the {role} corner is {point:.2%} "
                f"({where}), below the spec's {cfg['yield_min']:.2%} - the "
                "design misses; more runs will not fix it", "sim_run"))
        elif lower < cfg["yield_min"]:
            needed = runs_needed(cfg["yield_min"], g_conf,
                                 [1.0 - g["yield_frac"] for g in mine])
            yields[role]["runs_needed"] = needed
            violations.append(checklib.violation(
                "mc", "error", None, None, "yield_unproven",
                [f"{g['bench']}@{g['corner']}" for g in mine],
                f"Monte Carlo yield at the {role} corner ({where}) has a "
                f"{cfg['confidence']:.0%}-confidence lower bound of "
                f"{lower:.2%} (Clopper-Pearson, Bonferroni over "
                f"{len(groups)} groups), below the spec's "
                f"{cfg['yield_min']:.2%} - "
                + (f"raise mc.runs to about {needed} (at this failure "
                   "rate), or widen the design's margin"
                   if needed else
                   "at this failure rate no run count proves it: widen "
                   "the design's margin"), "sim_run", runs_needed=needed))

    # Spec sigmas: a measure's `sigma_max` (a comparator's offset sigma)
    # against each group's sample sigma and its chi-square upper bound at
    # the same per-group confidence as the yields (normal assumed).
    sigma_checks = []
    for m in spec.get("measures") or []:
        if not (isinstance(m, dict) and isinstance(m.get("name"), str)
                and "sigma_max" in m):
            continue
        key, smax = m["name"].lower(), float(m["sigma_max"])
        seen = [g for g in groups if key in g["stats"]]
        if not seen:
            sigma_checks.append({"measure": key, "sigma_max": smax,
                                 "verdict": "unmeasured"})
            violations.append(checklib.violation(
                "mc", "error", None, None, "sigma_unmeasured", [key],
                f"spec measure {key!r} has sigma_max {smax:g}, but no bench "
                "the mc gate judges measures it - add it to a tb/ bench "
                "scored at tt", "speclib"))
            continue
        for g in seen:
            st = g["stats"][key]
            upper = sigma_upper(st["sigma"], st["n"], g_conf)
            where = f"{g['bench']}@{g['corner']}"
            verdict = ("above_spec" if st["sigma"] > smax else
                       "unproven" if upper > smax else "met")
            sigma_checks.append({"measure": key, "group": where,
                                 "sigma_max": smax, "sigma": st["sigma"],
                                 "sigma_upper": upper, "n": st["n"],
                                 "verdict": verdict})
            if verdict == "met":
                continue
            violations.append(checklib.violation(
                "mc", "error", None, None,
                "sigma_above_spec" if verdict == "above_spec"
                else "sigma_unproven", [key, where],
                f"{key!r} at {where}: Monte Carlo 1-sigma {st['sigma']:.4g} "
                + (f"is above the spec's sigma_max {smax:g} - the design "
                   "misses; more runs will not fix it"
                   if verdict == "above_spec" else
                   f"has a {g_conf:.2%}-confidence upper bound of "
                   f"{upper:.4g} (chi-square, n={st['n']}, normal assumed), "
                   f"above the spec's sigma_max {smax:g} - raise mc.runs, "
                   "or widen the design's margin"), "sim_run"))

    # Ruling sigmas: each below_spread claim of a relative 1-sigma must sit
    # at or under this tt run's own (its upper confidence bound, the
    # largest across benches), or bench_strength excused a mutant on a
    # spread the block does not have.
    ruling_checks = []
    for c in ruling_sigma_claims(ws):
        seen = [(g["bench"], g["stats"][c["measure"]]) for g in groups
                if g["role"] == "tt" and c["measure"] in g["stats"]
                and g["stats"][c["measure"]]["rel_sigma"] is not None]
        if not seen:
            ruling_checks.append(dict(c, verdict="unverified"))
            violations.append(checklib.violation(
                "mc", "error", "spec/mutant_rulings.yaml", None,
                "ruling_sigma_unverified", [c["ruling"], c["measure"]],
                f"ruling {c['ruling']!r} claims an MC sigma of "
                f"{c['sigma']:g} for {c['measure']!r}, but no bench this "
                "gate runs at tt measures it (or its mean is 0) - measure it "
                "in a tb/ bench scored at tt, or set the claim to 0",
                "rulingslib"))
            continue
        bench, st = max(seen, key=lambda s: s[1]["rel_sigma"])
        upper = sigma_upper(st["rel_sigma"], st["n"], cfg["confidence"])
        ok = c["sigma"] <= upper
        ruling_checks.append(dict(
            c, bench=bench, mc_rel_sigma=st["rel_sigma"],
            mc_rel_sigma_upper=upper, n=st["n"],
            verdict="met" if ok else "unmet"))
        if not ok:
            violations.append(checklib.violation(
                "mc", "error", "spec/mutant_rulings.yaml", None,
                "ruling_sigma_unmet", [c["ruling"], c["measure"]],
                f"ruling {c['ruling']!r} claims a relative MC sigma of "
                f"{c['sigma']:g} for {c['measure']!r}, but this run measures "
                f"{st['rel_sigma']:.4g} ({bench}, n={st['n']}; "
                f"{cfg['confidence']:.0%} upper bound {upper:.4g}) - set the "
                "ruling's sigma to what MC measures and re-run "
                "bench_strength; the mutant may then need a stronger bench",
                "rulingslib"))

    all_sample_violations = [v for o in outcomes for v in o["violations"]]
    results = [{k: o[k] for k in ("bench", "corner", "run", "seed", "passed",
                                  "measures")} for o in outcomes]
    total = len(outcomes)
    hits = sum(1 for o in outcomes if o["passed"])
    payload = checklib.report(
        SCRIPT, ws / "netlist", violations, top=top, applicable=True,
        runs=runs, samples=total, hits=hits,
        yield_frac=round(hits / total, 4) if total else 0.0,
        yield_min=cfg["yield_min"], confidence=cfg["confidence"],
        yields=yields, groups=groups,
        worst_corners=worst, sweep=nominal["corners"],
        sweep_violations=len(nominal["violations"]),
        spec_sigmas=sigma_checks, ruling_sigmas=ruling_checks,
        assumptions=NORMAL_ASSUMPTION,
        seeds=seeds, par_added=par_added, res_mismatch=res_mismatch,
        nominal_only=nominal_only,
        not_scored=[b["name"] for b in benches
                    if b not in [x for x, _ in tt_groups]],
        results=results, sample_violations=all_sample_violations[:20])
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

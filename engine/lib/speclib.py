"""speclib.py - spec.yaml parsing and lint rules (docs/design.md 1.4, 1.5).

New at M2 (docs/design.md 1.3: "New: ... speclib ..."): nothing to port, this
is chip-flow's own shape, not /hwde's. spec.yaml is a block's machine-readable
half of its spec, and `spec_lint` (engine/scripts/check_spec_lint.py, gates.yaml
row of the same name) refuses a block without one.

Schema this module reads (docs/design.md 1.4 plus one field the design leaves
implicit: every other gate that names a top module - lint, sim, mutate,
synth, harden - needs one *somewhere*, and spec.yaml, read before any RTL
exists, is the only artifact early enough):

    top: str                    the top module name (new here; 1.4 does not
                                 spell this out, but every later gate needs it
                                 from somewhere, and this is the earliest
                                 artifact that exists)
    requirements:
      - id: str                 unique, non-empty
        text: str                non-empty prose
        check: sim|formal|both|measure
        bounds: {name: {min?, max?, equals?, source}}
                                 required when check == measure; each
                                 bound names the recorded fact it is held
                                 to (measurelib.py, issue #89)
    ports: {name: {dir, width}} optional, loose
    tt_pins: {name: pin}         optional (M4+); a non-empty mapping when present
    clock: {period_ns, domains, primary?}  optional, loose; primary must
                                 name a domain, and is required by harden
                                 when there is more than one
    must_keep: [str]             optional

Pass criteria (gates.yaml `spec_lint` row): "Every requirement has an id, a
check kind and, for `measure`, bounds; tt_pins fit the tile." The tile-fit
half needs the TT template (M4, engine/reference/tt/) and is out of scope
until then - lint_spec here validates everything that does not need it, and
raises nothing tile-shaped; a tt_pins mapping is required to be a real,
non-empty mapping when present, but never checked against a tile's pin count.

Every violation is checklib.violation()-shaped so check_spec_lint.py can
hand the list straight to checklib.report().
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml

CHECK_KINDS = {"sim", "formal", "both", "measure"}

# ---------------------------------------------------------------- /ade (M8)
# New at M8 (docs/design.md "### M8."): ade's own spec.yaml shape, on top of
# the fields lint_spec above already validates generically (top, clock,
# must_keep). gates.yaml's ade `spec_lint` row: "Every measure has bounds
# and a corner set; supply and devices declared." - the fault it must catch:
# "a measure without bounds".
#
#     supply: {vdd: float}            required; the nominal rail sim_run.py
#                                      resolves every corner's VDD from
#     devices: [str, ...]             required, non-empty; refdes this
#                                      block's netlist/*.cir must instantiate
#                                      (check_netlist_lint.py cross-checks)
#                                      and bench_strength mutates
#     corners: "default" | "all" | [str,...] | {grid: {...}, extra?: [str,...]}
#                                      optional (default: corners.py's own
#                                      default_corners()); the forms and
#                                      the grid's rules are corners.py's
#                                      spec_corners() / grid_corners(),
#                                      which check_spec_lint_ade.py runs
#     split_devices: [{refdes, cell, why}]   optional; a GF180 standard cell
#                                      the msde split put inside this
#                                      analog macro (a ladder's bit drivers),
#                                      the one kind of device the analog-
#                                      designer may add beyond its template.
#                                      `cell` must be a gf180mcu_fd_sc_*
#                                      cell and `refdes` must be in `devices`
#     measures:
#       - name: str                   required, unique
#         bounds: {min?, max?}        required, at least one of min/max
#         corners: "default"|[str,...]|"all"   optional (default: "default")
#         severity: error|warning     optional (default: error)
#         statistical: mismatch|none  optional; `mismatch` marks a measure
#                                      that only means something under
#                                      device mismatch (offset, mirror or
#                                      UP/DN ratio, INL/DNL), so spec_lint
#                                      requires `mc.enabled: true` for it
#                                      (lint_matching_measures). A name
#                                      with a MATCHING_NAME_TOKENS token
#                                      counts as `mismatch` unless the
#                                      measure says `statistical: none`
#                                      with an `mc_waiver` reason
#         sigma_max: float            optional; the largest Monte Carlo
#                                      1-sigma allowed, in the measure's
#                                      unit (a comparator's offset sigma).
#                                      check_mc.py holds each group's
#                                      chi-square upper confidence bound
#                                      on it to this; needs mc.enabled
#         mc_waiver: str              the reason a measure is not judged
#                                      under Monte Carlo (a digital divide
#                                      ratio; resistors with no
#                                      per-instance mismatch model; a
#                                      nominal decision whose mismatch part
#                                      another measure bounds), with
#                                      `statistical: none` - the mc gate
#                                      leaves such a measure out of every
#                                      sample (nominal_only_measures)
#     mc: {enabled: bool, runs?: int, yield_min?: float, global?: bool,
#          seed?: int}   optional - runs?/seed? are check_mc.py's own
#                        per-run count and seed base (`.option seed=<seed
#                        base + i>` per run, `runs: 0` refused outright);
#                        global? asks check_mc.py to also force
#                        `sw_stat_global=1` (process-level MC), not just
#                        `sw_stat_mismatch=1` (device-level, the default)
#     footprint_um: {width: float, height: float}   optional; the layout's
#                        largest allowed top-cell bounding box, in um,
#                        both positive. check_analog_drc.py measures the
#                        regenerated GDS against it (absent: not checked)
#     sim_timeout_s: {<check>: float, ...}   optional; the ngspice time
#                        limit, in seconds, of each named gate's every run -
#                        <check> one of SIM_TIMEOUT_CHECKS, each value in
#                        (0, MAX_SIM_TIMEOUT_S]. A check left out keeps its
#                        own default; the check's --timeout flag overrides
#                        it (sim_timeout()). A run past it is still a
#                        refusal or a finding, never a pass
#     pex_corners: "all" | [name, ...]   optional; the corners of the
#                        spec's sweep pex_sim runs its bench at. Absent:
#                        typical, slow/hot and sim_pvt's worst per measure
#                        (check_pex_sim.py's docstring)
#     pex_rc_knobs: "all" | [name, ...]   optional; scaled R and C runs of the
#                        pex bench, from engine/reference/pex_rc_knobs.yaml. An
#                        approximation, each knob one more ngspice run. Absent:
#                        wire R and C as extracted
#     pex_rc_corners: "all" | [name, ...]   optional; the PDK's own RC corners:
#                        one more magic extraction and ngspice run per variant
#                        in engine/reference/pex_rc_variants.yaml, each with the
#                        process the table pairs it with. Absent: none
#     pex_fill: "all" | [floating, grounded]   optional; pex_sim also
#                        fills a copy of the tile as the shuttle will
#                        (the PDK's fill_all.rb), runs DRC on it, and
#                        runs the bench at every scored corner with the
#                        fill left floating and/or tied to ground
#                        (engine/lib/filllib.py). Absent: no fill view
#     erc_nets: {<pin>: {power: V} | {min: V, max: V}}   optional; the
#                        voltage the ERC gate (check_analog_erc.py) gives a
#                        pin other than supply.vdd's `vdd` and the 0 V
#                        `vss`/`gnd`: a supply at `power` volts, or a signal
#                        anywhere in [min, max]. Absent: a signal pin swings
#                        0 V to supply.vdd (erc_nets())
#     ir_drop_mv: float   optional; the em_ir gate's bound on each supply
#                        rail's static drop from its pin to any extracted node
#                        of it, in mV, positive. Absent: 2 % of supply.vdd
#     ir_rails: [pin, ...]   optional; the pins em_ir holds to ir_drop_mv.
#                        Absent: the pins named like a supply key or vss/gnd.
#                        `[]` declares a block with no supply pin (EM alone)
#     symmetric_nets: [{nets: [a, b], max_dc_pct?, max_dr_pct?, min_dc_ff?,
#                       min_dr_ohm?, aggressors?: [net, ...]}]   optional; the
#                        net pairs a layout must route alike (a latch's
#                        outputs, UP/DN). pex_sim compares each pair's total
#                        C and worst pin-to-device R in the extracted netlist,
#                        and each one's C to every named aggressor, and fails
#                        a pair whose difference is over the percentage and
#                        over the absolute floor (default 0). At least one
#                        max_*_pct, and max_dc_pct with aggressors
#                        (symmetric_nets(); check_pex_sim.py)
#     matching: [{devices: [a, b, ...], style: common_centroid | interdigitate,
#                 tol_um?}]   optional; the devices a layout draws as one
#                        row of equal units (#118). layout_match reads the
#                        extracted row: unit centroids within tol_um
#                        (default 0.05, common_centroid only), one unit
#                        W/L, one orientation mix, a dummy or equal device
#                        beside every end unit (matching();
#                        check_layout_match.py)

ADE_MEASURE_CORNER_KINDS = {"default", "all"}
# the gates that run ngspice, each with a --timeout and a default of its own
SIM_TIMEOUT_CHECKS = ("netlist_lint", "sim_tt", "sim_pvt", "mc",
                      "bench_strength", "pex_sim", "em_ir")
MAX_SIM_TIMEOUT_S = 3600.0
# a measure name with one of these `_`-separated tokens is read as
# matching-sensitive (lint_matching_measures)
MATCHING_NAME_TOKENS = ("mismatch", "offset", "ratio", "inl", "dnl",
                        "matching")
STATISTICAL_KINDS = ("mismatch", "none")
STD_CELL_PATTERN = r"^gf180mcu_fd_sc_mcu(7t|9t)5v0__[a-z0-9_]+$"


def lint_spec_ade(spec: dict, rel_path: str = "spec/spec.yaml") -> list[dict]:
    """Return the list of ade spec_lint violations - [] means a clean spec.
    Deliberately independent of lint_spec() above (vde's own): that
    function requires a non-empty `requirements` list and validates
    `tt_pins`, neither of which gates.yaml's ade spec_lint row asks for
    ("Every measure has bounds and a corner set; supply and devices
    declared.") - reusing it wholesale would force every analog block to
    also carry digital-shaped requirements it has no use for. `top` is
    checked here too (every gate downstream needs it, same as vde's)."""
    import re

    from checklib import violation

    out: list[dict] = []

    def bad(kind: str, msg: str, refs=None, severity="error"):
        out.append(violation("spec_lint", severity, rel_path, None, kind,
                             refs or [], msg, "speclib"))

    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        bad("no_top", "spec.yaml has no non-empty 'top' block name")

    supply = spec.get("supply")
    if not isinstance(supply, dict) or not isinstance(supply.get("vdd"), (int, float)) \
            or isinstance(supply.get("vdd"), bool) or supply.get("vdd", 0) <= 0:
        bad("no_supply", "spec.yaml has no 'supply.vdd' (a positive number)")

    devices = spec.get("devices")
    if not isinstance(devices, list) or not devices \
            or not all(isinstance(d, str) and d.strip() for d in devices):
        bad("no_devices", "spec.yaml has no non-empty 'devices' list of "
                          "refdes strings")

    split = spec.get("split_devices", [])
    declared = set(devices) if isinstance(devices, list) else set()
    if not isinstance(split, list):
        bad("bad_split_devices", "spec.yaml 'split_devices' must be a list "
                                 "of {refdes, cell, why}")
        split = []
    for i, d in enumerate(split):
        where = f"split_devices[{i}]"
        if not (isinstance(d, dict) and set(d) == {"refdes", "cell", "why"}
                and all(isinstance(d[k], str) and d[k].strip() for k in d)):
            bad("bad_split_devices", f"{where} must be exactly {{refdes, "
                                     "cell, why}, each a non-empty string")
            continue
        if not re.match(STD_CELL_PATTERN, d["cell"]):
            # the exception is for the split's std-cell drivers only; any
            # other device a template lacks stays the designer's invention
            bad("bad_split_devices", f"{where} cell {d['cell']!r} is not a "
               "GF180 standard cell (gf180mcu_fd_sc_mcu7t5v0__* or "
               "mcu9t5v0__*)", refs=[d["refdes"]])
        if d["refdes"] not in declared:
            bad("bad_split_devices", f"{where} refdes {d['refdes']!r} is not "
               "in 'devices'", refs=[d["refdes"]])

    corners_field = spec.get("corners", "default")
    if not ((isinstance(corners_field, str) and corners_field in ADE_MEASURE_CORNER_KINDS)
           or (isinstance(corners_field, list) and corners_field
               and all(isinstance(c, str) for c in corners_field))
           or (isinstance(corners_field, dict) and set(corners_field) in ({"grid"}, {"grid", "extra"}))):
        bad("bad_corners", "spec.yaml 'corners' must be 'default', 'all', "
                           "a non-empty list of corner names, or "
                           "{grid: {process, temp_c, supply_pct}, "
                           "extra?: [corner name, ...]}")

    measures = spec.get("measures")
    if not isinstance(measures, list) or not measures:
        # this is the fault gates.yaml names for ade's spec_lint: "a
        # measure without bounds" widened one notch - no measures at all is
        # the same failure at its most extreme.
        bad("no_measures", "spec.yaml has no non-empty 'measures' list")
        measures = []

    seen: set[str] = set()
    for i, m in enumerate(measures):
        where = f"measures[{i}]"
        if not isinstance(m, dict):
            bad("measure_not_a_mapping", f"{where} is not a mapping")
            continue
        name = m.get("name")
        if not isinstance(name, str) or not name.strip():
            bad("measure_no_name", f"{where} has no non-empty 'name'")
            name = None
        elif name in seen:
            bad("measure_dup_name", f"measure name {name!r} is not unique",
               refs=[name])
        else:
            seen.add(name)

        bounds = m.get("bounds")
        if not isinstance(bounds, dict) or not ("min" in bounds or "max" in bounds):
            bad("measure_no_bounds",
               f"{where} (name {name!r}) has no 'bounds' with 'min' and/or "
               "'max'", refs=[name] if name else [])

        mc = m.get("corners", "default")
        if not ((isinstance(mc, str) and mc in ADE_MEASURE_CORNER_KINDS)
               or (isinstance(mc, list) and mc
                   and all(isinstance(c, str) for c in mc))):
            bad("measure_bad_corners",
               f"{where} (name {name!r}) 'corners' must be 'default', "
               "'all', or a non-empty list of corner names",
               refs=[name] if name else [])

        sev = m.get("severity", "error")
        if sev not in ("error", "warning"):
            bad("measure_bad_severity",
               f"{where} (name {name!r}) 'severity' must be 'error' or "
               "'warning'", refs=[name] if name else [])

        if "sigma_max" in m:
            smax = m["sigma_max"]
            mc_on = isinstance(spec.get("mc"), dict) and bool(
                spec["mc"].get("enabled"))
            if isinstance(smax, bool) or not isinstance(smax, (int, float)) \
                    or not 0 < smax < float("inf"):
                bad("bad_sigma_max",
                   f"{where} (name {name!r}) 'sigma_max' must be a positive "
                   f"number in the measure's own unit, got {smax!r}",
                   refs=[name] if name else [])
            elif not mc_on or m.get("statistical") == "none":
                bad("sigma_max_no_mc",
                   f"{where} (name {name!r}) has a 'sigma_max', which only "
                   "the mc gate checks, but "
                   + ("it says 'statistical: none'" if mc_on else
                      "the spec has no 'mc: {enabled: true}'")
                   + " - turn Monte Carlo on for it, or drop the sigma_max",
                   refs=[name] if name else [])

    # the ua pad model (padlib): ua_pins, the optional board load, and each
    # measure's pad condition
    import padlib
    try:
        ua_pins = padlib.ua_pins_of(spec)
    except ValueError as exc:
        bad("bad_ua_pins", str(exc))
        ua_pins = None
    try:
        padlib.board_of(spec)
    except ValueError as exc:
        bad("bad_ua_pad", str(exc))
    for i, m in enumerate(measures):
        if not isinstance(m, dict) or "pad" not in m:
            continue
        name = m.get("name")
        try:
            padlib.pad_scope(m)
        except ValueError as exc:
            bad("measure_bad_pad", f"measures[{i}] (name {name!r}) {exc}",
               refs=[name] if isinstance(name, str) else [])
            continue
        if ua_pins == []:
            bad("measure_bad_pad",
               f"measures[{i}] (name {name!r}) sets 'pad', but the spec "
               "lists no 'ua_pins', so there is no pad condition to scope",
               refs=[name] if isinstance(name, str) else [])

    mc_cfg = spec.get("mc")
    if mc_cfg is not None and not isinstance(mc_cfg, dict):
        bad("mc_not_a_mapping", "'mc' is present but not a mapping")

    if "footprint_um" in spec and footprint_um(spec) is None:
        bad("bad_footprint", "spec.yaml 'footprint_um' must be exactly "
                             "{width, height}, each a positive number of um")

    if "sim_timeout_s" in spec:
        problem = sim_timeout_problem(spec["sim_timeout_s"])
        if problem:
            bad("bad_sim_timeout", problem)

    if "erc_nets" in spec and erc_nets(spec) is None:
        bad("bad_erc_nets", "spec.yaml 'erc_nets' must map each pin name to "
                            "{power: volts} or {min: volts, max: volts} "
                            "with min <= max")

    if "ir_drop_mv" in spec:
        v = spec["ir_drop_mv"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            bad("bad_ir_drop", "spec.yaml 'ir_drop_mv' must be a positive "
                               "number of mV")

    if "ir_rails" in spec:
        field = spec["ir_rails"]
        if not (isinstance(field, list) and all(
                isinstance(n, str) and n for n in field)
                and len(set(field)) == len(field)):
            bad("bad_ir_rails", "spec.yaml 'ir_rails' must be a list of "
                                "distinct pin names ([] for a block with no "
                                "supply pin)")

    if "symmetric_nets" in spec and symmetric_nets(spec) is None:
        bad("bad_symmetric_nets", "spec.yaml 'symmetric_nets' must be a list "
                                  "of {nets: [a, b]} with two different net "
                                  "names, at least one of max_dc_pct and "
                                  "max_dr_pct, every bound and floor a "
                                  "number >= 0, and aggressors a list of net "
                                  "names other than the pair's")

    if "matching" in spec and matching(spec) is None:
        bad("bad_matching", "spec.yaml 'matching' must be a list of "
                            "{devices: [a, b, ...], style: common_centroid "
                            "or interdigitate, tol_um?: a number > 0} with "
                            "two or more different device names, no device "
                            "in two groups")

    if "pex_corners" in spec:
        field = spec["pex_corners"]
        if field != "all" and not (isinstance(field, list) and field and all(
                isinstance(n, str) for n in field)):
            bad("bad_pex_corners", "spec.yaml 'pex_corners' must be \"all\" "
                                   "or a non-empty list of corner names")

    if "pex_fill" in spec:
        import filllib
        try:
            if spec["pex_fill"] is None:
                raise ValueError(
                    "spec.yaml 'pex_fill' is empty: leave the field out for "
                    "no fill view, or give \"all\" or a list of fill modes")
            filllib.modes_of(spec["pex_fill"])
        except ValueError as exc:
            bad("bad_pex_fill", str(exc))

    if "pex_rc_knobs" in spec:
        field = spec["pex_rc_knobs"]
        if field != "all" and not (isinstance(field, list) and field and all(
                isinstance(n, str) for n in field)):
            bad("bad_pex_rc_knobs", "spec.yaml 'pex_rc_knobs' must be \"all\" "
                                    "or a non-empty list of knob names")

    if "pex_rc_corners" in spec:
        field = spec["pex_rc_corners"]
        if field != "all" and not (isinstance(field, list) and field and all(
                isinstance(n, str) for n in field)):
            bad("bad_pex_rc_corners", "spec.yaml 'pex_rc_corners' must be "
                                      "\"all\" or a non-empty list of variant "
                                      "names")

    out += _lint_feedback(spec, bad)
    return out


# a block whose top name says it closes a loop (an op amp, an LDO, a PLL)
FEEDBACK_NAME_RE = r"(^|_)(ota|opamp|op_amp|amp|ldo|regulator|pll)(_|$)"
STABILITY_MEASURE = "pm_deg"


def _lint_feedback(spec: dict, bad) -> list[dict]:
    """spec.yaml `feedback: true` says the block closes a loop (issue #100):
    a loop needs a stability measure, so the spec must bound `pm_deg` (the
    stb_tb template's Tian phase margin) with a `min`, at every corner the
    spec sweeps - a margin met at tt alone is the PLL buffer's failure. A
    top name that reads as a loop (FEEDBACK_NAME_RE) with no `feedback`
    key is a warning, `feedback_undeclared`: the owner says yes or no.
    Calls `bad` for each finding; returns [] (the caller's list holds them)."""
    import re

    fb = spec.get("feedback")
    if fb is not None and not isinstance(fb, bool):
        bad("bad_feedback", "spec.yaml 'feedback' must be true or false")
        return []
    if fb is None:
        top = spec.get("top")
        if isinstance(top, str) and re.search(FEEDBACK_NAME_RE, top.lower()):
            bad("feedback_undeclared",
                f"top {top!r} reads as a block that closes a loop, but "
                "spec.yaml has no 'feedback' key: say 'feedback: true' (and "
                f"bound {STABILITY_MEASURE}) or 'feedback: false'",
                refs=[top], severity="warning")
        return []
    if not fb:
        return []
    m = next((m for m in spec.get("measures") or []
              if isinstance(m, dict) and m.get("name") == STABILITY_MEASURE),
             None)
    bounds = m.get("bounds") if m else None
    if not isinstance(bounds, dict) or not isinstance(bounds.get("min"),
                                                      (int, float)):
        bad("feedback_no_stability",
            f"spec.yaml says 'feedback: true' but has no measure "
            f"{STABILITY_MEASURE!r} with a 'min' bound: a loop needs a "
            "phase margin (skills/ade/templates/stb_tb.cir)",
            refs=[STABILITY_MEASURE])
        return []
    scope = m.get("corners", "default")
    if not isinstance(scope, str) or scope not in ADE_MEASURE_CORNER_KINDS:
        bad("stability_not_all_corners",
            f"measure {STABILITY_MEASURE!r} is scoped to corners "
            f"{m.get('corners')}: a feedback block's phase margin is bound "
            "at every corner the spec sweeps ('default' or 'all')",
            refs=[STABILITY_MEASURE])
    if m.get("severity", "error") != "error":
        bad("stability_not_error",
            f"measure {STABILITY_MEASURE!r} has severity "
            f"{m.get('severity')!r}: a feedback block's phase margin is an "
            "error bound", refs=[STABILITY_MEASURE])
    return []


def _timeout_ok(v) -> bool:
    return (not isinstance(v, bool) and isinstance(v, (int, float))
            and 0 < v <= MAX_SIM_TIMEOUT_S)


def sim_timeout_problem(field) -> str | None:
    """What is wrong with a spec's `sim_timeout_s` field, or None."""
    if not isinstance(field, dict) or not field:
        return ("spec.yaml 'sim_timeout_s' must be a non-empty mapping "
                f"{{check: seconds}}, check one of {list(SIM_TIMEOUT_CHECKS)}")
    unknown = sorted(str(k) for k in field if k not in SIM_TIMEOUT_CHECKS)
    if unknown:
        return (f"spec.yaml 'sim_timeout_s' names {unknown}, which run no "
                f"ngspice; the checks it may name: {list(SIM_TIMEOUT_CHECKS)}")
    bad = sorted(k for k, v in field.items() if not _timeout_ok(v))
    if bad:
        return (f"spec.yaml 'sim_timeout_s' for {bad} must be a number of "
                f"seconds in (0, {MAX_SIM_TIMEOUT_S:g}]")
    return None


def sim_timeout(spec: dict | None, check: str, override: float | None,
                default: float) -> float:
    """The ngspice time limit `check` runs with: the CLI's --timeout
    (`override`) when given, else the spec's `sim_timeout_s[check]`, else
    `default`. A bad spec field or a bad override is a CheckError, so a
    gate never runs on a limit nobody asked for."""
    from checklib import CheckError

    if override is not None:
        if not _timeout_ok(override):
            raise CheckError(f"--timeout {override:g} must be a number of "
                             f"seconds in (0, {MAX_SIM_TIMEOUT_S:g}]")
        return float(override)
    field = (spec or {}).get("sim_timeout_s")
    if field is None:
        return default
    problem = sim_timeout_problem(field)
    if problem:
        raise CheckError(problem + " - run spec_lint")
    return float(field.get(check, default))


def timeout_hint(check: str, seconds: float) -> str:
    """The remediation a timed-out run carries: the knob that raises it."""
    return (f"{check}'s ngspice run hit its {seconds:g} s limit. If the "
            f"bench is just slow, raise it in spec/spec.yaml with "
            f"'sim_timeout_s: {{{check}: <seconds>}}' (at most "
            f"{MAX_SIM_TIMEOUT_S:g}), or pass --timeout; if it should not "
            "take that long, it hangs - fix the bench or the netlist")


def footprint_um(spec: dict) -> dict | None:
    """spec.yaml's `footprint_um` as {width, height} floats, or None when
    it is absent or malformed (lint_spec_ade refuses the malformed case)."""
    fp = spec.get("footprint_um") if isinstance(spec, dict) else None
    if not (isinstance(fp, dict) and set(fp) == {"width", "height"}):
        return None
    for k in ("width", "height"):
        v = fp[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not 0 < v < float("inf"):
            return None
    return {"width": float(fp["width"]), "height": float(fp["height"])}


def erc_nets(spec: dict) -> dict | None:
    """spec.yaml's `erc_nets` as {pin: {"power": V} | {"min": V, "max": V}}
    with float volts ({} when absent), or None when malformed
    (lint_spec_ade refuses that, and so does check_analog_erc.py)."""
    field = spec.get("erc_nets", {}) if isinstance(spec, dict) else {}
    if not isinstance(field, dict):
        return None
    out = {}
    for pin, rng in field.items():
        if not (isinstance(pin, str) and pin.strip() and isinstance(rng, dict)
                and set(rng) in ({"power"}, {"min", "max"})):
            return None
        vals = {}
        for k, v in rng.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v):
                return None
            vals[k] = float(v)
        if "min" in vals and vals["min"] > vals["max"]:
            return None
        out[pin] = vals
    return out


MATCH_STYLES = ("common_centroid", "interdigitate")
MATCH_TOL_UM = 0.05


def matching(spec: dict) -> list[dict] | None:
    """spec.yaml's `matching` as [{devices: [lower-case names], style,
    tol_um}] ([] when the field is absent), or None when malformed
    (lint_spec_ade refuses that, and so does check_layout_match.py)."""
    field = spec.get("matching", []) if isinstance(spec, dict) else []
    if not isinstance(field, list):
        return None
    out, seen = [], set()
    for row in field:
        if not (isinstance(row, dict) and set(row) <= {"devices", "style", "tol_um"}):
            return None
        devs = row.get("devices")
        if not (isinstance(devs, list) and len(devs) >= 2 and all(
                isinstance(d, str) and d.strip() for d in devs)):
            return None
        names = [d.strip().lower() for d in devs]
        if len(set(names)) != len(names) or seen & set(names):
            return None
        seen |= set(names)
        if row.get("style") not in MATCH_STYLES:
            return None
        tol = row.get("tol_um", MATCH_TOL_UM)
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) \
                or not math.isfinite(tol) or tol <= 0:
            return None
        out.append({"devices": names, "style": row["style"], "tol_um": float(tol)})
    return out


SYMMETRY_BOUNDS = ("max_dc_pct", "max_dr_pct", "min_dc_ff", "min_dr_ohm")


def symmetric_nets(spec: dict) -> list[dict] | None:
    """spec.yaml's `symmetric_nets` as [{nets: [a, b], max_dc_pct,
    max_dr_pct, min_dc_ff, min_dr_ohm, aggressors}] - a bound left out is
    None (not checked), a floor left out 0.0, aggressors [] - ([] when the
    field is absent), or None when malformed (lint_spec_ade refuses that,
    and so does check_pex_sim.py)."""
    field = spec.get("symmetric_nets", []) if isinstance(spec, dict) else []
    if not isinstance(field, list):
        return None
    out = []
    for row in field:
        if not (isinstance(row, dict) and set(row) <= {
                "nets", "aggressors", *SYMMETRY_BOUNDS}):
            return None
        nets = row.get("nets")
        if not (isinstance(nets, list) and len(nets) == 2 and all(
                isinstance(n, str) and n.strip() for n in nets)
                and nets[0].lower() != nets[1].lower()):
            return None
        vals = {}
        for k in SYMMETRY_BOUNDS:
            v = row.get(k)
            if v is None and k not in row:
                vals[k] = 0.0 if k.startswith("min_") else None
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or v < 0:
                return None
            vals[k] = float(v)
        if vals["max_dc_pct"] is None and vals["max_dr_pct"] is None:
            return None
        aggr = row.get("aggressors", [])
        if not (isinstance(aggr, list) and all(
                isinstance(n, str) and n.strip()
                and n.lower() not in {x.lower() for x in nets} for n in aggr)):
            return None
        if aggr and vals["max_dc_pct"] is None:
            return None  # an aggressor's C is bounded by max_dc_pct
        out.append({"nets": list(nets), **vals, "aggressors": list(aggr)})
    return out


def harden_signoff(spec: dict) -> dict | None:
    """spec.yaml's optional `harden_signoff` ({} when absent), or None when
    malformed: ir_budget_pct a number in (0, 100], floating_nets_waived and
    drv_pins_waived each a mapping of net or pin name to a non-empty
    reason, qor_regressions "warning" (the default) or "error" (#152: a
    QoR regression against the recorded harden fails the gate)."""
    hs = spec.get("harden_signoff", {}) if isinstance(spec, dict) else {}
    if not isinstance(hs, dict) \
            or set(hs) - {"ir_budget_pct", "floating_nets_waived",
                          "drv_pins_waived", "qor_regressions"}:
        return None
    if hs.get("qor_regressions", "warning") not in ("warning", "error"):
        return None
    if "ir_budget_pct" in hs:
        v = hs["ir_budget_pct"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not 0 < v <= 100:
            return None
    for key in ("floating_nets_waived", "drv_pins_waived"):
        w = hs.get(key, {})
        if not isinstance(w, dict) or not all(
                isinstance(r, str) and r.strip() for r in w.values()):
            return None
    return hs


def matching_by_name(name) -> bool:
    """Does a measure's name say it is matching-sensitive? True when one
    of its `_`-separated tokens (case-insensitive) is in
    MATCHING_NAME_TOKENS: `cp_updn_mismatch_pct`, `vos_offset_mv`,
    `inl_max_abs` - never a substring inside a token (`ratiometric`)."""
    if not isinstance(name, str):
        return False
    return any(t in MATCHING_NAME_TOKENS for t in name.lower().split("_"))


def nominal_only_measures(spec: dict) -> set[str]:
    """The lower-case names of the measures spec.yaml marks `statistical:
    none`: nominal checks the mc gate leaves out of its samples' judgement
    (check_mc.py). spec_lint requires each to carry an mc_waiver reason
    (lint_matching_measures)."""
    return {m["name"].lower() for m in (spec.get("measures") or [])
            if isinstance(m, dict) and isinstance(m.get("name"), str)
            and m.get("statistical") == "none"}


def lint_matching_measures(spec: dict, rel_path: str = "spec/spec.yaml",
                           mc_corner_names=("tt",)
                           ) -> tuple[list[dict], list[dict]]:
    """A matching-sensitive measure (offset, mirror or UP/DN ratio,
    INL/DNL) checked only at zero mismatch passes for the wrong reason:
    sim_tt/sim_pvt run with the PDK's sw_stat_mismatch=0, so they see the
    systematic part alone. Such a measure needs the mc gate - the one gate
    that turns mismatch on - so this returns (violations, waived):

    - `mismatch_measure_no_mc`: a measure with `statistical: mismatch`, or
      a matching name (matching_by_name) without a waiver, in a spec with
      no `mc.enabled: true`;
    - `mismatch_measure_not_at_tt`: the same measure scoped by its own
      `corners` list to corners without tt - tt is the one corner the mc
      gate always runs (`mc_corner_names`: every name of that corner,
      corners.names_of); its second run is at each bench's worst sweep
      corner, picked by margin, which need not be the measure's own;
    - `bad_statistical`: `statistical` not in STATISTICAL_KINDS, or
      `statistical: none` without a non-empty `mc_waiver` reason (the
      name heuristic is overridden only with a written reason).

    `waived` lists {measure, why} for each matching-named measure that
    says `statistical: none` with a reason - reported, never silent."""
    from checklib import violation

    out: list[dict] = []
    waived: list[dict] = []
    mc_cfg = spec.get("mc")
    mc_on = isinstance(mc_cfg, dict) and bool(mc_cfg.get("enabled"))
    for m in spec.get("measures") or []:
        if not isinstance(m, dict):
            continue
        name = m.get("name")
        refs = [name] if isinstance(name, str) else []
        stat = m.get("statistical")
        waiver = m.get("mc_waiver")
        if stat is not None and stat not in STATISTICAL_KINDS:
            out.append(violation(
                "spec_lint", "error", rel_path, None, "bad_statistical", refs,
                f"measure {name!r} 'statistical' must be one of "
                f"{list(STATISTICAL_KINDS)}, got {stat!r}", "speclib"))
            continue
        if stat == "none":
            if not (isinstance(waiver, str) and waiver.strip()):
                out.append(violation(
                    "spec_lint", "error", rel_path, None, "bad_statistical",
                    refs, f"measure {name!r} says 'statistical: none' with "
                    "no 'mc_waiver' reason - write why mismatch cannot "
                    "move it", "speclib"))
            elif matching_by_name(name):
                waived.append({"measure": name, "why": waiver.strip()})
            continue
        if stat != "mismatch" and not matching_by_name(name):
            continue
        if not mc_on:
            out.append(violation(
                "spec_lint", "error", rel_path, None, "mismatch_measure_no_mc",
                refs, f"measure {name!r} is matching-sensitive "
                f"({'statistical: mismatch' if stat else 'by its name'}) but "
                "the spec has no 'mc: {enabled: true}' - sim_tt/sim_pvt run "
                "at zero mismatch and check only its systematic part; turn "
                "Monte Carlo on, or, if mismatch truly cannot move it, write "
                "'statistical: none' with an 'mc_waiver' reason", "speclib"))
        want = m.get("corners")
        if isinstance(want, list) and not set(want) & set(mc_corner_names):
            out.append(violation(
                "spec_lint", "error", rel_path, None,
                "mismatch_measure_not_at_tt", refs,
                f"measure {name!r} is matching-sensitive but its corners "
                f"{want} leave out tt, the one corner the mc gate always runs "
                "- its worst-corner run need not land on one of these, so "
                "Monte Carlo might never score it; add tt", "speclib"))
    return out, waived


def lint_measures_vs_bench_bounds(spec: dict, bench_bounds: dict[str, list[dict]],
                                  rel_path: str = "spec/spec.yaml") -> list[dict]:
    """Cross-check spec.yaml's own `measures:` list against every tb/*.bounds.json
    sidecar's own measure names. design.md 1.3: "The bench-writer, in fresh
    context, writes tb/*.cir ... and a .bounds.json sidecar per bench" -
    independently of spec.yaml, so the two declarations of "what gets
    measured, with what bound" can silently drift apart with nothing
    catching it: a spec measure no bench ever scores (a requirement sim_pvt
    never actually checks, at any corner, yet the spec claims it is
    covered) or a bench bound the spec never named (scored and enforced
    every run, but not a declared requirement at all - undeclared scope no
    reviewer reading spec.yaml alone would ever see). gates.yaml's ade
    spec_lint row fault ("a measure without bounds") widened one notch:
    a measure without a BENCH bound is the same failure shape, and so is
    its mirror. Each bound's optional `corners` scope (absent = "all") must
    also equal its spec measure's own `corners` ("default" and "all" both
    count as "all") - bench_bound_corner_mismatch otherwise. Its `min`/`max`
    must equal the spec measure's own `bounds` - bench_bound_value_mismatch
    otherwise (a looser bench passes a block that misses the spec). A
    bound's optional `sensitivity` (and `sensitivity_abs`/
    `sensitivity_unit`) is not compared: it is no pass bound, only
    bench_strength's kill threshold against the unmutated design.

    `bench_bounds` is {bench filename: [bounds sidecar entries, already
    simlib.load_bounds()-validated]} - built by the caller (this module
    never touches the filesystem or imports sim_run/simlib, kept a plain
    lib/scripts boundary same as the rest of this file); an empty dict
    (no benches written yet - spec_lint runs right after spec-writer,
    before any bench-writer step in skills/ade/reference/tasks.yaml's own
    `spec` verb) means nothing to reconcile against yet, not a violation."""
    from checklib import violation

    out: list[dict] = []
    if not bench_bounds:
        return out

    measures = spec.get("measures")
    if not isinstance(measures, list):
        return out  # lint_spec_ade's own no_measures violation covers this

    def bad(kind: str, msg: str, refs=None, severity="error"):
        out.append(violation("spec_lint", severity, rel_path, None, kind,
                             refs or [], msg, "speclib"))

    spec_names = {m["name"] for m in measures
                 if isinstance(m, dict) and isinstance(m.get("name"), str)
                 and m["name"].strip()}
    bench_names: dict[str, list[str]] = {}
    for bench_name, bounds in bench_bounds.items():
        for b in bounds:
            name = b.get("measure")
            if isinstance(name, str) and name.strip():
                bench_names.setdefault(name, []).append(bench_name)

    for name in sorted(spec_names - bench_names.keys()):
        bad("measure_no_bench_bound",
           f"measure {name!r} is declared in spec.yaml's 'measures' but no "
           "tb/*.bounds.json sidecar scores it - a requirement with "
           "nothing actually checking it", refs=[name])

    for name in sorted(bench_names.keys() - spec_names):
        benches = ", ".join(sorted(set(bench_names[name])))
        bad("bench_bound_no_spec_measure",
           f"measure {name!r} has a tb/*.bounds.json bound ({benches}) but "
           "spec.yaml's own 'measures' list never declares it - an "
           "undeclared requirement being silently enforced every run",
           refs=[name])

    def scope(field) -> frozenset[str] | str:
        # "default" and "all" both mean every corner the gate runs - a
        # per-measure field narrows nothing unless it is a list.
        return frozenset(field) if isinstance(field, list) else "all"

    def show(x) -> str:
        return "'all'" if x == "all" else str(sorted(x))

    spec_scope = {m["name"]: scope(m.get("corners", "default"))
                  for m in measures if isinstance(m, dict)
                  and m.get("name") in spec_names}
    for bench_name, bounds in sorted(bench_bounds.items()):
        for b in bounds:
            name = b.get("measure")
            if name not in spec_scope:
                continue
            want, got = spec_scope[name], scope(b.get("corners", "all"))
            if want == got:
                continue
            bad("bench_bound_corner_mismatch",
               f"measure {name!r} in {bench_name}: the bound is scored at "
               f"corners {show(got)} but spec.yaml scores it at "
               f"{show(want)} - a narrower bound hides a corner the spec "
               "scores, a wider one fails a corner it does not",
               refs=[name, bench_name])

    # the ua pad condition (padlib.PAD_SCOPES): a bench scoring a measure
    # bare-only that the spec scores through the pad would hide the pad
    spec_pad = {m["name"]: m.get("pad", "both") for m in measures
                if isinstance(m, dict) and m.get("name") in spec_names}
    for bench_name, bounds in sorted(bench_bounds.items()):
        for b in bounds:
            name = b.get("measure")
            if name not in spec_pad or b.get("pad", "both") == spec_pad[name]:
                continue
            bad("bench_bound_pad_mismatch",
               f"measure {name!r} in {bench_name}: the bound is scored in "
               f"ua pad condition {b.get('pad', 'both')!r} but spec.yaml "
               f"scores it in {spec_pad[name]!r} - the spec says where a "
               "measure assumes a bare pin, and the bench follows it",
               refs=[name, bench_name])

    spec_bounds = {m["name"]: m.get("bounds") for m in measures
                   if isinstance(m, dict) and m.get("name") in spec_names}
    for bench_name, bounds in sorted(bench_bounds.items()):
        for b in bounds:
            name = b.get("measure")
            want = spec_bounds.get(name)
            if not isinstance(want, dict):
                continue
            for side in ("min", "max"):
                w, g = want.get(side), b.get(side)
                if w is None and g is None:
                    continue
                if (w is not None and g is not None
                        and math.isclose(float(w), float(g),
                                         rel_tol=1e-9, abs_tol=1e-18)):
                    continue
                bad("bench_bound_value_mismatch",
                   f"measure {name!r} in {bench_name}: the bench scores "
                   f"{side}={g!r} but spec.yaml's bound is {side}={w!r} - "
                   "a looser bench passes a block that misses the spec, a "
                   "tighter one fails a block that meets it",
                   refs=[name, bench_name])

    return out


def load_spec(path: Path) -> dict:
    """Parse spec.yaml. Anything that is not a YAML mapping raises - a lint
    violation needs a dict to report fields against, so a malformed file is
    an operational error (exit 2 via CheckError upstream), not a finding."""
    from checklib import CheckError

    p = Path(path)
    if not p.is_file():
        raise CheckError(f"no spec.yaml at {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CheckError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckError(f"{p} must be a YAML mapping, got {type(data).__name__}")
    return data


def lint_spec(spec: dict, rel_path: str = "spec/spec.yaml") -> list[dict]:
    """Return the list of spec_lint violations - [] means a clean spec."""
    from checklib import violation

    out: list[dict] = []

    def bad(kind: str, msg: str, refs=None, severity="error"):
        out.append(violation("spec_lint", severity, rel_path, None, kind,
                             refs or [], msg, "speclib"))

    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        bad("no_top", "spec.yaml has no non-empty 'top' module name")

    reqs = spec.get("requirements")
    if not isinstance(reqs, list) or not reqs:
        bad("no_requirements", "spec.yaml has no non-empty 'requirements' list")
        reqs = []

    seen_ids: set[str] = set()
    for i, req in enumerate(reqs):
        where = f"requirements[{i}]"
        if not isinstance(req, dict):
            bad("requirement_not_a_mapping", f"{where} is not a mapping")
            continue
        rid = req.get("id")
        if not isinstance(rid, str) or not rid.strip():
            bad("requirement_no_id", f"{where} has no non-empty 'id'")
            rid = None
        elif rid in seen_ids:
            bad("requirement_dup_id", f"requirement id {rid!r} is not unique",
               refs=[rid])
        else:
            seen_ids.add(rid)

        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            bad("requirement_no_text", f"{where} (id {rid!r}) has no "
                                       "non-empty 'text'", refs=[rid] if rid else [])

        check = req.get("check")
        if check not in CHECK_KINDS:
            # this is the fault gates.yaml names for spec_lint: "a
            # requirement with no way to check it".
            bad("requirement_no_check",
               f"{where} (id {rid!r}) has no valid 'check' kind (must be "
               f"one of {sorted(CHECK_KINDS)}, got {check!r})",
               refs=[rid] if rid else [])
        elif check == "measure" and not req.get("bounds"):
            bad("requirement_measure_no_bounds",
               f"{where} (id {rid!r}) has check: measure but no 'bounds'",
               refs=[rid] if rid else [])
        elif check == "measure":
            # issue #89: a measure is held to a recorded number, or it is
            # checked by nothing (measurelib.py; release evaluates it)
            import measurelib
            bounds = req.get("bounds")
            if not isinstance(bounds, dict):
                bad("requirement_measure_bad_bound",
                   f"{where} (id {rid!r}) bounds must be a mapping "
                   "{name: {min?, max?, equals?, source}}",
                   refs=[rid] if rid else [])
            else:
                known = {"vde": measurelib.gate_names("vde"),
                         "msde": measurelib.gate_names("msde")}
                for name, b in bounds.items():
                    for kind, msg in measurelib.lint_bound(name, b, known):
                        bad(kind, f"{where} (id {rid!r}): {msg}",
                           refs=[rid] if rid else [])
        elif check in ("formal", "both"):
            # M3 (docs/design.md "### M3."): check_formal.py joins a
            # requirement to its assert/cover in formal/*.sv by this label,
            # never by a text search - a requirement with no 'property'
            # would otherwise have to be searched for by name, and that
            # falls apart the moment the property's own name and the
            # requirement's id can't agree (dashes are illegal in a Verilog
            # statement label, so they never can).
            prop = req.get("property")
            if not isinstance(prop, str) or not prop.strip():
                bad("requirement_formal_no_property",
                   f"{where} (id {rid!r}) has check: {check} but no "
                   "'property' label naming its assert/cover in formal/*.sv",
                   refs=[rid] if rid else [])

    tt_pins = spec.get("tt_pins")
    if tt_pins is not None and (not isinstance(tt_pins, dict) or not tt_pins):
        bad("tt_pins_empty", "'tt_pins' is present but not a non-empty mapping")

    if spec.get("pad_reset_sync") is not None:
        import ttlib  # engine/lib, beside this module
        try:
            ttlib.pad_reset_opt_out(spec)
        except ttlib.PadResetOptOutError as exc:
            kind, _, msg = str(exc).partition(": ")
            bad(kind, msg)

    if harden_signoff(spec) is None:
        bad("bad_harden_signoff",
            "'harden_signoff' takes ir_budget_pct (a number in (0, 100]), "
            "floating_nets_waived ({net: non-empty reason}), "
            "drv_pins_waived ({pin: non-empty reason}) and qor_regressions "
            "(warning or error), nothing else")

    clock = spec.get("clock")
    if clock is not None:
        if not isinstance(clock, dict):
            bad("clock_not_a_mapping", "'clock' is present but not a mapping")
        else:
            period = clock.get("period_ns")
            if not isinstance(period, (int, float)) or isinstance(period, bool) \
                    or period <= 0:
                bad("clock_bad_period",
                   "'clock.period_ns' must be a positive number")
            domains = clock.get("domains")
            if not isinstance(domains, list) or not domains:
                bad("clock_no_domains",
                   "'clock.domains' must be a non-empty list")
            primary = clock.get("primary")
            if primary is not None and (not isinstance(domains, list)
                                        or primary not in domains):
                bad("clock_bad_primary",
                   "'clock.primary' must name one of 'clock.domains' (the "
                   "domain harden's CLOCK_PORT carries)")
            import clock_budget  # engine/lib, beside this module
            try:
                clock_budget.budgets(spec)
            except clock_budget.ClockBudgetError as exc:
                kind, _, msg = str(exc).partition(": ")
                bad(kind, msg)

    return out

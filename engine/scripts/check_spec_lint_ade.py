#!/usr/bin/env python
"""check_spec_lint_ade.py - /ade's spec_lint gate (docs/design.md 1.5,
"### M8."). A dedicated script rather than a skill-aware check_spec_lint.py:
that script is vde's own (M2), requires a non-empty `requirements` list and
validates `tt_pins`/`clock` - neither of which gates.yaml's ade spec_lint row
asks for ("Every measure has bounds and a corner set; supply and devices
declared."), and reusing it wholesale would force every analog block to also
carry digital-shaped fields it has no use for. Mirrors check_spec_lint.py's
own shape exactly otherwise.

    check_spec_lint_ade.py --workspace DIR [--out FILE]

Fault this gate must catch (gates.yaml): "a measure without bounds" - which
this script reads as covering BOTH halves of "bounds": spec.yaml's own
per-measure `bounds` dict (lint_spec_ade, unchanged) and, when tb/ already
has bench(es) written, whether each spec measure has a matching tb/*.bounds
.json sidecar bound and vice versa, each bound's optional `corners` equal
to its measure's own (speclib.lint_measures_vs_bench_bounds -
the bench-writer works in fresh context, per docs/design.md 1.3, so the two
declarations can drift apart with nothing else catching it). It also expands
the spec's `corners` field through corners.spec_corners(), so a `{grid: ...}`
sim_pvt would refuse is a `bad_corners` finding here, and a measure whose
own `corners` list names a corner no sweep of the spec could run, by any of
its names, is `measure_bad_corners` (measure_corner_names). A matching-sensitive
measure (offset, ratio, INL/DNL, UP/DN mismatch) in a spec without
`mc.enabled: true` is `mismatch_measure_no_mc` (speclib.
lint_matching_measures): every other sim gate runs at zero mismatch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import padlib  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402

SCRIPT = "check_spec_lint_ade"
SPEC_REL = "spec/spec.yaml"


def load_bench_bounds(ws: Path) -> dict[str, list[dict]]:
    """{bench filename: [bounds sidecar entries]} for every tb/*.cir with a
    matching tb/*.bounds.json sidecar - {} when tb/ does not exist yet or
    has none (spec_lint runs before any bench-writer step exists in
    skills/ade/reference/tasks.yaml's own `spec` verb; nothing to
    reconcile against yet is not itself a violation). Deliberately not
    sim_run.find_benches(): that raises when no bench exists at all, which
    is the ordinary, expected state right after spec-writer runs, not an
    error this gate should ever surface."""
    tb = ws / "tb"
    out: dict[str, list[dict]] = {}
    if not tb.is_dir():
        return out
    for cir in sorted(tb.glob("*.cir")):
        bounds_path = tb / f"{cir.stem}.bounds.json"
        if bounds_path.is_file():
            out[cir.name] = simlib.load_bounds(bounds_path)
    return out


def measure_corner_names(field) -> set[str]:
    """Every name a measure's `corners` list may use under the spec
    `corners` field `field`: each name (corners.names_of, so `tt_27c` names
    `tt`) of every corner sim_pvt could sweep for it - the spec's own
    corners plus the passive_corners a resistor or MIM netlist brings in
    (corners.spec_corners with every passive device). Read from the spec
    alone, so this gate keeps spec_yaml as its only input; whether this
    netlist really sweeps a passive corner is sim_pvt's to refuse
    (sim_bound_not_swept). Raises CheckError on a bad field."""
    sweep = corners_mod.spec_corners(corners_mod.load(), field,
                                     passives=corners_mod.PASSIVE_DEVICES)
    return set().union(*(corners_mod.names_of(c) for c in sweep))


def ua_pin_violations(ws: Path, spec: dict, found: list[dict]) -> list[dict]:
    """Every `ua_pins` entry must be a port of the top's `.subckt` in
    netlist/*.cir: sim_run puts the ua pad model on that port of every
    top instance in every bench. No netlist yet (spec_lint runs before the
    designer writes one) leaves it to sim_run, which refuses then."""
    if any(v["kind"] in ("bad_ua_pins", "no_top") for v in found):
        return []
    pins = padlib.ua_pins_of(spec)
    nets = sorted((ws / "netlist").glob("*.cir"))
    if not pins or not nets:
        return []
    top = spec["top"]
    try:
        ports = padlib.top_ports(
            nets[0].read_text(encoding="utf-8", errors="replace"), top)
    except padlib.PadRefused as exc:
        return [checklib.violation(
            "spec_lint", "error", SPEC_REL, None, "ua_pin_not_a_port",
            list(pins), f"ua_pins: {exc}", "padlib")]
    lower = {p.lower() for p in ports}
    bad = [p for p in pins if p.lower() not in lower]
    if not bad:
        return []
    return [checklib.violation(
        "spec_lint", "error", SPEC_REL, None, "ua_pin_not_a_port", bad,
        f"ua_pins {bad} are not ports of {top} ({', '.join(ports)}): the ua "
        "pad model goes on a port of the top's instance in every bench",
        "padlib")]


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec_path = ws / SPEC_REL
    spec = speclib.load_spec(spec_path)
    violations = speclib.lint_spec_ade(spec, rel_path=SPEC_REL)
    if not any(v["kind"] == "bad_corners" for v in violations):
        # the shape is right; now expand it the way sim_pvt will, so a grid
        # that misses ss or 125 C is refused here, not at P4
        try:
            swept = measure_corner_names(spec.get("corners", "default"))
        except checklib.CheckError as exc:
            swept = None
            violations.append(checklib.violation(
                "spec_lint", "error", SPEC_REL, None, "bad_corners", [],
                str(exc), "corners"))
        # a measure scoped to a corner no sweep of this spec runs (`[tt]`
        # under a grid, whose typical corners are tt_<temp>c) would never be
        # scored
        for m in spec.get("measures") or []:
            want = m.get("corners") if isinstance(m, dict) else None
            if swept is None or not isinstance(want, list):
                continue
            missing = [c for c in want if c not in swept]
            if missing:
                violations.append(checklib.violation(
                    "spec_lint", "error", SPEC_REL, None,
                    "measure_bad_corners", [m.get("name") or ""],
                    f"measure {m.get('name')!r} names corner(s) {missing} "
                    f"no sweep of this spec runs, whatever its netlist "
                    f"holds: {sorted(swept)}", "corners"))
        pex = spec.get("pex_corners")
        if swept is not None and isinstance(pex, list):
            missing = [c for c in pex if c not in swept]
            if missing:
                violations.append(checklib.violation(
                    "spec_lint", "error", SPEC_REL, None, "bad_pex_corners",
                    missing, f"pex_corners names corner(s) {missing} no sweep "
                    f"of this spec runs: {sorted(swept)}", "corners"))
        rc = spec.get("pex_rc_knobs")
        if swept is not None and not any(
                v["kind"] == "bad_pex_rc_knobs" for v in violations) and rc:
            problem = corners_mod.rc_knobs_problem(
                rc, corners_mod.spec_corners(
                    corners_mod.load(), spec.get("corners", "default"),
                    passives=corners_mod.PASSIVE_DEVICES),
                corners_mod.rc_knobs())
            if problem:
                violations.append(checklib.violation(
                    "spec_lint", "error", SPEC_REL, None, "bad_pex_rc_knobs",
                    [], problem, "corners"))
        variants = spec.get("pex_rc_corners")
        if swept is not None and not any(
                v["kind"] == "bad_pex_rc_corners"
                for v in violations) and variants:
            problem = corners_mod.rc_variants_problem(
                variants, corners_mod.spec_corners(
                    corners_mod.load(), spec.get("corners", "default"),
                    passives=corners_mod.PASSIVE_DEVICES),
                corners_mod.rc_variants())
            if problem:
                violations.append(checklib.violation(
                    "spec_lint", "error", SPEC_REL, None, "bad_pex_rc_corners",
                    [], problem, "corners"))
    violations += ua_pin_violations(ws, spec, violations)
    tt = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]
    matching, waived = speclib.lint_matching_measures(
        spec, rel_path=SPEC_REL, mc_corner_names=corners_mod.names_of(tt))
    violations += matching
    bench_bounds = load_bench_bounds(ws)
    violations += speclib.lint_measures_vs_bench_bounds(
        spec, bench_bounds, rel_path=SPEC_REL)
    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this gate ("spec_yaml" for ade's spec_lint too).
    payload = checklib.report(SCRIPT, spec_path, violations,
                              mc_waived=waived)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

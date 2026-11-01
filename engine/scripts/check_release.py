#!/usr/bin/env python
"""check_release.py - the release gate (docs/design.md 1.5, "### M3.").

    check_release.py --workspace DIR [--out FILE]

gates.yaml's `release` row names its tool "attest, strict" (docs/design.md
1.5's table) - this script is the check_<tool>.py wrapper gate.py's generic
dispatch (run_report_for_gate: import check_<tool>, call run(argv), expect
checklib's report shape) needs in order to run it exactly like every other
gate. The actual strict-release-coverage logic - walk every gate gates.yaml
registers for the skill, refuse unless each has a fresh recorded pass or a
durable waiver - already lives in attest.py's own build(); this script calls
it directly and reshapes its "problems" (plain strings) into checklib
violations so gate.py's evaluate() can apply gates.yaml's fail_severities/
max_count the same way it does for every other gate's findings.

"A gate that did not run is a refusal, never a pass" applies one level up
here too: attest.py's build() itself already refuses (returns problems, not
an attestation) for ANY gate with no recorded result, a stale one, or a
recorded FAIL - there is no path through this script that reports "pass"
without attest.py's own build() having produced a real attestation body.

On success, writes reports/checks.json (attest.py's write_attestation) -
docs/design.md section 2's "The record of what ran is the release."

A vde block's `check: measure` requirements (issue #89) are checked here:
each bound names the recorded gate fact it is held to (measurelib.py), and
release fails `measure_out_of_bounds` for a value outside its limits and
`measure_missing` for one no recorded run holds. A bound on the enclosing
msde block's record is deferred to that block's release, which runs after
cosim and checks it here too. facts.measures lists every bound as
pass/fail/missing/deferred; `eda python engine/scripts/measures.py
--workspace DIR` prints the same table any time before release.

An msde block (M10) owes one thing more: "both nested runs released, top
gates fresh" (docs/design.md 1.5's msde release row). Its two sides are
nested workspaces at `digital/` (skill vde) and `analog/` (skill ade), each
with its own state.json (docs/design.md 5); release refuses unless each
exists, carries the right skill, and its own reports/checks.json still
verifies against its current files and state (attest.py verify) - so a
nested RTL or netlist edit after that side released reads as a stale
nested release and refuses here, whatever the top gates say.

A hardened tile (vde or msde) also gets release/manifest.json, in the workspace
and in the staged tree: each deliverable's sha256 and the gate records holding
it (manifestlib.py, issue #132). A file in the tree no gate holds refuses as
`deliverable_untied`. A refused release writes the manifest too, marked
`released: false`; checks.json's tt_submission holds the digest of a released
one. `chip-flow release --verify DIR` checks a submission tree against it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import attest as attest_mod  # noqa: E402
import manifestlib  # noqa: E402
import measurelib  # noqa: E402
import speclib  # noqa: E402
import statelib  # noqa: E402
import ttship  # noqa: E402

SCRIPT = "check_release"
# msde's nested workspaces: directory under the msde block -> its skill.
NESTED = {"digital": "vde", "analog": "ade"}


def nested_problems(ws: Path) -> list[str]:
    """Why an msde block's nested runs do not count as released, one
    string per side ("<side>: <reason>"); empty when both do."""
    problems = []
    for side, skill in NESTED.items():
        sub = ws / side
        if not (sub / "state.json").is_file():
            problems.append(f"nested_{side}: no nested workspace at {side}/")
            continue
        data = checklib.load_json(sub / "state.json", f"{side}/state.json")
        if data.get("skill") != skill:
            problems.append(f"nested_{side}: {side}/ is skill "
                            f"{data.get('skill')!r}, expected {skill!r}")
            continue
        verdict = attest_mod.verify(sub)
        if not verdict.get("valid"):
            problems.append(f"nested_{side}: not released: "
                            f"{verdict.get('reason')}")
    return problems


def input_path(ws: Path) -> Path:
    """The path this release's report digests: the first input kind
    invalidation.yaml's gate_inputs names for this skill's release (rtl
    for vde, netlist for ade, interface for msde)."""
    data = checklib.load_json(ws / "state.json", "state.json")
    imap = statelib.load_map()
    kinds = (imap["gate_inputs"].get(data.get("skill")) or {}).get("release")
    if not kinds:
        raise checklib.CheckError(
            f"invalidation.yaml has no gate_inputs.{data.get('skill')}."
            "release - a release with no input hashes is not evidence")
    return ws / statelib.kind_path(kinds[0], imap, data.get("artifacts"))


def with_tt_submission(att: dict, record: dict) -> dict:
    """The attestation with the TT submission record (signoff versions and
    what the workflow ships) inside it, its digest recomputed over the
    whole body exactly as attest.build/verify compute it."""
    body = {k: v for k, v in att.items() if k != "attestation_sha256"}
    body["tt_submission"] = record
    body["attestation_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    return body


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--max-report-age-h", type=float, default=24.0)
    ap.add_argument("--tt-repo", help="the Tiny Tapeout repo to stage and "
                    "check (default: <workspace>/release/tt)")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    att, problems = attest_mod.build(ws, max_report_age_h=args.max_report_age_h)
    skill = checklib.load_json(ws / "state.json", "state.json").get("skill")
    if skill == "msde":
        nested = nested_problems(ws)
        if nested:
            att, problems = None, problems + nested
    # A hardened tile ships through a TT workflow: the GDS it ships must be
    # the one the gates signed off (ttship.py), and every file in the tree
    # must be held by a gate record (manifestlib.py). Staged even when the
    # gates refuse, so the tree that would ship is there to look at.
    tt_record = None
    harden = ttship.HARDEN_DIR.get(skill)
    is_tile = harden and (ws / harden / "config.json").is_file()
    repo = Path(args.tt_repo) if args.tt_repo else ws / "release" / "tt"
    entries: list[dict] = []
    try:
        waivers = attest_mod.load_waivers(ws)
    except ValueError:
        waivers = []    # attest.build above has already refused on this
    if is_tile:
        ship, tt_record = ttship.release_check(ws, repo, skill=skill)
        if ship:
            att, problems = None, problems + ship
        if tt_record:
            entries, untied = manifestlib.deliverables(ws, skill, repo, tt_record,
                                                       waivers)
            if untied:
                att, problems = None, problems + untied
    # issue #89: every vde `check: measure` bound is read from the gate
    # record it names; a bound it misses, or a number nobody recorded,
    # refuses the release (missing is never a pass)
    measures = []
    if skill == "vde":
        spec = speclib.load_spec(ws / "spec" / "spec.yaml")
        measures = measurelib.evaluate(ws, spec)
    elif skill == "msde" and (ws / "digital" / "spec" / "spec.yaml").is_file():
        # digital deferred these to here: cosim runs after its release, and
        # attest above has required this block's cosim fresh and passing
        spec = speclib.load_spec(ws / "digital" / "spec" / "spec.yaml")
        measures = measurelib.nested_msde_rows(ws / "digital", spec)
    measure_violations = [
        checklib.violation(
            "release", "error", "spec/spec.yaml", None,
            "measure_out_of_bounds" if m["status"] == "fail"
            else "measure_missing", [m["req"]] if m["req"] else [],
            f"{m['req']} {m['name']} ({m['source']}): "
            + (m["why"] if m["status"] == "fail"
               else f"no value: {m['why']}"), "measurelib")
        for m in measures if m["status"] in ("fail", "missing")]
    if measure_violations:
        att = None
    violations = measure_violations + [
        checklib.violation("release", "error", None, p.split(":", 1)[0]
                           if ":" in p else None,
                           "nested_not_released" if p.startswith("nested_")
                           else "tt_ship_mismatch" if p.startswith("tt_")
                           else "deliverable_untied"
                           if p.startswith("deliverable_untied")
                           else "gate_not_ready", [], p, "attest")
        for p in problems
    ]
    manifest = None
    if is_tile:
        manifest = manifestlib.write(
            ws, repo, skill, entries, tt_record or {}, att,
            [v["msg"] for v in violations], waivers)
        if att is not None:
            sealed = {k: v for k, v in tt_record.items() if k != "sources"}
            att = with_tt_submission(att, {
                **sealed, "manifest": manifestlib.PATH,
                "manifest_sha256": manifest["manifest_sha256"]})

    facts: dict = {}
    if att is not None:
        path = attest_mod.write_attestation(ws, att)
        waived = sorted(c["gate"] for c in att["checks"] if c.get("waived"))
        facts = {
            "attestation": str(path.relative_to(ws)),
            "attestation_sha256": att["attestation_sha256"],
            "disposition": attest_mod.disposition(ws)["disposition"],
            "waived": waived,
        }
    if tt_record is not None:
        facts["tt_submission"] = {k: v for k, v in tt_record.items()
                                  if k != "sources"}
    if manifest is not None:
        facts["manifest"] = manifestlib.PATH
        facts["manifest_released"] = manifest["released"]
    if measures:
        facts["measures"] = measures

    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this skill's release row (input_path above) - so an
    # /ade block, which has no rtl/, still records its release pass.
    payload = checklib.report(SCRIPT, input_path(ws), violations, **facts)
    return payload, args.out



def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

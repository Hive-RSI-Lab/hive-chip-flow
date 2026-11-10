#!/usr/bin/env python
"""attest.py - the record of what ran (docs/design.md 1.5's `release` gate,
section 2's "The record of what ran is the release").

Ported from /hwde's scripts/attest.py, scoped down for M1: /hwde's
attest.py delegates to lib/releaselib.py (fab package hashes, durable
waiver bindings, a hand-set-proof disposition ladder from draft through
ordered/built) - none of that is in engine/lib/ (docs/design.md 1.1 lists
checklib, safelib, statelib, benchlib, simlib, speclib only) because a
board's fab/order concepts don't port. What attest.py owns is the part that
does: walk every gate gates.yaml registers for the block's skill, check each
has a recorded pass whose input hashes match the files now (statelib
freshness), and write reports/checks.json - gate, tool, result, inputs,
timestamp. M1's gates were all stubs, so build() always refused; M3 ("###
M3.") is where a real release can happen, and where a scoped-down waiver/
durability mechanism lands (load_waivers/waiver_problems below) - scoped
down because a board's fab/order concepts (/hwde releaselib's durable-waiver
required fields beyond reason+approved, its manufacturing-option waivers)
don't port, but the CORE idea does: a waiver binds to the gate's CURRENT
input hashes (freshness_report's current_inputs, re-hashed from the files on
disk every call - never the gate's own last RECORDED inputs, which stay
frozen at record time and so would let a waiver approved on RTL A silently
keep covering the gate after the RTL moves to B with no re-run in between)
and to checklib.CHECKER_VERSION, so an artifact edit or a checker semantics
bump invalidates it and it must be re-approved. A waiver is also refused
outright whenever the gate's own hash_valid is False (its last recorded run
no longer matches the current files): a waiver never covers a gate that has
not actually run against the inputs it is being asked to cover.

A dimension the person scoped out at H1 (corners.py, "Scoped-out
dimensions" - sim_pvt pins it at typical) is part of the record: build()
writes each, with the person's quote, as the attestation's `scoped_out`
(re-verified against the H1 answer; one that does not verify refuses), and
verify() treats a scope-out added or dropped since as a stale attestation.

Subcommands:
  build        Assemble + write reports/checks.json. Refuses (status
               violations, exit 1, nothing written) unless every applicable
               gate has a fresh recorded pass. The refusal lists every miss
               at once. Also refuses over a record `release` sealed: only
               release writes that seal, so re-run release to replace it.
  verify       Re-verify the written checks.json against the current tree
               and state. Read-only. Exit 0 valid / 1 invalid. An invalid
               record names the gates that changed since it was written.
  verify-tree  Verify every block committed at a revision, from its tracked
               files only (git archive: what a clone sees). Exit 1 names
               each block that does not attest valid and its stale gates.
               blocks/pll is the known exception (KNOWN_STALE, #212).
  disposition  Print the DERIVED release disposition (never hand-set):
               draft (nothing has passed yet), in-progress (some gates
               green), gates-green (every applicable gate fresh-pass),
               blocked (a gate has a recorded FAIL). An msde block counts
               its sides: a recorded FAIL in one blocks it, and a side not
               released holds it at in-progress. Read-only.

CLI: JSON to stdout or --out; exit 0 ok / 1 refused-or-invalid /
2 operational error.

  attest.py build --workspace DIR [--max-report-age-h H] [--out FILE]
  attest.py verify --workspace DIR [--out FILE]
  attest.py disposition --workspace DIR [--out FILE]
  attest.py verify-tree [--repo DIR] [--rev REV] [--out FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import boundary  # noqa: E402
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import gate as gate_mod  # noqa: E402
import statelib  # noqa: E402

SCRIPT = "attest"
CHECKS_PATH = "reports/checks.json"
# msde's nested workspaces: directory under the msde block -> its skill.
NESTED = {"digital": "vde", "analog": "ade"}
WAIVERS_PATH = "reports/waivers.json"
# a waiver missing any of these is not durable - never applied, always a
# problem attest.py's own build() surfaces (docs/design.md 1.5's release
# row: "waivers carry reason, approval and durability").
REQUIRED_WAIVER_FIELDS = ("reason", "approved_by", "inputs_sha256")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_waivers(ws: Path) -> list[dict]:
    """The block's own reports/waivers.json - a JSON list, absent by
    default (no file = no waivers, not an error)."""
    path = ws / WAIVERS_PATH
    if not path.is_file():
        return []
    data = checklib.load_json(path, "waivers.json")
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON list of waivers")
    return data


def waiver_inputs_sha256(current_inputs: dict | None) -> str | None:
    """The durability binding: a canonical hash of the gate's CURRENT input
    hashes (freshness_report's current_inputs for this gate - re-hashed from
    the files on disk every call) - never the gate's own last RECORDED
    inputs (state.json's gates.<g>.last.inputs), which stay frozen at record
    time. Binding to the recorded value let a waiver approved while the RTL
    read A silently keep covering the gate after the RTL moved to B, with no
    re-run in between - entry.last.inputs never moved, so the old hash kept
    matching. Binding to current_inputs instead means the hash itself moves
    the moment a file does; waiver_problems below additionally refuses
    outright whenever hash_valid is False, so a waiver can never be written
    against a hypothetical "current" state the gate has not actually run
    against either."""
    if not isinstance(current_inputs, dict) or not current_inputs:
        return None
    return hashlib.sha256(
        json.dumps(current_inputs, sort_keys=True).encode("utf-8")).hexdigest()


def waiver_problems(w: dict, gate: str, entry: dict, verdict: dict) -> list[str]:
    """Durability problems for one candidate waiver against the gate entry
    it claims to cover - every one raises the release refusal, none of them
    silently drop the waiver back to "no waiver" (a malformed or stale
    waiver is worse than none: it looks like cover for a gate nobody
    actually re-approved). `verdict` is this gate's own statelib.
    gate_freshness verdict (fresh["gates"][gate]) - the same one gate_check
    already computed, so this never re-derives freshness on its own."""
    label = f"{gate}"
    missing = [f for f in REQUIRED_WAIVER_FIELDS if not w.get(f)]
    if missing:
        return [f"waiver [{label}]: not durable - missing "
                f"{'/'.join(missing)} (a release waiver must bind reason, "
                "approval and the gate's own input hashes)"]
    problems = []
    if verdict.get("hash_valid") is False:
        problems.append(
            f"waiver [{label}]: the gate's inputs changed since its last "
            "recorded run (" + ", ".join(verdict.get("changed_inputs") or [])
            + ") - a waiver never covers a gate that has not actually run "
            "against its current inputs; re-run the gate first")
    if verdict.get("strength_stale"):
        problems.append(f"waiver [{label}]: {verdict['strength_stale']} - a "
                        "waiver never covers a result the stricter gate has "
                        "not given")
    for why in verdict.get("upstream_stale") or []:
        problems.append(f"waiver [{label}]: {why} - a waiver never covers a "
                        "gate whose upstream has to re-run first (#147)")
    current = waiver_inputs_sha256(verdict.get("current_inputs"))
    if current is None:
        problems.append(f"waiver [{label}]: the gate has no current input "
                        "hashes to bind against - nothing durable to waive")
    elif w["inputs_sha256"] != current:
        problems.append(f"waiver [{label}]: approved against different "
                        "inputs than the gate's current ones - "
                        "re-approve under the current inputs")
    checker_version = w.get("checker_version")
    if checker_version != checklib.CHECKER_VERSION:
        problems.append(f"waiver [{label}]: approved under checker_version "
                        f"{checker_version!r}, current is "
                        f"{checklib.CHECKER_VERSION} - re-approve")
    expires = w.get("expires")
    if expires:
        from datetime import datetime, timezone
        try:
            exp = datetime.fromisoformat(str(expires))
        except (TypeError, ValueError):
            problems.append(f"waiver [{label}]: unparsable expires {expires!r}")
        else:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                problems.append(f"waiver [{label}]: expired {expires}")
    return problems


def applicable_gates(skill: str, imap: dict | None = None) -> list[str]:
    """Every gate this skill owes - every gates.yaml row for the skill
    applies, unconditionally (no /hwde-style constraints.json
    verification.not_applicable list here: a gate a later milestone has not
    built yet is not "not applicable", it is simply not fresh-passed, and
    build() below refuses on it like any other unbuilt/unrun gate - the one
    way past that for a SPECIFIC, reviewed exception is a durable waiver,
    never a blanket applicability carve-out).

    One narrow exception is decided per build, not here: a gate the
    block's OWN current spec says it does not ask for (not_applicable_reason
    below - /ade's `mc`, /vde's `cdc`) still appears in checks.json, as an ok
    entry that did not run and says why, bound to spec.yaml's hash so
    attest.verify() notices the spec changing afterwards. It is never
    dropped from the list. owed_gates below is the one per-block subtraction.

    The one gate left out is `release` itself: check_release.py calls
    build() to decide release, so counting release among the gates it owes
    would make its first pass demand an earlier one, and it could never
    pass."""
    imap = imap or statelib.load_map()
    return sorted(g for g in (imap["gate_inputs"].get(skill) or {})
                  if g != "release")


def _mc_not_applicable(ws: Path) -> str | None:
    """/ade's `mc`, not applicable when spec.yaml asks for no Monte Carlo.

    The decision: release accepts a DECLARED
    not-applicable, read from the block's current spec.yaml on every
    build - it does not accept a recorded pass for it. check_mc.py's own
    not-applicable answer cannot be recorded in state.json (see its
    comment), and should not be: a pass recorded while MC was off is keyed
    on netlist/tb and would stay fresh after the spec turned MC on. So the
    spec is asked again here, with check_mc's own predicate. If the spec
    is missing or unreadable, MC is owed, never skipped: spec_lint refuses
    that workspace anyway, and doubt never counts as "not applicable". A
    mutant ruling that claims an MC sigma with MC off makes it owed too
    (check_mc refuses that spec), since nothing else checks the claim."""
    import check_mc
    import speclib
    try:
        spec = speclib.load_spec(ws / "spec" / "spec.yaml")
        if check_mc.mc_applicable(spec, ws):
            return None
    except checklib.CheckError:
        return None
    return ("not applicable: spec.yaml asks for no Monte Carlo (no "
            "'mc.enabled: true'), re-read at this release")


def _cdc_not_applicable(ws: Path) -> str | None:
    """/vde's `cdc`, not applicable only to a netlist with no sequential
    cell. A design with flops and no clock info is NOT CHECKED, and owes the
    gate here: doubt never counts as "not applicable"."""
    import check_cdc
    import speclib
    try:
        return check_cdc.not_applicable_at_release(
            ws, speclib.load_spec(ws / "spec" / "spec.yaml"))
    except checklib.CheckError:
        return None


def _boundary_not_applicable(ws: Path) -> str | None:
    """/msde's `boundary`, not applicable only with no crossing pin."""
    import check_boundary
    return check_boundary.not_applicable_at_release(ws)


def owed_gates(ws: Path, skill: str, imap: dict | None = None) -> list[str]:
    """applicable_gates, less `boundary` and `top_erc` for a released block
    that engine/lib/boundary.py's V1_BLOCKS names while it is still on
    interface version 1: both gates came after those blocks were released.
    That list is two names a test pins, not a carve-out a block can opt
    into: each block leaves it by moving to version 2."""
    gates = applicable_gates(skill, imap)
    if skill != "msde" or not boundary.is_v1_block(ws):
        return gates
    import yaml
    try:
        data = yaml.safe_load((Path(ws) / "interface.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return gates
    if boundary.interface_version(data or {}) == boundary.TYPED_VERSION:
        return gates
    return [g for g in gates if g not in ("boundary", "top_erc")]


def _macro_lib_not_applicable(ws: Path) -> str | None:
    """/msde's `macro_lib`, not applicable only when the analog spec declares
    no macro_timing and the macro has no crossing pin (#177): every macro with
    a crossing pin owes the lib view, and top_harden fails macro_lib_missing
    without it, so this cannot hide a macro's timing."""
    import check_macro_lib
    return check_macro_lib.not_applicable_at_release(ws)


def _layout_match_not_applicable(ws: Path) -> str | None:
    """/ade's `layout_match` (#118), not applicable only when spec.yaml
    declares no `matching:` groups: nothing is drawn as a matched row."""
    import check_layout_match
    return check_layout_match.not_applicable_at_release(ws)


# (skill, gate) -> fn(ws) -> reason str when the block's own spec declares
# the gate not applicable, None when the gate is owed. Deliberately a short
# explicit list, never a general carve-out (see applicable_gates).
NOT_APPLICABLE = {("ade", "mc"): _mc_not_applicable,
                  ("ade", "layout_match"): _layout_match_not_applicable,
                  ("vde", "cdc"): _cdc_not_applicable,
                  ("msde", "macro_lib"): _macro_lib_not_applicable,
                  ("msde", "boundary"): _boundary_not_applicable}


def not_applicable_reason(ws: Path, skill: str, gate: str) -> str | None:
    fn = NOT_APPLICABLE.get((skill, gate))
    return fn(ws) if fn else None


def gate_check(ws: Path, skill: str, gate: str, data: dict,
              imap: dict, fresh: dict, waivers: list[dict] | None = None,
              gates_row: dict | None = None) -> dict:
    """`gates_row` is this gate's own gates.yaml row (build() looks it up
    per-skill) - its `tool` names the check that ran, alongside `version`
    (checklib.CHECKER_VERSION, the checker SEMANTICS version every gate in
    one attestation shares, same one a waiver binds against): "the record
    of what ran is ... gate, tool and version" (docs/design.md section 2)."""
    tool = (gates_row or {}).get("tool")
    version = checklib.CHECKER_VERSION
    na = not_applicable_reason(ws, skill, gate)
    if na:
        # checks.schema.json keeps `applies: true` for every entry - the
        # gate is still owed an answer, and this is the answer. ran=False
        # plus the reason and the spec_yaml hash it was read from say it
        # was declared, not run; verify() rebuilds and compares this entry,
        # so a later spec edit turning the gate on invalidates checks.json.
        _rel, spec_sha = statelib.hash_kind(ws, "spec_yaml", imap,
                                            data.get("artifacts"))
        return {"gate": gate, "applies": True, "ran": False, "ok": True,
                "tool": tool, "version": version, "status": None,
                "inputs": {"spec_yaml": spec_sha}, "reason": na}
    entry = (data.get("gates") or {}).get(gate)
    if entry is None or not entry.get("status"):
        return {"gate": gate, "applies": True, "ran": False,
                "tool": tool, "version": version,
                "reason": "no recorded result"}
    verdict = fresh["gates"].get(gate, {})
    ok = entry.get("status") == "pass" and verdict.get("fresh")
    reason = None
    if entry.get("status") != "pass":
        reason = "last recorded result is FAIL"
    elif not verdict.get("fresh"):
        if verdict.get("hash_valid") is False:
            reason = ("stale: input changed since the last pass ("
                      + ", ".join(verdict.get("changed_inputs") or []) + ")")
            if verdict.get("strength_stale"):
                reason += "; " + verdict["strength_stale"]
        elif verdict.get("hash_valid") is None:
            reason = "freshness unknown (no recorded input hashes)"
        elif verdict.get("strength_stale"):
            reason = "stale: " + verdict["strength_stale"]
        elif verdict.get("upstream_stale"):
            reason = "stale: " + "; ".join(verdict["upstream_stale"])
        elif any(m.get("edit_class") == "gate_error"
                 for m in verdict.get("stale_marks") or []):
            reason = "stale: a later run of this gate ended in error"
        else:
            reason = "stale: marked by a later edit"
    result = {"gate": gate, "applies": True, "ran": True, "ok": bool(ok),
             "tool": tool, "version": version,
             "status": entry.get("status"),
             "attempts": entry.get("attempts"),
             "ts": (entry.get("last") or {}).get("ts"),
             "inputs": (entry.get("last") or {}).get("inputs"),
             "reason": reason}
    if ok:
        return result
    # A waiver only ever COVERS a gate that is not itself ok - one on an
    # already-passing gate is inert, never surfaced as a problem (a stale
    # leftover approval for a gate that has since genuinely passed on its
    # own is not this release's concern).
    for w in waivers or []:
        if w.get("gate") != gate:
            continue
        problems = waiver_problems(w, gate, entry, verdict)
        if problems:
            result["waiver_problems"] = problems
            continue
        result["ok"] = True
        result["waived"] = True
        result["waiver"] = {"reason": w["reason"], "approved_by": w["approved_by"]}
        result.pop("waiver_problems", None)
        return result
    return result


def build(ws: Path, max_report_age_h: float = 24.0) -> tuple[dict | None, list[str]]:
    state_path = ws / "state.json"
    data = checklib.load_json(state_path, "state.json")
    skill = data.get("skill")
    if not skill:
        return None, ["state.json has no 'skill'"]
    imap = statelib.load_map()
    fresh = statelib.freshness_report(data, ws, imap)
    skill_rows = gate_mod.load_gates(gate_mod.DEFAULT_GATES).get(skill) or {}
    try:
        waivers = load_waivers(ws)
    except ValueError as exc:
        return None, [str(exc)]
    checks = [gate_check(ws, skill, g, data, imap, fresh, waivers,
                         skill_rows.get(g))
             for g in owed_gates(ws, skill, imap)]
    problems = []
    for c in checks:
        if c.get("ok"):
            continue
        problems.append(f"{c['gate']}: {c['reason']}")
        problems.extend(c.get("waiver_problems") or [])
    # "escalated" (M5, found running the fix loop for real) is NOT
    # resolved - it is the fix loop's own outcome for a finding a human
    # has to decide on, and release refusing to notice one would be
    # exactly the silent-failure class docs/design.md section 2 exists to
    # answer. Only "fixed"/"waived" are genuinely closed - and a "waived"
    # issue only counts as closed when state.py's own --note/--approved-by
    # requirement is actually satisfied on the record (M5, found the hard
    # way: `issue --status waived` used to close one with neither):
    # trusting the status LABEL alone here would let a hand-edited
    # state.json, or a waiver written before that requirement landed,
    # close an issue with no recorded reason or approver, which is exactly
    # the kind of gap release exists to refuse.
    # A "superseded" issue is closed only while the issue that replaced
    # it is a fixed one of the same gate (statelib.issue_unresolved).
    issues = data.get("open_issues", [])
    open_issues = [i for i in issues
                   if statelib.issue_unresolved(i, issues)
                   or (i.get("status") == "waived"
                       and not (i.get("note") and i.get("approved_by")))]
    if open_issues:
        problems.append(f"{len(open_issues)} open issue(s) unresolved")
    try:
        scoped_out = corners_mod.recorded_scope_outs(data)
    except checklib.CheckError as exc:
        problems.append(str(exc))
    if problems:
        return None, problems
    # the PDK it was signed off on (#138); `root` is the machine's path,
    # not part of the record
    pdk = {k: v for k, v in statelib.pdk_version().items() if k != "root"}
    body = {"skill": skill, "block": data.get("block"), "ts": _now(),
           "pdk": pdk, "checks": checks}
    if scoped_out:
        body["scoped_out"] = scoped_out
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    body["attestation_sha256"] = digest
    return body, []


def write_attestation(ws: Path, attestation: dict) -> Path:
    path = ws / CHECKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(attestation, indent=1), encoding="utf-8")
    return path


def sealed_problem(ws: Path) -> str | None:
    """A refusal when reports/checks.json holds release's manifest seal.

    check_release.py adds tt_submission.manifest_sha256 after build(), so a
    record the CLI rebuilt would have no seal and `release --verify` would
    refuse the tile. release is the only writer that can seal: the manifest
    it seals is made there from the staged tree."""
    path = ws / CHECKS_PATH
    if not path.is_file():
        return None
    recorded = checklib.load_json(path, "checks.json")
    if not (recorded.get("tt_submission") or {}).get("manifest_sha256"):
        return None
    return (f"sealed_record: {CHECKS_PATH} holds the manifest seal `release` "
            "wrote and a rebuild here would drop it - run the `release` gate "
            "for a new record, `attest.py verify` to check this one")


def nested_problems(ws: Path) -> list[str]:
    """Why an msde block's nested runs do not count as released, one
    string per side ("nested_<side>: <reason>"); empty when both do."""
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
        verdict = verify(sub)
        if not verdict.get("valid"):
            problems.append(f"nested_{side}: not released: "
                            f"{verdict.get('reason')}")
    return problems


def changed_checks(recorded: list[dict], current: list[dict]) -> list[str]:
    """The gates whose check entry differs between the release record and
    the gate results now: re-run, re-recorded, added or dropped."""
    was = {c.get("gate"): c for c in recorded}
    now = {c.get("gate"): c for c in current}
    order = [c.get("gate") for c in current] + [
        g for g in was if g not in now]
    return [str(g) for g in order if was.get(g) != now.get(g)]


# Committed blocks that verify_tree does not hold to a valid record, each
# with the reason. blocks/pll's tile is stale while #212 (its tb releases
# rst_n with clk stopped) is open: its gates are not re-run until that is
# settled, and its staleness is tracked in thread pll-paper-update. Every
# other block must attest valid from a clean checkout (#248).
KNOWN_STALE = {"pll": "#212: PLL tile left stale until its reset tb is "
                      "settled (see pll-paper-update)"}


def verify_tree(repo: Path, rev: str = "HEAD") -> dict:
    """Verify every block committed at `rev` as a clean checkout sees it.

    `git archive` gives the tracked files only (no runs/, no release/), the
    same tree a clone has, so a block whose record holds only because of
    files left on this machine fails here as it would in CI. A gate re-run
    and committed without a new `release` fails, naming the gate (#248)."""
    import subprocess
    import tarfile
    import tempfile
    repo = Path(repo)
    blocks: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="attest-tree-") as tmp:
        tar = Path(tmp) / "blocks.tar"
        with open(tar, "wb") as fh:
            subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                            rev, "--", "blocks"], stdout=fh, check=True)
        with tarfile.open(tar) as tf:
            tf.extractall(tmp, filter="data")
        root = Path(tmp) / "blocks"
        for st in sorted(root.glob("*/state.json")):
            name = st.parent.name
            if name in KNOWN_STALE:
                blocks[name] = {"valid": None, "known_stale": KNOWN_STALE[name]}
                continue
            v = verify(st.parent)
            row = {"valid": bool(v.get("valid"))}
            if not row["valid"]:
                row["reason"] = v.get("reason")
                row["stale_gates"] = v.get("stale_gates", [])
            blocks[name] = row
    invalid = sorted(n for n, r in blocks.items() if r["valid"] is False)
    return {"rev": rev, "blocks": blocks, "invalid": invalid}


def verify(ws: Path) -> dict:
    path = ws / CHECKS_PATH
    if not path.is_file():
        return {"valid": False, "reason": f"no {CHECKS_PATH}"}
    recorded = checklib.load_json(path, "checks.json")
    body = {k: v for k, v in recorded.items() if k != "attestation_sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    if digest != recorded.get("attestation_sha256"):
        return {"valid": False, "reason": "attestation_sha256 mismatch - "
                "the file was edited after it was written"}
    att, problems = build(ws)
    if att is None:
        return {"valid": False, "reason": "; ".join(problems),
                "attestation_sha256": recorded["attestation_sha256"]}
    # an msde block is released only with both its sides: its own gates can
    # all be green while a side fails, and "valid" here reads as "Released"
    # in the design doc
    if att["skill"] == "msde":
        nested = nested_problems(ws)
        if nested:
            return {"valid": False, "reason": "; ".join(nested),
                    "attestation_sha256": recorded["attestation_sha256"]}
    if att.get("pdk") != recorded.get("pdk"):
        return {"valid": False,
                "reason": f"signed off on PDK {recorded.get('pdk')}, the "
                          f"toolchain carries {att.get('pdk')} - re-run the "
                          "PDK gates and the `release` gate",
                "attestation_sha256": recorded["attestation_sha256"]}
    if att.get("scoped_out") != recorded.get("scoped_out"):
        return {"valid": False,
                "reason": "the scope-outs recorded at H1 changed since the "
                          "attestation - re-run the `release` gate",
                "attestation_sha256": recorded["attestation_sha256"]}
    if att["checks"] != recorded["checks"]:
        return {"valid": False,
                "reason": "recorded checks no longer match the current "
                          "gate results (changed since the release record: "
                          f"{', '.join(changed_checks(recorded['checks'], att['checks']))})"
                          " - re-run the `release` gate",
                "stale_gates": changed_checks(recorded["checks"], att["checks"]),
                "attestation_sha256": recorded["attestation_sha256"]}
    return {"valid": True, "attestation_sha256": recorded["attestation_sha256"]}


def disposition(ws: Path) -> dict:
    """The derived release disposition. Never hand-set: draft (nothing has
    passed), in-progress (some applicable gates green), gates-green (every
    applicable gate fresh-pass), blocked (a gate's last recorded result is
    FAIL).

    An msde block counts its sides too: a recorded FAIL in one blocks the
    top, named "<side>/<gate>", and a side that does not verify as released
    holds a green top at in-progress with the reason under `nested`."""
    state_path = ws / "state.json"
    if not state_path.is_file():
        return {"disposition": "draft", "reason": "no state.json"}
    data = checklib.load_json(state_path, "state.json")
    skill = data.get("skill")
    if not skill:
        return {"disposition": "draft", "reason": "state.json has no skill"}
    imap = statelib.load_map()
    fresh = statelib.freshness_report(data, ws, imap)
    checks = [gate_check(ws, skill, g, data, imap, fresh)
             for g in owed_gates(ws, skill, imap)]
    failing = [c["gate"] for c in checks if c.get("status") == "fail"]
    if skill == "msde":
        for side in NESTED:
            if (ws / side / "state.json").is_file():
                failing += [f"{side}/{g}" for g in
                            disposition(ws / side).get("failing", [])]
    if failing:
        return {"disposition": "blocked", "failing": failing}
    if all(c.get("ok") for c in checks):
        nested = nested_problems(ws) if skill == "msde" else []
        if not nested:
            return {"disposition": "gates-green",
                    "gates": [c["gate"] for c in checks]}
        return {"disposition": "in-progress",
                "gates_passed": [c["gate"] for c in checks], "nested": nested}
    if any(c.get("ran") for c in checks):
        return {"disposition": "in-progress",
                "gates_passed": [c["gate"] for c in checks if c.get("ok")]}
    return {"disposition": "draft", "reason": "no gate has run yet"}


def run(argv=None) -> tuple[dict, str | None, int]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--workspace", required=True,
                       help="block workspace (state.json inside)")
        p.add_argument("--out", help="write result JSON here too")

    p = sub.add_parser("build", help="assemble + write reports/checks.json")
    common(p)
    p.add_argument("--max-report-age-h", type=float, default=24.0)

    common(sub.add_parser("verify", help="re-verify checks.json (read-only)"))
    p = sub.add_parser("verify-tree", help="verify every committed block "
                       "from a clean checkout of REV (read-only)")
    p.add_argument("--repo", default=".", help="git repository (default .)")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--out", help="write result JSON here too")
    common(sub.add_parser("disposition",
                          help="derived release disposition (read-only)"))

    args = ap.parse_args(argv)
    if args.cmd == "verify-tree":
        t = verify_tree(Path(args.repo), args.rev)
        problems = [f"{n}: not released: {t['blocks'][n]['reason']}"
                    for n in t["invalid"]]
        return ({"script": SCRIPT, "cmd": args.cmd,
                 "status": "violations" if problems else "pass",
                 "problems": problems, **t},
                args.out, 1 if problems else 0)
    ws = Path(args.workspace)
    result: dict = {"script": SCRIPT, "cmd": args.cmd,
                    "workspace": str(ws).replace("\\", "/")}

    if args.cmd == "build":
        att, problems = build(ws, max_report_age_h=args.max_report_age_h)
        if att is not None and att["skill"] == "msde":
            problems = nested_problems(ws)
            if problems:
                att = None
        if att is not None:
            sealed = sealed_problem(ws)
            if sealed:
                att, problems = None, [sealed]
        if att is None:
            result.update(status="violations", problems=problems,
                          attestation=None)
            return result, args.out, 1
        path = write_attestation(ws, att)
        result.update(status="pass", attestation=str(path).replace("\\", "/"),
                      attestation_sha256=att["attestation_sha256"],
                      disposition=disposition(ws))
        return result, args.out, 0

    if args.cmd == "verify":
        v = verify(ws)
        result.update(status="pass" if v["valid"] else "violations", **v)
        return result, args.out, 0 if v["valid"] else 1

    result.update(status="pass", **disposition(ws))
    return result, args.out, 0


def main(argv=None) -> int:
    checklib.utf8_stdout()
    try:
        payload, out, code = run(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001  (any error -> exit 2)
        print(json.dumps({"script": SCRIPT, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}",
                          "remediation": str(exc)}))
        return 2
    text = json.dumps(payload, indent=1)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

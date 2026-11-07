#!/usr/bin/env python
"""precheck_drift.py - is the vendored precheck still the shuttle's? (#133)

    precheck_drift.py [--out FILE] [--summary FILE] [--klayout VERSION]
                      [--fixture FILE]

The precheck gate runs the tt-support-tools commit VENDORED.md pins. The
shuttle's precheck (tt-gds-action, precheck/action.yml, at the tag ttship
ships with) checks out `tools-ref`, which defaults to main. So a gf180 rule
changed upstream passes here and fails at the shuttle. This compares the two
through GitHub's API, read-only, and writes nothing but its report:

- `precheck_drift_gf180` (finding, exit 1): a path that can change what
  precheck decides for gf180 changed between the pin and main (`classify`).
  It goes by path, not by reading the change: a person reads the diff when
  bumping the pin (VENDORED.md, "Bumping the pin"), and that bump is what
  clears it. The message names the files and the commits that touched them.
- `precheck_drift_diverged` (finding): the pin is not an ancestor of main, so
  the compare doesn't show everything between them.
- `precheck_klayout_out_of_range` (finding): the image's klayout, the one the
  gate runs precheck on, is outside the klayout requirement of the vendored
  or the upstream precheck/requirements.txt.
- `precheck_vendored_modified` (finding): a vendored file isn't the pin's
  blob, so the pin doesn't say what is vendored and this compare proves
  nothing.
- `precheck_drift_other_pdk` (warning, exit 0): only another PDK's tech data
  changed. It sits in the report's `warnings`, never in `violations`.

Anything else that moved (the ROM, docs, tt_tool) is listed under `outside`.
A compare that can't run or is cut short is a refusal, exit 2, never a pass.

--fixture FILE reads the API's answers from FILE instead of the network, so
a planted upstream diff goes through this same code (`Fixture`): the CI job
proves on one that it fails before it trusts a pass
(tests/fixtures/precheck_drift/plant.json).

This needs Python's standard library and gh, no EDA tool: the CI job
(.github/workflows/precheck-drift.yml) runs it with the runner's python3 and
no toolchain. So it can't import ttship, whose imports need the image; a test
(tests/test_precheck_drift.py) keeps ACTION_REF and the pin's reading equal to
ttship's, and IMAGE_KLAYOUT equal to what the image runs.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402

TT_DIR = ENGINE / "reference" / "tt"
VENDORED_MD = TT_DIR / "VENDORED.md"
VENDORED_TOOLS = TT_DIR / "tt-support-tools"
TOOLS_REPO = "TinyTapeout/tt-support-tools"
ACTION_REPO = "TinyTapeout/tt-gds-action"
ACTION_FILE = "precheck/action.yml"
# ttship.DEFAULT_REF: the tag chip-flow's tiles ship with.
ACTION_REF = "ttgf26c"
# `bin/eda klayout -b -v` on the pinned image (.github/actions/toolchain).
IMAGE_KLAYOUT = "0.30.12"
REQUIREMENTS = "precheck/requirements.txt"
# GitHub's compare answer stops at this many files, whatever the diff holds.
COMPARE_FILE_LIMIT = 300
SOURCE = "gh api compare"
BUMP_DOC = "engine/reference/tt/VENDORED.md, \"Bumping the pin\""
GF180 = "gf180"
OTHER_PDK = "other_pdk"
OUTSIDE = "outside"
# Under precheck/, what can't change a result. Anything else there counts as
# gf180-relevant, a file nobody has seen before included.
PRECHECK_INERT = ("precheck/README.md", "precheck/.gitignore",
                  "precheck/reports/*")
OTHER_PDK_NAMES = ("sky130", "sg13", "ihp", "fpga", "ice40")


def classify(path: str) -> str:
    """Which side of the rule a changed upstream path falls on. Fails towards
    gf180: only a path that names another PDK is merely another PDK's."""
    parts = path.split("/")
    if path == "tech.py":
        return GF180
    if parts[0] == "tech":
        if len(parts) > 2 and not parts[1].startswith("gf180"):
            return OTHER_PDK
        return GF180
    if parts[0] == "precheck":
        if any(fnmatch.fnmatch(path, pat) for pat in PRECHECK_INERT):
            return OUTSIDE
        if parts[1:2] == ["tech-files"] and any(
                name in parts[-1].lower() for name in OTHER_PDK_NAMES):
            return OTHER_PDK
        return GF180
    return OUTSIDE


def classify_change(change: dict) -> str:
    """A rename counts by whichever end of it matters more."""
    sides = {classify(change["filename"])}
    if change.get("previous_filename"):
        sides.add(classify(change["previous_filename"]))
    for side in (GF180, OTHER_PDK):
        if side in sides:
            return side
    return OUTSIDE


def vendored_pin(vendored_md: Path = VENDORED_MD) -> str:
    text = vendored_md.read_text(encoding="utf-8")
    m = re.search(r"tt-support-tools/`.*?@ `([0-9a-f]{40})`", text, re.S)
    if not m:
        raise checklib.CheckError(
            f"no tt-support-tools commit in {vendored_md.name}: it must keep "
            "the form `tt-support-tools/` ... @ `<40 hex>`")
    return m.group(1)


def action_tools_ref(action_yml: str) -> str:
    """The default of the action's `tools-ref` input."""
    m = re.search(r"^\s*tools-ref:\s*\n(?:\s+(?!default:)\S.*\n)*"
                  r"\s+default:\s*['\"]?([^'\"\s]+)", action_yml, re.M)
    if not m:
        raise checklib.CheckError(
            f"no tools-ref default in {ACTION_REPO}@{ACTION_REF} "
            f"{ACTION_FILE}: the action changed shape, so the ref the shuttle "
            "uses is not known; read it and update action_tools_ref")
    return m.group(1)


def _version(text: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(p) for p in text.split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def _satisfies(version: tuple[int, ...], op: str, want: str) -> bool | None:
    """None when this reader can't tell, which a caller reports, not passes."""
    if op in ("==", "!=") and want.endswith(".*"):
        prefix = _version(want[:-2])
        if prefix is None:
            return None
        same = version[:len(prefix)] == prefix
        return same if op == "==" else not same
    target = _version(want)
    if target is None:
        return None
    c = _cmp(version, target)
    return {"==": c == 0, "!=": c != 0, ">=": c >= 0, "<=": c <= 0,
            ">": c > 0, "<": c < 0}.get(op)


def klayout_problem(requirements: str, klayout: str) -> str | None:
    """Why klayout `klayout` doesn't meet this requirements.txt, or None."""
    spec = None
    for line in requirements.splitlines():
        m = re.fullmatch(r"\s*klayout\s*([^#\s].*?)?\s*(#.*)?", line)
        if m:
            spec = m.group(1) or ""
            break
    if spec is None:
        return "it has no klayout requirement"
    version = _version(klayout)
    if version is None:
        return f"{klayout!r} is not a version this can compare"
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        m = re.fullmatch(r"(==|!=|>=|<=|>|<)\s*(\S+)", clause)
        ok = _satisfies(version, m.group(1), m.group(2)) if m else None
        if ok is None:
            return f"it requires klayout {spec}, which this can't evaluate"
        if not ok:
            return f"it requires klayout {spec}"
    return None


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHub:
    """GitHub's REST API through gh, which finds its own token."""

    def _get(self, path: str):
        cmd = ["gh", "api", path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise checklib.CheckError(
                f"gh api {path} did not run ({exc}): this needs gh and the "
                "network; a compare that did not run is no pass") from exc
        if res.returncode != 0:
            raise checklib.CheckError(
                f"gh api {path} failed: {res.stderr.strip()[:300]}. A compare "
                "that did not run is no pass; if the pin is gone upstream, "
                "see " + BUMP_DOC)
        return json.loads(res.stdout)

    def compare(self, pin: str, ref: str) -> dict:
        return self._get(f"repos/{TOOLS_REPO}/compare/{pin}...{ref}")

    def commit_files(self, sha: str) -> list[str]:
        files = self._get(f"repos/{TOOLS_REPO}/commits/{sha}")["files"]
        return [p for f in files
                for p in (f["filename"], f.get("previous_filename")) if p]

    def _file(self, repo: str, ref: str, path: str) -> str:
        doc = self._get(f"repos/{repo}/contents/{path}?ref={ref}")
        return base64.b64decode(doc["content"]).decode("utf-8")

    def action_yml(self) -> str:
        return self._file(ACTION_REPO, ACTION_REF, ACTION_FILE)

    def requirements(self, ref: str) -> str:
        return self._file(TOOLS_REPO, ref, REQUIREMENTS)

    def tree(self, pin: str) -> dict[str, str] | None:
        doc = self._get(f"repos/{TOOLS_REPO}/git/trees/{pin}?recursive=1")
        if doc.get("truncated"):
            raise checklib.CheckError(
                f"GitHub truncated the tree of {pin}, so the vendored files "
                "can't all be checked against it")
        return {e["path"]: e["sha"] for e in doc["tree"]
                if e["type"] == "blob"}


class Fixture:
    """The same answers from a file: {action_yml, compare, commit_files:
    {sha: [path]}, requirements, tree}. `tree: null` leaves the vendored files
    unchecked, which only a fixture can do."""

    def __init__(self, doc: dict):
        self.doc = doc

    def compare(self, pin: str, ref: str) -> dict:
        return self.doc["compare"]

    def commit_files(self, sha: str) -> list[str]:
        return self.doc["commit_files"][sha]

    def action_yml(self) -> str:
        return self.doc["action_yml"]

    def requirements(self, ref: str) -> str:
        return self.doc["requirements"]

    def tree(self, pin: str) -> dict[str, str] | None:
        return self.doc.get("tree")


def _commit_fact(commit: dict) -> dict:
    return {"sha": commit["sha"],
            "date": commit["commit"]["author"]["date"],
            "title": commit["commit"]["message"].splitlines()[0]}


def _named(sha: str, by_sha: dict[str, dict]) -> str:
    return f"{sha[:8]} {by_sha[sha]['title']}"


def analyse(api, pin: str, vendored: Path, klayout: str) -> dict:
    """The report. Everything it reads from upstream comes through `api`."""
    ref = action_tools_ref(api.action_yml())
    cmp_ = api.compare(pin, ref)
    files = cmp_.get("files", [])
    commits = [_commit_fact(c) for c in cmp_.get("commits", [])]
    if (len(files) >= COMPARE_FILE_LIMIT
            or len(commits) < cmp_.get("total_commits", len(commits))):
        raise checklib.CheckError(
            f"the compare of {pin[:8]}...{ref} is cut short ({len(files)} "
            f"files, {len(commits)} of {cmp_.get('total_commits')} commits), "
            "so a gf180 change could be hidden past the cut: bump the pin in "
            "steps (" + BUMP_DOC + ")")
    by_sha = {c["sha"]: c for c in commits}
    sides = {change["filename"]: classify_change(change) for change in files}

    touched_by: dict[str, list[str]] = {}
    if any(side != OUTSIDE for side in sides.values()):
        for sha in by_sha:
            for path in api.commit_files(sha):
                touched_by.setdefault(path, []).append(sha)

    def fact(change: dict) -> dict:
        path = change["filename"]
        return {"path": path, "status": change.get("status"),
                "additions": change.get("additions"),
                "deletions": change.get("deletions"),
                "commits": touched_by.get(path, [])}

    listed = {side: [fact(c) for c in files
                     if sides[c["filename"]] == side]
              for side in (GF180, OTHER_PDK, OUTSIDE)}

    violations = []
    warnings = []

    def describe(side: str) -> str:
        lines = []
        for f in listed[side]:
            who = "; ".join(_named(s, by_sha) for s in f["commits"])
            lines.append(f"{f['path']} ({f['status']}; {who or 'net diff'})")
        return ", ".join(lines)

    if listed[GF180]:
        refs = sorted({s for f in listed[GF180] for s in f["commits"]})
        violations.append(checklib.violation(
            "precheck_drift", "error", listed[GF180][0]["path"], None,
            "precheck_drift_gf180", refs,
            f"tt-support-tools {ref} moved past the pin {pin[:8]} on paths "
            f"that can change gf180's precheck: {describe(GF180)}. The "
            f"shuttle runs {ref}, the gate runs the pin. Read the diff and "
            f"bump the pin ({BUMP_DOC}).", SOURCE,
            files=[f["path"] for f in listed[GF180]]))
    if listed[OTHER_PDK]:
        warnings.append({
            "kind": "precheck_drift_other_pdk",
            "msg": f"tt-support-tools {ref} changed only another PDK's data "
                   f"on these paths: {describe(OTHER_PDK)}",
            "files": [f["path"] for f in listed[OTHER_PDK]]})
    if cmp_.get("status") not in ("ahead", "identical"):
        violations.append(checklib.violation(
            "precheck_drift", "error", "engine/reference/tt/VENDORED.md",
            None, "precheck_drift_diverged", [],
            f"the pin {pin[:8]} is not an ancestor of {ref} (compare status "
            f"{cmp_.get('status')}, ahead {cmp_.get('ahead_by')}, behind "
            f"{cmp_.get('behind_by')}): upstream rewrote history or the pin "
            f"was never on {ref}. Move the pin onto {ref} ({BUMP_DOC}).",
            SOURCE))

    vendored_req = vendored / REQUIREMENTS
    for where, text in (("vendored", vendored_req.read_text(encoding="utf-8")),
                        (f"upstream {ref}", api.requirements(ref))):
        why = klayout_problem(text, klayout)
        if why:
            violations.append(checklib.violation(
                "precheck_drift", "error", REQUIREMENTS, None,
                "precheck_klayout_out_of_range", [],
                f"the image's klayout is {klayout}, and {where} {REQUIREMENTS}"
                f" doesn't allow it: {why}. The gate would run precheck on a "
                "klayout precheck doesn't accept.", SOURCE))

    tree = api.tree(pin)
    if tree is not None:
        for path in sorted(p for p in vendored.rglob("*")
                           if p.is_file() and "__pycache__" not in p.parts):
            rel = path.relative_to(vendored).as_posix()
            if tree.get(rel) != git_blob_sha(path):
                how = "differs from" if rel in tree else "is not in"
                violations.append(checklib.violation(
                    "precheck_drift", "error", rel, None,
                    "precheck_vendored_modified", [],
                    f"vendored {rel} {how} the pin {pin[:8]}: VENDORED.md says "
                    f"the file is that commit's and it isn't ({BUMP_DOC}).",
                    SOURCE))

    return checklib.report(
        "precheck_drift", None, violations, pin=pin, upstream_ref=ref,
        head=commits[-1]["sha"] if commits else pin,
        compare_status=cmp_.get("status"), ahead_by=cmp_.get("ahead_by"),
        behind_by=cmp_.get("behind_by"), commits=commits,
        gf180_relevant=listed[GF180], other_pdk=listed[OTHER_PDK],
        outside=listed[OUTSIDE], klayout=klayout, warnings=warnings,
        vendored_checked=tree is not None)


def summary_md(payload: dict) -> str:
    """The report for a person: the job's summary, the issue a red run files."""
    pin, ref = payload["pin"], payload["upstream_ref"]
    head = f"**precheck drift: {payload['status']}.** Pin `{pin[:8]}`, "
    head += (f"`{TOOLS_REPO}` {ref} is {payload['ahead_by']} ahead, "
             f"{payload['behind_by']} behind ({payload['compare_status']}).")
    lines = [head, ""]
    if payload["violations"]:
        lines += ["## Findings", ""]
        lines += [f"- `{v['kind']}`: {v['msg']}" for v in payload["violations"]]
        lines.append("")
    for w in payload["warnings"]:
        lines += [f"Warning `{w['kind']}`: {w['msg']}", ""]
    for title, key in (("gf180-relevant", "gf180_relevant"),
                       ("Other PDKs", "other_pdk"), ("Outside", "outside")):
        if payload[key]:
            lines += [f"## {title}", ""]
            lines += [f"- `{f['path']}` ({f['status']})" for f in payload[key]]
            lines.append("")
    if payload["commits"]:
        lines += ["## Commits", ""]
        lines += [f"- `{c['sha'][:8]}` {c['date'][:10]} {c['title']}"
                  for c in payload["commits"]]
        lines.append("")
    return "\n".join(lines)


def run(argv=None) -> tuple[dict, str | None]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the JSON report here, not stdout")
    ap.add_argument("--summary", help="append a markdown report to this file")
    ap.add_argument("--klayout", default=IMAGE_KLAYOUT,
                    help="the klayout version to hold against the requirements")
    ap.add_argument("--fixture",
                    help="read upstream's answers from this JSON, not GitHub")
    args = ap.parse_args(argv)
    api = (Fixture(checklib.load_json(args.fixture, "fixture"))
           if args.fixture else GitHub())
    payload = analyse(api, vendored_pin(), VENDORED_TOOLS, args.klayout)
    for w in payload["warnings"]:
        print(f"WARNING {w['kind']}: {w['msg']}", file=sys.stderr)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary_md(payload) + "\n")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap("precheck_drift", lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())

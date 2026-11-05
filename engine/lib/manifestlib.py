"""manifestlib.py - nothing submitted is untied from the gates that signed it
(issue #132).

check_release writes `release/manifest.json` for a hardened tile (vde or msde):
every deliverable's path and sha256, the gate records that read or produced
it, the tool, PDK and precheck versions, and the waivers. `chip-flow release
--verify DIR` (engine/scripts/release.py) re-hashes a submission tree against
it, byte for byte.

A tie is read off invalidation.yaml: a gate reads a file when one of its input
kinds covers the file's path, so its recorded input hash holds that file. Files
a harden run writes that no later gate hashes (an msde LEF, SDF, SPEF) tie to
the harden gate as `produced`, which is the weaker claim and says so.

A file in the tree with no tie refuses the release (`deliverable_untied`),
except test/ (not silicon, listed as such). A hand-kept copy under a block's
tt/ ties only when it says the same as the file the gates read: same code once
comments and whitespace go for Verilog, same top_module/tiles/analog_pins for
info.yaml. A project.v may instead say the same as the hardened wrapper with
the TT analog template's VGND/VDPWR ports and the macros' power ties
(ttship.powered_project), which holds only while top/spec/spec.yaml still
regenerates that wrapper byte for byte (#165). Otherwise it needs a waiver bound to its bytes in
reports/waivers.json: {deliverable, sha256, reason, approved_by}.

A refused release still writes its manifest, with `released: false` and the
reasons, so the tree can be compared; --verify never passes one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import yaml

import checklib
import statelib
import ttship

PATH = "release/manifest.json"
SCHEMA = 1
# Where a file nobody signed could hide: --verify refuses one it does not list.
SHIPPED_DIRS = ("gds", "lef", "gl", "src", ".github/workflows")
ARCHIVE_VIEWS = ("pnl", "sdf", "spef")
HARDEN_GATE = {"vde": "harden", "msde": "top_harden"}
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
INFO_FIELDS = ("top_module", "tiles", "analog_pins")


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _code(path: Path) -> str:
    return _code_of(path.read_text(encoding="utf-8", errors="replace"))


def _code_of(text: str) -> str:
    return " ".join(_COMMENT.sub(" ", text).split())


def _gate_ties(ws: Path, skill: str, source: str, data: dict, imap: dict,
               fresh: dict) -> list[dict]:
    """The gate records that hold `source`, one per gate."""
    ties: dict[str, dict] = {}

    def add(gate: str, relation: str, kind: str | None) -> None:
        entry = (data.get("gates") or {}).get(gate) or {}
        inputs = (entry.get("last") or {}).get("inputs")
        ties.setdefault(gate, {
            "gate": gate, "relation": relation, "kind": kind,
            "verdict": entry.get("status"),
            "fresh": bool((fresh["gates"].get(gate) or {}).get("fresh")),
            "ts": (entry.get("last") or {}).get("ts"),
            "inputs_sha256": _digest(inputs) if inputs else None})

    for gate, kinds in (imap["gate_inputs"].get(skill) or {}).items():
        if gate == "release":   # release is what decides, not a signature
            continue
        for kind in kinds:
            path = statelib.kind_path(kind, imap, data.get("artifacts"))
            if not statelib.is_pdk_path(path) and (
                    source == path or source.startswith(path + "/")):
                add(gate, "read", kind)
                break
    harden = ttship.HARDEN_DIR.get(skill)
    if harden and source.startswith(harden + "/"):
        add(HARDEN_GATE[skill], "produced", None)
    return sorted(ties.values(), key=lambda t: t["gate"])


def _sha_differs(ws: Path, src: str, path: Path) -> bool:
    ref = ws / src
    return ref.is_file() and path.is_file() and ttship._sha256(ref) != ttship._sha256(path)


def _stamp_matches(ws: Path, source: str, path: Path) -> bool:
    try:
        want = ttship.stamped_info(ws, (ws / source).read_text(encoding="utf-8"))
    except ttship.ShipError:
        return False
    return path.read_text(encoding="utf-8") == want


def _signed_counterpart(ws: Path, source: str, top: str) -> tuple[str, list[str]] | None:
    """For a hand-kept msde file under tt/: the file the gates read that it
    must agree with, and what differs (empty when it agrees)."""
    p = ws / source
    if source == "tt/info.yaml":
        ref = "top/harden/info.yaml"
        if not (ws / ref).is_file():
            return None
        ours = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("project") or {}
        theirs = (yaml.safe_load((ws / ref).read_text(encoding="utf-8")) or {}).get("project") or {}
        return ref, [f for f in INFO_FIELDS if f in theirs
                     and str(ours.get(f)) != str(theirs[f])]
    if source.startswith("tt/src/"):
        name = p.name
        ref = (f"top/harden/{top}.v" if name == "project.v"
               else f"top/macros/{name}")
        if not (ws / ref).is_file():
            return None
        if _code(p) == _code(ws / ref):
            return ref, []
        if name == "project.v" and _code(p) == _code_of(ttship.powered_project(ws, top) or ""):
            return ref, []
        return ref, ["code"]
    return None


def _deliverable_waiver(waivers: list[dict], path: str, sha: str) -> dict | None:
    for w in waivers:
        if (w.get("deliverable") == path and w.get("sha256") == sha
                and w.get("reason") and w.get("approved_by")):
            return {"deliverable": path, "sha256": sha, "reason": w["reason"],
                    "approved_by": w["approved_by"]}
    return None


def deliverables(ws: Path, skill: str, repo: Path, record: dict,
                 waivers: list[dict]) -> tuple[list[dict], list[str]]:
    """(manifest entries, refusals) for a staged tile. `record` is
    ttship.release_check's; its `sources` maps tree path -> workspace path."""
    ws, repo = Path(ws), Path(repo)
    data = checklib.load_json(ws / "state.json", "state.json")
    imap = statelib.load_map()
    fresh = statelib.freshness_report(data, ws, imap)
    top = (record.get("signoff") or {}).get("top_module") or ""
    sources: dict = record.get("sources") or {}
    entries: list[dict] = []
    problems: list[str] = []

    wf_dir = repo / ".github" / "workflows"
    workflows = [p.relative_to(repo).as_posix()
                 for p in sorted(wf_dir.glob("*.y*ml"))] if wf_dir.is_dir() else []
    for rel in sorted(set(sources) | set(workflows)):
        src = sources.get(rel)
        e = {"path": rel, "source": src, "sha256": ttship._sha256(repo / rel)}
        if rel in workflows:
            # every workflow in the tree is read by the ship check
            e["gates"] = [{"gate": "release", "relation": "checked",
                           "check": "ttship.ship_problems"}]
        elif rel.startswith("test/"):
            e["gates"] = []
            e["unsigned"] = "test bench: not silicon, no gate reads it here"
        else:
            e["gates"] = _gate_ties(ws, skill, src, data, imap, fresh) if src else []
            why = "no gate reads it"
            if (skill == "msde" and rel == ttship.ANALOG_NETLIST and e["gates"]
                    and _sha_differs(ws, src, repo / rel)):
                # the powered project.v is derived from the wrapper, not
                # copied, and must still be what that derives
                if _code(repo / rel) == _code_of(ttship.powered_project(ws, top) or ""):
                    e["derived"] = "ttship.powered_project"
                else:
                    e["gates"] = []
                    why = f"differs from {src}, which it was staged from"
            stamp_broken = False
            if rel == "info.yaml" and src and _sha_differs(ws, src, repo / rel):
                # stage writes the author into this copy: it must still be
                # exactly what that writes, whatever else ties the source
                if _stamp_matches(ws, src, repo / rel):
                    e["derived"] = "ttship.stamped_info"
                else:
                    stamp_broken = True
                    e["gates"] = []
                    why = f"differs from {src} by more than the author stage writes"
            if not e["gates"] and src and not stamp_broken:
                ref = _signed_counterpart(ws, src, top)
                if ref:
                    if ref[1]:
                        why = (f"hand-kept {src} differs from {ref[0]}, which the "
                               f"gates read ({', '.join(ref[1])})")
                    else:
                        e["agrees_with"] = ref[0]
                        e["gates"] = _gate_ties(ws, skill, ref[0], data, imap, fresh)
            if not e["gates"]:
                waiver = _deliverable_waiver(waivers, rel, e["sha256"])
                if waiver:
                    e["waiver"] = waiver
                else:
                    e["unsigned"] = why
                    problems.append(f"deliverable_untied: {rel}: {why}")
        entries.append(e)

    # the signoff views the shuttle does not take but the archive keeps
    final = ws / ttship.HARDEN_DIR[skill] / "runs" / ttship.RUN_TAG / "final"
    for view in ARCHIVE_VIEWS:
        for f in sorted((final / view).rglob("*")) if (final / view).is_dir() else []:
            if f.is_file():
                src = f.relative_to(ws).as_posix()
                entries.append({"path": None, "source": src,
                                "sha256": ttship._sha256(f),
                                "gates": _gate_ties(ws, skill, src, data, imap, fresh)})
    return entries, problems


@lru_cache(maxsize=1)
def toolchain_versions() -> dict:
    """The toolchain release runs on: its image and the signoff tools a
    LibreLane run does not name. The attestation holds the gates to this
    toolchain's PDK. None for a tool that does not answer, never a guess."""
    eda = Path(__file__).resolve().parents[2] / "bin" / "eda"

    def ask(args, rx):
        try:
            proc = subprocess.run([str(eda), *args], capture_output=True, text=True,
                                  errors="replace", timeout=30, input="")
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(rx, (proc.stdout or "") + (proc.stderr or ""))
        return m.group(1) if m and proc.returncode == 0 else None

    root = ask(["--print-toolchain-root"], r"(\S+)")
    return {"image": Path(root).name if root else None,
            "klayout": ask(["klayout", "-v"], r"KLayout (\S+)"),
            "magic": ask(["magic", "--version"], r"^\s*(\d\S*)"),
            "netgen": ask(["netgen", "-batch", "quit"], r"Netgen (\S+)")}


def versions(ws: Path, skill: str, record: dict) -> dict:
    """What it was signed off with. None where nothing says, never a guess."""
    import qorlib
    harden = ttship.HARDEN_DIR[skill]
    sign = record.get("signoff") or {}
    out = {"librelane": sign.get("librelane")}
    out.update({k: v for k, v in
                qorlib.run_versions(ws / harden / "runs" / ttship.RUN_TAG).items()
                if v is not None})
    report = ws / "reports" / f"gate-{HARDEN_GATE[skill]}.json"
    if report.is_file():
        try:
            qor = json.loads(report.read_text(encoding="utf-8")).get("qor") or {}
            out.update({k: v for k, v in (qor.get("versions") or {}).items()
                        if v is not None and k not in out})
        except ValueError:
            pass
    pdk = statelib.pdk_version()
    out.update(pdk=sign.get("pdk"),
               open_pdks=sign.get("open_pdks") or pdk.get("open_pdks"),
               ciel=pdk.get("ciel"),
               precheck_tt_support_tools=sign.get("tt_support_tools"),
               checker_version=checklib.CHECKER_VERSION,
               toolchain=toolchain_versions())
    return out


def write(ws: Path, repo: Path, skill: str, entries: list[dict], record: dict,
          att: dict | None, refusals: list[str], waivers: list[dict]) -> dict:
    """Write the manifest beside the workspace's release/ and into the tree."""
    ws, repo = Path(ws), Path(repo)
    try:
        tree = repo.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        tree = None     # a tree elsewhere: not a machine path in the record
    gate_waivers = [{"gate": c["gate"], **c["waiver"]}
                    for c in (att or {}).get("checks") or [] if c.get("waived")]
    body = {
        "schema": SCHEMA,
        "block": checklib.load_json(ws / "state.json", "state.json").get("block"),
        "skill": skill,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "released": att is not None,
        "refused": checklib.workspace_relative(refusals, ws),
        "tree": tree,
        "versions": versions(ws, skill, record) if record else {},
        "shipped": record.get("shipped") or [],
        "waivers": {"gates": gate_waivers,
                    "deliverables": [e["waiver"] for e in entries if e.get("waiver")]},
        "deliverables": entries,
    }
    body["manifest_sha256"] = _digest(body)
    text = json.dumps(body, indent=1) + "\n"
    for dest in (ws / PATH, repo / PATH):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return body


def _extras(root: Path, listed: set[str]) -> list[str]:
    found = []
    for d in SHIPPED_DIRS:
        base = root / d
        if base.is_dir():
            found += [f.relative_to(root).as_posix()
                      for f in sorted(base.rglob("*")) if f.is_file()]
    return [f for f in found if f not in listed]


def verify(root: Path, man: dict, workspace: Path | None = None) -> tuple[list[tuple], dict]:
    """([(kind, file, message)], facts): `root` against manifest `man`."""
    root = Path(root)
    bad: list[tuple] = []
    body = {k: v for k, v in man.items() if k != "manifest_sha256"}
    if _digest(body) != man.get("manifest_sha256"):
        bad.append(("manifest_edited", PATH,
                    "manifest_sha256 does not match: the manifest was edited "
                    "after release wrote it"))
    listed = [e for e in man.get("deliverables") or [] if e.get("path")]
    for e in listed:
        digest = ttship._sha256(root / e["path"])
        if digest is None:
            bad.append(("deliverable_missing", e["path"],
                        f"{e['path']} is in the manifest, not in the tree"))
        elif digest != e["sha256"]:
            bad.append(("deliverable_changed", e["path"],
                        f"{e['path']} sha256 {digest[:12]}, the manifest has "
                        f"{(e['sha256'] or 'none')[:12]}"))
    for f in _extras(root, {e["path"] for e in listed}):
        bad.append(("deliverable_extra", f,
                    f"{f} is in the tree, not in the manifest"))
    files_match = not bad
    if workspace is not None:
        ws = Path(workspace)
        for e in man.get("deliverables") or []:
            if e.get("source") and not e.get("path"):
                digest = ttship._sha256(ws / e["source"])
                if digest != e["sha256"]:
                    bad.append(("deliverable_changed", e["source"],
                                f"{e['source']} no longer matches the manifest"))
    if not man.get("released"):
        bad.append(("manifest_not_released", PATH,
                    "release refused this manifest: "
                    + "; ".join(man.get("refused") or ["no reason recorded"])))
    return bad, {"files_checked": len(listed), "files_match": files_match,
                 "released": bool(man.get("released")), "tree": os.fspath(root)}

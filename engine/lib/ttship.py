"""ttship.py - the GDS a Tiny Tapeout tile ships is the GDS we signed off.

A digital TT project is shipped by a GitHub workflow in its repo. Either
the workflow re-hardens the RTL with `TinyTapeout/tt-gds-action@<ref>`
(LibreLane from pip at the action's `librelane-version`, the PDK that
LibreLane version pins, tt-support-tools at `tools-ref`), or it ships a
committed GDS as-is with `tt-gds-action/custom_gds@<ref>`. chip-flow signs
off one LibreLane run: harden/runs/run, on the toolchain image's own
LibreLane and PDK. If the workflow re-hardens with other versions, timing,
DRC, LVS, glsim and precheck all signed off a GDS nobody submits.

Two parts, both used by check_release for a vde tile:

- `stage(ws, dest)` writes what the shuttle reads, in the TT repo layout:
  info.yaml, src/config.json (harden/config.json with its `dir::` files
  copied into src/), the RTL, wrapper and SDC it names, gds/ and lef/ (the
  signed-off run's final views), and a .github/workflows/gds.yaml that
  ships that GDS with custom_gds. It never overwrites a workflow already
  there: that is the workflow the release checks.
- `ship_problems(repo, signoff)` reads every workflow under repo/.github/
  workflows and refuses unless each step that ships the tile reproduces
  the signoff: a re-harden step must install the signoff LibreLane, get
  the signoff PDK and use the vendored tt-support-tools commit; a
  custom_gds step must point at a GDS and LEF whose sha256 are the
  signed-off ones.

`signoff(ws)` reads the signoff side from the run itself (resolved.json's
meta.librelane_version, its PDK and PDK_ROOT, the PDK's SOURCES), so it is
what ran, not what is installed now.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import yaml

import statelib

ENGINE_DIR = Path(__file__).resolve().parents[1]
VENDORED_MD = ENGINE_DIR / "reference" / "tt" / "VENDORED.md"
RUN_TAG = "run"
ACTION = "TinyTapeout/tt-gds-action"
# The tt-gds-action ref chip-flow's TT tiles ship with (the ttgf26c shuttle).
DEFAULT_REF = "ttgf26c"
# What the re-harden action does when its inputs are left out, per ref
# (action.yml at that ref). A ref not listed here is refused: its defaults
# are not known, so what it would install is not known either.
ACTION_DEFAULTS = {
    "ttgf26c": {"librelane-version": "3.0.14", "tools-ref": "main"},
}
# LibreLane's pdk_hashes.yaml keys on the PDK family.
PDK_FAMILY = {"gf180mcuD": "gf180mcu", "gf180mcuC": "gf180mcu",
              "sky130A": "sky130", "sky130B": "sky130",
              "ihp-sg13g2": "ihp-sg13g2"}
_EXPR = re.compile(r"\$\{\{\s*([^}]*?)\s*\}\}")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class ShipError(RuntimeError):
    """The signoff side cannot be read (no run, no resolved.json)."""


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def vendored_tools_commit(vendored_md: Path = VENDORED_MD) -> str | None:
    """The tt-support-tools commit chip-flow vendors (VENDORED.md): the
    tile config (tech.py), DEF templates and precheck it signs off with."""
    text = vendored_md.read_text(encoding="utf-8")
    m = re.search(r"tt-support-tools/`.*?@ `([0-9a-f]{40})`", text, re.S)
    return m.group(1) if m else None


def _open_pdks_commit(pdk_root: str | None, pdk: str | None) -> str | None:
    if not pdk_root or not pdk:
        return None
    sources = Path(pdk_root) / pdk / "SOURCES"
    if not sources.is_file():
        return None
    for line in sources.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "open_pdks":
            return parts[1]
    return None


def librelane_pdk_hash(version: str, pdk: str) -> str | None:
    """The PDK (open_pdks commit, as ciel names it) LibreLane `version`
    fetches when the action runs it without a PDK of its own. Known only
    for the LibreLane installed here (its pdk_hashes.yaml); None for any
    other version, which the caller reads as "not the signoff PDK"."""
    try:
        from importlib import metadata
        import librelane
    except ImportError:
        return None
    try:
        if metadata.version("librelane") != version:
            return None
    except metadata.PackageNotFoundError:
        return None
    hashes = Path(librelane.__file__).parent / "pdk_hashes.yaml"
    if not hashes.is_file():
        return None
    data = yaml.safe_load(hashes.read_text(encoding="utf-8")) or {}
    return data.get(PDK_FAMILY.get(pdk, pdk))


def signoff(ws: Path, pdk_hash=None) -> dict:
    """What chip-flow signed off, read from the signed-off run."""
    pdk_hash = pdk_hash or librelane_pdk_hash
    run = Path(ws) / "harden" / "runs" / RUN_TAG
    resolved = run / "resolved.json"
    if not resolved.is_file():
        raise ShipError(f"no signed-off harden run: {resolved} is missing - "
                        "run the harden gate (and its signoff gates) first")
    cfg = json.loads(resolved.read_text(encoding="utf-8"))
    top = cfg.get("DESIGN_NAME")
    pdk = cfg.get("PDK")
    librelane = (cfg.get("meta") or {}).get("librelane_version")
    gds = run / "final" / "gds" / f"{top}.gds"
    lef = run / "final" / "lef" / f"{top}.lef"
    return {
        "top_module": top,
        "librelane": librelane,
        "pdk": pdk,
        # the run's own PDK_ROOT; else (a run copied from another checkout)
        # the PDK the toolchain carries, which release's attestation pins
        # and gate freshness already holds the signoff gates to (#138)
        "open_pdks": (_open_pdks_commit(cfg.get("PDK_ROOT"), pdk)
                      or statelib.pdk_version().get("open_pdks")),
        # the PDK the signoff LibreLane would fetch for itself: when it is
        # not the one the run used, even a matching LibreLane pin re-hardens
        # on another PDK
        "librelane_pdk": pdk_hash(librelane, pdk) if librelane and pdk else None,
        "tt_support_tools": vendored_tools_commit(),
        "gds": f"harden/runs/{RUN_TAG}/final/gds/{top}.gds",
        "gds_sha256": _sha256(gds),
        "lef": f"harden/runs/{RUN_TAG}/final/lef/{top}.lef",
        "lef_sha256": _sha256(lef),
    }


# --------------------------------------------------------------- staging

def default_workflow(ref: str = DEFAULT_REF, tools_ref: str | None = None) -> str:
    """The workflow chip-flow stages: ship the signed-off GDS as-is, then
    run the shuttle's precheck on it (tools-ref pinned to the vendored
    tt-support-tools, so it is the precheck we ran)."""
    tools = f"\n          tools-ref: {tools_ref}" if tools_ref else ""
    return f"""name: gds

# Staged by chip-flow release (engine/lib/ttship.py): ships the GDS chip-flow
# signed off, byte for byte, instead of re-hardening it with another
# LibreLane and PDK. check_release refuses any workflow here whose shipped
# GDS is not the signed-off one.
on:
  push:
  workflow_dispatch:

jobs:
  gds:
    runs-on: ubuntu-24.04
    steps:
      - name: checkout repo
        uses: actions/checkout@v6
        with:
          submodules: recursive

      - name: Read top module name
        id: top_module
        run: |
          echo TOP_MODULE=`yq '.project.top_module' info.yaml` | tee $GITHUB_OUTPUT

      - name: Create and publish the GDS artifact
        uses: {ACTION}/custom_gds@{ref}
        with:
          top_module: ${{{{ steps.top_module.outputs.TOP_MODULE }}}}
          gds_path: gds/${{{{ steps.top_module.outputs.TOP_MODULE }}}}.gds
          lef_path: lef/${{{{ steps.top_module.outputs.TOP_MODULE }}}}.lef
          pdk: gf180mcuD{tools}

  precheck:
    needs: gds
    runs-on: ubuntu-24.04
    steps:
      - name: Run Tiny Tapeout Precheck
        uses: {ACTION}/precheck@{ref}
        with:{tools if tools else " {}"}
"""


def stage(ws: Path, dest: Path, sign: dict) -> list[str]:
    """Write the TT repo tree for this tile at `dest`; return the paths
    written (relative to dest). An existing workflow is left alone."""
    ws, dest = Path(ws), Path(dest)
    harden = ws / "harden"
    src = dest / "src"
    src.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    copied: dict[str, Path] = {}

    def put(rel: str, data: bytes) -> None:
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        written.append(rel)

    def sub(v):
        # harden/config.json names its files `dir::<relative to harden/>`;
        # the TT repo's design dir is src/, so each moves there by name.
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if not isinstance(v, str) or not v.startswith("dir::"):
            return v
        p = (harden / v[len("dir::"):]).resolve()
        if not p.is_file():
            return v
        if copied.setdefault(p.name, p) != p:
            raise ShipError(f"two files named {p.name} in harden/config.json: "
                            f"{copied[p.name]} and {p}")
        put(f"src/{p.name}", p.read_bytes())
        return f"dir::{p.name}"

    cfg = json.loads((harden / "config.json").read_text(encoding="utf-8"))
    put("src/config.json",
        (json.dumps(sub(cfg), indent=2) + "\n").encode("utf-8"))
    sdc = harden / "constraints.sdc"
    if sdc.is_file() and "constraints.sdc" not in copied:
        put("src/constraints.sdc", sdc.read_bytes())
    if (harden / "info.yaml").is_file():
        put("info.yaml", (harden / "info.yaml").read_bytes())
    top = sign["top_module"]
    for kind in ("gds", "lef"):
        f = ws / sign[kind]
        if f.is_file():
            put(f"{kind}/{top}.{kind}", f.read_bytes())
    wf = dest / ".github" / "workflows"
    if not any(wf.glob("*.y*ml")):
        put(".github/workflows/gds.yaml",
            default_workflow(tools_ref=sign.get("tt_support_tools"))
            .encode("utf-8"))
    return written


# --------------------------------------------------------------- checking

def _steps(repo: Path):
    """(workflow file, step) for every step in repo's workflows."""
    for wf in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if isinstance(step, dict):
                    yield wf.relative_to(repo).as_posix(), step


def _parse_uses(uses: str):
    """(subaction or '', ref) for a tt-gds-action step, else None."""
    m = re.match(rf"^{re.escape(ACTION)}(?:/([\w-]+))?@(\S+)$", uses.strip())
    return (m.group(1) or "", m.group(2)) if m else None


def _same_commit(a: str | None, b: str | None) -> bool:
    if not a or not b or not _SHA.match(a) or not _SHA.match(b):
        return False
    return a.startswith(b) or b.startswith(a)


def _resolve_path(path: str, top: str | None) -> str | None:
    """A workflow path input with its expressions substituted: the TT
    workflow's top-module output is info.yaml's top_module; any other
    expression cannot be resolved here (None)."""
    def rep(m):
        if m.group(1).endswith("TOP_MODULE") and top:
            return top
        raise KeyError(m.group(1))
    try:
        return _EXPR.sub(rep, path)
    except KeyError:
        return None


def ship_problems(repo: Path, sign: dict) -> tuple[list[str], dict]:
    """(problems, shipped) for the workflows in `repo` against `sign`. A
    problem is "<rule>: <message>"; `shipped` records what each shipping
    step would produce, for the release record."""
    repo = Path(repo)
    problems: list[str] = []
    shipped: list[dict] = []
    for wf, step in _steps(repo):
        parsed = _parse_uses(str(step.get("uses", "")))
        if parsed is None:
            continue
        sub, ref = parsed
        with_ = {k: str(v) for k, v in (step.get("with") or {}).items()}
        where = f"{wf} ({step.get('name') or step['uses']})"
        if sub == "":
            defaults = ACTION_DEFAULTS.get(ref)
            if defaults is None:
                problems.append(
                    f"tt_action_unknown: {where} uses {ACTION}@{ref}, whose "
                    "defaults chip-flow does not know (engine/lib/ttship.py "
                    "ACTION_DEFAULTS) - so what it installs is unknown")
                shipped.append({"workflow": wf, "mode": "reharden", "ref": ref})
                continue
            ll = with_.get("librelane-version", defaults["librelane-version"])
            tools = with_.get("tools-ref", defaults["tools-ref"])
            pdk = sign.get("librelane_pdk") if ll == sign.get("librelane") else None
            rec = {"workflow": wf, "mode": "reharden", "ref": ref,
                   "librelane": ll, "open_pdks": pdk, "tt_support_tools": tools}
            shipped.append(rec)
            if ll != sign.get("librelane"):
                problems.append(
                    f"tt_librelane_mismatch: {where} re-hardens with LibreLane "
                    f"{ll}, signed off with {sign.get('librelane')} - set "
                    f"`librelane-version: {sign.get('librelane')}`, or ship "
                    "the signed-off GDS with custom_gds")
            elif not _same_commit(pdk, sign.get("open_pdks")):
                problems.append(
                    f"tt_pdk_mismatch: {where} re-hardens on the PDK LibreLane "
                    f"{ll} fetches (open_pdks {pdk}), signed off on open_pdks "
                    f"{sign.get('open_pdks')}; the action cannot pin a PDK - "
                    "ship the signed-off GDS with custom_gds")
            if not _same_commit(tools, sign.get("tt_support_tools")):
                problems.append(
                    f"tt_tools_mismatch: {where} uses tt-support-tools "
                    f"{tools}, signed off with the vendored "
                    f"{sign.get('tt_support_tools')} - set `tools-ref` to it")
        elif sub == "custom_gds":
            top = with_.get("top_module")
            top = _resolve_path(top, sign.get("top_module")) if top else None
            rec = {"workflow": wf, "mode": "custom_gds", "ref": ref,
                   "top_module": top, "pdk": with_.get("pdk")}
            if top != sign.get("top_module"):
                problems.append(
                    f"tt_top_mismatch: {where} ships top_module {top}, "
                    f"signed off {sign.get('top_module')}")
            if with_.get("pdk") != sign.get("pdk"):
                problems.append(
                    f"tt_pdk_mismatch: {where} ships for PDK "
                    f"{with_.get('pdk')}, signed off on {sign.get('pdk')}")
            for kind in ("gds", "lef"):
                raw = with_.get(f"{kind}_path")
                rel = _resolve_path(raw, sign.get("top_module")) if raw else None
                digest = _sha256(repo / rel) if rel else None
                rec[f"{kind}_path"] = rel
                rec[f"{kind}_sha256"] = digest
                if digest is None:
                    problems.append(
                        f"tt_{kind}_missing: {where} ships {kind}_path "
                        f"{raw!r}, which is not a file in the repo")
                elif digest != sign.get(f"{kind}_sha256"):
                    problems.append(
                        f"tt_{kind}_mismatch: {where} ships {rel} (sha256 "
                        f"{digest[:12]}), not the signed-off {sign.get(kind)} "
                        f"({(sign.get(f'{kind}_sha256') or 'none')[:12]})")
            shipped.append(rec)
    if not any(s["mode"] in ("reharden", "custom_gds") for s in shipped):
        problems.append(
            f"tt_not_shipped: no workflow under {repo}/.github/workflows "
            f"ships the tile ({ACTION}@<ref> or {ACTION}/custom_gds@<ref>)")
    return problems, {"steps": shipped}


def release_check(ws: Path, repo: Path | None = None,
                  pdk_hash=None) -> tuple[list[str], dict]:
    """Stage the TT repo for a hardened vde tile and check what its
    workflows ship against the signoff. (problems, record)."""
    ws = Path(ws)
    repo = Path(repo) if repo else ws / "release" / "tt"
    try:
        sign = signoff(ws, pdk_hash=pdk_hash)
        staged = stage(ws, repo, sign)
    except ShipError as exc:
        return [f"tt_no_signoff: {exc}"], {}
    problems, shipped = ship_problems(repo, sign)
    try:
        repo_rel = repo.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        repo_rel = str(repo)
    return problems, {"repo": repo_rel, "staged": sorted(staged),
                      "signoff": sign, "shipped": shipped["steps"]}

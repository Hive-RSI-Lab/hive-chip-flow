"""ttship.py - the GDS a Tiny Tapeout tile ships is the GDS we signed off.

A digital TT project is shipped by a GitHub workflow in its repo. Either
the workflow re-hardens the RTL with `TinyTapeout/tt-gds-action@<ref>`
(LibreLane from pip at the action's `librelane-version`, the PDK that
LibreLane version pins, tt-support-tools at `tools-ref`), or it ships a
committed GDS as-is with `tt-gds-action/custom_gds@<ref>`. chip-flow signs
off one LibreLane run: harden/runs/run, on the toolchain image's own
LibreLane and PDK. If the workflow re-hardens with other versions, timing,
DRC, LVS, glsim and precheck all signed off a GDS nobody submits.

Two parts, both used by check_release for a hardened tile (a vde tile's run
is under harden/, an msde top's under top/harden/):

- `stage(ws, dest)` writes what the shuttle reads, in the TT repo layout:
  info.yaml, src/config.json (harden/config.json with its `dir::` files
  copied into src/), the RTL, wrapper and SDC it names, gds/ and lef/ (the
  signed-off run's final views), gl/<top>.v (final/pnl: the powered netlist
  TT's precheck reads and glsim's functional pass simulated), test/ (the
  block's cocotb suite, a Makefile for it and the glsim harness), and a
  .github/workflows/gds.yaml that ships that GDS with custom_gds and runs the
  suite on the shipped netlist. It never overwrites a workflow already there:
  that is the workflow the release checks.
- `stage_analog(ws, dest, sign)` is the same for an msde tile, in the shape
  of TT's analog template: no src/config.json, since TT takes the committed
  GDS, and src/project.v as the netlist when it has power pins, else the
  run's final/pnl. It is what a block's hand-written assemble.py did, driven
  by top/.
- `ship_problems(repo, signoff)` reads every workflow under repo/.github/
  workflows and refuses unless each step that ships the tile reproduces
  the signoff: a re-harden step must install the signoff LibreLane, get
  the signoff PDK and use the vendored tt-support-tools commit; a
  custom_gds step must point at a GDS and LEF whose sha256 are the
  signed-off ones, and at a netlist (without one the action ships an empty
  .v, which TT's precheck fails: "Verilog doesn't contain VGND"). A vde
  tile's must be the run's final/pnl, by sha256, the one glsim recorded, and
  a job must run the test suite on it on the signoff PDK: custom_gds writes
  no PDK_VERSION, so TT's own gl_test action can't.

`signoff(ws)` reads the signoff side from the run itself (resolved.json's
meta.librelane_version, its PDK and PDK_ROOT, the PDK's SOURCES), so it is
what ran, not what is installed now.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import yaml

import cocotblib
import speclib
import statelib
import ttlib

ENGINE_DIR = Path(__file__).resolve().parents[1]
VENDORED_MD = ENGINE_DIR / "reference" / "tt" / "VENDORED.md"
RUN_TAG = "run"
# Where each skill's tile is hardened.
HARDEN_DIR = {"vde": "harden", "msde": "top/harden"}
ACTION = "TinyTapeout/tt-gds-action"
CIEL_ACTION = "TinyTapeout/ciel-action"
# TT's analog template ships the tile's top as its netlist. An msde tile does
# when that top has power pins (a hand-kept tt/src/project.v); top_harden's own
# wrapper has none, so that tile ships its run's powered netlist instead.
ANALOG_NETLIST = "src/project.v"
# What the staged test job installs. cocotb is the image's, the one glsim
# runs on; pytest is the TT template's.
TEST_REQUIREMENTS = "pytest==8.4.2\ncocotb==2.1.0\n"
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
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


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


def signoff(ws: Path, pdk_hash=None, harden: str = "harden") -> dict:
    """What chip-flow signed off, read from the signed-off run."""
    pdk_hash = pdk_hash or librelane_pdk_hash
    run = Path(ws) / harden / "runs" / RUN_TAG
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
    pnl = run / "final" / "pnl" / f"{top}.pnl.v"
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
        "gds": f"{harden}/runs/{RUN_TAG}/final/gds/{top}.gds",
        "gds_sha256": _sha256(gds),
        "lef": f"{harden}/runs/{RUN_TAG}/final/lef/{top}.lef",
        "lef_sha256": _sha256(lef),
        "gl": f"{harden}/runs/{RUN_TAG}/final/pnl/{top}.pnl.v",
        "gl_sha256": _sha256(pnl),
        # A vde tile ships its run's netlist, which glsim simulated. An msde
        # tile may ship TT's analog template's netlist (ANALOG_NETLIST).
        "gl_is_shipped": harden == HARDEN_DIR["vde"],
        "glsim_gl_sha256": _glsim_netlist(Path(ws)),
    }


def _glsim_netlist(ws: Path) -> str | None:
    """The sha256 of the powered netlist glsim's report says it simulated."""
    try:
        report = json.loads((ws / "reports" / "gate-glsim.json")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (report.get("netlists") or {}).get("pnl_sha256")


# --------------------------------------------------------------- staging

GL_TEST_JOB = """
  # Not TT's gl_test action: custom_gds writes pdk.json with no PDK_VERSION,
  # so that action would ask ciel for `null`. Same steps, on the PDK this GDS
  # was signed off on, against the netlist in the submission.
  gl_test:
    needs: gds
    runs-on: ubuntu-24.04
    env:
      PDK_ROOT: /home/runner/pdk
    steps:
      - name: checkout repo
        uses: actions/checkout@v6
        with:
          submodules: recursive

      - name: Download GDS artifact
        uses: actions/download-artifact@v7
        with:
          name: tt_submission

      - name: Install the signoff PDK
        uses: {ciel}@v1
        with:
          pdk_name: {pdk}
          pdk_version: {open_pdks}
          pdk_root: /home/runner/pdk

      - name: Setup python
        uses: actions/setup-python@v6
        with:
          python-version: '3.11'

      - name: Install Python packages
        run: pip install -r test/requirements.txt

      - name: Install iverilog
        run: |
          wget https://github.com/TinyTapeout/iverilog/releases/download/v13.0/iverilog_13.0-1_amd64.deb
          sudo apt-get update -y
          sudo apt-get install -y ./iverilog_13.0-1_amd64.deb

      - name: Run the tests on the submission's netlist
        run: |
          cp tt_submission/*.v test/gate_level_netlist.v
          cd test
          rm -f results.xml
          make clean
          GATES=yes make
          # make succeeds even when a test fails, so read results.xml
          test -f results.xml
          ! grep -E '<(failure|error)' results.xml

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: gatelevel_test_results
          path: test/results.xml
"""


def default_workflow(ref: str = DEFAULT_REF, tools_ref: str | None = None,
                     verilog_path: str = ANALOG_NETLIST,
                     gl_test_pdk: str | None = None, pdk: str = "gf180mcuD") -> str:
    """The workflow chip-flow stages: ship the signed-off GDS as-is with the
    netlist at `verilog_path`, then run the shuttle's precheck on it (tools-ref
    pinned to the vendored tt-support-tools, so it is the precheck we ran).
    `gl_test_pdk`, an open_pdks commit, adds the job that runs test/ on that
    netlist on that PDK."""
    tools = f"\n          tools-ref: {tools_ref}" if tools_ref else ""
    gl_test = (GL_TEST_JOB.format(ciel=CIEL_ACTION, pdk=pdk, open_pdks=gl_test_pdk)
               if gl_test_pdk else "")
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
          verilog_path: {verilog_path}
          pdk: {pdk}{tools}

  precheck:
    needs: gds
    runs-on: ubuntu-24.04
    steps:
      - name: Run Tiny Tapeout Precheck
        uses: {ACTION}/precheck@{ref}
        with:{tools if tools else " {}"}
{gl_test}"""


def gl_test_makefile(top: str, modules: list[str], harness: str) -> str:
    """test/Makefile: what glsim's functional pass builds, as TT's template
    spells it (GATES=yes, gate_level_netlist.v copied in by the workflow)."""
    return f"""# Staged by chip-flow release (engine/lib/ttship.py): the block's tb/ suite on
# the gate-level netlist, as the glsim gate's functional pass runs it.
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

ifneq ($(GATES),yes)
$(error this tile ships a hardened netlist: run with GATES=yes)
endif

SIM_BUILD = sim_build/gl
COMPILE_ARGS += -DGL_TEST
COMPILE_ARGS += -DFUNCTIONAL
COMPILE_ARGS += -DUSE_POWER_PINS
VERILOG_SOURCES += $(PWD)/{harness}
VERILOG_SOURCES += $(PDK_ROOT)/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/primitives.v
VERILOG_SOURCES += $(PDK_ROOT)/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/gf180mcu_fd_sc_mcu7t5v0.v
# copied in by the workflow from tt_submission
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

TOPLEVEL = {top}
COCOTB_TEST_MODULES = {",".join(modules)}

include $(shell cocotb-config --makefiles)/Makefile.sim
"""


def stage(ws: Path, dest: Path, sign: dict,
          sources: dict | None = None) -> list[str]:
    """Write the TT repo tree for this tile at `dest`; return the paths
    written (relative to dest). An existing workflow is left alone. `sources`,
    if given, gets tree path -> the workspace file it came from."""
    ws, dest = Path(ws), Path(dest)
    sources = {} if sources is None else sources
    harden = ws / "harden"
    src = dest / "src"
    src.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    copied: dict[str, Path] = {}

    def put(rel: str, data: bytes, source: str | None = None) -> None:
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        written.append(rel)
        sources[rel] = source

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
        put(f"src/{p.name}", p.read_bytes(),
            Path(os.path.relpath(p, ws.resolve())).as_posix())
        return f"dir::{p.name}"

    cfg = json.loads((harden / "config.json").read_text(encoding="utf-8"))
    put("src/config.json",
        (json.dumps(sub(cfg), indent=2) + "\n").encode("utf-8"),
        "harden/config.json")
    sdc = harden / "constraints.sdc"
    if sdc.is_file() and "constraints.sdc" not in copied:
        put("src/constraints.sdc", sdc.read_bytes(), "harden/constraints.sdc")
    if (harden / "info.yaml").is_file():
        put("info.yaml", (harden / "info.yaml").read_bytes(), "harden/info.yaml")
    top = sign["top_module"]
    for kind in ("gds", "lef"):
        f = ws / sign[kind]
        if f.is_file():
            put(f"{kind}/{top}.{kind}", f.read_bytes(), sign[kind])
    if (ws / sign["gl"]).is_file():
        put(f"gl/{top}.v", (ws / sign["gl"]).read_bytes(), sign["gl"])
    _stage_gl_test(ws, put)
    wf = dest / ".github" / "workflows"
    if not any(wf.glob("*.y*ml")):
        if not sign.get("open_pdks"):
            raise ShipError("no open_pdks commit for the signed-off run, so "
                            "there is no PDK to run its gate-level test on")
        put(".github/workflows/gds.yaml",
            default_workflow(tools_ref=sign.get("tt_support_tools"),
                             verilog_path="gl/${{ steps.top_module.outputs.TOP_MODULE }}.v",
                             gl_test_pdk=sign["open_pdks"], pdk=sign["pdk"])
            .encode("utf-8"))
    return written


def _stage_gl_test(ws: Path, put) -> None:
    """test/: tb/, the glsim harness and a Makefile. Nothing when the block
    has no test modules or pin map; ship_problems then refuses."""
    tb = ws / "tb"
    spec_path = ws / "spec" / "spec.yaml"
    if not tb.is_dir() or not spec_path.is_file():
        return
    modules = cocotblib.test_modules(tb)
    spec = speclib.load_spec(spec_path)
    if not modules or not spec.get("tt_pins"):
        return
    try:
        harness = ttlib.generate_glsim_harness(spec)
    except ttlib.TTError as exc:
        raise ShipError(str(exc)) from exc
    for p in sorted(tb.iterdir()):
        if p.is_file():
            put(f"test/{p.name}", p.read_bytes(), f"tb/{p.name}")
    name = "glsim_harness.v"
    put(f"test/{name}", harness.encode("utf-8"))
    put("test/Makefile",
        gl_test_makefile(spec["top"], modules, name).encode("utf-8"))
    put("test/requirements.txt", TEST_REQUIREMENTS.encode("utf-8"))


def stage_analog(ws: Path, dest: Path, sign: dict,
                 sources: dict | None = None) -> list[str]:
    """Write an msde tile's TT analog-project tree at `dest`.

    gds/ and lef/ are the signed-off top's. src/ is the wrapper top_harden
    generated (as project.v) and the macro blackboxes from top/macros, or the
    block's hand-kept tt/src/*.v instead when it has them, plus digital/rtl.
    info.yaml is tt/info.yaml, else top/harden/info.yaml; test/ is tt/test/*;
    the workflow is tt/workflows/*, else the custom_gds default, and one
    already in the tree is left alone. Also removes what only the digital
    template uses: src/config.json and the fpga workflow, which can't build a
    design with an analog macro."""
    ws, dest = Path(ws), Path(dest)
    sources = {} if sources is None else sources
    top = sign["top_module"]
    tt = ws / "tt"
    plan: dict[str, Path] = {}

    def want(rel: str, src: Path) -> None:
        if rel in plan and plan[rel].read_bytes() != src.read_bytes():
            raise ShipError(f"two different files for {rel}: {plan[rel]} and {src}")
        plan[rel] = src

    for kind in ("gds", "lef"):
        want(f"{kind}/{top}.{kind}", ws / sign[kind])
    kept = sorted((tt / "src").glob("*.v")) if (tt / "src").is_dir() else []
    if kept:
        for p in kept:
            want(f"src/{p.name}", p)
    else:
        want("src/project.v", ws / "top" / "harden" / f"{top}.v")
        for p in sorted((ws / "top" / "macros").glob("*.v")):
            want(f"src/{p.name}", p)
    for p in sorted((ws / "digital" / "rtl").glob("*.v")):
        want(f"src/{p.name}", p)
    info = tt / "info.yaml"
    want("info.yaml", info if info.is_file() else ws / "top" / "harden" / "info.yaml")
    if (tt / "test").is_dir():
        for p in sorted((tt / "test").iterdir()):
            if p.is_file():
                want(f"test/{p.name}", p)
    missing = sorted(rel for rel, p in plan.items() if not p.is_file())
    if missing:
        raise ShipError("nothing to stage for " + ", ".join(missing)
                        + " - run top_harden first")
    written: list[str] = []
    for rel, p in plan.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
        written.append(rel)
        sources[rel] = p.relative_to(ws).as_posix()
    if not _has_power_pins(dest / ANALOG_NETLIST) and (ws / sign["gl"]).is_file():
        out = dest / "gl" / f"{top}.v"
        out.parent.mkdir(exist_ok=True)
        shutil.copyfile(ws / sign["gl"], out)
        written.append(f"gl/{top}.v")
        sources[f"gl/{top}.v"] = sign["gl"]
        verilog_path = "gl/${{ steps.top_module.outputs.TOP_MODULE }}.v"
    else:
        verilog_path = ANALOG_NETLIST
    wf = dest / ".github" / "workflows"
    kept_wf = sorted((tt / "workflows").glob("*.y*ml")) if (tt / "workflows").is_dir() else []
    if not any(wf.glob("*.y*ml")):
        wf.mkdir(parents=True, exist_ok=True)
        for p in kept_wf:
            shutil.copyfile(p, wf / p.name)
            written.append(f".github/workflows/{p.name}")
        if not kept_wf:
            (wf / "gds.yaml").write_text(
                default_workflow(tools_ref=sign.get("tt_support_tools"),
                                 verilog_path=verilog_path, pdk=sign["pdk"]),
                encoding="utf-8")
            written.append(".github/workflows/gds.yaml")
    (dest / "src" / "config.json").unlink(missing_ok=True)
    (wf / "fpga.yaml").unlink(missing_ok=True)
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


def _has_power_pins(path: Path) -> bool:
    """What TT's precheck asks of the netlist (precheck.py power_pin_check)."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _COMMENT.sub("", text).replace("VPWR", "VDPWR")
    return "VGND" in text and "VDPWR" in text


def _netlist_problems(repo: Path, where: str, raw: str | None, sign: dict,
                      rec: dict) -> list[str]:
    """A custom_gds step's verilog_path against the signoff."""
    if not raw:
        return [f"tt_verilog_missing: {where} has no verilog_path, so custom_gds "
                "ships an empty .v and TT's precheck fails (\"Verilog doesn't "
                "contain VGND\") - remove the workflow from the tree so release "
                "stages one, or set verilog_path"]
    rel = _resolve_path(raw, sign.get("top_module"))
    digest = _sha256(repo / rel) if rel else None
    rec["verilog_path"] = rel
    rec["verilog_sha256"] = digest
    if digest is None:
        return [f"tt_verilog_missing: {where} ships verilog_path {raw!r}, which "
                "is not a file in the repo"]
    if not _has_power_pins(repo / rel):
        return [f"tt_verilog_unpowered: {where} ships {rel} with no VGND or no "
                "VPWR/VDPWR, which TT's precheck fails - it must be the powered "
                "netlist"]
    if sign.get("gl_is_shipped"):
        if digest != sign.get("gl_sha256"):
            return [f"tt_verilog_mismatch: {where} ships {rel} (sha256 "
                    f"{digest[:12]}), not the signed-off {sign.get('gl')} "
                    f"({(sign.get('gl_sha256') or 'none')[:12]})"]
        rec["glsim_sha256"] = sign.get("glsim_gl_sha256")
        if digest != sign.get("glsim_gl_sha256"):
            return [f"tt_verilog_not_simulated: {rel} (sha256 {digest[:12]}) is "
                    "not the netlist glsim's report says it simulated "
                    f"({(sign.get('glsim_gl_sha256') or 'none')[:12]}) - run "
                    "the glsim gate"]
    elif rel != ANALOG_NETLIST and digest != sign.get("gl_sha256"):
        return [f"tt_verilog_mismatch: {where} ships {rel} (sha256 {digest[:12]}), "
                f"neither {ANALOG_NETLIST} nor the signed-off {sign.get('gl')}"]
    return []


def _gl_test_problems(repo: Path, sign: dict, ships_custom_gds: bool) -> list[str]:
    """The test job of a custom_gds workflow: TT's gl_test action can't run,
    any PDK it installs must be the signoff's, and a vde tile must have one."""
    if not ships_custom_gds:
        return []
    problems = []
    pinned = False
    ran = False
    for wf, step in _steps(repo):
        uses = str(step.get("uses", ""))
        parsed = _parse_uses(uses)
        where = f"{wf} ({step.get('name') or uses})"
        if parsed and parsed[0] == "gl_test":
            problems.append(
                f"tt_gl_test_no_pdk: {where} runs gl_test, which reads "
                "PDK_VERSION from pdk.json; custom_gds writes none, so it asks "
                "ciel for `null` - install the signoff PDK in a job of its own")
        if re.match(rf"^{re.escape(CIEL_ACTION)}@", uses):
            version = str((step.get("with") or {}).get("pdk_version") or "")
            if _same_commit(version, sign.get("open_pdks")):
                pinned = True
            else:
                problems.append(
                    f"tt_gl_pdk_mismatch: {where} installs PDK {version!r}, "
                    f"signed off on open_pdks {sign.get('open_pdks')}")
        if "GATES=yes" in str(step.get("run", "")):
            ran = True
    if not sign.get("gl_is_shipped"):
        return problems
    if not (ran and pinned):
        problems.append(
            "tt_gl_test_missing: no workflow installs the signoff PDK with "
            f"{CIEL_ACTION} and runs `GATES=yes make`, so nothing tests the "
            "shipped netlist on the PDK it was signed off on")
    if not (repo / "test" / "Makefile").is_file():
        problems.append(
            "tt_gl_test_missing: no test/Makefile - the block has no tb/test_*.py "
            "or no tt_pins, so there is nothing to run on the shipped netlist")
    return problems


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
            problems.extend(_netlist_problems(
                repo, where, with_.get("verilog_path"), sign, rec))
            shipped.append(rec)
    if not any(s["mode"] in ("reharden", "custom_gds") for s in shipped):
        problems.append(
            f"tt_not_shipped: no workflow under {repo}/.github/workflows "
            f"ships the tile ({ACTION}@<ref> or {ACTION}/custom_gds@<ref>)")
    problems.extend(_gl_test_problems(
        repo, sign, any(s["mode"] == "custom_gds" for s in shipped)))
    return problems, {"steps": shipped}


def release_check(ws: Path, repo: Path | None = None, pdk_hash=None,
                  skill: str = "vde") -> tuple[list[str], dict]:
    """Stage the TT repo for a hardened tile and check what its workflows
    ship against the signoff. (problems, record)."""
    ws = Path(ws)
    repo = Path(repo) if repo else ws / "release" / "tt"
    sources: dict = {}
    try:
        sign = signoff(ws, pdk_hash=pdk_hash, harden=HARDEN_DIR[skill])
        staged = (stage_analog if skill == "msde" else stage)(ws, repo, sign, sources)
    except ShipError as exc:
        return [f"tt_no_signoff: {exc}"], {}
    problems, shipped = ship_problems(repo, sign)
    try:
        repo_rel = repo.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        repo_rel = str(repo)
    return problems, {"repo": repo_rel, "staged": sorted(staged),
                      "sources": sources, "signoff": sign,
                      "shipped": shipped["steps"]}

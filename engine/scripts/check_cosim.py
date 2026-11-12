#!/usr/bin/env python
"""check_cosim.py - the cosim gate (docs/design.md 1.5 msde table, "### M10.").

    check_cosim.py --workspace DIR [--out FILE]

Builds <workspace>/tb's Verilog (the digital side, Icarus-compiled) and runs
its cocotb test(s) - engine/lib/cocotblib.py, same shape check_sim.py uses.
The bench drives an analog block (a SPICE subcircuit) through
cocotbext-ams's MixedSignalBridge (docs/spikes/dcosim.md: ngspice's own
`d_cosim`/`ivlng` bridge is broken in this image; design.md section 5's
first fallback is what M10 builds on) so ONE bench genuinely drives both
sides - the Icarus-compiled digital side reacts to real ngspice-computed
analog transitions, not a stub.

A bench should build its bridge as engine/lib/cosimlib.py's CosimBridge
(`from cosimlib import CosimBridge` - this gate's engine/lib is on the
bench's PYTHONPATH), MixedSignalBridge with two fixes: a sync point missed
by a float rounding counts as reached (else ngspice's "Timestep too small"
at the first sync), and an analog output is written on change to a `reg`,
not Forced at every sync (which crashed vvp after a few hundred forces).

`tb/cosim_bench.json` names the top module, its bounds sidecar, and the
analog netlist it actually simulated:
    {"top": "<hdl_toplevel>", "bounds": "<name>.bounds.json",
     "analog_netlist": "<name>.sp", "analog_kind": "ideal"|"transistor",
     "corners": {...}}          (optional, see "Corners" below)
The bench itself is the authority on what it measured: it writes
`reports/cosim_measures.json` (a plain dict of measure name -> value, plus
`digital_toggles` - the count of digital edges it actually observed, backed
by that many `edge_times_ns` entries, not a bare literal - and
`spice_time_reached_ns`, the absolute analog time the bridge actually
synced to, evidence the analog side ran rather than the digital side
toggling on its own) BEFORE any assertion that could raise - a bench that
cannot converge or never sees the digital side toggle still leaves a
measures file behind, so this gate can say exactly what was missing rather
than just "the test failed".

Why this gate does not trust cocotb's own pass/fail, or ngspice's exit code,
alone (docs/design.md, CLAUDE.md: "ngspice exits 0 on many failures, so
parse the measures" / "a gate that did not run is a refusal, never a
pass"): ngspice's shared library (the only way a live cosim bridge can run -
batch `-b` has no bidirectional channel) logs a run's own non-convergence as
free text on its normal callback channel, not as a process exit code -
there is no exit code here at all, only a Python return. So several things
are checked independently, every run, regardless of what cocotb reported:
  1. the raw sim log for ngspice's own failure signatures (a timestep that
     collapsed, a singular matrix, a trouble node) - `ngspice_non_convergence`
  2. the raw sim log for an `.ic` naming a node that does not exist - ngspice
     does not error on this, it logs "IC on non-existent node - <n>,
     ignored" and carries on with that initial condition silently dropped
     (docs/spikes/dcosim.md's own M10 fix was exactly this typo) -
     `ic_ignored`
  3. `reports/cosim_measures.json` exists, and `digital_toggles` is nonzero
     AND backed by that many recorded edge times, not a bare literal -
     `measures_missing` / `digital_side_never_toggled` / `digital_toggles_unbacked`
  4. the bench's own recorded `spice_time_reached_ns` covers its last
     measured edge - evidence the analog bridge actually ran that far,
     not just that the digital side toggled - `spice_time_missing` /
     `spice_time_behind_last_edge`
  5. every measure `tb/*.bounds.json` names is present and inside its bound -
     `measure_missing` / `measure_out_of_bounds`
A cocotb test failure (a bench-side assertion, or the digital side timing
out waiting for an edge that never came) is ALSO reported (`test_failed`),
additively - never instead of the above, since a bench can fail its own
assertion for exactly the reason a measure is out of bounds and both facts
are worth keeping.

An msde block with a digital/ side ships as an analog tile: the digital
block behind the generated TT wrapper (its pad reset synchroniser in front
of rst_n) with the analog cell as a hard macro. The bench runs through that
same frame (ttlib.write_sim_frame on the top spec check_top_harden hardens,
ttlib.macro_top_spec): the build gains the wrapper, a harness module
`tt_frame_<digital top>` with the digital spec's own port list, and a
pin-only model of the macro, and the bench instantiates
`tt_frame_<digital top>` where it would instantiate the digital top. A
bench whose digital top sits anywhere but inside that wrapper is refused,
before the sim runs (`cosim_top_unframed`): it would not see the pad
reset latency the tile ships with.

Corners. The runs above are one point: the schematic netlist and the RTL at
typical. A transistor-level top signs off on its post-layout views at the
corners that bound the analog side, so after those runs pass the gate runs
the same bench again at each corner, one process per corner and digital
view, from a copy of tb/ under log/cosim_corners/<view>/<corner>/ (`jobs` at
a time, each limited by `timeout_s`):
  - which corners: `corners.list`, or the corners at the ends `corners.bound_by`
    names ([[measure, "min"|"max"], ...]) in the analog side's recorded pex
    sweep (gate-pex_sim.json or pex_pvt.json, whichever covers more corners),
    or, with neither, the min and max corner of every measure that sweep holds
  - the analog view: the extracted netlist the sweep ran on when there is a
    sweep, else the schematic
  - the digital view: `corners.digital`, a list of "rtl" and "gl". By default
    gl when digital/harden/runs/run/final has the netlist, else rtl. gl is
    glsim's harness on the digital side's netlist with the SDF of the corner's
    process and nearest temperature (cosim_corners.sdf_corner_for), generated
    with OpenSTA when the harden did not write that corner. It is the digital
    side's harden and not top/harden's, which runs after this gate
A bench with no `corners` key is held to this when its analog_kind is
transistor, it has a pex record and it is not a released v1 block; the facts
say why a bench was not. A corner run is the bare cell: the pad condition
stays at typical.

The bench reads what to apply from cosimlib.corner_setup() (the COSIM_SETUP
environment variable, None on the typical runs): `corner`, `process`,
`res_corner`, `mim_corner`, `temp_c`, `vdd`, `netlist`, `netlist_path`,
`sizing`, `lines` (the SPICE lines that apply it, the netlist's `.include`
among them, so tb/'s analog_netlist must be a wrapper that instantiates the
cell, not hold it) and `out`. It adds `cosimlib.corner_evidence(setup)` to its
measures, or the run is `cosim_corner_not_applied`: a bench that ignored the
setup would score its typical run at every corner. Every finding carries its
`<corner>/<view>` label, and a run that gave no result is
`cosim_corner_no_result`; the other corners keep their data.

CLI/exit contract: checklib's (argparse, JSON to stdout or --out, exit 0
pass, 1 violations, 2 error with a remediation string).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from multiprocessing.pool import ThreadPool
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(SCRIPTS))
import boundary  # noqa: E402
import check_glsim  # noqa: E402
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import cocotblib  # noqa: E402
import cosim_corners  # noqa: E402
import layoutlib  # noqa: E402
import padlib  # noqa: E402
import sim_run  # noqa: E402
import simlib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_cosim"
EDA_BIN = REPO / "bin" / "eda"
BUILD_SUBDIR = "log/cosim_build"
GL_BUILD_FLAGS = ("-gspecify", "-ginterconnect")
GL_BUILD_LOG = "build.log"
CORNER_LINKED_SOURCES = ("analog", "digital", "interface.yaml")
RESULTS_NAME = "cosim_results.xml"
MEASURES_REL = "reports/cosim_measures.json"
BENCH_META_NAME = "cosim_bench.json"
NGSPICE_LIB_REL = "foss/tools/ngspice/lib"
LIBNGSPICE_REL = "foss/tools/ngspice/lib/libngspice.so.0"
TOOLCHAIN_TIMEOUT_S = 30.0
# A cosim bench runs inside ngspice's shared library, not a subprocess this
# gate can attach a plain `timeout=` to (cocotblib.run_cocotb's own SIGALRM
# wraps this), and a shared-library co-sim wedged on a sync point that never
# fires would otherwise hang the gate forever with no exit code to catch it
# on. run_cocotb ends it when vvp stops making progress (checklib.stall_s());
# this is only the wall ceiling. A fixed 240 s used to be the whole limit,
# and pll's transistor-level co-sim (~100 s alone) ran past it on a loaded
# box while still working.
COCOTB_TIMEOUT_S = 2400.0

# ngspice's own free-text failure signatures on its callback channel - none
# of these change its return value (there isn't one, this runs through the
# shared library), so this is the only place a non-convergent run is
# visible at all. Kept as a short, named list (rather than one broad
# "Error"/"trouble" match) so a benign "warning"-level line never trips it.
NONCONVERGENCE_PATTERNS = [
    re.compile(r"Timestep too small", re.I),
    re.compile(r"trouble with node", re.I),
    re.compile(r"singular matrix", re.I),
    re.compile(r"doAnalyses:\s*TRAN", re.I),
]

# ngspice does not error on an `.ic` naming a node that does not exist - it
# logs this on the same free-text callback channel and carries on with that
# initial condition silently dropped (docs/spikes/dcosim.md: the wrapper
# netlist cocotbext-ams generates always instantiates the user subcircuit as
# "x1", so an .ic on an internal node's bare name, without that prefix, is
# exactly this - a no-op that looks like a pass until the node in question
# never actually starts where the bench meant it to).
IC_IGNORED_PATTERNS = [
    re.compile(r"IC on non-existent node\s*-\s*([^,]+),\s*ignored", re.I),
]


def toolchain_root(timeout: float = TOOLCHAIN_TIMEOUT_S) -> Path:
    proc = subprocess.run([str(EDA_BIN), "--print-toolchain-root"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CheckError("could not resolve the eda toolchain root: "
                         f"{(proc.stderr or proc.stdout).strip()}")
    return Path(proc.stdout.strip())


def ensure_ngspice_env(toolchain: Path) -> None:
    """cocotbext-ams's ngspice backend loads libngspice.so via ctypes, and
    that .so in turn dlopen()s its own codemodel .so's relative to
    LD_LIBRARY_PATH - both must be set before cocotb ever imports
    cocotbext.ams (docs/spikes/dcosim.md's run.sh sets the same two vars for
    the same reason). Set once, additively, never overwriting a caller's
    own LD_LIBRARY_PATH."""
    lib_dir = str(toolchain / NGSPICE_LIB_REL)
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in existing.split(":"):
        os.environ["LD_LIBRARY_PATH"] = (
            f"{lib_dir}:{existing}" if existing else lib_dir)
    os.environ.setdefault("LIBNGSPICE_PATH", str(toolchain / LIBNGSPICE_REL))


ANALOG_KINDS = ("ideal", "transistor")


def load_bench_meta(tb_dir: Path) -> dict:
    path = tb_dir / BENCH_META_NAME
    if not path.is_file():
        raise CheckError(f"no {BENCH_META_NAME} at {path} - the bench must "
                         "name its top module and bounds sidecar")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CheckError(f"{path} is not valid JSON: {exc}") from exc
    top = data.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError(f"{path}: no non-empty 'top'")
    bounds_name = data.get("bounds")
    if not isinstance(bounds_name, str) or not bounds_name.strip():
        raise CheckError(f"{path}: no non-empty 'bounds'")
    bounds_path = tb_dir / bounds_name
    if not bounds_path.is_file():
        raise CheckError(f"{path}: bounds sidecar not found at {bounds_path}")
    try:
        bounds = json.loads(bounds_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CheckError(f"{bounds_path} is not valid JSON: {exc}") from exc
    if not isinstance(bounds, dict) or not bounds:
        raise CheckError(f"{bounds_path}: no non-empty bounds mapping")
    for name, bound in bounds.items():
        try:
            padlib.pad_scope(bound if isinstance(bound, dict) else {})
        except ValueError as exc:
            raise CheckError(f"{bounds_path}: bound {name!r}: {exc}") from exc
    # Which SPICE netlist this bench actually simulated, and whether it is
    # an ideal behavioral stand-in or a real transistor-level block - a
    # reader of the report should not have to open tb/ to tell the two
    # apart (docs/spikes/dcosim.md: ring_osc_div's own ring5.sp is
    # deliberately ideal RC/comparator stages, not a gf180 ring, precisely
    # so this distinction matters).
    netlist_name = data.get("analog_netlist")
    if not isinstance(netlist_name, str) or not netlist_name.strip():
        raise CheckError(f"{path}: no non-empty 'analog_netlist' - which "
                         "SPICE netlist this bench actually simulated")
    if not (tb_dir / netlist_name).is_file():
        raise CheckError(f"{path}: analog_netlist {netlist_name!r} not "
                         f"found under {tb_dir}")
    analog_kind = data.get("analog_kind")
    if analog_kind not in ANALOG_KINDS:
        raise CheckError(f"{path}: 'analog_kind' must be one of "
                         f"{ANALOG_KINDS}, got {analog_kind!r}")
    return {"top": top, "bounds": bounds, "bounds_path": bounds_path,
           "analog_netlist": netlist_name, "analog_kind": analog_kind,
           "corners": cosim_corners.validate_config(data.get("corners"),
                                                    str(path))}


def collect_sources(tb_dir: Path) -> list[Path]:
    files = sorted(tb_dir.glob("*.v")) + sorted(tb_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {tb_dir}")
    return files


def frame_spec(ws: Path) -> dict | None:
    """The analog tile's top spec, as check_top_harden assembles it
    (ttlib.macro_top_spec) without the layout: the digital spec with the
    analog cell, named by the analog spec's `top`, as its one macro. None
    for a workspace with no digital/spec/spec.yaml (a bench-only rung such
    as corpus/msde/ring_osc_div), which has no tile to frame."""
    dpath = ws / "digital" / "spec" / "spec.yaml"
    if not dpath.is_file():
        return None
    dspec = speclib.load_spec(dpath)
    apath = ws / "analog" / "spec" / "spec.yaml"
    aspec = speclib.load_spec(apath) if apath.is_file() else None
    cell = aspec.get("top") if isinstance(aspec, dict) else None
    if not isinstance(dspec, dict) or not dspec.get("top"):
        raise CheckError(f"{dpath}: no `top` - the frame wraps it")
    if not isinstance(cell, str) or not cell:
        raise CheckError(f"no analog `top` at {apath} - the frame's macro "
                         "model is named after it")
    import yaml
    iface = yaml.safe_load((ws / "interface.yaml").read_text(
        encoding="utf-8")) or {}
    signals = {str(s["name"]).lower() for s in iface.get("signals") or []}
    ports = {p.lower(): p for p in dspec.get("ports") or {}}
    missing = sorted(signals - set(ports))
    if missing:
        raise CheckError(f"interface.yaml names {missing}, which the digital "
                         "spec.yaml's ports do not have")
    macro = {"cell": cell, "instance": ttlib.ANALOG_MACRO_INSTANCE,
             "pins": {s: ports[s] for s in sorted(signals)},
             "ua": {str(p).lower(): k
                    for p, k in (iface.get("ua_pins") or {}).items()}}
    # pad: true signals keep their tt_pins pad, as in check_top_harden
    return ttlib.macro_top_spec(dspec, macro,
                                signals - ttlib.interface_pad_signals(iface),
                                iface.get("tiles"))


SCOPE_RE = re.compile(r'^S_(\w+) \.scope module, "([^"]+)" "([^"]+)"'
                      r'[^;]*?(?:, S_(\w+))?;$', re.M)


def scope_parents(vvp_text: str) -> list[tuple[str, str, str | None]]:
    """[(instance, module, parent module or None)] for every module scope
    an Icarus vvp file elaborates (`S_<id> .scope module, "<inst>"
    "<module>" ..., S_<parent>;`, the root scopes with no parent)."""
    rows = [(m.group(1), m.group(2), m.group(3), m.group(4))
            for m in SCOPE_RE.finditer(vvp_text)]
    module_of = {sid: mod for sid, _inst, mod, _par in rows}
    return [(inst, mod, module_of.get(par) if par else None)
            for _sid, inst, mod, par in rows]


def unframed(scopes: list[tuple[str, str, str | None]], block: str,
             wrapper: str) -> list[str]:
    """Why the bench's elaborated hierarchy does not run `block` through
    the tile's `wrapper` (empty = framed): every instance of block must sit
    inside wrapper, and at least one must."""
    hosts = [(inst, parent) for inst, mod, parent in scopes if mod == block]
    if not hosts:
        return [f"no instance of {block} at all"]
    return [f"{inst} ({block}) sits in {parent or 'the root'}, not in "
            f"{wrapper}" for inst, parent in hosts if parent != wrapper]


def elaborate(sources: list[Path], top: str, out: Path,
              flags: tuple[str, ...] = (),
              log: Path | None = None) -> str | None:
    """The bench's vvp text from a plain Icarus compile (no sim), or None
    when it does not compile - the cocotb build then reports why. `log`
    keeps what Icarus said."""
    exe = shutil.which("iverilog")
    cmd = [exe] if exe else [str(EDA_BIN), "iverilog"]
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([*cmd, "-g2012", *flags, "-o", str(out), "-s", top,
                           *map(str, sources)], capture_output=True,
                          text=True, timeout=300)
    if log is not None:
        log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not out.is_file():
        return None
    return out.read_text(encoding="utf-8", errors="replace")


def check_framed(spec: dict, sources: list[Path], top: str,
                 build_dir: Path) -> None:
    """Refuse (cosim_top_unframed) a bench whose digital top the tile's
    wrapper does not hold."""
    text = elaborate(sources, top, build_dir / "frame_check.vvp")
    if text is None:
        return
    why = unframed(scope_parents(text), spec["top"], ttlib.wrapper_name(spec))
    if why:
        frame = ttlib.SIM_FRAME_PREFIX + spec["top"]
        raise CheckError(
            f"cosim_top_unframed: {'; '.join(why)}. The tile ships "
            f"{spec['top']} behind its pad reset synchroniser, so the bench "
            f"must instantiate {frame} (same ports as {spec['top']}, built "
            "by this gate) where it instantiates the digital top")


def scan_nonconvergence(log_text: str) -> list[str]:
    hits = []
    for pat in NONCONVERGENCE_PATTERNS:
        if pat.search(log_text):
            hits.append(pat.pattern)
    # a failed gmin step counts only when no later step converged
    if simlib.gmin_unrecovered(log_text):
        hits.append("gmin stepping failed")
    return hits


def scan_ic_ignored(log_text: str) -> list[str]:
    """Node names ngspice's own sim log names as an `.ic` target it could
    not find - it does not error, only logs and carries on, so this is the
    only place a dropped initial condition is visible at all."""
    hits: list[str] = []
    for pat in IC_IGNORED_PATTERNS:
        hits.extend(m.group(1).strip() for m in pat.finditer(log_text))
    return hits


def evaluate_measures(measures: dict | None, bounds: dict,
                      condition: str | None = None,
                      measures_rel: str = MEASURES_REL,
                      label: str | None = None) -> list[dict]:
    """The pure, unit-testable half of this gate: given what the bench
    measured (or None - the file never appeared) and the bounds sidecar,
    return the violations. No workspace, no subprocess, no ngspice -
    tests/test_check_cosim.py exercises this directly for the fault shapes
    CLAUDE.md names ("a non-converging run, a bench where the digital side
    never toggled, or a missing measure") plus two evidence checks: a bench
    could otherwise write `digital_toggles: 8` as a bare literal (no
    analog side needed to produce it), or drive a purely digital counter
    with the analog bridge never actually running - `digital_toggles_unbacked`
    and `spice_time_missing`/`spice_time_behind_last_edge` close those.
    `condition` says which pad-scoped bounds are scored; `label` names a
    corner run in every finding."""
    out = []
    mark = label or ("pad" if condition == "pad" else None)
    tag = f"[{mark}] " if mark else ""

    def bad(kind, refs, msg):
        out.append(checklib.violation(
            "cosim", "error", measures_rel, None, kind,
            refs + ([mark] if mark else []), tag + msg, "check_cosim"))

    if measures is None:
        bad("measures_missing", [], f"{measures_rel} was never written - "
           "the bench did not run to the point of recording a result")
        return out

    toggles = measures.get("digital_toggles")
    edge_times = measures.get("edge_times_ns")
    edges = (edge_times if isinstance(edge_times, list) and all(
        isinstance(t, (int, float)) and not isinstance(t, bool)
        for t in edge_times) else None)

    if (not isinstance(toggles, (int, float)) or isinstance(toggles, bool)
            or toggles <= 0):
        bad("digital_side_never_toggled", [],
           f"digital_toggles is {toggles!r} - the digital side never acted "
           "on the analog waveform")
    elif edges is None or len(edges) != toggles:
        bad("digital_toggles_unbacked", [],
           f"digital_toggles is {toggles!r} but edge_times_ns is "
           f"{edge_times!r} - a toggle count must be backed by that many "
           "recorded edges, not a bare literal")

    # spice_time_reached_ns is the bench's own record of how far the analog
    # bridge's SPICE simulation actually got (cocotbext-ams's own
    # _last_sync_spice_time, updated only from the analog side's ngspice
    # thread) - evidence the analog side genuinely ran, not just that the
    # digital side toggled on its own with no real analog block behind it.
    spice_time = measures.get("spice_time_reached_ns")
    if not isinstance(spice_time, (int, float)) or isinstance(spice_time, bool):
        bad("spice_time_missing", [],
           f"spice_time_reached_ns is {spice_time!r} - no evidence the "
           "analog bridge actually ran")
    elif edges:
        last_edge = max(edges)
        if spice_time < last_edge:
            bad("spice_time_behind_last_edge", [],
               f"spice_time_reached_ns={spice_time!r} is behind the last "
               f"measured edge at {last_edge!r} ns - the analog side never "
               "reached the point the digital side claims to have toggled at")

    for name, bound in sorted(bounds.items()):
        if not padlib.scored_in(bound, condition):
            continue
        if name not in measures or measures.get(name) is None:
            bad("measure_missing", [name],
               f"{name!r} has a bound but no recorded value")
            continue
        value = measures[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            bad("measure_missing", [name],
               f"{name!r} recorded a non-numeric value: {value!r}")
            continue
        lo, hi = bound.get("min"), bound.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            bad("measure_out_of_bounds", [name],
               f"{name!r} = {value!r} is outside its bound [{lo}, {hi}]")

    return out


PAD_SUBDIR = "log/cosim_pad"


def pad_setup(ws: Path, tb_dir: Path, meta: dict) -> dict | None:
    """None for a workspace with no interface.yaml `ua_pins` (one bare run).
    Otherwise the padded analog netlist {text, block, inserted}, or
    {refused: violation} when the pad model cannot be put in: no subckt
    to wrap, no analog supply.vdd for the ESD's DVDD, or a bench that never
    names its analog netlist (the padded copy would then never be loaded,
    and the "pad" run would only be a second bare run)."""
    import yaml
    iface = yaml.safe_load((ws / "interface.yaml").read_text(
        encoding="utf-8")) or {}
    ua = iface.get("ua_pins") or {}
    if not isinstance(ua, dict):
        raise CheckError("interface.yaml 'ua_pins' must map each analog "
                         "port on a ua pad to its ua index")
    if not ua:
        return None
    pins = [str(p) for p in ua]
    apath = ws / "analog" / "spec" / "spec.yaml"
    aspec = speclib.load_spec(apath) if apath.is_file() else {}
    aspec = aspec if isinstance(aspec, dict) else {}
    vdd = (aspec.get("supply") or {}).get("vdd") \
        if isinstance(aspec.get("supply"), dict) else None
    try:
        board = padlib.board_of({**aspec, "ua_pins": pins}) \
            if "ua_pad" in aspec else {}
    except ValueError as exc:
        raise CheckError(f"{apath}: {exc}") from exc
    netlist = tb_dir / meta["analog_netlist"]
    try:
        if isinstance(vdd, bool) or not isinstance(vdd, (int, float)) \
                or vdd <= 0:
            raise padlib.PadRefused(
                "ua_pad_not_inserted", f"{apath} has no numeric "
                "'supply.vdd', and the pad's ESD diodes sit on DVDD")
        if not any(meta["analog_netlist"] in f.read_text(
                encoding="utf-8", errors="replace")
                for f in sorted(tb_dir.glob("test_*.py"))):
            raise padlib.PadRefused(
                "ua_pad_not_inserted", "no tb/test_*.py names "
                f"{meta['analog_netlist']}, so a padded copy of it would "
                "never be the netlist the bench loads")
        text, block, inserted = padlib.wrap_subckt_pads(
            netlist.read_text(encoding="utf-8"), pins,
            padlib.pad_subckt(str(layoutlib.pdk_root()), "typical",
                              float(vdd), board))
    except padlib.PadRefused as exc:
        return {"refused": checklib.violation(
            "cosim", "error", f"tb/{meta['analog_netlist']}", None,
            exc.kind, ["pad"], f"{exc} - interface.yaml puts "
            f"{', '.join(pins)} on ua pads, so cosim runs the block "
            "through the pad model too (engine/lib/padlib.py): the "
            "analog netlist must hold the block as its one outermost "
            "subckt with those ports (the subckt the bench loads, or one "
            "it instantiates), loaded by the bench from tb/",
            "check_cosim")}
    return {"text": text, "block": block, "inserted": inserted}


def run_condition(ws: Path, test_dir: Path, report_root: Path,
                  build_dir: Path, sources: list[Path], modules: list[str],
                  meta: dict, condition: str | None, results_xml: Path,
                  violations: list[dict], label: str | None = None,
                  build_args: list[str] | None = None,
                  timeout_s: float = COCOTB_TIMEOUT_S) -> dict:
    """One cocotb run of the bench from test_dir (its measures land under
    report_root, as the bench writes them beside its tb/), its findings
    appended to violations, tagged with the corner run's label or with the
    condition when it is "pad"."""
    mark = label or ("pad" if condition == "pad" else None)
    tag = f"[{mark}] " if mark else ""
    refs = [mark] if mark else []
    # A stale measures.json from a previous run must never be mistaken for
    # this run's result - if this run crashes before the bench's own
    # finally-block writes one, that must read as "missing", not "old".
    measures_path = report_root / MEASURES_REL
    measures_path.unlink(missing_ok=True)
    xml_path = cocotblib.run_cocotb(build_dir, test_dir, sources, meta["top"],
                                    modules, results_xml,
                                    timeout_s=timeout_s,
                                    stall_s=checklib.stall_s(),
                                    build_args=build_args)
    results = cocotblib.parse_results_xml(xml_path)
    if not results:
        raise CheckError(f"cocotb produced no test cases in {xml_path}")

    log_path = build_dir / "sim.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") \
        if log_path.is_file() else ""
    log_rel = str(log_path.relative_to(ws)) if mark else "log/sim.log"
    nonconv = scan_nonconvergence(log_text)
    ic_ignored = scan_ic_ignored(log_text)
    if nonconv:
        violations.append(checklib.violation(
            "cosim", "error", log_rel, None, "ngspice_non_convergence",
            refs, tag + "ngspice logged a non-convergence signature during "
            f"this run: {', '.join(sorted(set(nonconv)))}", "ngspice"))
    if ic_ignored:
        nodes = sorted(set(ic_ignored))
        violations.append(checklib.violation(
            "cosim", "error", log_rel, None, "ic_ignored", nodes + refs,
            tag + "ngspice ignored an .ic on a node that does not exist: "
            f"{', '.join(nodes)} - the initial condition it named was "
            "silently dropped, not applied", "ngspice"))

    measures = None
    if measures_path.is_file():
        try:
            measures = json.loads(measures_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CheckError(f"{measures_path} is not valid JSON: {exc}") from exc
    violations.extend(evaluate_measures(
        measures, meta["bounds"], condition,
        str(measures_path.relative_to(ws)), label))

    for name, res in sorted(results.items()):
        if res["passed"]:
            continue
        if res.get("skipped"):
            violations.append(checklib.violation(
                "cosim", "error", None, name, "test_skipped", refs,
                f"{tag}{name} was skipped - it never ran", "cocotb"))
            continue
        violations.append(checklib.violation(
            "cosim", "error", None, name, "test_failed", refs,
            f"{tag}{name} failed: {res['message'] or 'see the sim log'}",
            "cocotb"))
    return {"measures": measures, "tests_run": sorted(results),
            "signatures": sorted(set(nonconv)),
            "ic_ignored": sorted(set(ic_ignored))}


CORNER_SUBDIR = "log/cosim_corners"
WORKER_GRACE_S = 300.0
HARDEN_FINAL = "digital/harden/runs/run/final"
RUN_FACT_KEYS = ("source", "bounding", "views", "view_reason", "netlist",
                 "netlist_kind")


def not_owed(reason: str) -> dict:
    return {"owed": False, "reason": reason}


def pex_netlist(analog: Path, top: str, record: dict) -> Path:
    """The extracted netlist the recorded sweep ran on."""
    dirs = [record["log_dir"]] + [d for _r, _k, d in cosim_corners.PEX_RECORDS
                                  if d != record["log_dir"]]
    for folder in dirs:
        path = analog / folder / f"{top}.pex.ordered.spice"
        if path.is_file():
            return path
    raise CheckError(f"analog/{record['path']} records a post-layout sweep "
                     f"but no {top}.pex.ordered.spice is under analog/log/"
                     f"{{pex_sim,pex_pvt}} - run pex_sim, then cosim")


def plan_views(ws: Path, cfg: dict | None) -> tuple[list[str], str]:
    """The digital views to run and why those."""
    dpath = ws / "digital" / "spec" / "spec.yaml"
    netlist = None
    if dpath.is_file():
        dspec = speclib.load_spec(dpath)
        netlist = ws / HARDEN_FINAL / "nl" / \
            f"{ttlib.wrapper_name(dspec)}.nl.v"
    have_gl = netlist is not None and netlist.is_file()
    asked = cfg["digital"] if cfg else None
    if asked:
        if "gl" in asked and not have_gl:
            raise CheckError(f"corners.digital asks for gl but {HARDEN_FINAL}"
                             "/nl holds no netlist of the digital side - "
                             "harden the digital side first")
        return asked, "cosim_bench.json corners.digital"
    if have_gl:
        return ["gl"], "the digital side has a hardened netlist"
    return ["rtl"], "the digital side has no hardened netlist yet"


def plan_corners(ws: Path, meta: dict) -> dict:
    """What the corner runs owe for this bench: {owed: False, reason} or the
    corners, netlist and views. Refuses when the bench asks for a run its
    workspace cannot give, before any simulation time is spent."""
    cfg = meta["corners"]
    analog = ws / "analog"
    apath = analog / "spec" / "spec.yaml"
    if cfg is None:
        reason = cosim_corners.not_owed_reason(ws, meta["analog_kind"])
        if reason:
            return not_owed(reason)
    elif not apath.is_file():
        raise CheckError(f"{BENCH_META_NAME} declares corners but "
                         f"{apath} does not exist - a corner is defined "
                         "by the analog side's spec")
    record = cosim_corners.pex_record(analog)
    if cfg is not None and record is None and not cfg["list"]:
        raise CheckError(f"{BENCH_META_NAME} picks corners from the "
                         "analog side's recorded pex sweep and it has "
                         "none - run pex_sim, or name them in "
                         "corners.list")
    aspec = speclib.load_spec(apath)
    vdd = (aspec.get("supply") or {}).get("vdd") \
        if isinstance(aspec.get("supply"), dict) else None
    if isinstance(vdd, bool) or not isinstance(vdd, (int, float)):
        raise CheckError(f"{apath} has no numeric 'supply.vdd' to scale "
                         "each corner's supply from")
    if record is not None:
        netlist, kind = pex_netlist(analog, aspec["top"], record), "pex"
    else:
        netlist, kind = sim_run.find_netlist(analog), "schematic"
    if cfg and cfg["list"]:
        bounding = {n: ["named in corners.list"] for n in cfg["list"]}
        source = "corners.list"
    else:
        bounding = cosim_corners.bounding_corners(
            record["measured"], cfg["bound_by"] if cfg else None)
        source = f"analog/{record['path']}"
    sweep = {n: c for c in sim_run.spec_sweep(analog)[0]
             for n in corners_mod.names_of(c)}
    unknown = sorted(set(bounding) - set(sweep))
    if unknown:
        raise CheckError(f"corners {unknown} are not in the analog spec's "
                         f"sweep ({sorted(sweep)})")
    views, why = plan_views(ws, cfg)
    defaults = {"env": {}, "timeout_s": cosim_corners.DEFAULT_TIMEOUT_S,
                "jobs": cosim_corners.DEFAULT_JOBS}
    return {"owed": True, "source": source, "bounding": bounding,
            "views": views, "view_reason": why,
            "netlist": str(netlist.relative_to(ws)), "netlist_kind": kind,
            "corners": {n: sweep[n] for n in bounding},
            "nominal_vdd": float(vdd),
            "sizing": simlib.sizing_param_line(sim_run.load_sizing(analog)),
            **{k: (cfg or defaults)[k] for k in defaults}}


def corner_setup(pdk: Path, corner: dict, nominal_vdd: float, netlist_rel: str,
                 netlist: Path, sizing: str, out: Path) -> dict:
    """COSIM_SETUP for one corner: what to apply, and the lines that apply
    it (a bench that needs its own lines builds them from the fields)."""
    process = corner["process"]
    res = f"res_{corners_mod.passive_of(corner, 'resistor')}"
    mim = f"mimcap_{corners_mod.passive_of(corner, 'mim_cap')}"
    models = f"{pdk}/libs.tech/ngspice"
    lines = [f".include '{models}/design.spice'",
             f".lib '{models}/sm141064.spice' {process}",
             f".lib '{models}/sm141064.spice' {mim}",
             f".lib '{models}/sm141064.spice' {res}",
             f".include '{netlist}'", sizing, f".temp {corner['temp_c']}"]
    return {"corner": corner["name"], "process": process, "res_corner": res,
            "mim_corner": mim, "temp_c": corner["temp_c"],
            "vdd": corners_mod.resolve_vdd(corner, nominal_vdd),
            "netlist": netlist_rel, "netlist_path": str(netlist),
            "sizing": sizing, "lines": lines, "out": str(out)}


def generate_sdf(ws: Path, final: Path, dut: str, sdf_corner: str) -> Path:
    """An SDF for a corner the harden does not write: OpenSTA on the
    harden's own netlist, nominal SPEF and SDC with that corner's Liberty."""
    out = ws / CORNER_SUBDIR / "sdf" / sdf_corner / f"{dut}__{sdf_corner}.gen.sdf"
    lib = (layoutlib.pdk_root() / "libs.ref" / "gf180mcu_fd_sc_mcu7t5v0" /
           "lib" / ("gf180mcu_fd_sc_mcu7t5v0__"
                    f"{sdf_corner.removeprefix('nom_')}.lib"))
    netlist = final / "nl" / f"{dut}.nl.v"
    spef = final / "spef" / "nom" / f"{dut}.nom.spef"
    sdc = final / "sdc" / f"{dut}.sdc"
    missing = [str(p) for p in (lib, netlist, spef, sdc) if not p.is_file()]
    if missing:
        raise CheckError(f"no SDF for {sdf_corner} under {final}/sdf, and "
                         f"cannot generate one: missing {missing} - re-run "
                         "the digital harden")
    out.parent.mkdir(parents=True, exist_ok=True)
    tcl = out.with_suffix(".tcl")
    tcl.write_text(cosim_corners.GEN_SDF_TCL.format(
        lib=lib, netlist=netlist, top=dut, spef=spef, sdc=sdc, out=out),
        encoding="utf-8")
    proc = checklib.run_tool([str(EDA_BIN), "sta", "-no_init", "-exit",
                              str(tcl)], 600.0, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
    if proc.returncode != 0 or not out.is_file():
        raise CheckError(f"OpenSTA did not write {out}: "
                         f"{(proc.stderr or proc.stdout).strip()[-400:]}")
    return out


def gl_view(ws: Path, tb_dir: Path, framed) -> dict:
    """What every gl job shares: the bench's own Verilog (not the RTL
    tb/ links or copies in), the netlist, the cell models."""
    spec = speclib.load_spec(ws / "digital" / "spec" / "spec.yaml")
    dut = ttlib.wrapper_name(spec)
    final = ws / HARDEN_FINAL
    rtl = {p.name for p in (ws / "digital" / "rtl").glob("*")}
    bench = [f for f in collect_sources(tb_dir)
             if not f.is_symlink() and f.name not in rtl]
    cells = check_glsim._cell_sources(check_glsim._pdk_root())
    return {"dut": dut, "final": final,
            "netlist": str(final / "nl" / f"{dut}.nl.v"),
            "bench": [str(f.resolve()) for f in bench],
            "cells": [str(f) for f in cells],
            "module": f"{ttlib.SIM_FRAME_PREFIX}{spec['top']}" if framed
                      else None}


def gl_sources(ws: Path, root: Path, gl: dict, top: str) -> tuple[list[Path], Path]:
    """A gl job's sources, and the SDF as Icarus reads it. The harness takes
    the name the bench instantiates; a bench that still builds the RTL
    would simulate that and never the netlist, so that is a refusal."""
    work = root / "gl"
    work.mkdir(parents=True, exist_ok=True)
    netlist, sdf = Path(gl["netlist"]), Path(gl["sdf"])
    renames = check_glsim.escaped_dot_renames(
        sdf.read_text(encoding="utf-8", errors="replace"),
        netlist.read_text(encoding="utf-8", errors="replace"))
    isdf, inl = work / "icarus.sdf", work / "icarus.nl.v"
    check_glsim.sdf_for_icarus(sdf, isdf, renames)
    check_glsim.netlist_for_icarus(netlist, inl, renames)
    spec = speclib.load_spec(ws / "digital" / "spec" / "spec.yaml")
    harness = work / "harness.v"
    harness.write_text(ttlib.generate_glsim_harness(
        spec, sdf_path=isdf, module_name=gl["module"]), encoding="utf-8")
    sources = [*map(Path, gl["bench"]), harness, inl, *map(Path, gl["cells"])]
    # the cocotb test step overwrites sim.log, so this compile, with the
    # run's own flags, is what records the specify paths Icarus refused
    text = elaborate(sources, top, work / "check.vvp", GL_BUILD_FLAGS,
                     work / GL_BUILD_LOG)
    if text is not None and not any(
            mod == gl["dut"] for _i, mod, _p in scope_parents(text)):
        raise CheckError(f"cosim_gl_netlist_not_instantiated: {top} does not "
                         f"reach {gl['dut']}, so this run would not simulate "
                         "the netlist")
    return sources, isdf


def run_corner_job(job: dict) -> dict:
    """One corner and view, in the worker process that holds the bench's
    cocotb, Icarus and ngspice."""
    ws, root = Path(job["ws"]), Path(job["root"])
    tb = root / "tb"
    meta = load_bench_meta(tb)
    os.environ.update(job["env"])
    os.environ["COSIM_SETUP"] = json.dumps(job["setup"])
    gl = job.get("gl")
    sources, build_args, isdf = [Path(s) for s in job.get("sources", [])], None, None
    if gl:
        sources, isdf = gl_sources(ws, root, gl, meta["top"])
        build_args = list(GL_BUILD_FLAGS)
    violations: list[dict] = []
    start = time.monotonic()
    out = run_condition(ws, tb, root, root / "build", sources,
                        cocotblib.test_modules(tb), meta, job["condition"],
                        root / RESULTS_NAME, violations, job["label"],
                        build_args, job["timeout_s"])
    waived = []
    if gl:
        log = root / "build" / "sim.log"
        waived = check_glsim.check_sdf_logs(
            job["label"], isdf, root / "gl" / GL_BUILD_LOG, log)
    why = cosim_corners.corner_not_applied(out["measures"], job["setup"])
    if why:
        violations.append(checklib.violation(
            "cosim", "error", str(root.relative_to(ws)), None,
            "cosim_corner_not_applied", [job["label"]],
            f"[{job['label']}] the bench did not show it ran this corner: "
            f"{'; '.join(why)}. Read cosimlib.corner_setup() and add "
            "cosimlib.corner_evidence(setup) to the measures", "check_cosim"))
    return {**out, "violations": violations, "sdf_waived": waived,
            "wall_s": round(time.monotonic() - start, 1)}


def corner_worker(job_path: str) -> int:
    """Run one job and write its result.json; exit 0 once that is written,
    whatever the run found. No result.json at all means the worker died."""
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    try:
        result = run_corner_job(job)
    except Exception as exc:  # noqa: BLE001  (each corner reports its own)
        result = {"refused": f"{type(exc).__name__}: {exc}"}
    (Path(job["root"]) / "result.json").write_text(
        json.dumps(result), encoding="utf-8")
    return 0


def launch_corner_job(job: dict) -> dict:
    """The parent's side of a job: its result, or {refused: why} when the
    worker left none."""
    root = Path(job["root"])
    try:
        proc = checklib.run_tool(
            [sys.executable, str(Path(__file__).resolve()), "--corner-job",
             str(root / "job.json")], job["timeout_s"] + WORKER_GRACE_S,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return {"refused": f"worker {checklib.timeout_why(exc)}"}
    path = root / "result.json"
    if not path.is_file():
        return {"refused": f"worker exited {proc.returncode} with no result: "
                           f"{(proc.stderr or proc.stdout).strip()[-400:]}"}
    return json.loads(path.read_text(encoding="utf-8"))


def scalar_measures(measures: dict | None) -> dict | None:
    """The numbers of a run for the facts; edge lists stay in its log dir."""
    if measures is None:
        return None
    return {k: v for k, v in measures.items()
            if not isinstance(v, (list, dict))}


def aggregate_corner(label: str, root_rel: str, result: dict, sdf: str | None,
                     violations: list[dict]) -> dict:
    """One job's entry in the facts, its findings added to violations."""
    if "refused" in result:
        violations.append(checklib.violation(
            "cosim", "error", root_rel, None, "cosim_corner_no_result",
            [label], f"[{label}] gave no result: {result['refused']}",
            "check_cosim"))
        return {"status": "no_result", "reason": result["refused"], "sdf": sdf}
    found = result["violations"]
    violations.extend(found)
    return {"status": "fail" if found else "pass", "sdf": sdf,
            "wall_s": result["wall_s"],
            "measures": scalar_measures(result["measures"]),
            "tests_run": result["tests_run"],
            "signatures": result["signatures"],
            "sdf_waived": result["sdf_waived"],
            "violations": [v["kind"] for v in found]}


def stage_corner_bench(ws: Path, tb_dir: Path, root: Path) -> None:
    """Give one corner its own copy of the bench, with the block's design
    sources linked beside it: a bench that finds `../analog` from its own
    file reads the same block as it does from `tb/`. Only sources are
    linked, so `reports/` and the build stay this corner's own."""
    shutil.copytree(tb_dir, root / "tb",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in CORNER_LINKED_SOURCES:
        if (ws / name).exists():
            (root / name).symlink_to((ws / name).resolve())


def run_corners(ws: Path, tb_dir: Path, plan: dict, sources: list[Path],
                condition: str | None, framed, violations: list[dict]) -> dict:
    """Every corner and view the plan owes, `jobs` at a time."""
    ws = ws.resolve()
    base = ws / CORNER_SUBDIR
    shutil.rmtree(base, ignore_errors=True)
    gl = gl_view(ws, tb_dir, framed) if "gl" in plan["views"] else None
    sdf_by_corner: dict[str, Path] = {}
    jobs = []
    for name, corner in plan["corners"].items():
        for view in plan["views"]:
            label, root = f"{name}/{view}", base / view / name
            stage_corner_bench(ws, tb_dir, root)
            job = {"ws": str(ws), "root": str(root), "label": label,
                   "condition": condition, "env": plan["env"],
                   "timeout_s": plan["timeout_s"],
                   "setup": corner_setup(
                       layoutlib.pdk_root(), corner, plan["nominal_vdd"],
                       plan["netlist"], ws / plan["netlist"], plan["sizing"],
                       root / MEASURES_REL)}
            if view == "gl":
                sdf_corner = cosim_corners.sdf_corner_for(corner)
                if sdf_corner not in sdf_by_corner:
                    sdf_by_corner[sdf_corner] = (
                        cosim_corners.sdf_file(gl["final"], gl["dut"], sdf_corner)
                        or generate_sdf(ws, gl["final"], gl["dut"], sdf_corner))
                job["gl"] = {**{k: v for k, v in gl.items() if k != "final"},
                             "sdf": str(sdf_by_corner[sdf_corner])}
                job["sdf_corner"] = sdf_corner
            else:
                job["sources"] = [str(s.resolve()) for s in sources]
            (root / "job.json").write_text(json.dumps(job), encoding="utf-8")
            jobs.append(job)
    with ThreadPool(plan["jobs"]) as pool:
        results = pool.map(launch_corner_job, jobs)
    runs = {job["label"]: aggregate_corner(
                job["label"], str(Path(job["root"]).relative_to(ws)), result,
                job.get("sdf_corner"), violations)
            for job, result in zip(jobs, results)}
    return {"owed": True, **{k: plan[k] for k in RUN_FACT_KEYS}, "runs": runs}


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    tb_dir = ws / "tb"
    # invalidation.yaml's gate_inputs.msde.cosim lists `interface` first -
    # gate.py cross-checks a recorded result's input_digest against THAT
    # kind's current hash (state.py record_gate: "kinds[0]"), so this
    # report must be stamped against interface.yaml, not tb/, or every real
    # recording would raise a digest mismatch. interface.yaml also has to
    # exist by the time cosim runs regardless (P1 split precedes P3
    # integrate, docs/design.md 1.4's msde phase order).
    interface_path = ws / "interface.yaml"
    if not interface_path.is_file():
        raise CheckError(f"no interface.yaml at {interface_path} - split "
                         "must run (and pass) before cosim")
    meta = load_bench_meta(tb_dir)
    sources = collect_sources(tb_dir)
    modules = cocotblib.test_modules(tb_dir)
    if not modules:
        raise CheckError(f"no test_*.py modules under {tb_dir}")

    pads = pad_setup(ws, tb_dir, meta)
    if pads is not None and pads.get("refused"):
        payload = checklib.report(SCRIPT, interface_path, [pads["refused"]],
                                  top=meta["top"],
                                  analog_netlist=meta["analog_netlist"],
                                  analog_kind=meta["analog_kind"])
        return payload, args.out

    plan = plan_corners(ws, meta)
    ensure_ngspice_env(toolchain_root())

    build_dir = ws / BUILD_SUBDIR
    shutil.rmtree(build_dir, ignore_errors=True)
    # through the tile's pad, as the top ships it (ttlib.write_sim_frame)
    spec = frame_spec(ws)
    framed = spec and ttlib.write_sim_frame(spec, build_dir / "frame")
    if framed:
        sources = sources + framed[0]
        check_framed(spec, sources, meta["top"], build_dir)

    violations: list[dict] = []
    # no_pad: the bench as written. pad (interface.yaml ua_pins): the same
    # bench from a copy of tb/ whose analog netlist has the block behind
    # the TT ua pad model (padlib.wrap_subckt_pads)
    bare_condition = None if pads is None else "no_pad"
    bare = run_condition(ws, tb_dir, ws, build_dir, sources, modules,
                         meta, bare_condition,
                         ws / "log" / RESULTS_NAME, violations)
    extra = {}
    if pads is not None:
        root = ws / PAD_SUBDIR
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(tb_dir, root / "tb",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (root / "tb" / meta["analog_netlist"]).write_text(
            pads["text"], encoding="utf-8")
        padded = run_condition(ws, root / "tb", root, root / "build",
                               sources, modules, meta, "pad",
                               root / RESULTS_NAME, violations)
        extra = {"pad": {"block": pads["block"], "pins": pads["inserted"],
                         "netlist": f"{PAD_SUBDIR}/tb/"
                                    f"{meta['analog_netlist']}",
                         "source": padlib.SOURCE,
                         "measures": padded["measures"],
                         "tests_run": padded["tests_run"]}}
    if not plan["owed"]:
        extra["corners"] = plan
    elif violations:
        extra["corners"] = {
            "owed": True, "skipped": "the typical run already fails, so this "
            "gate is failing; fix that first and the corners run then",
            **{k: plan[k] for k in RUN_FACT_KEYS}}
    else:
        extra["corners"] = run_corners(ws, tb_dir, plan, sources,
                                       bare_condition, framed, violations)

    payload = checklib.report(SCRIPT, interface_path, violations, top=meta["top"],
                              analog_netlist=meta["analog_netlist"],
                              analog_kind=meta["analog_kind"],
                              sim_frame=framed[1] if framed else None,
                              measures=bare["measures"],
                              tests_run=bare["tests_run"],
                              ngspice_signatures=bare["signatures"],
                              ic_ignored_nodes=bare["ic_ignored"], **extra)
    return payload, args.out


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "--corner-job":
        return corner_worker(args[1])
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

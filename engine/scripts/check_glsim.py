#!/usr/bin/env python
"""check_glsim.py - the glsim gate (docs/design.md 1.5, "### M4.").

    check_glsim.py --workspace DIR [--out FILE]

Runs the SAME tb/*.py cocotb suite `sim` uses (docs/design.md section 2:
"a design can be right in RTL and wrong after synthesis") against the
hardened gate-level netlist, functional once and with SDF at three corners:

  functional - the powered netlist the tile ships
  (`harden/runs/run/final/pnl/*.pnl.v`, ttship.py), on the PDK's own cell
  models compiled with `-DFUNCTIONAL` (their own convention, `` `ifndef
  FUNCTIONAL `` around every `specify` block -
  engine/reference/tt/VENDORED.md) and `-DUSE_POWER_PINS`, zero-delay, proves
  the netlist is logically equivalent to the RTL through a real gate-level
  structural sim.

  sdf - `final/nl/*.nl.v` on the same models WITHOUT those defines, built with -gspecify and
  -ginterconnect so the specify blocks stay live, with the
  hardened design's own nominal-corner SDF back-annotated
  (`$sdf_annotate`, engine/lib/ttlib.py's `generate_glsim_harness`) -
  proves the netlist is not just logically right but free of the races an
  X-propagating read-before-write only shows up under real timing (the
  fault this gate must catch, gates.yaml's `glsim` row).

  sdf_ff, sdf_ss - the sdf pass again on the min_ff_n40C_3v60 SDF at its
  minimum delays (Icarus -Tmin) and the max_ss_125C_3v00 SDF at its maximum
  (-Tmax), the corners where a hold race or a path too long for the tb's
  clock shows (SDF_PASSES). Each corner's SDF is required; a missing one is
  a refusal. No simulator in the image runs the cells' $setup/$hold/
  $recovery checks (Icarus prints TIMING_CHECKS_UNSUPPORTED), so these
  passes prove delays, not those limits, which stay the timing gate's; the
  report's `sdf.corners` says so per pass under `timing_checks`. For now a
  test failing in these two is listed in `corner_failures`, not
  `violations` (REPORT_ONLY_PASSES says why).

The harness (`ttlib.generate_glsim_harness`) is the tt_pins map read in
reverse: a module named exactly spec['top'], the original RTL's own port
list, wrapping the hardened `tt_um_<top>` netlist - so tb/*.py drives and
observes it exactly as it drives the pre-synthesis RTL, unmodified.

The two netlists must be one design: a final/pnl whose cells and connections,
power pins aside, are not final/nl's is a `pnl_differs_from_nl` violation,
since lec and the sdf pass read one and the tile ships the other. The report's
`netlists` holds both sha256; release refuses a shipped netlist that is not
the `pnl_sha256` recorded here.

Passes when every test in tb/ passes in the functional and nominal sdf
passes (gates.yaml: "every test passes both ways"). Failure classification (the same three-way split every
M4 gate uses): no hardened netlist yet, or no PDK cell verilog/SDF for a
pass -> CheckError. Icarus/cocotb producing no results.xml at all (a build
failure, a crash before a single test ran) -> CheckError - a pass that
never ran is a refusal, never silently 2-for-2. So is an sdf pass whose
logs show Icarus omitted or could not apply the SDF (check_sdf_logs). A completed run with a
failing or skipped test, in either pass -> a `violations` finding, exit 1.

The sdf pass builds from its own copies of the netlist and SDF, with every
instance whose escaped name holds a dot (CTS names the buffer on a
flattened net `u.clk` `\\clkbuf_0_u.clk `) renamed to a plain identifier
the same way in both, because Icarus cannot find such an instance from an SDF at all
(escaped_dot_renames). The result's `sdf` facts list them as
`instances_renamed`; an SDF naming one the netlist lacks is a CheckError.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import cocotblib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
import ttship  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_glsim"
EDA_BIN = REPO / "bin" / "eda"
SDF_CORNER = "nom_tt_025C_3v30"
# The sdf pass runs once per corner, each on LibreLane's own SDF for it,
# with Icarus's -T picking which value of every (min:typ:max) triple it
# annotates: the fast corner at its minimum delays, where a hold race shows,
# the slow one at its maximum, where a path too long for the tb's clock
# does. (pass name, corner, -T value). "sdf" keeps its name from when it was
# the only one.
SDF_PASSES = (
    ("sdf", SDF_CORNER, "typ"),
    ("sdf_ff", "min_ff_n40C_3v60", "min"),
    ("sdf_ss", "max_ss_125C_3v00", "max"),
)
# The ff and ss passes run and are reported, but a test failing in them is
# a `corner_failures` entry, not yet a violation: every corpus rung's tb
# clocks at 10 ns, faster than its spec's period_ns (20-40 ns), and at ss
# counter8's ~12 ns path cannot keep up with that clock (all four passes go
# green with its tb at the spec's 20 ns). Until the benches run at the spec
# period these passes would fail the corpus on the bench, not the design.
# Moving them into `violations` is that rung's one-line change here.
REPORT_ONLY_PASSES = frozenset({"sdf_ff", "sdf_ss"})
# What Icarus says once per cell model with a $setup/$hold/$recovery check
# (and Verilator ignores them silently): no simulator in the image fires a
# timing check's notifier, so the sdf passes prove delays, never the cells'
# setup, hold or recovery limits - those are the timing gate's (STA). The
# report says so in `sdf.corners[*].timing_checks`, whether or not Icarus
# printed the warning, rather than let a pass imply it.
TIMING_CHECKS_UNSUPPORTED = "Timing checks are not supported"
# Icarus drops $sdf_annotate without -gspecify (and interconnect delays
# without -ginterconnect) and says so with the first marker; an SDF entry
# it could not apply prints the second. Either one means the "sdf" pass ran
# some or all of the design at zero delay, so it proved nothing about timing.
SDF_OMITTED = "Omitting $sdf_annotate"
SDF_ERROR = "SDF ERROR"
# Two things Icarus cannot do, both seen on the counter, and the only SDF
# errors the gate lets through:
#  - an INTERCONNECT from a tie cell (tiel/tieh) drives a constant, so there
#    is no net for the delay to sit on. Only all-zero INTERCONNECTs are
#    dropped from the copy handed to Icarus, and a zero delay loses nothing.
#  - the PDK's xor/xnor-style cells declare their paths as `ifnone` plus an
#    edge-sensitive path, which Icarus refuses at build time ("sorry: ifnone
#    with an edge-sensitive path is not supported"), so their IOPATHs have
#    nothing to match. Waived only on an instance whose cell type the build
#    log named that way, and listed in the result as `sdf_unannotated`.
ZERO_INTERCONNECT_RE = re.compile(
    r"^\s*\(INTERCONNECT\s+\S+\s+\S+"
    r"(?:\s+\(\s*0(?:\.0*)?(?::0(?:\.0*)?){0,2}\s*\))+\s*\)\s*$")
UNSUPPORTED_PATH_RE = re.compile(
    r"^(\S+?):(\d+): sorry: ifnone with an edge-sensitive path is not supported")
# An SDF error is attributed by the SDF file and line Icarus names, never by
# the instance at the end of its text: cocotb's stdout shares the sim log
# and can tear a line anywhere after that. The text is only checked to be
# (a prefix of) the unmatched-ModPath message, far enough in to tell it from
# Icarus's other "Unable to ..." errors, and, when the line is intact, to
# name the same instance the line number points at.
SDF_ERROR_AT_RE = re.compile(r"SDF ERROR: (\S+?):(\d+): (.*)$")
UNMATCHED_MODPATH = "Unable to match ModPath "
UNMATCHED_MODPATH_MIN = len("Unable to m")
UNMATCHED_MODPATH_RE = re.compile(
    r"^Unable to match ModPath \S+ -> \S+ in (\S+)\s*$")
CELLTYPE_RE = re.compile(r'\(CELLTYPE\s+"([^"]+)"\)')
INSTANCE_RE = re.compile(r"\(INSTANCE\s+([^)\s]*)\s*\)")
MODULE_RE = re.compile(r"^\s*module\s+(\w+)")
# Icarus's SDF reader drops an identifier's escapes and then splits an
# INTERCONNECT port on every '.', so `a\.b.I` looks up an instance `a`,
# finds none and aborts vvp ("NULL handle passed to vpi_scan"). No spelling
# of the name gets past that (`a\\.b.I`, `\\a.b .I` and DIVIDER `/` all
# fail the same way or worse), and an INSTANCE so named prints an SDF ERROR.
# So the sdf pass renames each such instance to a plain identifier.
# SDF_SEGMENT_RE is one level of an SDF hierarchical name (unescaped '.'
# separates levels); VERILOG_ESCAPED_RE an escaped Verilog identifier.
SDF_SEGMENT_RE = re.compile(r"(?:[A-Za-z0-9_$]|\\\S)+")
VERILOG_ESCAPED_RE = re.compile(r"\\(\S+)(?=\s)")
SIMPLE_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# Timing-driven placement can leave a buffer driving both a port and a cell,
# which the netlist writes as `assign uio_out[7] = net115;`. The SDF still
# has `(INTERCONNECT place115.Z uio_out[7] ...)`, and Icarus finds no wire
# path through an assign ("Could not find intermodpath!"). So the sdf pass
# collapses each such alias onto the port. ALIAS_ASSIGN_RE is the assign with
# a plain net on the right; a constant or a port there does not match.
ALIAS_ASSIGN_RE = re.compile(
    r"^[ \t]*assign\s+(\S+)\s*=\s*([A-Za-z_][A-Za-z0-9_$]*)\s*;[ \t]*\n",
    re.MULTILINE)
# The cells' power pins, and the module's own power ports: what final/pnl has
# and final/nl does not.
CELL_POWER_PIN_RE = re.compile(r"\.(?:VDD|VSS|VNW|VPW)\(\s*\w+\s*\)\s*,?")
INSTANCE_DECL_RE = re.compile(
    r"^\s*(\w+)\s+(\\\S+\s|\w+)\s*\(((?:\s*\..*?)?\s*)\)\s*;", re.S | re.M)
PIN_RE = re.compile(r"\.(\w+)\(\s*(.*?)\s*\)")


def netlist_cells(text: str) -> list[tuple]:
    """Every instance in a netlist as (cell, name, its pins but the power
    pins), sorted, so a powered and an unpowered write of a design compare."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    text = CELL_POWER_PIN_RE.sub(" ", text)
    return sorted((m.group(1), m.group(2).strip(),
                   tuple(sorted(PIN_RE.findall(m.group(3)))))
                  for m in INSTANCE_DECL_RE.finditer(text))


def compare_netlists(nl: Path, pnl: Path) -> str | None:
    """Why final/pnl is not final/nl with power pins added; None if it is."""
    ours, theirs = (netlist_cells(p.read_text(encoding="utf-8", errors="replace"))
                    for p in (nl, pnl))
    if ours == theirs:
        return None
    only = sorted(set(ours) ^ set(theirs))
    return (f"{len(ours)} instances in final/nl, {len(theirs)} in final/pnl, "
            f"{len(only)} differ, first {only[0][1] if only else '(duplicates)'}")


def escaped_dot_renames(sdf_text: str, nl_text: str) -> dict[str, str]:
    r"""{name: plain identifier} for each instance the SDF names with an
    escaped dot (`a\.b` in the SDF, `\a.b ` in the netlist), the plain name
    unused anywhere in the netlist. CheckError when the netlist has no
    escaped identifier of that name, since Icarus would abort on it."""
    escaped = set(VERILOG_ESCAPED_RE.findall(nl_text))
    taken = set(SIMPLE_ID_RE.findall(nl_text)) | escaped
    renames: dict[str, str] = {}
    for seg in SDF_SEGMENT_RE.findall(sdf_text):
        if "\\." not in seg:
            continue
        name = re.sub(r"\\(.)", r"\1", seg)
        if name in renames:
            continue
        if name not in escaped:
            raise CheckError(f"the SDF names instance {seg}, which the "
                             "netlist has no escaped identifier for")
        base = re.sub(r"[^A-Za-z0-9_$]", "_", name.replace(".", "__"))
        if not re.match(r"[A-Za-z_]", base):
            base = "_" + base
        plain, n = base, 0
        while plain in taken:
            n += 1
            plain = f"{base}_{n}"
        taken.add(plain)
        renames[name] = plain
    return renames


def collapse_port_aliases(text: str) -> str:
    """text, a flat netlist, with each `assign port = net;` removed and net
    replaced by port everywhere, so the driver and the net's other loads sit
    on the port itself: the same connectivity, written so Icarus can apply
    the SDF's INTERCONNECT. Only a declared `wire` collapses, and only into
    the first port assigned from it; a second port keeps its assign, and if
    the SDF has a delay to it the sdf pass still fails on it."""
    taken: set[str] = set()
    for m in ALIAS_ASSIGN_RE.finditer(text):
        lhs, net = m.group(1), m.group(2)
        decl = re.compile(rf"^[ \t]*wire\s+{re.escape(net)}\s*;[ \t]*\n",
                          re.MULTILINE)
        if net in taken or not decl.search(text):
            continue
        taken.add(net)
        text = decl.sub("", text, count=1)
        text = text.replace(m.group(0), "", 1)
        text = re.sub(rf"(?<![\\\w$.]){re.escape(net)}(?![\w$])",
                      lambda _: lhs, text)
    return text


def netlist_for_icarus(src: Path, dest: Path, renames: dict[str, str]) -> None:
    """Copy src to dest with its port aliases collapsed and each escaped
    identifier in renames replaced by its plain name. An instance shares its
    module's namespace with its nets, so an exact match on the escaped name
    is that instance."""
    text = collapse_port_aliases(
        src.read_text(encoding="utf-8", errors="replace"))
    dest.write_text(VERILOG_ESCAPED_RE.sub(
        lambda m: renames.get(m.group(1), m.group(0)), text), encoding="utf-8")


def sdf_for_icarus(src: Path, dest: Path,
                   renames: dict[str, str] | None = None) -> int:
    """Copy src to dest without its all-zero INTERCONNECT entries and with
    each escaped-dot instance name in renames replaced by its plain name;
    return how many INTERCONNECTs were dropped."""
    renames = renames or {}

    def plain(m: re.Match) -> str:
        seg = m.group(0)
        if "\\." not in seg:
            return seg
        return renames[re.sub(r"\\(.)", r"\1", seg)]

    kept, dropped = [], 0
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
        if ZERO_INTERCONNECT_RE.match(line):
            dropped += 1
            continue
        kept.append(SDF_SEGMENT_RE.sub(plain, line) if "\\." in line else line)
    dest.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return dropped


def unsupported_cells(build_log_text: str) -> set[str]:
    """Cell modules whose specify paths Icarus refused at build time, from
    the "sorry: ifnone ..." lines' file:line mapped to the enclosing
    `module`."""
    cells: set[str] = set()
    by_file: dict[str, list[tuple[int, str]]] = {}
    for line in build_log_text.splitlines():
        m = UNSUPPORTED_PATH_RE.match(line.strip())
        if not m:
            continue
        path, lineno = m.group(1), int(m.group(2))
        if path not in by_file:
            starts = []
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for i, src in enumerate(text.splitlines(), start=1):
                mm = MODULE_RE.match(src)
                if mm:
                    starts.append((i, mm.group(1)))
            by_file[path] = starts
        owner = None
        for start, name in by_file[path]:
            if start > lineno:
                break
            owner = name
        if owner:
            cells.add(owner)
    return cells


def sdf_cell_at(sdf_lines: list[str], lineno: int) -> tuple[str, str] | None:
    """(instance, celltype) of the CELL block holding 1-based line lineno of
    the SDF, when that line is an IOPATH entry; None otherwise."""
    if not 1 <= lineno <= len(sdf_lines) or "IOPATH" not in sdf_lines[lineno - 1]:
        return None
    cell = inst = None
    for line in sdf_lines[:lineno]:
        m = CELLTYPE_RE.search(line)
        if m:
            cell, inst = m.group(1), None
        m = INSTANCE_RE.search(line)
        if m and cell is not None:
            inst = m.group(1)
    return (inst, cell) if inst and cell else None


def waivable_sdf_error(line: str, sdf: Path, sdf_lines: list[str],
                       refused: set[str]) -> str | None:
    """"inst (celltype)" when this SDF ERROR line is an unmatched ModPath on
    an instance of a cell Icarus refused at build time, else None."""
    m = SDF_ERROR_AT_RE.search(line)
    if not m:
        return None
    path, lineno, rest = m.group(1), int(m.group(2)), m.group(3)
    try:
        if Path(path).resolve() != sdf.resolve():
            return None
    except OSError:
        return None
    k = 0
    while (k < len(rest) and k < len(UNMATCHED_MODPATH)
           and rest[k] == UNMATCHED_MODPATH[k]):
        k += 1
    if k < UNMATCHED_MODPATH_MIN:
        return None
    at = sdf_cell_at(sdf_lines, lineno)
    if at is None or at[1] not in refused:
        return None
    whole = UNMATCHED_MODPATH_RE.match(rest)
    if whole and whole.group(1).rsplit(".", 1)[-1] != at[0]:
        return None
    return f"{at[0]} ({at[1]})"


def check_sdf_logs(name: str, sdf: Path, build_log: Path,
                   sim_log: Path) -> list[str]:
    """CheckError when this pass's logs show the SDF was not applied, bar
    the one Icarus limitation documented above. Returns the waived
    instances as "inst (celltype)"."""
    build_text = (build_log.read_text(encoding="utf-8", errors="replace")
                  if build_log.is_file() else "")
    sim_text = (sim_log.read_text(encoding="utf-8", errors="replace")
                if sim_log.is_file() else "")
    for log, text in ((build_log, build_text), (sim_log, sim_text)):
        for line in text.splitlines():
            if SDF_OMITTED in line:
                raise CheckError(f"{name} pass: the SDF was not applied "
                                 f"({log.name}: {line.strip()})")
    sdf_lines = sdf.read_text(encoding="utf-8", errors="replace").splitlines()
    refused = unsupported_cells(build_text)
    waived = []
    for log, text in ((build_log, build_text), (sim_log, sim_text)):
        for line in text.splitlines():
            if SDF_ERROR not in line:
                continue
            ok = waivable_sdf_error(line, sdf, sdf_lines, refused)
            if ok:
                waived.append(ok)
                continue
            raise CheckError(f"{name} pass: the SDF was not fully applied "
                             f"({log.name}: {line.strip()})")
    return sorted(set(waived))


def _pdk_root() -> Path:
    proc = subprocess.run([str(EDA_BIN), "--print-toolchain-root"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CheckError(f"eda --print-toolchain-root failed: "
                         f"{proc.stderr.strip()}")
    return Path(proc.stdout.strip()) / "foss" / "pdks"


def _cell_sources(pdk_root: Path) -> list[Path]:
    verilog_dir = (pdk_root / ttlib.PDK_NAME / "libs.ref" /
                   "gf180mcu_fd_sc_mcu7t5v0" / "verilog")
    primitives = verilog_dir / "primitives.v"
    cells = verilog_dir / "gf180mcu_fd_sc_mcu7t5v0.v"
    for p in (primitives, cells):
        if not p.is_file():
            raise CheckError(f"no PDK gate-level verilog at {p}")
    return [primitives, cells]


def run_pass(name: str, ws: Path, spec: dict, final_dir: Path,
            cell_sources: list[Path], build_dir: Path,
            functional: bool, sdf: Path | None,
            delay_selection: str = "typ") -> dict:
    top = spec["top"]
    dut_module = ttlib.wrapper_name(spec)
    nl = (final_dir / "nl" / f"{dut_module}.nl.v" if sdf is not None
          else final_dir / "pnl" / f"{dut_module}.pnl.v")
    if not nl.is_file():
        raise CheckError(f"no gate-level netlist at {nl} - has the harden "
                         "gate run?")
    harness = build_dir / f"{top}_glsim_{name}.v"
    dropped, renames = 0, {}
    if sdf is not None:
        icarus_sdf = build_dir / f"{name}_icarus.sdf"
        icarus_nl = build_dir / f"{name}_icarus.nl.v"
        renames = escaped_dot_renames(
            sdf.read_text(encoding="utf-8", errors="replace"),
            nl.read_text(encoding="utf-8", errors="replace"))
        dropped = sdf_for_icarus(sdf, icarus_sdf, renames)
        netlist_for_icarus(nl, icarus_nl, renames)
        sdf, nl = icarus_sdf, icarus_nl
    try:
        harness.write_text(
            ttlib.generate_glsim_harness(spec, sdf_path=sdf), encoding="utf-8")
    except ttlib.TTError as exc:
        raise CheckError(str(exc)) from exc

    tb_dir = ws / "tb"
    modules = cocotblib.test_modules(tb_dir)
    if not modules:
        raise CheckError(f"no test_*.py modules under {tb_dir}")

    sources = [harness, nl, *cell_sources]
    defines_file = None
    # -gspecify keeps the cell models' specify blocks, the only thing
    # $sdf_annotate can write delays into; without it Icarus skips the SDF.
    build_args = (["-gspecify", "-ginterconnect", f"-T{delay_selection}"]
                  if sdf is not None else [])
    if functional:
        defines_file = build_dir / "functional_define.v"
        defines_file.write_text("`define FUNCTIONAL\n`define USE_POWER_PINS\n",
                                encoding="utf-8")
        sources = [defines_file, *sources]

    from cocotb_tools.runner import get_runner
    runner = get_runner("icarus")
    build_log = build_dir / f"build_{name}.log"
    sim_log = build_dir / f"sim_{name}.log"
    pass_build_dir = build_dir / f"build_{name}"
    results_xml = build_dir / f"results_{name}.xml"
    runner.build(sources=[str(s) for s in sources], hdl_toplevel=top,
                build_dir=str(pass_build_dir), waves=False,
                timescale=("1ns", "1ps"), log_file=str(build_log),
                build_args=build_args)
    try:
        runner.test(hdl_toplevel=top, test_module=modules, test_dir=str(tb_dir),
                   build_dir=str(pass_build_dir), results_xml=str(results_xml),
                   waves=False, log_file=str(sim_log))
    except SystemExit:
        pass
    waived = (check_sdf_logs(name, sdf, build_log, sim_log)
              if sdf is not None else [])
    if not Path(results_xml).is_file():
        raise CheckError(f"{name} pass: cocotb produced no {results_xml} - "
                         "the build or the run never completed")
    results = cocotblib.parse_results_xml(Path(results_xml))
    if not results:
        raise CheckError(f"{name} pass: cocotb produced no test cases in "
                         f"{results_xml}")
    notes = {"zero_interconnects_dropped": dropped,
             "sdf_unannotated": waived,
             "instances_renamed": renames}
    if sdf is not None:
        logs = "".join(f.read_text(encoding="utf-8", errors="replace")
                       for f in (build_log, sim_log) if f.is_file())
        notes["timing_checks"] = (
            "not simulated" + (f' (Icarus: "{TIMING_CHECKS_UNSUPPORTED}")'
                               if TIMING_CHECKS_UNSUPPORTED in logs else ""))
    return results, notes


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint first")
    if not spec.get("tt_pins"):
        raise CheckError("spec.yaml has no 'tt_pins' mapping")

    final_dir = ws / "harden" / "runs" / "run" / "final"
    if not final_dir.is_dir():
        raise CheckError(f"no {final_dir} - the harden gate has not run yet")

    dut_module = ttlib.wrapper_name(spec)
    sdfs = {}
    for name, corner, _sel in SDF_PASSES:
        sdfs[name] = final_dir / "sdf" / corner / f"{dut_module}__{corner}.sdf"
        if not sdfs[name].is_file():
            raise CheckError(f"no SDF at {sdfs[name]} - the {name} pass "
                             f"needs the {corner} corner")

    pdk_root = _pdk_root()
    cell_sources = _cell_sources(pdk_root)

    build_root = ws / "log" / "glsim_build"
    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True, exist_ok=True)

    violations = []
    all_results = {}
    notes = {}
    corners = {}
    corner_failures = []
    for name, functional, sdf_path, sel in (
        ("functional", True, None, None),
        *((n, False, sdfs[n], s) for n, _c, s in SDF_PASSES),
    ):
        results, sdf_notes = run_pass(name, ws, spec, final_dir, cell_sources,
                                      build_root, functional, sdf_path,
                                      sel or "typ")
        if name == "sdf":
            notes = dict(sdf_notes)
        if sdf_path is not None:
            corners[name] = {"corner": sdf_path.parent.name,
                             "delay_selection": sel, **sdf_notes}
        all_results[name] = {k: v["passed"] for k, v in results.items()}
        for tname, res in sorted(results.items()):
            if res["passed"]:
                continue
            kind = "test_skipped" if res.get("skipped") else "test_failed"
            if name in REPORT_ONLY_PASSES:
                corner_failures.append({
                    "glsim_pass": name, "test": tname, "kind": kind,
                    "msg": res["message"] or f"see log/glsim_build/sim_{name}.log"})
                continue
            violations.append(checklib.violation(
                "glsim", "error", None, tname, kind, [],
                f"[{name}] {tname}: "
                f"{res['message'] or f'see log/glsim_build/sim_{name}.log'}",
                "cocotb", glsim_pass=name))

    nl = final_dir / "nl" / f"{dut_module}.nl.v"
    pnl = final_dir / "pnl" / f"{dut_module}.pnl.v"
    differs = compare_netlists(nl, pnl)
    if differs:
        violations.append(checklib.violation(
            "glsim", "error", str(pnl.relative_to(ws)), None,
            "pnl_differs_from_nl", [],
            f"the powered netlist is not the netlist with power pins: {differs}",
            "check_glsim"))
    payload = checklib.report(SCRIPT, ws / "harden", violations, top=top,
                              results=all_results,
                              sdf={**notes, "corners": corners},
                              corner_failures=corner_failures,
                              netlists={"nl_sha256": ttship._sha256(nl),
                                        "pnl_sha256": ttship._sha256(pnl)})
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

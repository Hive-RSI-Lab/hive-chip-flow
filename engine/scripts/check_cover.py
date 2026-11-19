#!/usr/bin/env python
"""check_cover.py - the cover gate (docs/design.md 1.5, "### M3.").

    check_cover.py --workspace DIR [--out FILE]

Runs the SAME cocotb tests `sim` runs (tb/test_*.py), but over the VERILATOR
simulator instead of Icarus, built with `--coverage --coverage-line
--coverage-toggle --coverage-fsm --coverage-expr`. `verilator_coverage --write-info` (an lcov-shaped .info:
`DA:<line>,<count>` for line hits, `BRDA:<line>,<branch>,<label>,<count>`
for both real branches AND verilator's own toggle points - the two are told
apart here by the label's own shape: a toggle label always ends
`<signal>:0->1`/`<signal>:1->0`, a real branch label never does) is the
parsed source, not the raw `coverage.dat` (a packed, escaped key format not
meant for hand-parsing) and not `--annotate`'s text report (a rendering,
not data).

Two things this gate had to prove empirically before trusting either
number, both now load-bearing here:
  - `bin/eda python3 ...` did not put verilator's own driver script on PATH
    at all (only `eda verilator ...`'s top-level dispatch did) until this
    milestone's fix to bin/eda's `python3|python)` case - a cocotb-over-
    verilator run from inside a check script is new at M3, nothing before
    it needed this path.
  - `eda verilator_coverage ...` used to always exec the `verilator`
    COMPILER driver regardless of which of the three names
    (verilator|verilator_coverage|verilator_gantt) was asked for - a latent
    bug in bin/eda's verilator_entry/dispatch (verilator_coverage is its
    own perl driver, with its own dbg binary, not a flag to `verilator`
    itself) that this milestone's fix also closes, for the same reason: no
    gate needed the real tool before this one.

Pass criteria (gates.yaml `cover` row): line coverage >= `line_min` (spec.yaml
`cover.line_min`, default 95%), toggle coverage >= `cover.toggle_min`
(default 90%), restricted to the DUT's own rtl/ file(s) - a testbench or
cocotb support file is never instrumented in the first place (only rtl/
sources are handed to `--coverage`), so no extra filtering is needed for
that half; multiple rtl/ files are each counted by their own `SF:` block.
`rtl/cover_exclude.yaml` (optional; a list of `{file, line, reason}` for a
line and/or `{file, line, label, reason}` for one toggle edge) removes
exactly the named point from both the covered and total counts - "exclusions
carry reasons" (gates.yaml) mirrors check_lint.py's own `lint_allow.yaml`.

State machines and conditions (#80): line and toggle saturate early - a
test can visit every state of a state machine and flip every bit of its
register while never taking one transition, or run an `if (a && b)` line
without ever letting `b` decide it. Two more counts close that, both read
from `coverage.dat` itself (copied to log/coverage.dat), because
`--write-info` drops the state register's name and the reset mark from
them:
  - fsm: every state Verilator found in each state machine and every
    transition it extracted, `cover.fsm_min` (default 100%). Each missed
    one is a `fsm_state_not_covered`/`fsm_arc_not_covered` finding, labeled
    as `coverage.dat` labels it (`top.state::RUN->IDLE`).
  - expr: every term of each logical expression that alone decides its
    value (`(b==0) => 0` for `a && b`), `cover.expr_min` (default 90%).
    Verilator counts a term, not every combination: `(b==0) => 0` is
    covered by a=0,b=0 too. Each missed one is `expr_not_covered`.
Verilator 5.052 extracts a transition only from a plain if/else or ?: per
state: an else-if chain's transitions are not counted. A state with no
transition out of it other than reset is named in a `fsm_arcs_not_seen`
info finding, so a missing count is never mistaken for coverage; line and
branch coverage still see that code. The same `rtl/cover_exclude.yaml`
entry (`file`, `line`, `label`, `reason`) excludes one of these points.

Requirement cover points (#80b): the build also takes `--coverage-user`,
so every SV `cover property` and immediate `cover` is counted - in rtl/
itself, or in `cover/*.sv`, files of cover modules `bind`-ed into the
DUT that only this Verilator build reads (sim's Icarus and simdiff never
see them, and synthesis never does). A spec.yaml requirement names its
points with `cover: [LABEL, ...]`, each the statement label of a cover
(`WRAP: cover property (...)`). The gate fails a requirement whose label
no visible test ever hit (`requirement_cover_not_hit`, naming the
requirement, the label and the instance), or that no cover statement
carries at all (`requirement_cover_label_missing`): a label that names
nothing would otherwise pass for free. A label bound into several
instances must be hit in each. cover_exclude.yaml never removes a
requirement's cover point. A cover no requirement names is counted
(`user_pct`) and a missed one is an info `cover_point_not_hit`.

Holdout merged (#80c): when holdout/ has tests, they run once more on the
same coverage build and `verilator_coverage --write` sums the two
coverage.dat files into log/coverage_merged.dat (log/coverage_merged.info
its --write-info). The report's `merged` block carries the merged counts
and percentages (line, toggle, fsm, expr, user) and nothing else: no
holdout point, label or test name. Every top-level count, every finding
and the verdict stay tb-only, so a point only a holdout test reaches still
fails the gate (the fixer must cover it from tb/). `merged` is null when
the block has no holdout tests.

Fault this gate must catch (gates.yaml): "an unreachable state" - a branch
or line the visible tb/ suite never reaches, dragging line coverage below
`line_min`.

simdiff (#86): the same tests, same seed, same sources and top, are run
once more over Icarus, and each test's pass/fail must match the Verilator
run's. A test that passes on one simulator and fails (or is missing) on the
other is a `sim_mismatch` error naming the test: the result hangs on the
simulator's event order, which is a race (a blocking write another
always block reads on the same edge, say) and gate-level sim and silicon
won't pick one order for you. The report's `simdiff` carries each test's
pass/fail and wall seconds per simulator, and each simulator's whole run
(build plus tests) wall and CPU seconds. CPU is not per test: cocotb runs
every test inside one simulator process.
"""
from __future__ import annotations

import argparse
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import cocotblib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

import yaml  # noqa: E402

SCRIPT = "check_cover"
EDA_BIN = REPO / "bin" / "eda"
BUILD_SUBDIR = "log/cover_build"
RESULTS_NAME = "cover_results.xml"
ICARUS_BUILD_SUBDIR = "log/cover_icarus_build"
ICARUS_RESULTS_NAME = "cover_icarus_results.xml"
SIMULATORS = ("icarus", "verilator")
INFO_NAME = "log/coverage.info"
DAT_NAME = "log/coverage.dat"
HOLDOUT_SUBDIR = "holdout"
HOLDOUT_RESULTS_NAME = "cover_holdout_results.xml"
HOLDOUT_DAT_NAME = "log/coverage_holdout.dat"
MERGED_DAT_NAME = "log/coverage_merged.dat"
MERGED_INFO_NAME = "log/coverage_merged.info"
EXCLUDE_REL = "rtl/cover_exclude.yaml"
COVER_DIR = "cover"  # bind files of SV cover points, Verilator cover only
DEFAULT_LINE_MIN = 95.0
DEFAULT_TOGGLE_MIN = 90.0
DEFAULT_FSM_MIN = 100.0
DEFAULT_EXPR_MIN = 90.0
# A wall ceiling only, ten times the old fixed limit: the tool is ended
# sooner when it stops making progress (checklib.STALL_S).
TIMEOUT_S = 2400.0

TOGGLE_LABEL_RE = re.compile(r":[01]->[01]$")
DA_RE = re.compile(r"^DA:(\d+),(-?\d+)")
BRDA_RE = re.compile(r"^BRDA:(\d+),(\d+),([^,]+),(-|\d+)")
SF_RE = re.compile(r"^SF:(.+)$")
# coverage.dat: `C '<\x01key\x02value...>' count`, one point per line
DAT_RE = re.compile(r"^C '(.*)' (\d+)\s*$")
DAT_KINDS = {"fsm_state": "fsm", "fsm_arc": "fsm", "expr": "expr",
             "user": "user"}


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def collect_cover_sources(ws: Path) -> list[Path]:
    """cover/*.sv and *.v: cover modules bound into the DUT."""
    d = ws / COVER_DIR
    if not d.is_dir():
        return []
    return sorted(d.glob("*.v")) + sorted(d.glob("*.sv"))


def load_excludes(ws: Path) -> dict[str, dict]:
    """{'line': {(file, line): reason}, 'toggle': {(file, line, label): reason}}
    - a labeled entry names one toggle, fsm or expr point (labels never
    collide across the three kinds)."""
    out = {"line": {}, "toggle": {}}
    p = ws / EXCLUDE_REL
    if not p.is_file():
        return out
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise CheckError(f"{p} must be a YAML list of exclusions")
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or not entry.get("reason") \
                or not entry.get("file") or not entry.get("line"):
            raise CheckError(f"{p}[{i}] must have 'file', 'line' and 'reason'")
        key_file, line = entry["file"], int(entry["line"])
        if entry.get("label"):
            out["toggle"][(key_file, line, entry["label"])] = entry["reason"]
        else:
            out["line"][(key_file, line)] = entry["reason"]
    return out


def run_coverage(ws: Path, top: str, sources: list[Path],
                 seed: int | None = None,
                 cover_sources: list[Path] | None = None,
                 holdout_seed: int | None = None) -> tuple[Path, Path | None]:
    """cocotb over the verilator simulator, --coverage instrumented.
    `runner.test()`'s own `test_dir` is where the compiled simulation
    binary actually RUNS (proved empirically - not `ws`, whatever cwd this
    script itself was launched from, or `build_dir`), so `coverage.dat` (a
    runtime artifact verilator writes to its OWN process cwd, never a path
    you hand it) lands under tb_dir, not ws. It is always removed again
    before returning, success or failure: tb_dir is `dir_text`-hashed as
    the `tb` artifact kind (invalidation.yaml) for sim/mutate/holdout too,
    and a stray coverage.dat left sitting in tb/ would flip THEIR recorded
    input hash on every cover run, marking them stale for a reason that has
    nothing to do with the design under test.

Returns (tb-only info path, holdout coverage.dat or None). When holdout/
has tests they run once more on the same build (holdout_seed, no pad
frame: the same top the tb run drives, so the two .dat files share their
points) and their coverage.dat is moved to log/, out of holdout/ (a
hashed artifact, like tb/)."""
    from cocotb_tools.runner import get_runner

    build_dir = ws / BUILD_SUBDIR
    shutil.rmtree(build_dir, ignore_errors=True)
    tb_dir = ws / "tb"
    modules = cocotblib.test_modules(tb_dir)
    if not modules:
        raise CheckError(f"no test_*.py modules under {tb_dir}")

    coverage_dat = tb_dir / "coverage.dat"
    coverage_dat.unlink(missing_ok=True)
    # log_file=, on BOTH calls: without it cocotb_tools.runner's own
    # Simulator._execute runs verilator/the compiled sim binary with
    # stdout=None, inheriting THIS script's own stdout fd - the same fd
    # checklib.emit prints the JSON report to (cocotblib.run_cocotb's own
    # docstring names this exact failure mode for check_sim.py/check_
    # holdout.py; this gate is not exempt just because it drives verilator
    # instead of Icarus).
    log_file = str(build_dir / "cover.log")
    try:
        runner = get_runner("verilator")
        try:
            runner.build(sources=[str(s) for s in
                                  list(sources) + list(cover_sources or [])],
                        hdl_toplevel=top,
                        build_dir=str(build_dir), always=True,
                        build_args=["--coverage", "--coverage-line",
                                    "--coverage-toggle", "--coverage-fsm",
                                    "--coverage-expr", "--coverage-user",
                                    # a construct --coverage-fsm/-expr cannot
                                    # model (a state reached only through
                                    # `default`, say) is left uncounted, not a
                                    # build failure: line and toggle still
                                    # count it, and the lint gate owns warnings
                                    "-Wno-COVERIGN"], log_file=log_file)
            # A failing cocotb TEST is never this gate's concern (that is
            # `sim`'s job) - only coverage is, and coverage.dat is written
            # regardless of whether the test's own assertions held. No
            # special handling is needed for that here: proved empirically
            # that a genuinely-failing test, run the same way gate.py runs
            # this script for real (a plain subprocess, not nested in
            # pytest), returns from runner.test() normally with coverage.dat
            # written - cocotb_tools.runner.test() only ever calls
            # sys.exit() on a failed test when PYTEST_CURRENT_TEST is set,
            # which is true only when THIS SCRIPT is itself running nested
            # inside pytest (i.e. this gate's own test suite), never for a
            # real run. A previous `except SystemExit: pass` here existed
            # only to paper over that pytest-only artifact and did nothing
            # for a real run - removed rather than kept as dead code that
            # described a production behavior it did not actually have.
            runner.test(hdl_toplevel=top, test_module=modules,
                       test_dir=str(tb_dir), build_dir=str(build_dir),
                       results_xml=str(ws / "log" / RESULTS_NAME),
                       log_file=log_file, seed=seed)
        except Exception as exc:  # noqa: BLE001 - a build/launcher crash
            raise CheckError(f"cocotb-over-verilator coverage run failed: "
                             f"{type(exc).__name__}: {exc}") from exc

        if not coverage_dat.is_file():
            raise CheckError(f"no {coverage_dat} - the coverage-"
                             "instrumented run never wrote one (a crashed "
                             "launcher or a build that produced no "
                             "coverage binary)")
        info = write_coverage_info(ws, coverage_dat)
        shutil.copyfile(coverage_dat, ws / DAT_NAME)
        coverage_dat.unlink()

        holdout_dir = ws / HOLDOUT_SUBDIR
        holdout_modules = cocotblib.test_modules(holdout_dir) \
            if holdout_dir.is_dir() else []
        if not holdout_modules:
            (ws / HOLDOUT_DAT_NAME).unlink(missing_ok=True)
            return info, None
        holdout_coverage_dat = holdout_dir / "coverage.dat"
        holdout_coverage_dat.unlink(missing_ok=True)
        try:
            try:
                runner.test(hdl_toplevel=top, test_module=holdout_modules,
                            test_dir=str(holdout_dir),
                            build_dir=str(build_dir),
                            results_xml=str(ws / "log" /
                                            HOLDOUT_RESULTS_NAME),
                            log_file=str(build_dir / "cover_holdout.log"),
                            seed=holdout_seed)
            except Exception as exc:  # noqa: BLE001
                raise CheckError(
                    "cocotb-over-verilator holdout coverage run failed: "
                    f"{type(exc).__name__}") from exc
            if not holdout_coverage_dat.is_file():
                raise CheckError("the holdout coverage run wrote no "
                                 "coverage.dat")
            shutil.copyfile(holdout_coverage_dat, ws / HOLDOUT_DAT_NAME)
        finally:
            holdout_coverage_dat.unlink(missing_ok=True)
        return info, ws / HOLDOUT_DAT_NAME
    finally:
        coverage_dat.unlink(missing_ok=True)


def merge_coverage(ws: Path, dats: list[Path]) -> tuple[Path, Path]:
    """`verilator_coverage --write` the points of several coverage.dat
    files summed into log/coverage_merged.dat, then `--write-info` it to
    log/coverage_merged.info. Returns (merged .dat, merged .info)."""
    merged = ws / MERGED_DAT_NAME
    proc = checklib.run_tool(
        [str(EDA_BIN), "verilator_coverage", "--write", str(merged)]
        + [str(d) for d in dats],
        cwd=str(ws), capture_output=True, text=True, encoding="utf-8",
        errors="replace", wall=TIMEOUT_S)
    if proc.returncode != 0 or not merged.is_file():
        raise CheckError(
            f"verilator_coverage --write exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-2000:]}")
    return merged, write_coverage_info(ws, merged, MERGED_INFO_NAME)


def merged_counts(info_path: Path, dat_path: Path, rtl_names: set[str],
                  cover_names: set[str], excludes: dict[str, dict],
                  requirements: list) -> dict:
    """Counts and percentages only over the tb+holdout merge: the
    findings are dropped, so no holdout point, label or test reaches the
    report, and the gate's verdict stays the tb-only one."""
    _, c = count_coverage(parse_info(info_path), rtl_names, excludes,
                          0.0, 0.0)
    points = parse_dat(dat_path)
    _, fe = count_fsm_expr(points, rtl_names, excludes, 0.0, 0.0)
    _, req = count_requirement_covers(points, rtl_names | cover_names,
                                      requirements)
    out = {"line_pct": checklib.rnd(c["line_pct"]),
           "line_covered": c["line_covered"], "line_total": c["line_total"],
           "toggle_pct": checklib.rnd(c["toggle_pct"]),
           "toggle_covered": c["toggle_covered"],
           "toggle_total": c["toggle_total"]}
    for group in ("fsm", "expr", "user"):
        for k in ("pct", "covered", "total"):
            key = f"{group}_{k}"
            src = req if group == "user" else fe
            if key in src:
                out[key] = src[key]
    return out


def timed(fn, *args, **kwargs):
    """(fn's result, {"wall_s", "cpu_s"}): CPU is the user+system time of
    the child processes fn waited on (the simulator build and run), which
    is where cocotb's work happens."""
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    out = fn(*args, **kwargs)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    return out, {"wall_s": checklib.rnd(time.monotonic() - t0),
                 "cpu_s": checklib.rnd(cpu)}


def run_icarus(ws: Path, top: str, sources: list[Path],
               seed: int | None) -> Path:
    """The same tests over Icarus, with the same sources, top and seed as
    run_coverage's Verilator run, for simdiff. Its results never decide
    sim's verdict: sim runs its own (framed, monitored) bench."""
    tb_dir = ws / "tb"
    build_dir = ws / ICARUS_BUILD_SUBDIR
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        return cocotblib.run_cocotb(
            build_dir, tb_dir, sources, top, cocotblib.test_modules(tb_dir),
            ws / "log" / ICARUS_RESULTS_NAME, seed=seed, simulator="icarus")
    except Exception as exc:  # noqa: BLE001 - a build/launcher crash
        raise CheckError(f"cocotb-over-icarus simdiff run failed: "
                         f"{type(exc).__name__}: {exc}") from exc


def _state(res: dict | None) -> str:
    if res is None:
        return "missing"
    if res.get("skipped"):
        return "skipped"
    return "pass" if res.get("passed") else "fail"


def simdiff(results: dict[str, dict[str, dict]]) -> tuple[list[dict], dict]:
    """results: {simulator: parse_results_xml output}. Returns (findings,
    {test: {simulator: {"result", "wall_s"}}}): a `sim_mismatch` error per
    test whose result differs between simulators (missing counts)."""
    names = sorted(set().union(*(r.keys() for r in results.values())))
    if not names:
        raise CheckError("simdiff: neither simulator ran any test")
    table: dict[str, dict] = {}
    violations = []
    for name in names:
        row = {sim: {"result": _state(res.get(name)),
                     "wall_s": (res.get(name) or {}).get("wall_s")}
               for sim, res in results.items()}
        table[name] = row
        states = {sim: r["result"] for sim, r in row.items()}
        if len(set(states.values())) > 1:
            said = ", ".join(f"{s} {v}" for s, v in states.items())
            violations.append(checklib.violation(
                "cover", "error", None, name, "sim_mismatch", [name],
                f"test {name} gives a different result on each simulator "
                f"({said}): its outcome hangs on event order - a race, such "
                "as a blocking write another always block reads on the same "
                "edge", "simdiff", test=name, results=states))
    return violations, table


def write_coverage_info(ws: Path, coverage_dat: Path,
                        info_name: str = INFO_NAME) -> Path:
    """`verilator_coverage --write-info` on its own - split out from
    run_coverage() so a launcher failure here (bin/eda missing, exit 127,
    EDA_TOOLCHAIN pointed nowhere) is testable without a real cocotb+
    verilator build in front of it."""
    info_path = ws / info_name
    info_path.parent.mkdir(parents=True, exist_ok=True)
    proc = checklib.run_tool(
        [str(EDA_BIN), "verilator_coverage", "--write-info",
         str(info_path), str(coverage_dat)],
        cwd=str(ws), capture_output=True, text=True, encoding="utf-8",
        errors="replace", wall=TIMEOUT_S)
    if proc.returncode != 0 or not info_path.is_file():
        raise CheckError(
            f"verilator_coverage --write-info exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-2000:]}")
    return info_path


def parse_info(info_path: Path) -> dict[str, dict]:
    """{source_file: {'line': {lineno: count}, 'toggle': {(lineno, label): count}}}"""
    out: dict[str, dict] = {}
    cur = None
    for raw in info_path.read_text(encoding="utf-8").splitlines():
        m = SF_RE.match(raw)
        if m:
            cur = out.setdefault(m.group(1), {"line": {}, "toggle": {}})
            continue
        if cur is None:
            continue
        m = DA_RE.match(raw)
        if m:
            cur["line"][int(m.group(1))] = int(m.group(2))
            continue
        m = BRDA_RE.match(raw)
        if m and TOGGLE_LABEL_RE.search(m.group(3)):
            count = 0 if m.group(4) == "-" else int(m.group(4))
            cur["toggle"][(int(m.group(1)), m.group(3))] = count
    if not out:
        raise CheckError(f"{info_path} carries no SF:/DA: records - "
                         "verilator_coverage produced an empty report")
    return out


def parse_dat(dat_path: Path) -> list[dict]:
    """The fsm, expr and user (SV cover) points of a Verilator coverage.dat:
    [{"kind" ("fsm_state"|"fsm_arc"|"expr"|"user"), "file", "line", "label",
    "count", "var", "from", "to", "reset", "hier"}]. var/from/to/reset are
    set on fsm points only (Fv/Ff/Ft/Fg keys); label is the point's own `o`
    key (a cover's statement label), with a reset arc's `[reset]` mark
    kept; hier is the `h` key (`TOP.counter8.u_cov.WRAP`)."""
    points = []
    for raw in dat_path.read_text(encoding="utf-8",
                                  errors="replace").splitlines():
        m = DAT_RE.match(raw)
        if not m:
            continue
        keys = {}
        for part in m.group(1).split("\x01"):
            k, sep, v = part.partition("\x02")
            if sep:
                keys[k] = v
        kind = keys.get("t")
        if kind not in DAT_KINDS or "f" not in keys or "l" not in keys:
            continue
        points.append({
            "kind": kind, "file": keys["f"], "line": int(keys["l"]),
            "label": keys.get("o", ""), "count": int(m.group(2)),
            "var": keys.get("Fv"), "from": keys.get("Ff"),
            "to": keys.get("Ft"), "reset": keys.get("Fg") == "reset",
            "hier": keys.get("h", "")})
    return points


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--seed", type=int, help="cocotb random seed, overriding "
                    "spec sim.seed and the one drawn from the bench (replay)")
    args = ap.parse_args(argv)

    # Resolved to absolute FIRST (M5, found by actually running this gate
    # against a relative --workspace): write_coverage_info's own `cwd=
    # str(ws)` combined with an unresolved-relative `info_path`/
    # `coverage_dat` (both built from this same `ws`) doubles the
    # workspace's own relative prefix under the subprocess's cwd and the
    # file is never found there. Absolute throughout sidesteps this
    # regardless of what the caller passed.
    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    sources = collect_sources(ws)
    cover_sources = collect_cover_sources(ws)
    cover_cfg = spec.get("cover") or {}
    line_min = float(cover_cfg.get("line_min", DEFAULT_LINE_MIN))
    toggle_min = float(cover_cfg.get("toggle_min", DEFAULT_TOGGLE_MIN))
    fsm_min = float(cover_cfg.get("fsm_min", DEFAULT_FSM_MIN))
    expr_min = float(cover_cfg.get("expr_min", DEFAULT_EXPR_MIN))
    excludes = load_excludes(ws)

    # the same bench as sim, so coverage counts sim's own draw
    seed = cocotblib.sim_seed(spec, ws / "tb", args.seed)
    holdout_seed = cocotblib.sim_seed(spec, ws / HOLDOUT_SUBDIR, args.seed) \
        if (ws / HOLDOUT_SUBDIR).is_dir() else None
    (info_path, holdout_dat), verilator_cost = timed(
        run_coverage, ws, top, sources, seed, cover_sources, holdout_seed)
    per_file = parse_info(info_path)
    rtl_names = {f.name for f in sources}
    cover_names = {f.name for f in cover_sources}

    violations, counts = count_coverage(per_file, rtl_names, excludes,
                                        line_min, toggle_min)
    points = parse_dat(ws / DAT_NAME)
    more, fe_counts = count_fsm_expr(points, rtl_names, excludes, fsm_min,
                                     expr_min)
    violations += more
    more, req_counts = count_requirement_covers(
        points, rtl_names | cover_names, spec.get("requirements") or [])
    violations += more

    merged = None
    if holdout_dat is not None:
        merged_dat, merged_info = merge_coverage(
            ws, [ws / DAT_NAME, holdout_dat])
        merged = merged_counts(merged_info, merged_dat, rtl_names,
                               cover_names, excludes,
                               spec.get("requirements") or [])

    icarus_xml, icarus_cost = timed(run_icarus, ws, top, sources, seed)
    diffs, per_test = simdiff({
        "icarus": cocotblib.parse_results_xml(icarus_xml),
        "verilator": cocotblib.parse_results_xml(ws / "log" / RESULTS_NAME)})
    violations += diffs

    payload = checklib.report(
        SCRIPT, ws / "rtl", violations, top=top, seed=seed,
        replay=cocotblib.replay_command(SCRIPT, seed),
        line_pct=checklib.rnd(counts["line_pct"]),
        toggle_pct=checklib.rnd(counts["toggle_pct"]),
        line_covered=counts["line_covered"], line_total=counts["line_total"],
        toggle_covered=counts["toggle_covered"],
        toggle_total=counts["toggle_total"],
        **fe_counts, **req_counts, merged=merged,
        simdiff={"agree": not diffs, "tests": per_test,
                 "runs": {"icarus": icarus_cost,
                          "verilator": verilator_cost}})
    return payload, args.out


def count_coverage(per_file: dict[str, dict], rtl_names: set[str],
                   excludes: dict[str, dict], line_min: float,
                   toggle_min: float) -> tuple[list[dict], dict]:
    """The aggregation logic on its own - a pure function over parse_info()'s
    own output, so the exclusion/threshold math is testable without a real
    verilator+cocotb build (that build is what makes this gate's own tests
    slow; tests/check.sh has a two-minute budget, docs/design.md 1.1, and
    this is the part of the gate that does NOT need a real toolchain run to
    exercise)."""
    line_total = line_covered = toggle_total = toggle_covered = 0
    violations = []
    for sf, data in per_file.items():
        name = Path(sf).name
        if name not in rtl_names:
            continue  # cocotb/verilator support code, not the DUT itself
        for lineno, count in sorted(data["line"].items()):
            reason = excludes["line"].get((name, lineno))
            if reason:
                continue
            line_total += 1
            if count > 0:
                line_covered += 1
            else:
                # "info", not "error": gates.yaml's own pass criteria is a
                # PERCENTAGE floor ("line at or above 95%"), not zero misses
                # allowed - a single miss is visible here (and would need
                # rtl/cover_exclude.yaml's own reason to stop showing up at
                # all) but only the aggregate threshold below fails the gate.
                violations.append(checklib.violation(
                    "cover", "info", name, None, "line_not_covered", [],
                    f"{name}:{lineno} was never executed by tb/", "verilator",
                    line=lineno))
        for (lineno, label), count in sorted(data["toggle"].items()):
            reason = excludes["toggle"].get((name, lineno, label))
            if reason:
                continue
            toggle_total += 1
            if count > 0:
                toggle_covered += 1
            else:
                violations.append(checklib.violation(
                    "cover", "info", name, None, "toggle_not_covered", [],
                    f"{name}:{lineno} {label} never toggled", "verilator",
                    line=lineno))

    if line_total == 0:
        raise CheckError("no line-coverage points found for the DUT's own "
                         "rtl/ file(s) - an empty coverage report is a "
                         "refusal, never a pass")
    line_pct = 100.0 * line_covered / line_total
    toggle_pct = 100.0 * toggle_covered / toggle_total if toggle_total else 100.0

    if line_pct < line_min:
        violations.append(checklib.violation(
            "cover", "error", None, None, "line_coverage_below_threshold", [],
            f"line coverage {line_pct:.1f}% ({line_covered}/{line_total}) is "
            f"below {line_min:g}%", "verilator"))
    if toggle_pct < toggle_min:
        violations.append(checklib.violation(
            "cover", "error", None, None, "toggle_coverage_below_threshold", [],
            f"toggle coverage {toggle_pct:.1f}% ({toggle_covered}/"
            f"{toggle_total}) is below {toggle_min:g}%", "verilator"))

    return violations, {
        "line_pct": line_pct, "toggle_pct": toggle_pct,
        "line_covered": line_covered, "line_total": line_total,
        "toggle_covered": toggle_covered, "toggle_total": toggle_total,
    }


def count_fsm_expr(points: list[dict], rtl_names: set[str],
                   excludes: dict[str, dict], fsm_min: float,
                   expr_min: float) -> tuple[list[dict], dict]:
    """fsm and expr counts over parse_dat()'s points, pure like
    count_coverage(). A design with no state machine (or no logical
    expression) has nothing to miss there: its pct is null and its
    threshold does not apply."""
    tally = {"fsm": [0, 0], "expr": [0, 0]}  # [covered, total]
    missed = {"fsm": [], "expr": []}
    violations = []
    arcs_out: dict[tuple, set] = {}   # (file, var) -> states with an arc out
    states: dict[tuple, dict] = {}    # (file, var) -> {state: line}
    for p in points:
        name = Path(p["file"]).name
        if name not in rtl_names or p["kind"] == "user":
            continue
        if p["kind"] == "fsm_state":
            states.setdefault((name, p["var"]), {})[p["to"]] = p["line"]
        elif p["kind"] == "fsm_arc" and not p["reset"]:
            arcs_out.setdefault((name, p["var"]), set()).add(p["from"])
        if excludes["toggle"].get((name, p["line"], p["label"])):
            continue
        group = DAT_KINDS[p["kind"]]
        tally[group][1] += 1
        if p["count"] > 0:
            tally[group][0] += 1
            continue
        missed[group].append(f"{name}:{p['line']} {p['label']}")
        if p["kind"] == "fsm_state":
            kind, what = "fsm_state_not_covered", f"state {p['label']} was " \
                "never entered"
        elif p["kind"] == "fsm_arc":
            kind, what = "fsm_arc_not_covered", f"transition {p['label']} " \
                "was never taken"
        else:
            kind, what = "expr_not_covered", f"condition term {p['label']} " \
                "never decided its expression"
        violations.append(checklib.violation(
            "cover", "info", name, None, kind, [],
            f"{name}:{p['line']} {what} by tb/", "verilator",
            line=p["line"], label=p["label"]))

    for (name, var), found in sorted(states.items()):
        blind = sorted(s for s in found
                       if s != "ANY" and s not in arcs_out.get((name, var), ()))
        if blind:
            violations.append(checklib.violation(
                "cover", "info", name, None, "fsm_arcs_not_seen", [],
                f"{name}: Verilator extracted no transition out of state(s) "
                f"{', '.join(blind)} of {var} (an else-if chain or nested ?:, say), so "
                "fsm coverage does not count them; line coverage still "
                "does", "verilator", line=found[blind[0]], fsm=var,
                states=blind))

    out = {}
    for group, floor in (("fsm", fsm_min), ("expr", expr_min)):
        covered, total = tally[group]
        pct = 100.0 * covered / total if total else None
        out[f"{group}_pct"] = checklib.rnd(pct) if pct is not None else None
        out[f"{group}_covered"], out[f"{group}_total"] = covered, total
        if pct is not None and pct < floor:
            names = "; ".join(missed[group][:5])
            more = len(missed[group]) - 5
            violations.append(checklib.violation(
                "cover", "error", None, None,
                f"{group}_coverage_below_threshold", [],
                f"{group} coverage {pct:.1f}% ({covered}/{total}) is below "
                f"{floor:g}%; missed: {names}"
                + (f" and {more} more" if more > 0 else ""), "verilator",
                missed=missed[group]))
    return violations, out


def count_requirement_covers(points: list[dict], names: set[str],
                             requirements: list) -> tuple[list[dict], dict]:
    """SV cover points against the requirements that name them, pure like
    count_fsm_expr(). A requirement's `cover: [LABEL]` passes when every
    instance of every label was hit at least once."""
    by_label: dict[str, list[dict]] = {}
    for p in points:
        if p["kind"] == "user" and Path(p["file"]).name in names:
            by_label.setdefault(p["label"], []).append(p)
    violations = []
    named: set[str] = set()
    per_req: dict[str, dict] = {}
    for req in requirements:
        if not isinstance(req, dict) or not req.get("cover"):
            continue
        rid = str(req.get("id"))
        labels = req["cover"]
        if isinstance(labels, str):
            labels = [labels]
        row = per_req.setdefault(rid, {})
        for label in labels:
            label = str(label)
            named.add(label)
            found = by_label.get(label, [])
            row[label] = sum(p["count"] for p in found)
            if not found:
                violations.append(checklib.violation(
                    "cover", "error", None, None,
                    "requirement_cover_label_missing", [rid],
                    f"requirement {rid} names cover point {label}, but no "
                    f"`{label}: cover ...` statement exists in rtl/ or "
                    f"{COVER_DIR}/", "verilator", label=label))
                continue
            for p in found:
                if p["count"] > 0:
                    continue
                fname = Path(p["file"]).name
                violations.append(checklib.violation(
                    "cover", "error", fname, None,
                    "requirement_cover_not_hit", [rid],
                    f"requirement {rid}: cover point {label} "
                    f"({p['hier'] or fname}, {fname}:{p['line']}) was never "
                    "hit by tb/", "verilator", line=p["line"], label=label,
                    hier=p["hier"]))
    covered = total = 0
    for label, found in sorted(by_label.items()):
        for p in found:
            total += 1
            if p["count"] > 0:
                covered += 1
            elif label not in named:
                fname = Path(p["file"]).name
                violations.append(checklib.violation(
                    "cover", "info", fname, None, "cover_point_not_hit", [],
                    f"{fname}:{p['line']} cover point {label} "
                    f"({p['hier']}) was never hit by tb/", "verilator",
                    line=p["line"], label=label, hier=p["hier"]))
    return violations, {
        "user_pct": checklib.rnd(100.0 * covered / total) if total else None,
        "user_covered": covered, "user_total": total,
        "requirement_covers": per_req}


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

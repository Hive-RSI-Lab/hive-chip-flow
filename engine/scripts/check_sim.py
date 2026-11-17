#!/usr/bin/env python
"""check_sim.py - the sim gate (docs/design.md 1.5, "### M2.").

    check_sim.py --workspace DIR [--out FILE]

Builds rtl/ and runs every `tb/test_*.py` module as one cocotb regression
over Icarus (engine/lib/cocotblib.py). Passes when every test passes AND
every requirement whose spec.yaml `check` is `sim` or `both` has at least
one test tagged with it (a `# req: ID` comment above the test - cocotblib's
convention for "every test carries the requirement ids it covers", docs/
design.md section 2).

Runs in-process (this script is always reached through `eda python ...`,
docs/design.md 1.2 - the interpreter already has cocotb/cocotb_tools on its
path and Icarus on PATH via the shim directory, so no bin/eda subprocess is
needed here the way check_lint.py needs one for verilator).

Faults this gate must catch (gates.yaml): "counter wraps one early" (an RTL
bug a real test catches) and, named separately in M2's done criteria, "a
requirement without a test fails sim". A bench that ignores the pad's reset
latency fails too, as `tb_ignores_pad_latency` rather than `test_failed`, so
it reaches the tb-writer and not the rtl fixer (cocotblib.ignore_pad_latency).

Seeds (#83): every run is at one pinned seed (--seed, else spec sim.seed,
else a digest of the bench). Spec `sim.seeds: K` (or --seeds K) then runs
the same build at K more seeds (cocotblib.extra_seeds); a test that passed
at the pinned seed and fails at another is `test_failed`, naming that seed
and its replay line. --seed alone is a replay: one run, no extra seeds.

Waves (#93): when tests fail, only those tests run again with FST on (in
log/sim_build/waves/, at the seed they failed at), `eda fst2vcd` converts
the dump, and each `test_failed` finding gets `window`: the block's ports
and the signals the test reads, sampled at the last N rising clock edges
before the failure (wavelib.py), plus `waves` (the VCD) and `failure_ns`
(`chip-flow wave VCD --signals .. --at failure_ns` widens it). Every test_failed
also gets `signature`: the assert's file:line and its message with numbers
masked, so cluster_violations groups one assert failing in several tests.
A passing run writes no waves. --window N sets the edges (0: no re-run).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import cocotblib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
import wavelib  # noqa: E402
import xmonlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_sim"
BUILD_SUBDIR = "log/sim_build"
RESULTS_NAME = "sim_results.xml"
WINDOW_CYCLES = 16      # clock edges in a failing test's waves window
EXTRA_SIGNALS = 8       # signals the test reads, beyond the block's ports


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def run(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--seed", type=int, help="cocotb random seed, overriding "
                    "spec sim.seed and the one drawn from the bench (replay)")
    ap.add_argument("--seeds", type=int, help="extra seeds to run after the "
                    "pinned one, overriding spec sim.seeds (default: 0 with "
                    "--seed, so a replay is one run)")
    ap.add_argument("--window", type=int, default=WINDOW_CYCLES,
                    help="clock edges in a failing test's waves window "
                    f"(default {WINDOW_CYCLES}; 0 skips the waves re-run)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    sources = collect_sources(ws)
    tb_dir = ws / "tb"
    modules = cocotblib.test_modules(tb_dir)
    if not modules:
        raise CheckError(f"no test_*.py modules under {tb_dir}")
    seed = cocotblib.sim_seed(spec, tb_dir, args.seed)
    replay = cocotblib.replay_command(SCRIPT, seed)
    n_extra = cocotblib.sim_seeds(
        spec, 0 if args.seed is not None and args.seeds is None
        else args.seeds)
    tags = cocotblib.scan_requirement_tags(tb_dir)
    tagged_ids: set[str] = set().union(*tags.values()) if tags else set()
    required = cocotblib.required_ids(spec, checks=("sim", "both"))

    violations = []
    failing: list[tuple[int, str, dict, dict]] = []   # seed, test, finding, res
    for rid in sorted(required - tagged_ids):
        violations.append(checklib.violation(
            "sim", "error", "spec/spec.yaml", None, "requirement_no_test",
            [rid], f"requirement {rid} has no test tagged with it (a "
            f"'# req: {rid}' comment above the @cocotb.test() that covers "
            "it)", "cocotblib"))

    build_dir = ws / BUILD_SUBDIR
    shutil.rmtree(build_dir, ignore_errors=True)
    # through the tile's pad, as glsim runs it (ttlib.write_sim_frame):
    # a bench sees the same pad reset latency in both
    hdl_top = top
    block_sources = sources
    frame = ttlib.write_sim_frame(spec, build_dir / "frame")
    if frame:
        sources, hdl_top = sources + frame[0], frame[1]
    results_xml = ws / "log" / RESULTS_NAME
    # the X/Z output monitor (xmonlib, #84): Icarus' optimistic X would
    # hide an output register no reset reaches until gate-level sim
    xmon, xmon_src, xmon_args = xmonlib.write_monitor(
        spec, hdl_top, build_dir / "xmon",
        latency=ttlib.pad_reset_latency(spec) if frame else 0)
    sources = sources + xmon_src
    xml_path = cocotblib.run_cocotb(build_dir, tb_dir, sources, hdl_top, modules,
                                    results_xml, seed=seed,
                                    build_args=xmon_args)
    results = cocotblib.parse_results_xml(xml_path)
    if not results:
        raise CheckError(f"cocotb produced no test cases in {xml_path}")
    x_found = xmonlib.findings(build_dir / "sim.log", "sim",
                               name_test=True)
    ignored = set()
    if frame:
        ignored = cocotblib.ignore_pad_latency(
            results, build_dir, tb_dir, block_sources, top, modules, [tb_dir],
            seed=seed)

    for name, res in sorted(results.items()):
        if res["passed"]:
            continue
        refs = sorted(tags.get(name, []))
        if res.get("skipped"):
            violations.append(checklib.violation(
                "sim", "error", None, name, "test_skipped", refs,
                f"{name} was skipped - it never ran, so it cannot cover "
                "the requirement(s) it is tagged with", "cocotb"))
            continue
        if name in ignored:
            violations.append(checklib.violation(
                "sim", "error", None, name, "tb_ignores_pad_latency", refs,
                f"{name} fails through the tile's pad and passes driving the "
                "block directly, and the bench never reads the pad's reset "
                f"latency ({ttlib.pad_reset_latency(spec)} clocks after "
                "release): reset through padreset.pad_reset (engine/lib/"
                f"padreset.py). Failure: {res['message'] or 'see the sim log'}",
                "cocotb"))
            continue
        v = checklib.violation(
            "sim", "error", None, name, "test_failed", refs,
            f"{name} failed at seed {seed}: "
            f"{res['message'] or 'see the sim log'} (replay: {replay})",
            "cocotb")
        violations.append(v)
        failing.append((seed, name, v, res))

    violations += x_found

    # multi-seed regression (#83, spec sim.seeds): the same build at K more
    # seeds. A test that passed at the pinned seed and fails at another is
    # a failure only some draws reach; each is reported with its seed and
    # the line that replays it. Tests already failing above are not
    # repeated.
    extra = cocotblib.extra_seeds(seed, n_extra)
    seed_failures: dict[str, list[str]] = {}
    for s in extra:
        xml_s = cocotblib.run_cocotb(
            build_dir, tb_dir, sources, hdl_top, modules,
            build_dir / "seeds" / f"results_{s}.xml", seed=s,
            build_args=xmon_args)
        res_s = cocotblib.parse_results_xml(xml_s)
        if not res_s:
            raise CheckError(f"cocotb produced no test cases at seed {s} "
                             f"in {xml_s}")
        for name, res in sorted(res_s.items()):
            if res["passed"] or res.get("skipped") \
                    or not results.get(name, {}).get("passed"):
                continue
            seed_failures.setdefault(str(s), []).append(name)
            v = checklib.violation(
                "sim", "error", None, name, "test_failed",
                sorted(tags.get(name, [])),
                f"{name} passed at seed {seed} and failed at extra seed "
                f"{s}: {res['message'] or 'see the sim log'} (replay: "
                f"{cocotblib.replay_command(SCRIPT, s)})", "cocotb")
            violations.append(v)
            failing.append((s, name, v, res))

    for s, name, v, res in failing:
        v["signature"] = failure_signature(name, res, tb_dir)
    if failing and args.window > 0:
        attach_waves(failing, spec, build_dir / "waves", tb_dir, sources,
                     hdl_top, modules, xmon_args, args.window)

    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this gate ("rtl" for sim) - the whole workspace would
    # never match that and silently fail every real recording.
    payload = checklib.report(SCRIPT, ws / "rtl", violations, top=top,
                              tests_run=sorted(results),
                              tests_passed=sum(1 for r in results.values()
                                               if r["passed"]),
                              seed=seed, replay=replay,
                              extra_seeds=extra, seed_failures=seed_failures,
                              x_monitor=xmonlib.facts(xmon))
    return payload, args.out


def failure_signature(name: str, res: dict, tb_dir: Path) -> str:
    """The assert's tb file:line (innermost traceback frame under tb/) and
    its message with numbers masked; the test's name stands in for the
    location when the traceback names no tb line."""
    f, line = wavelib.assert_location(res.get("traceback") or "", tb_dir)
    msg = (res.get("message") or "").strip()
    if f is None:
        return wavelib.signature(None, None, f"{name}: {msg}")
    return wavelib.signature(f, line, msg)


def _source_line(path, line) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()[line - 1]
    except (OSError, TypeError, IndexError, ValueError):
        return ""


def attach_waves(failing, spec, wave_dir: Path, tb_dir: Path, sources,
                 hdl_top, modules, build_args, cycles: int) -> None:
    """Re-run each failing test with FST on, at the seed it failed at, and
    put its waves window on its finding (`window`, `waves`), or say in
    `window_note` why there is none. Never changes the gate's verdict: the
    findings already stand; this only shows what the design did."""
    ports = list(spec.get("ports") or {})
    doms = (spec.get("clock") or {}).get("domains") or []
    clock = next((d for d in doms if d in ports), None)
    by_seed: dict[int, list] = {}
    for item in failing:
        by_seed.setdefault(item[0], []).append(item)
    for s, items in sorted(by_seed.items()):
        d = wave_dir / f"seed_{s}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        try:
            notes = _waves_for_seed(items, s, d, ports, clock, tb_dir,
                                    sources, hdl_top, modules, build_args,
                                    cycles)
        except (CheckError, OSError, ValueError,
                cocotblib.CocotbTimeout) as exc:
            notes = {name: f"no waves: {exc}" for _, name, _, _ in items}
        for _, name, v, _ in items:
            if name in notes:
                v["window_note"] = notes[name]


def _candidates(name: str, res: dict, tb_dir: Path) -> list[str]:
    """Names that may be signals for one failing test: the `dut.X` it
    reads, then the identifiers in its assert line and message."""
    import re
    f, line = wavelib.assert_location(res.get("traceback") or "", tb_dir)
    src = ""
    if f:
        try:
            src = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            src = ""
    text = " ".join([_source_line(f, line), res.get("message") or ""])
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return list(dict.fromkeys(wavelib.test_reads(src, name) + words))


def _waves_for_seed(items, s, d, ports, clock, tb_dir, sources, hdl_top,
                    modules, build_args, cycles) -> dict[str, str]:
    names = [name for _, name, _, _ in items]
    xml = cocotblib.run_cocotb(d, tb_dir, sources, hdl_top, modules,
                               d / "results.xml", seed=s,
                               build_args=build_args, waves=True,
                               testcase=names)
    rerun = cocotblib.parse_results_xml(xml)
    fst = d / f"{hdl_top}.fst"
    if not fst.is_file():
        return {n: "no waves: the re-run wrote no FST dump" for n in names}
    vcd_path = wavelib.fst_to_vcd(fst, d / "waves.vcd")
    wanted = {name: [c for c in _candidates(name, res, tb_dir)
                     if c not in ports] for _, name, _, res in items}
    leaves = set(ports) | {c for cs in wanted.values() for c in cs}
    vcd = wavelib.parse_vcd(vcd_path,
                            keep=lambda p: p.rsplit(".", 1)[-1] in leaves)
    scope = wavelib.block_scope(vcd, ports)
    if scope is None or clock is None:
        why = ("no scope in the dump holds every spec port" if scope is None
               else "the spec's clock.domains names none of its ports")
        return {n: f"no waves window: {why} (dump: {vcd_path})"
                for n in names}
    notes = {}
    for _, name, v, res in items:
        r = rerun.get(name) or {}
        if r.get("passed", True) or r.get("sim_time_ns") is None:
            notes[name] = ("no waves window: this test passed when re-run "
                           "alone, so its failure needs the tests run "
                           "before it (state, or the random draws they "
                           f"used); dump: {vcd_path}")
            continue
        sigs = [p for p in ports if p != clock]
        extra = [p for c in wanted[name]
                 if (p := _find_var(vcd, scope, c)) and p not in sigs]
        sigs += list(dict.fromkeys(extra))[:EXTRA_SIGNALS]
        win = wavelib.window(vcd, scope, clock, sigs,
                             r["sim_time_ns"] * 1000.0, cycles)
        v["window"] = wavelib.format_window(win)
        v["waves"] = str(vcd_path)
        v["failure_ns"] = win["failure_ns"]
    return notes


def _find_var(vcd, scope: str, leaf: str) -> str | None:
    """leaf as a var of scope, else the shallowest var named leaf below
    it (a register inside the block), as a path relative to scope."""
    if leaf in vcd.scope_vars(scope):
        return leaf
    pre = scope + "."
    hits = sorted((p for p in vcd.vars
                   if p.startswith(pre) and p.rsplit(".", 1)[-1] == leaf),
                  key=lambda p: p.count("."))
    return hits[0][len(pre):] if hits else None


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

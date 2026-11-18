#!/usr/bin/env python
"""check_reset_indep.py - the reset_indep gate (docs/design.md 1.5, #84).

    check_reset_indep.py --workspace DIR [--out FILE] [--seed N]

Runs the visible tb/ suite (the same cocotb tests `sim` runs) on Verilator,
built with `--x-assign unique --x-initial unique`, once with every
uninitialised variable set to zero (`+verilator+rand+reset+0`) and then
once per init seed with each one set at random (`+verilator+rand+reset+2
+verilator+seed+N`, seeds INIT_SEEDS). The cocotb seed stays pinned
(cocotblib.sim_seed), so the stimulus is the same draw in every run and
only the power-up state of the design changes.

Why it exists: Icarus starts every reg at x, and an `if (x)` takes the else
branch, so a register the reset never sets can still pass `sim` when its x
only reaches the outputs through a condition. Silicon powers up at 0 or 1,
so the same design can stick on a real chip. A design whose behaviour after
reset depends on its power-up state is the fault; this gate shows it as a
test that passes at zero init and fails at a random one.

Findings:
  - `reset_dependent` (error): the test passes at zero init and fails at
    the named init seed(s). The fix is to reset the register, not to change
    the bench.
  - `verilator_baseline_failed` (warning): the test fails on Verilator even
    at zero init, so it is left out of the comparison (Icarus and Verilator
    disagree on it for a reason this gate does not judge; `sim` owns test
    failures).
A test that is skipped in any run is left out the same way, as info.

Fault this gate must catch (gates.yaml): "a register with no reset whose x
only reaches the output through an if" (corpus counter8
reset_indep_unreset_mode).

The compiled simulation runs with tb/ as its working directory; anything
it leaves there is removed again before returning, since tb/ is hashed as
the `tb` input of sim, cover and the others (see check_cover.py).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import cocotblib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_reset_indep"
CHECK = "reset_indep"
BUILD_SUBDIR = "log/reset_indep_build"
INIT_SEEDS = (1, 2, 3)
BUILD_ARGS = ["--x-assign", "unique", "--x-initial", "unique"]


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def init_plusargs(init_seed: int | None) -> list[str]:
    """None is the zero-init baseline; an int is a random init seed."""
    if init_seed is None:
        return ["+verilator+rand+reset+0"]
    return ["+verilator+rand+reset+2", f"+verilator+seed+{init_seed}"]


def run_suite(ws: Path, top: str, sources: list[Path], seed: int,
              init_seeds=INIT_SEEDS) -> dict:
    """{None: results, seed: results, ...}: cocotblib.parse_results_xml of
    the zero-init run and of each random-init run. One build serves all."""
    from cocotb_tools.runner import get_runner

    ws = ws.resolve()
    build_dir = ws / BUILD_SUBDIR
    shutil.rmtree(build_dir, ignore_errors=True)
    tb_dir = ws / "tb"
    modules = cocotblib.test_modules(tb_dir)
    if not modules:
        raise CheckError(f"no test_*.py modules under {tb_dir}")
    # log_file= on every call keeps the simulator's output off this
    # script's stdout, where the JSON report goes (see run_cocotb).
    log_file = str(build_dir / "reset_indep.log")
    before = set(tb_dir.iterdir())
    out: dict = {}
    try:
        runner = get_runner("verilator")
        try:
            runner.build(sources=[str(s) for s in sources], hdl_toplevel=top,
                        build_dir=str(build_dir), always=True,
                        build_args=BUILD_ARGS, log_file=log_file)
        except Exception as exc:  # noqa: BLE001 - a build crash
            raise CheckError(f"verilator build failed: {type(exc).__name__}: "
                             f"{exc}") from exc
        for init in (None, *init_seeds):
            name = "zero" if init is None else f"seed{init}"
            xml = build_dir / f"results_{name}.xml"
            try:
                runner.test(hdl_toplevel=top, test_module=modules,
                           test_dir=str(tb_dir), build_dir=str(build_dir),
                           results_xml=str(xml), log_file=log_file,
                           seed=seed, plusargs=init_plusargs(init))
            except SystemExit:
                # runner.test() exits 1 on a failed test only when nested
                # in pytest; results_xml is written in full by then.
                pass
            except Exception as exc:  # noqa: BLE001 - a launcher crash
                raise CheckError(f"verilator run ({name} init) failed: "
                                 f"{type(exc).__name__}: {exc}") from exc
            if not xml.is_file():
                raise CheckError(f"the {name}-init run wrote no {xml.name}")
            out[init] = cocotblib.parse_results_xml(xml)
    finally:
        for p in set(tb_dir.iterdir()) - before:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    return out


def compare(runs: dict, tags: dict[str, set[str]] | None = None
            ) -> list[dict]:
    """The findings from run_suite's results, on their own (hermetic tests
    feed this directly). runs[None] is the zero-init baseline."""
    tags = tags or {}
    base = runs[None]
    seeds = [k for k in runs if k is not None]
    violations: list[dict] = []
    for test in sorted(base):
        res = base[test]
        refs = tags.get(test, set())
        if res.get("skipped") or any(runs[s].get(test, {}).get("skipped")
                                     for s in seeds):
            violations.append(checklib.violation(
                CHECK, "info", None, test, "test_skipped", refs,
                f"{test} was skipped, so its reset independence was not "
                "checked", "verilator"))
            continue
        if not res["passed"]:
            violations.append(checklib.violation(
                CHECK, "warning", None, test, "verilator_baseline_failed",
                refs, f"{test} fails on Verilator even with every register "
                f"at zero ({res.get('message') or 'no message'}), so it is "
                "left out of the comparison", "verilator"))
            continue
        bad = [s for s in seeds if not runs[s].get(test, {}).get("passed")]
        if bad:
            first = runs[bad[0]].get(test) or {}
            why = ((first.get("message") or "").splitlines() or
                   ["missing from the run's results"])[0]
            violations.append(checklib.violation(
                CHECK, "error", None, test, "reset_dependent", refs,
                f"{test} passes with every register at zero but fails with "
                f"random power-up values (init seed "
                f"{', '.join(map(str, bad))}: {why}): some state the reset "
                "does not set reaches the outputs", "verilator",
                init_seeds=bad))
    return violations


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--seed", type=int, help="cocotb random seed, overriding "
                    "spec sim.seed and the one drawn from the bench (replay)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    sources = collect_sources(ws)
    seed = cocotblib.sim_seed(spec, ws / "tb", args.seed)
    runs = run_suite(ws, top, sources, seed)
    if not runs[None]:
        raise CheckError("the zero-init run reported no tests - an empty "
                         "run is a refusal, never a pass")
    violations = compare(runs, cocotblib.scan_requirement_tags(ws / "tb"))
    payload = checklib.report(
        SCRIPT, ws / "rtl", violations, top=top, seed=seed,
        init_seeds=list(INIT_SEEDS),
        replay=cocotblib.replay_command(SCRIPT, seed),
        tests=len(runs[None]))
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())

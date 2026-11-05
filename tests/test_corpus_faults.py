"""Every vde, ade and msde corpus rung's planted faults, run end to end
through faults.py's own run_rung: the untouched reference passes every gate
its faults/manifest.yaml names, and each planted fault is caught with the
expected finding (docs/design.md section 3). tests/smoke-faults.sh does
the same for vde by hand; these are marked slow, so tests/check-slow.sh
(and its CI job) runs them, not tests/check.sh. Only the holdout check of
references no manifest reaches is fast (seconds) and runs there. Measured unmarked on
2026-10-15: the seven ade and msde rungs run here took about 45 s of CPU
together (comparator 25 s, mirror 11 s, bandgap 7 s, the rest under 4 s
each), and vde riscv 28 s, uart 25 s, spi_fifo 2 s.

HARDEN_RUNGS are the rungs whose run needs full LibreLane hardens (the
msde rungs three to five top hardens each: the clean reference, then each
plant that hardens the untouched top first; vde counter8 one harden per
M4 plant, plus mutate twice). They are marked `harden_faults` as well as
`slow`: check-engine.sh's `not slow` keeps them out of check.sh,
check-slow.sh's `slow and not harden_faults` keeps them out of its 1800 s
cap, and tests/check-harden-faults.sh runs them, one CI job per rung (the
`harden-faults` matrix in .github/workflows/harden-faults.yml, which a fast test
below keeps equal to HARDEN_RUNGS).

HELD_BACK lists the rungs not run in either lane yet, each with why. The
GOALS.md goal "Run every ade and msde rung's planted faults on CI" is done
when it names no ade or msde rung, and "Run every vde rung's planted
faults on CI" when it names no vde rung, each with both lanes' CI jobs
passing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
CORPUS = REPO / "corpus"
SKILLS = ("vde", "ade", "msde")
WORKFLOW = REPO / ".github" / "workflows" / "harden-faults.yml"

HARDEN_RUNGS: dict[tuple[str, str], str] = {
    ("vde", "counter8"): "harden, timing, drc, lvs, glsim, lec and precheck's "
                         "plants each run a full LibreLane harden",
    ("msde", "dac_tile"): "top_harden, top_lvs and precheck each need a full "
                          "LibreLane top harden",
    ("msde", "sensor_counted"): "top_harden, top_drc and top_lvs each need a "
                                "full LibreLane top harden",
}
HELD_BACK: dict[tuple[str, str], str] = {}


def _rungs() -> list[tuple[str, str]]:
    return [(m.parts[-4], m.parts[-3]) for skill in SKILLS
            for m in sorted((CORPUS / skill).glob("*/faults/manifest.yaml"))]


def test_held_back_and_harden_rungs_name_only_real_rungs():
    for name, rungs in (("HELD_BACK", HELD_BACK),
                        ("HARDEN_RUNGS", HARDEN_RUNGS)):
        stale = sorted(set(rungs) - set(_rungs()))
        assert not stale, f"{name} entries with no corpus manifest: {stale}"
    both = sorted(set(HELD_BACK) & set(HARDEN_RUNGS))
    assert not both, f"held back and run in the harden lane: {both}"


def test_harden_faults_ci_matrix_runs_every_harden_rung():
    import yaml

    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    matrix = jobs["harden-faults"]["strategy"]["matrix"]["rung"]
    want = sorted(rung for _, rung in HARDEN_RUNGS)
    assert sorted(matrix) == want, (
        f"harden-faults.yml matrix {sorted(matrix)} != HARDEN_RUNGS "
        f"{want}: a rung missing there never runs on CI")


def _holdouts_no_rung_runs() -> list[Path]:
    """Reference designs whose held-out tests no fault run reaches: run_rung
    runs only the gates a manifest names, and a side's own directory (an msde
    rung's digital/) has no manifest at all."""
    unreached = []
    for holdout in sorted(CORPUS.glob("**/holdout")):
        manifest = holdout.parent / "faults" / "manifest.yaml"
        if not (manifest.exists() and "gate: holdout" in manifest.read_text()):
            unreached.append(holdout.parent)
    return unreached


@pytest.mark.parametrize(
    "design", _holdouts_no_rung_runs(),
    ids=lambda d: str(d.relative_to(CORPUS)))
def test_reference_passes_its_own_holdout(tmp_path, design):
    """A reference failing its own held-out test went unseen on main (#207)."""
    import check_holdout
    import faults

    ws = faults.make_scratch_workspace(tmp_path, design, "vde", design.name)
    payload, _out = check_holdout.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload


def _run(tmp_path, skill, rung):
    import faults
    import gate as gate_mod

    gates = gate_mod.load_gates(gate_mod.DEFAULT_GATES)
    result = faults.run_rung(tmp_path, CORPUS / skill / rung, skill, gates)
    assert not result["problems"], result


@pytest.mark.slow
@pytest.mark.parametrize("skill, rung", [
    r for r in _rungs() if r not in HELD_BACK and r not in HARDEN_RUNGS])
def test_rung_catches_every_planted_fault(tmp_path, skill, rung):
    _run(tmp_path, skill, rung)


@pytest.mark.slow
@pytest.mark.harden_faults
@pytest.mark.parametrize("skill, rung", [
    r for r in _rungs() if r in HARDEN_RUNGS and r not in HELD_BACK])
def test_harden_rung_catches_every_planted_fault(tmp_path, skill, rung):
    _run(tmp_path, skill, rung)

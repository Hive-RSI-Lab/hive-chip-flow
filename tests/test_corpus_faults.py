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

import json
import os
import shutil
import subprocess
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

# The corpus step runs as parallel CI jobs, one per shard: bin/chip-flow
# test SHARD runs one shard's rungs (pytest --corpus-shard), each under the
# same 1800 s cap, and .github/workflows/test.yml's `shard` matrix runs every
# shard (a fast test below keeps the two equal). Every rung runs whole in
# REST_SHARD except the SPLIT_RUNGS, too slow for one job: their pieces
# (faults.rung_parts: each gate the untouched reference passes, then each
# fault by name) are grouped into shards of their own, and a fast test
# below holds that every piece is in exactly one group, so a fault added
# to the manifest is never left out of CI. Measured on this box
# (2026-11-08), the comparator rung took 295 s: its reference's Monte
# Carlo 157 s, its other reference gates 40 s and its 13 faults 100 s; on
# CI the whole rung took 1120 s, and 1340 s on a slow runner, against the
# step's 1800 s cap. Its Monte Carlo now runs alone and the rest of the
# rung beside it, each shard under half the cap on a slow runner.
REST_SHARD = "rest"
SPLIT_RUNGS: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    ("ade", "comparator"): {
        "comparator-mc": ("reference:mc",),
        "comparator-rest": (
            "reference:spec_lint", "reference:netlist_lint",
            "reference:sim_tt", "reference:sim_pvt",
            "reference:bench_strength", "reference:drc", "reference:lvs",
            "reference:pex_sim",
            "spec_lint_measure_no_bounds", "netlist_lint_model_not_in_pdk",
            "sim_tt_input_w_and_l_swapped",
            "sim_pvt_meets_typical_loses_headroom",
            "bench_strength_bounds_too_wide", "mc_offset_sigma_above_spec",
            "metal_spacing", "missing_guard_ring", "tail_sized_differently",
            "swapped_clk_vdd_labels", "long_min_width_output_line",
            "asymmetric_output_detour", "release_netlist_changed_after_sim"),
    },
}
CORPUS_SHARDS = (REST_SHARD, *(shard for groups in SPLIT_RUNGS.values()
                               for shard in groups))
TEST_WORKFLOW = REPO / ".github" / "workflows" / "test.yml"


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


def _run(tmp_path, skill, rung, only=None):
    import faults
    import gate as gate_mod

    gates = gate_mod.load_gates(gate_mod.DEFAULT_GATES)
    result = faults.run_rung(tmp_path, CORPUS / skill / rung, skill, gates,
                             None if only is None else set(only))
    assert not result["problems"], result


def _corpus_params() -> list:
    """The corpus step's tests: each rung whole, or a SPLIT_RUNGS rung as
    one test per group of pieces, each marked with the shard it runs in."""
    params = []
    for skill, rung in _rungs():
        if (skill, rung) in HELD_BACK or (skill, rung) in HARDEN_RUNGS:
            continue
        groups = SPLIT_RUNGS.get((skill, rung))
        if groups is None:
            params.append(pytest.param(
                skill, rung, None, id=f"{skill}-{rung}",
                marks=pytest.mark.corpus_shard(REST_SHARD)))
            continue
        for shard, pieces in groups.items():
            params.append(pytest.param(
                skill, rung, pieces, id=f"{skill}-{shard}",
                marks=pytest.mark.corpus_shard(shard)))
    return params


def test_split_rungs_run_every_piece_exactly_once():
    import faults
    import gate as gate_mod

    gates = gate_mod.load_gates(gate_mod.DEFAULT_GATES)
    for (skill, rung), groups in SPLIT_RUNGS.items():
        assert (skill, rung) in _rungs(), f"SPLIT_RUNGS: no rung {skill}/{rung}"
        assert (skill, rung) not in HARDEN_RUNGS | HELD_BACK.keys(), (
            f"SPLIT_RUNGS: {skill}/{rung} does not run in this lane")
        named = [p for pieces in groups.values() for p in pieces]
        want = faults.rung_parts(CORPUS / skill / rung, skill, gates)
        assert sorted(named) == sorted(want), (
            f"{skill}/{rung}: SPLIT_RUNGS groups {sorted(named)} but the "
            f"rung's pieces are {want}: a piece in no group never runs on "
            "CI, one in two runs twice")
    assert len(set(CORPUS_SHARDS)) == len(CORPUS_SHARDS), CORPUS_SHARDS
    assert "engine" not in CORPUS_SHARDS, "engine is check-engine.sh's job"


def test_ci_matrix_runs_every_corpus_shard():
    import yaml

    jobs = yaml.safe_load(TEST_WORKFLOW.read_text())["jobs"]
    matrix = jobs["shard"]["strategy"]["matrix"]["shard"]
    want = sorted(["engine", *CORPUS_SHARDS])
    assert sorted(matrix) == want, (
        f"test.yml shard matrix {sorted(matrix)} != engine + CORPUS_SHARDS "
        f"{want}: a shard missing there never runs on CI")
    # .hive/project.json's forge_tests names the job `test`: it must stand
    # for every shard, and a shard that failed, was cancelled or was
    # skipped must not read as a pass.
    agg = jobs["test"]
    assert agg.get("name", "test") == "test", agg
    assert agg["needs"] in ("shard", ["shard"]), agg
    assert agg["if"].strip() == "always()", agg
    run = "\n".join(step.get("run", "") for step in agg["steps"])
    assert "needs.shard.result" in run and "success" in run, run


@pytest.mark.slow
@pytest.mark.parametrize("skill, rung, only", _corpus_params())
def test_rung_catches_every_planted_fault(tmp_path, skill, rung, only):
    _run(tmp_path, skill, rung, only)


@pytest.mark.slow
@pytest.mark.harden_faults
@pytest.mark.parametrize("skill, rung", [
    r for r in _rungs() if r in HARDEN_RUNGS and r not in HELD_BACK])
def test_harden_rung_catches_every_planted_fault(tmp_path, skill, rung):
    _run(tmp_path, skill, rung)


def _plant_connectivity(kind: str, clean: Path, planted: Path) -> str:
    """The net tests/fixtures/harden_signoff/plant_connectivity.tcl cut."""
    tcl = REPO / "tests" / "fixtures" / "harden_signoff" / "plant_connectivity.tcl"
    proc = subprocess.run(
        [str(REPO / "bin" / "eda"), "openroad", "-no_splash", "-exit", str(tcl)],
        capture_output=True, text=True, timeout=300, check=True,
        env={**os.environ, "PLANT_KIND": kind, "PLANT_IN": str(clean),
             "PLANT_OUT": str(planted)})
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PLANTED\t")]
    assert len(lines) == 1, proc.stdout
    return lines[0].split("\t")[1]


@pytest.mark.slow
@pytest.mark.harden_faults
def test_connectivity_names_the_net_cut_from_counter8_final_odb(
        tmp_path, monkeypatch, capsys):
    """#156: a real harden of counter8 passes, and each net cut from its final
    ODB fails the gate by name. A gate run re-runs the flow over the run dir, so
    a manifest plant can't reach this ODB: here only the flow itself is stubbed,
    and it leaves the planted ODB where the flow would."""
    import check_harden
    import faults
    import gate as gate_mod

    gates = gate_mod.load_gates(gate_mod.DEFAULT_GATES)
    ws = faults.make_scratch_workspace(tmp_path, CORPUS / "vde" / "counter8",
                                       "vde", "counter8")
    report, result = faults.run_gate(ws, "harden", gates, "vde")
    assert result["status"] == "pass", report
    assert report["connectivity"]["undriven"] == []
    assert report["connectivity"]["unloaded"] == []
    final_odb = ws / "harden" / "runs" / "run" / "final" / "odb" / "tt_um_counter8.odb"
    clean = tmp_path / "clean.odb"
    shutil.copy(final_odb, clean)

    real_run = subprocess.run
    planted = tmp_path / "planted.odb"

    def stubbed_flow(cmd, *args, **kwargs):
        if cmd[1] != "librelane":
            return real_run(cmd, *args, **kwargs)
        shutil.copy(planted, final_odb)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(check_harden.subprocess, "run", stubbed_flow)
    for kind, finding in (("undriven", "net_undriven"), ("unloaded", "net_unloaded")):
        net = _plant_connectivity(kind, clean, planted)
        code = check_harden.main(["--workspace", str(ws)])
        out = json.loads(capsys.readouterr().out)
        assert code == 1, out
        hits = [v for v in out["violations"] if v["kind"] == finding]
        assert [v["refs"] for v in hits] == [[net]], out["violations"]
        assert net in hits[0]["msg"]

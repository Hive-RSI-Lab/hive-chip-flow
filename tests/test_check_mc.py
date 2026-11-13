"""engine/scripts/check_mc.py: the mc gate, run only when spec.yaml asks for
it (docs/design.md 1.5, "### M8."). Fast: fakes `eda`; a real MC run relies
on the gf180 PDK's own live agauss() mismatch and its own `.option seed=`
handling - test_real_mirror_mc_yields_a_spread (slow) proves that against
the real toolchain, since a fake eda that ignores seed/mismatch entirely
could otherwise "pass" a gate that is secretly running one sample over
and over."""
from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
CORPUS_MIRROR = REPO / "corpus" / "ade" / "mirror"
CORPUS_BANDGAP = REPO / "corpus" / "ade" / "bandgap"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest
import yaml  # noqa: E402

import check_mc  # noqa: E402
import sim_run  # noqa: E402

SPEC_YAML_NO_MC = """\
top: mirror
supply: {vdd: 3.3}
devices: [xmref, xmout]
measures:
  - {name: iout_ratio, bounds: {min: 1.8, max: 2.2}}
"""

BENCH_TEMPLATE = """\
.include '{{PDK}}/libs.tech/ngspice/design.spice'
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}
.temp {{TEMP_C}}
.include '{{NETLIST}}'
{{SIZING}}
vdd vdd 0 {{VDD}}
.control
op
print v(vdd)
.endc
.end
"""


def make_ws(tmp_path: Path, mc_yaml: str = "") -> Path:
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(SPEC_YAML_NO_MC + mc_yaml,
                                           encoding="utf-8")
    (ws / "netlist" / "mirror.cir").write_text("* netlist\n", encoding="utf-8")
    (ws / "tb" / "mirror_tb.cir").write_text(BENCH_TEMPLATE, encoding="utf-8")
    (ws / "tb" / "mirror_tb.bounds.json").write_text(
        json.dumps([{"measure": "iout_ratio", "min": 1.8, "max": 2.2}]),
        encoding="utf-8")
    return ws


def make_fake_eda(tmp_path: Path, ratios: list[str],
                  nominal: str = "2.0", by_process: dict | None = None,
                  seed_base: int = 1, pad_drop: float = 0.0) -> Path:
    """A fake eda that reads the deck it is handed. A Monte Carlo deck
    (`sw_stat_mismatch=1`, `.option seed=N`) reports
    ratios[(N - seed_base) % len(ratios)]; a nominal deck (the gate's own
    PVT sweep) reports `nominal`. `by_process` maps a `.lib ... <process>`
    word (typical, ss, ff, sf, fs) to its own (ratios, nominal) pair, so a
    test can script a corner that misses. A deck with the ua pad model in
    it reports its value less `pad_drop`. Keyed by seed,
    never by call order: the gate runs its samples in a pool."""
    fake_root = tmp_path / "fake_toolchain"
    (fake_root / "foss" / "pdks" / "gf180mcuD").mkdir(parents=True,
                                                    exist_ok=True)
    # one table line per process word: `<proc> <nominal> <ratio>...`, `*`
    # the default. A shell + awk fake, not a Python one: the gate runs
    # ~150 decks per test and a Python start per deck was most of this
    # file's CPU (tests/check-engine.sh's budget).
    rows = [["*", nominal, *ratios]]
    for proc, (r, nom) in (by_process or {}).items():
        rows.append([proc, nom, *r])
    table = tmp_path / "fake_eda.table"
    table.write_text("".join(" ".join(map(str, r)) + "\n" for r in rows),
                     encoding="utf-8")
    script = tmp_path / "fake_eda"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --print-toolchain-root ]; then\n'
        f"  echo '{fake_root}'; exit 0\n"
        "fi\n"
        'for deck; do :; done\n'
        f"exec awk -v base={int(seed_base)} -v padd={float(pad_drop)!r} '\n"
        "FNR == NR { t[$1] = $0; next }\n"
        "/sm141064[.]spice/ && !proc { for (i = 1; i < NF; i++)\n"
        "    if ($i ~ /sm141064[.]spice/) { proc = $(i + 1); break } }\n"
        "/sw_stat_mismatch=1/ { mm = 1 }\n"
        "/tt_ua_pad/ { pad = 1 }\n"
        "match($0, /[.]option seed=[0-9]+/) && seed == \"\" {\n"
        "    seed = substr($0, RSTART + 13, RLENGTH - 13) + 0 }\n"
        "END {\n"
        "    row = (proc in t) ? t[proc] : t[\"*\"]\n"
        "    n = split(row, f, \" \")\n"
        "    if (mm && seed != \"\") {\n"
        "        k = (seed - base) % (n - 2); if (k < 0) k += n - 2\n"
        "        v = f[3 + k] } else v = f[2]\n"
        "    if (pad) v = v - padd\n"
        "    print \"\"; print \"  Measurements for Transient Analysis\"\n"
        "    print \"iout_ratio            =  \" v\n"
        f"}}' '{table}' \"$deck\"\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_not_applicable_when_no_mc_block(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    eda = make_fake_eda(tmp_path, ["2.0"])
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    code = check_mc.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["applicable"] is False
    assert out["violations"] == []


def test_not_applicable_when_disabled(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: false}\n")
    eda = make_fake_eda(tmp_path, ["2.0"])
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    code = check_mc.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["applicable"] is False


def passing(n: int = 10) -> list[str]:
    """n distinct ratios, all inside the 1.8..2.2 bound."""
    return [f"{2.0 + 0.001 * i:.3f}" for i in range(n)]


def run_mc(ws, capsys):
    code = check_mc.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def kinds_of(out) -> set:
    return {v["kind"] for v in out["violations"]}


def test_yield_proven_passes_and_reports_stats(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 0, out
    assert out["applicable"] is True
    assert out["runs"] == 60 and out["confidence"] == 0.95
    tt = next(g for g in out["groups"] if g["role"] == "tt")
    st = tt["stats"]["iout_ratio"]
    assert st["n"] == 60
    assert st["mean"] == pytest.approx(2.0045, abs=1e-6)
    assert st["sigma"] > 0 and st["cpk"] > 1
    assert out["yields"]["tt"]["yield_lower"] >= 0.9
    assert "normal" in out["assumptions"].lower()


def test_yield_below_spec_fails(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 20, yield_min: 0.9}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(
        tmp_path, ["2.0", "2.01", "2.02", "9.0"]))  # 75% < 90%
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    assert "yield_below_spec" in kinds_of(out)


def test_all_passing_but_too_few_runs_is_unproven(tmp_path, monkeypatch, capsys):
    # 20 of 20 is no proof of 99%: the Clopper-Pearson lower bound at 20/20
    # is 0.05**(1/20) = 86% before Bonferroni. The gate names the run count.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 20, yield_min: 0.99}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    v = next(v for v in out["violations"] if v["kind"] == "yield_unproven")
    assert v["runs_needed"] > 20
    assert out["hits"] == out["samples"]


def test_default_runs_are_sized_from_yield_min(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, yield_min: 0.95}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 0, out
    assert out["runs"] == check_mc.default_runs(0.95, 0.95, 2, 1)
    assert out["yields"]["tt"]["yield_lower"] >= 0.95
    assert out["yields"]["worst"]["yield_lower"] >= 0.95


def test_worst_corner_miss_is_caught_when_typical_passes(
        tmp_path, monkeypatch, capsys):
    # tt is clean; ff sits near the max nominally (2.15) and a third of its
    # mismatch samples go over. Every nominal corner passes, so sim_pvt
    # would pass it, and a gate that ran MC at tt only would too.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 90, yield_min: 0.9}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(
        tmp_path, passing(), by_process={
            "ff": (["2.14", "2.16", "2.25"], "2.15")}))
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    assert out["sweep_violations"] == 0
    assert out["worst_corners"]["mirror_tb.cir"]["corner"].startswith("ff")
    assert out["yields"]["tt"]["yield_lower"] >= 0.9
    v = next(v for v in out["violations"] if v["kind"] == "yield_below_spec")
    assert "worst" in v["msg"]


def test_clopper_pearson_known_answers():
    # one-sided lower bounds, checked against the beta quantile
    assert check_mc.cp_lower(20, 20, 0.95) == pytest.approx(0.05 ** (1 / 20),
                                                             abs=1e-9)
    assert check_mc.cp_lower(19, 20, 0.95) == pytest.approx(0.7839, abs=1e-4)
    assert check_mc.cp_lower(45, 50, 0.95) == pytest.approx(0.8012, abs=1e-4)
    assert check_mc.cp_lower(0, 20, 0.95) == 0.0
    # zero failures: the classic "rule of three" run count for 95%/95%
    assert check_mc.runs_for(0.95, 0.95, 0.0) == 59
    assert check_mc.runs_needed(0.95, 0.95, [0.10]) is None


def write_ruling(ws: Path, sigma: float) -> None:
    (ws / "spec" / "mutant_rulings.yaml").write_text(
        "below_spread:\n"
        "  - id: xmout_size_doubled_w\n"
        "    netlist_line: \"xmout out iref vss vss nfet_03v3 w=2u l=1u\"\n"
        "    measure: iout_ratio\n"
        "    delta: 0.001\n"
        f"    sigma: {sigma}\n"
        "    ruling: \"test, 2026-11-07\"\n", encoding="utf-8")


def test_ruling_sigma_above_measured_is_unmet(tmp_path, monkeypatch, capsys):
    # the samples' relative sigma is about 0.15%; a ruling claiming 5%
    # would let bench_strength excuse a mutant on a spread that is not there
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    write_ruling(ws, 0.05)
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    assert kinds_of(out) == {"ruling_sigma_unmet"}
    assert out["ruling_sigmas"][0]["verdict"] == "unmet"


def test_ruling_sigma_within_measured_is_met(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    write_ruling(ws, 0.001)
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 0, out
    assert out["ruling_sigmas"][0]["verdict"] == "met"


def test_ruling_sigma_with_mc_off_is_refused(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    write_ruling(ws, 0.05)
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 2, out
    assert "mutant_rulings" in out["error"]


@pytest.mark.parametrize("cfg", ["yield_min: 0.0", "yield_min: 1.0", "",
                                 "yield_min: 0.9, confidence: 1.0"])
def test_unprovable_config_is_refused(tmp_path, monkeypatch, capsys, cfg):
    ws = make_ws(tmp_path, "mc: {enabled: true" + (", " + cfg if cfg else "")
                 + "}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 2, out


def test_runs_zero_is_refused_not_defaulted(tmp_path, monkeypatch, capsys):
    # `0 or DEFAULT_RUNS` used to silently widen an explicit "run zero
    # samples" back up to the default - Python's `or` treats 0 as falsy.
    # This must refuse outright instead.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 0, yield_min: 0.5}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 2, out
    assert "runs" in out.get("error", "")


def test_seeds_are_injected_recorded_and_vary_the_result(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 5, yield_min: 0.3, "
                          "seed: 100}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN",
                        make_fake_eda(tmp_path, passing(5), seed_base=100))
    code, out = run_mc(ws, capsys)
    assert code == 0, out
    assert out["seeds"] == [100, 101, 102, 103, 104]
    tt = [r for r in out["results"] if r["corner"] == "tt"]
    assert {r["seed"] for r in tt} == set(out["seeds"])
    assert len({r["measures"]["iout_ratio"] for r in tt}) == 5, out


def test_bit_identical_samples_across_runs_is_refused(tmp_path, monkeypatch, capsys):
    # A fake eda that ignores seed entirely (the bug this whole gate exists
    # to catch: sw_stat_mismatch/seed injection silently doing nothing, so
    # the "runs" are one sample wearing a yield percentage) must be refused,
    # never reported as a clean 100% yield.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 5, yield_min: 0.5}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, ["2.0"]))
    code, out = run_mc(ws, capsys)
    assert code == 2, out
    assert "identical" in out.get("error", "").lower()


def test_bench_with_no_bound_at_tt_is_skipped_and_named(tmp_path, monkeypatch, capsys):
    # A bench scoped to another corner prints no measure at tt. It has nothing
    # for MC at tt to judge, and must not read as one sample repeated.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 3, yield_min: 0.1}\n")
    src = next((ws / "tb").glob("*.cir"))
    shutil.copy(src, ws / "tb" / "other_tb.cir")
    (ws / "tb" / "other_tb.bounds.json").write_text(json.dumps([
        {"measure": "iout_ratio", "min": 0, "max": 9, "corners": ["ff_m40c_vp10"]}]))
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert out["not_scored"] == ["other_tb.cir"], out
    assert out["runs"] == 3
    assert {r["bench"] for r in out["results"] if r["corner"] == "tt"} \
        == {src.name}


def test_bench_scored_only_off_tt_is_judged_at_its_worst_corner(
        tmp_path, monkeypatch, capsys):
    # nothing to judge at tt, but the sweep scores it at ff: MC runs there
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    for f in (ws / "tb").glob("*.bounds.json"):
        f.write_text(json.dumps([
            {"measure": "iout_ratio", "min": 0, "max": 9, "corners": ["ff_m40c_vp10"]}]))
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 0, out
    assert "tt" not in out["yields"]
    assert [g["role"] for g in out["groups"]] == ["worst"]
    assert out["groups"][0]["corner"].startswith("ff")


def test_no_bench_scored_anywhere_is_not_a_pass(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 3, yield_min: 0.1}\n")
    for f in (ws / "tb").glob("*.bounds.json"):
        f.write_text(json.dumps([
            {"measure": "iout_ratio", "min": 0, "max": 9, "corners": ["nosuch"]}]))
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 2, out


def test_every_sample_failed_is_always_a_violation(tmp_path, monkeypatch, capsys):
    # yield_min set low enough that a `yield_frac < yield_min` check alone
    # would nearly pass a run where EVERY sample failed - that must still be
    # refused. Ratios vary per run so this is not also caught by the
    # bit-identical check above.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 4, yield_min: 0.01}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(
        tmp_path, ["9.00", "9.01", "9.02", "9.03"]))
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    assert out["hits"] == 0
    assert "yield_all_failed" in kinds_of(out)


PAD_SPEC = """\
top: mirror
supply: {vdd: 3.3}
devices: [xmref, xmout]
ua_pins: [out]
measures:
  - {name: iout_ratio, bounds: {min: 1.8, max: 2.2}, pad: pad}
mc: {enabled: true, runs: 40, yield_min: 0.5}
"""


def test_pad_only_bound_is_sampled_through_the_pad_never_bare(
        tmp_path, monkeypatch, capsys):
    # sim_pvt scores a `pad: pad` bound only in the run through the ua pad
    # model. The mc gate once sampled the bare deck alone and scored every
    # bound there, so a design whose bare pin reads far off (a glitch the
    # pad's R/C then filters) failed every sample of a design sim_pvt
    # passes. Here the bare deck reads 9 and the padded one 7 less.
    ws = make_ws(tmp_path)
    (ws / "spec" / "spec.yaml").write_text(PAD_SPEC, encoding="utf-8")
    (ws / "netlist" / "mirror.cir").write_text(
        ".subckt mirror out vss\nr1 out vss 1k\n.ends mirror\n",
        encoding="utf-8")
    (ws / "tb" / "mirror_tb.cir").write_text(BENCH_TEMPLATE.replace(
        "vdd vdd 0 {{VDD}}\n", "vdd vdd 0 {{VDD}}\nxdut out 0 mirror\n"),
        encoding="utf-8")
    (ws / "tb" / "mirror_tb.bounds.json").write_text(json.dumps(
        [{"measure": "iout_ratio", "min": 1.8, "max": 2.2, "pad": "pad"}]),
        encoding="utf-8")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(
        tmp_path, ["9.0", "9.05", "9.1", "9.15"], nominal="9.0",
        pad_drop=7.0))
    code, out = run_mc(ws, capsys)
    assert "yield_all_failed" not in kinds_of(out), out
    benches = {g["bench"] for g in out["groups"]}
    assert benches == {"mirror_tb.cir[pad]"}, out["groups"]
    assert out["hits"] == out["samples"] > 0


@pytest.mark.slow
def test_real_mirror_mc_yields_a_spread(tmp_path):
    # The real fault this test guards: a fake ngspice can be made to return
    # whatever a test script wants, so only a REAL run against the PDK's
    # own agauss()-driven mismatch proves the injected sw_stat_mismatch=1 +
    # per-run seed actually varies iout_raw from run to run.
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb"):
        (ws / sub).mkdir(parents=True)
    # the rung's own spec.yaml asks for Monte Carlo (runs sized from
    # its yield_min)
    shutil.copy2(CORPUS_MIRROR / "spec.yaml", ws / "spec" / "spec.yaml")
    shutil.copy2(CORPUS_MIRROR / "netlist" / "mirror.cir",
                ws / "netlist" / "mirror.cir")
    for f in (CORPUS_MIRROR / "tb").iterdir():
        shutil.copy2(f, ws / "tb" / f.name)

    payload, _ = check_mc.run(["--workspace", str(ws)])
    assert payload["applicable"] is True
    assert payload["runs"] >= 59
    values = [r["measures"]["iout_raw"] for r in payload["results"]]
    assert len(set(values)) > 1, values  # never one sample repeated
    mean = sum(values) / len(values)
    stddev = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    assert stddev > 0, values


@pytest.mark.slow
def test_real_mirror_rung_passes_mc_and_its_planted_fault_fails_it(tmp_path):
    # The corpus rung's own mc fault, run the way faults.py runs it: the
    # untouched mirror yields 20/20, and the planted window that typical
    # still meets (sim_tt passes) drops yield below the spec's 95%.
    import faults
    import gate as gate_mod

    gates = gate_mod.load_gates(gate_mod.DEFAULT_GATES)
    clean = faults.make_scratch_workspace(tmp_path / "clean", CORPUS_MIRROR,
                                          "ade", "mirror")
    report, result = faults.run_gate(clean, "mc", gates, "ade")
    assert result["status"] == "pass", report
    assert report["hits"] == report["samples"], report

    entry = next(f for f in faults.load_manifest(CORPUS_MIRROR)["faults"]
                 if f["gate"] == "mc")
    ws = faults.make_scratch_workspace(tmp_path / "planted", CORPUS_MIRROR,
                                       "ade", "mirror")
    faults.import_plant(CORPUS_MIRROR, entry["plant"])(ws)
    report, result = faults.run_gate(ws, "mc", gates, "ade")
    assert not faults.check_expect(entry["name"], result, entry["expect"]), \
        report
    assert 0 < report["hits"] < report["samples"], report
    _, tt = faults.run_gate(ws, "sim_tt", gates, "ade")
    assert tt["status"] == "pass", tt


def test_not_applicable_answer_cannot_be_recorded_as_a_pass(
        tmp_path, monkeypatch):
    # check_mc's not-applicable report is stamped with spec.yaml, while
    # invalidation.yaml keys mc on the netlist: state.json refuses to
    # record it. Release reads the current spec instead (attest.
    # _mc_not_applicable), so a stale "not applicable" pass can never
    # outlive a spec that later turns MC on.
    import gate as gate_mod
    import state as state_mod
    from checklib import CheckError

    ws = make_ws(tmp_path)
    state_mod.State.init(ws, "ade", "mirror")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, ["2.0"]))
    payload, _ = check_mc.run(["--workspace", str(ws)])
    assert payload["applicable"] is False
    row = gate_mod.load_gates(gate_mod.DEFAULT_GATES)["ade"]["mc"]
    result = gate_mod.evaluate("mc", row, payload)
    assert result["status"] == "not_applicable"
    st = state_mod.State.load(ws / "state.json")
    with pytest.raises(CheckError, match="pass\\|fail"):
        st.record_gate("mc", result, row["phase"])
    # even retitled as a pass, the spec-stamped digest is still refused
    with pytest.raises(CheckError, match="input_digest"):
        st.record_gate("mc", {**result, "status": "pass"}, row["phase"])


def test_non_mapping_mc_block_is_an_error(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: true\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, ["2.0"]))
    code = check_mc.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "mapping" in out.get("remediation", "") + out.get("error", "")


def test_design_ngspice_include_is_injected_too():
    from checklib import CheckError
    # The PDK's design.ngspice is the same file as design.spice; a bench
    # that includes it must get the mismatch switch and seed, not a refusal.
    bench = (".include '{{PDK}}/libs.tech/ngspice/design.ngspice'\n"
             ".lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}\n")
    out = check_mc.inject_mc_params(bench, 7, False)
    assert out.splitlines()[1:3] == [".param sw_stat_mismatch=1",
                                     ".option seed=7"]
    with pytest.raises(CheckError, match="design.ngspice"):
        check_mc.inject_mc_params(".lib 'x/sm141064.spice' typical\n", 7, False)


@pytest.mark.parametrize("spec_add, flag, want", [
    ("", [], 60.0),
    ("sim_timeout_s: {mc: 400}\n", [], 400.0),
    ("sim_timeout_s: {mc: 400}\n", ["--timeout", "90"], 90.0),
])
def test_mc_timeout_comes_from_spec_then_flag(
        tmp_path, monkeypatch, capsys, ngspice_timeouts, spec_add, flag, want):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 4, yield_min: 0.75}\n"
                 + spec_add)
    monkeypatch.setattr(sim_run, "EDA_BIN",
                        make_fake_eda(tmp_path, ["2.0", "2.0", "2.0", "9.0"]))
    code = check_mc.main(["--workspace", str(ws), *flag])
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1) and out["applicable"], out
    assert ngspice_timeouts and set(ngspice_timeouts) == {want}


# --- m=N devices: the PDK's mismatch term reads par, not m (issue #98) ---

MULT_NETLIST = """\
* multiplied units
.subckt mirror vdd iref iout vss
XMREF iref iref vss vss nfet_03v3 w=2u l=1u m=4
XMOUT iout iref vss vss nfet_03v3 w=2u l=1u
+ m={m_out} ; m=9 in a comment is not a parameter
XMP1 a b vdd vdd pfet_03v3 w=1u l=1u m=1
XMP2 a b vdd vdd pfet_03v3 w=1u l=1u m=2 par=2
XSUB a b mysub m=3
R1 a b 1k m=2
.include 'models/extra.inc'
.ends
"""


def test_add_par_for_multiplier_rewrites_multiplied_mos_only():
    new, done = check_mc.add_par_for_multiplier(MULT_NETLIST)
    assert [d["instance"] for d in done if "unhandled" not in d] \
        == ["XMREF", "XMOUT"]
    assert [d["instance"] for d in done if "unhandled" in d] == ["XSUB"]
    assert "XMREF iref iref vss vss nfet_03v3 w=2u l=1u m=4 par=4\n" in new
    assert "+ m={m_out} par={m_out} ; m=9" in new
    # m=1, an existing par, a user subckt, a resistor: untouched
    for keep in ("pfet_03v3 w=1u l=1u m=1\n", "m=2 par=2\n",
                 "XSUB a b mysub m=3\n", "R1 a b 1k m=2\n"):
        assert keep in new
    # idempotent on what it rewrote
    assert [d["instance"] for d in check_mc.add_par_for_multiplier(new)[1]] \
        == ["XSUB"]


def test_mc_decks_include_the_par_copy_never_the_block_netlist(
        tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 2, yield_min: 0.5}\n")
    (ws / "netlist" / "mirror.cir").write_text(MULT_NETLIST, encoding="utf-8")
    eda = make_fake_eda(tmp_path, ["2.0", "2.1"])
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    code = check_mc.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1) and out["applicable"], out
    assert [d["instance"] for d in out["par_added"]] \
        == ["XMREF", "XMOUT", "XSUB"]
    assert "unhandled" in out["par_added"][2]
    assert (ws / "netlist" / "mirror.cir").read_text() == MULT_NETLIST
    copy = ws / "log" / "mc" / "mirror__mc_par.cir"
    text = copy.read_text()
    assert "m=4 par=4" in text
    # the relative include now resolves from the copy's directory
    assert f"'{(ws / 'netlist').resolve()}/models/extra.inc'" in text
    decks = sorted((ws / "log" / "mc").glob("mirror_tb__mc*__*.cir"))
    assert len(decks) == 2 * len(out["groups"]) >= 2
    for deck in decks:
        assert str(copy.resolve()) in deck.read_text()


def test_netlist_without_multiplied_mos_is_included_as_is(tmp_path):
    net = tmp_path / "n.cir"
    net.write_text("XM1 d g s b nfet_03v3 w=1u l=1u\n", encoding="utf-8")
    path, done, res = check_mc.mc_netlist(net, tmp_path / "out")
    assert path == net and done == [] and res == []
    assert not (tmp_path / "out").exists()


HIER_NETLIST = """\
.subckt unit d ; m=9 in a comment
XM d d 0 0 nfet_03v3 w=2u l=1u
.ends
.subckt pair d
XI d unit m=2
.ends
XTOP a unit m=4
XTWO b pair m=3
XONE c unit
"""


def test_hierarchical_m_carries_down_and_multiplies_across_levels():
    new, done = check_mc.add_par_for_multiplier(HIER_NETLIST)
    assert ".subckt unit d mc_mult=1 ; m=9 in a comment\n" in new
    assert "XM d d 0 0 nfet_03v3 w=2u l=1u par={mc_mult}\n" in new
    assert ".subckt pair d mc_mult=1\n" in new
    assert "XI d unit m=2 mc_mult={mc_mult*(2)}\n" in new
    assert "XTOP a unit m=4 mc_mult=4\n" in new
    assert "XTWO b pair m=3 mc_mult=3\n" in new
    assert "XONE c unit\n" in new  # one unit: the default of 1 is right
    assert [d["instance"] for d in done] == ["XM", "XI", "XTOP", "XTWO"]
    assert [d.get("subckt") for d in done] == ["unit", "pair", None, None]


def test_inner_mos_own_m_or_par_joins_the_carried_multiplier():
    new, _ = check_mc.add_par_for_multiplier(
        ".subckt unit d\n"
        "XA d d 0 0 nfet_03v3 w=2u l=1u m=3\n"
        "XB d d 0 0 nfet_03v3 w=2u l=1u m=3 par=5\n"
        "XC d d 0 0 nfet_03v3 w=2u l=1u m={k} ; note\n"
        ".ends\n"
        "XU a unit m=2\n")
    assert "l=1u m=3 par={mc_mult*(3)}\n" in new
    assert "l=1u m=3 par={mc_mult*(5)}\n" in new
    assert "l=1u m={k} par={mc_mult*(k)} ; note\n" in new


def test_instance_that_writes_par_and_subckt_without_mos_stay_as_written():
    text = (".subckt unit d\nXM d d 0 0 nfet_03v3 w=2u l=1u\n.ends\n"
            ".subckt rs a\nR1 a 0 1k\n.ends\n"
            "XU a unit m=4 par=4\nXR a rs m=2\n")
    assert check_mc.add_par_for_multiplier(text) == (text, [])


def test_bench_multiplying_a_netlist_subckt_reaches_both_copies(tmp_path):
    net = tmp_path / "n.cir"
    net.write_text(".subckt unit d\nXM d d 0 0 nfet_03v3 w=2u l=1u\n.ends\n",
                   encoding="utf-8")
    bench = "XU a unit m={{M}}\n"
    path, done, _ = check_mc.mc_netlist(net, tmp_path / "out", [bench])
    assert path != net and "par={mc_mult}" in path.read_text()
    assert [d["instance"] for d in done] == ["XM"]
    new, done = check_mc.add_par_for_multiplier(bench, [net.read_text()])
    # the token keeps its braces for materialize()
    assert new == "XU a unit m={{M}} mc_mult={{M}}\n"
    assert done[0]["m"] == "{{M}}"


def test_multiplied_model_not_rewritten_is_reported_unless_it_has_no_mismatch():
    text = ("XE a elsewhere m=4\n"
            "XR a b 0 rm1 w=1u l=1u m=2\nXC a b cap_mim_1f0_m2m3_noshield m=2\n"
            "XF a elsewhere m=1\n")
    new, done = check_mc.add_par_for_multiplier(text)
    assert new == text
    assert [d["instance"] for d in done] == ["XE"]
    assert all("unhandled" in d for d in done)


def test_multiplied_bipolar_gets_par_flat_and_in_a_carrier():
    new, done = check_mc.add_par_for_multiplier(
        "XQ c b e pnp_10p00x10p00 m=8\nXN c b e s npn_05p00x05p00 m=2\n"
        "XO c b e pnp_05p00x00p42\n"
        ".subckt cell c b e\nXI c b e pnp_05p00x05p00 m=2\n.ends\n"
        "XU c b e cell m=4\n")
    lines = new.split("\n")
    assert "XQ c b e pnp_10p00x10p00 m=8 par=8" in lines
    assert "XN c b e s npn_05p00x05p00 m=2 par=2" in lines
    assert "XO c b e pnp_05p00x00p42" in lines
    assert "XI c b e pnp_05p00x05p00 m=2 par={mc_mult*(2)}" in lines
    assert "XU c b e cell m=4 mc_mult=4" in lines
    assert not any("unhandled" in d for d in done)


def test_bandgap_rung_asks_for_mc_and_its_multiplied_devices_are_rewritten():
    spec = yaml.safe_load((CORPUS_BANDGAP / "spec.yaml").read_text(encoding="utf-8"))
    assert spec["mc"]["enabled"] is True
    _, done = check_mc.add_par_for_multiplier(
        (CORPUS_BANDGAP / "netlist" / "bandgap.cir").read_text(encoding="utf-8"))
    assert done == [{"instance": "xm1", "model": "nfet_03v3", "m": "4"},
                    {"instance": "xm2", "model": "nfet_03v3", "m": "4"},
                    {"instance": "xq2", "model": "pnp_10p00x10p00", "m": "8"}]


def test_unknown_model_inside_a_carrier_is_reported():
    _, done = check_mc.add_par_for_multiplier(
        ".subckt unit d\nXM d d 0 0 nfet_03v3 w=2u l=1u\nXE d elsewhere\n.ends\n"
        "XU a unit m=4\n")
    assert [(d["instance"], d.get("subckt")) for d in done if "unhandled" in d] \
        == [("XE", "unit")]


def test_recursive_subckt_definitions_do_not_hang():
    new, _ = check_mc.add_par_for_multiplier(
        ".subckt a d\nXB d b\nXM d d 0 0 nfet_03v3 w=2u l=1u\n.ends\n"
        ".subckt b d\nXA d a\n.ends\nXU z a m=2\n")
    assert "XB d b mc_mult={mc_mult}\n" in new


def _hier_vgs(tmp_path, lines, fixed, mismatch, tag, count):
    """Node voltages of one seeded op over `lines`, MC copy or as written."""
    import re

    import corners as corners_mod
    import simlib

    net = tmp_path / f"{tag}.cir"
    net.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bench = (".include '{{PDK}}/libs.tech/ngspice/design.spice'\n"
             ".lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}\n"
             ".include '{{NETLIST}}'\n"
             ".control\nop\nprint all\n.endc\n.end\n")
    eda = sim_run.EDA_BIN
    root = sim_run.toolchain_root(eda)
    tt = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]
    out = tmp_path / f"{tag}_{int(fixed)}_{int(mismatch)}"
    out.mkdir(parents=True, exist_ok=True)
    nl = check_mc.mc_netlist(net, out)[0] if fixed else net
    text = check_mc.inject_mc_params(bench, 7, False) if mismatch else bench
    deck = out / "tb.cir"
    deck.write_text(simlib.materialize(
        text, sim_run.build_subs(root, nl, tt, 3.3, {})), encoding="utf-8")
    stdout, _, _ = simlib.run_ngspice(eda, deck, out, 280)
    v = {k.lower(): float(x) for k, x in re.findall(
        r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$", stdout, re.MULTILINE)}
    return ([v[f"a{i}"] for i in range(count)],
            [v[f"b{i}"] for i in range(count)])


@pytest.mark.slow
@pytest.mark.parametrize("tag,head,inst", [
    # the issue's measurement (#144): unit m=4 against flat w=8u
    ("one", [], "unit m=4"),
    # the multiplier on two levels multiplies: m=2 of (m=2 of unit)
    ("two", [".subckt twice d", "XI d unit m=2", ".ends"], "twice m=2"),
])
def test_real_hierarchical_m_mismatch_matches_the_wider_device(
        tmp_path, tag, head, inst):
    # Known answer: one seeded op over 200 diode-connected pairs at 40 uA,
    # the unit w=2u under a multiplier of 4 next to a flat w=8u. Carried
    # through, the sigma matches the device of the same area within 25%
    # (seed 7: 1.97 mV against 2.22 mV; over seeds 1-8 both average 1.97 mV);
    # the planted fault, the netlist as written, gives about 1.8x (3.94 mV).
    # With mismatch off, the rewrite leaves DC alone.
    import statistics

    n = 200
    lines = ["* hierarchical m=4 vs w*4", ".subckt unit d",
             "XM d d 0 0 nfet_03v3 w=2u l=1u", ".ends", *head]
    for i in range(n):
        lines += [f"XA{i} a{i} {inst}", f"IA{i} 0 a{i} 40u",
                  f"XB{i} b{i} b{i} 0 0 nfet_03v3 w=8u l=1u",
                  f"IB{i} 0 b{i} 40u"]
    a_fix, b = _hier_vgs(tmp_path, lines, True, True, tag, n)
    sd_b = statistics.stdev(b)
    ratio = statistics.stdev(a_fix) / sd_b
    print(f"{tag}: sd fixed {statistics.stdev(a_fix)*1e3:.2f} mV "
          f"flat {sd_b*1e3:.2f} mV ratio {ratio:.2f}")
    assert 0.75 < ratio < 1.33, ratio
    a_asis, _ = _hier_vgs(tmp_path, lines, False, True, tag, n)
    planted = statistics.stdev(a_asis) / sd_b
    print(f"{tag}: planted sd {statistics.stdev(a_asis)*1e3:.2f} mV "
          f"ratio {planted:.2f}")
    assert planted > 1.5, planted
    assert _hier_vgs(tmp_path, lines, True, False, tag, n)[0] \
        == _hier_vgs(tmp_path, lines, False, False, tag, n)[0]


@pytest.mark.slow
def test_real_pnp_m8_mismatch_follows_the_pdk_scaling(tmp_path):
    # Known answer: one seeded op over 200 diode-connected pnp_10p00x10p00
    # at the same current density, m=8 at 8 uA next to m=1 at 1 uA. The PDK
    # has no larger pnp and sets sigma per geometry, so the answer is its
    # documented term, mis_*/sqrt(par): the rewritten m=8 has 1/sqrt(8)=0.354
    # of the m=1 sigma. The planted fault - the netlist as written, no par -
    # draws one unit's sigma, a ratio about 1. With mismatch off, the rewrite
    # leaves the DC point exactly where it was. numdgt: this sigma is tens
    # of uV, below `print all`'s default six digits at a 0.7 V node.
    import re
    import statistics

    import corners as corners_mod
    import simlib

    n = 200
    lines = ["* pnp m=8 vs m=1"]
    for i in range(n):
        lines += [f"XA{i} 0 0 a{i} pnp_10p00x10p00 m=8",
                  f"IA{i} 0 a{i} 8u",
                  f"XB{i} 0 0 b{i} pnp_10p00x10p00",
                  f"IB{i} 0 b{i} 1u"]
    net = tmp_path / "ka.cir"
    net.write_text("\n".join(lines) + "\n", encoding="utf-8")
    header = (CORPUS_BANDGAP / "tb" / "bandgap_tb.cir").read_text(
        encoding="utf-8").split(".temp {{TEMP_C}}")[0]
    header = header[header.index(".include '{{PDK}}"):]
    bench = (header + ".temp {{TEMP_C}}\n.include '{{NETLIST}}'\n"
             ".control\nset numdgt=10\nop\nprint all\n.endc\n.end\n")
    eda = sim_run.EDA_BIN
    root = sim_run.toolchain_root(eda)
    tt = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]

    def vbe(fixed: bool, mismatch: bool):
        out = tmp_path / f"{'par' if fixed else 'asis'}_{int(mismatch)}"
        nl, done, _ = (check_mc.mc_netlist(net, out) if fixed
                       else (net, [], []))
        assert len(done) == (n if fixed else 0)
        text = check_mc.inject_mc_params(bench, 7, False) if mismatch \
            else bench
        deck = out / "ka_tb.cir"
        out.mkdir(parents=True, exist_ok=True)
        deck.write_text(simlib.materialize(
            text, sim_run.build_subs(root, nl, tt, 3.3, {})), encoding="utf-8")
        stdout, _, _ = simlib.run_ngspice(eda, deck, out, 120)
        v = {k.lower(): float(x) for k, x in re.findall(
            r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$", stdout, re.MULTILINE)}
        return ([v[f"a{i}"] for i in range(n)], [v[f"b{i}"] for i in range(n)])

    a_fix, b = vbe(True, True)
    ratio = statistics.stdev(a_fix) / statistics.stdev(b)
    print(f"pnp m=8 par=8 sd {statistics.stdev(a_fix)*1e6:.1f} uV, "
          f"m=1 sd {statistics.stdev(b)*1e6:.1f} uV, ratio {ratio:.3f}")
    assert 0.354 * 0.75 < ratio < 0.354 * 1.25, ratio
    a_asis, b2 = vbe(False, True)
    assert b2 == b  # same seed, same draws for the untouched devices
    planted = statistics.stdev(a_asis) / statistics.stdev(b)
    print(f"planted ratio {planted:.3f}")
    assert 0.75 < planted < 1.25, planted
    # DC point: with mismatch off the rewrite changes nothing
    assert vbe(True, False)[0] == vbe(False, False)[0]


@pytest.mark.slow
def test_real_m4_mismatch_matches_the_w4_device(tmp_path):
    # Known answer (issue #98): one seeded op over 200 diode-connected
    # nfet_03v3 pairs at 40 uA, w=2u m=4 next to w=8u. With par added, the
    # m=4 sigma matches the w*4 device (the same area) within MC noise; the
    # planted fault - the block's netlist as written, no par - gives about
    # sqrt(4)=2x the sigma (measured 3.9 mV against 2.0 mV). With mismatch
    # off, the rewrite leaves the DC point exactly where it was.
    import re
    import statistics

    import corners as corners_mod
    import simlib

    n = 200
    lines = ["* m=4 vs w*4"]
    for i in range(n):
        lines += [f"XA{i} a{i} a{i} 0 0 nfet_03v3 w=2u l=1u m=4",
                  f"IA{i} 0 a{i} 40u",
                  f"XB{i} b{i} b{i} 0 0 nfet_03v3 w=8u l=1u",
                  f"IB{i} 0 b{i} 40u"]
    net = tmp_path / "ka.cir"
    net.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bench = (".include '{{PDK}}/libs.tech/ngspice/design.spice'\n"
             ".lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}\n"
             ".include '{{NETLIST}}'\n"
             ".control\nop\nprint all\n.endc\n.end\n")
    eda = sim_run.EDA_BIN
    root = sim_run.toolchain_root(eda)
    tt = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]

    def vgs(fixed: bool, mismatch: bool):
        out = tmp_path / f"{'par' if fixed else 'asis'}_{int(mismatch)}"
        nl, done, _ = (check_mc.mc_netlist(net, out) if fixed
                       else (net, [], []))
        assert len(done) == (n if fixed else 0)
        text = check_mc.inject_mc_params(bench, 7, False) if mismatch \
            else bench
        deck = out / "ka_tb.cir"
        out.mkdir(parents=True, exist_ok=True)
        deck.write_text(simlib.materialize(
            text, sim_run.build_subs(root, nl, tt, 3.3, {})), encoding="utf-8")
        stdout, _, _ = simlib.run_ngspice(eda, deck, out, 120)
        v = {k.lower(): float(x) for k, x in re.findall(
            r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$", stdout, re.MULTILINE)}
        return ([v[f"a{i}"] for i in range(n)], [v[f"b{i}"] for i in range(n)])

    a_fix, b = vgs(True, True)
    ratio = statistics.stdev(a_fix) / statistics.stdev(b)
    assert 0.75 < ratio < 1.33, ratio
    a_asis, b2 = vgs(False, True)
    assert b2 == b  # same seed, same draws for the untouched devices
    planted = statistics.stdev(a_asis) / statistics.stdev(b)
    assert planted > 1.5, planted
    # DC point: with mismatch off the rewrite changes nothing
    assert vgs(True, False)[0] == vgs(False, False)[0]


def spec_with(ws: Path, measure_extra: str, extra_measures: str = "") -> None:
    """Rewrite the workspace's spec.yaml: iout_ratio gets `measure_extra`
    (indented YAML keys), plus any further measures."""
    p = ws / "spec" / "spec.yaml"
    text = p.read_text(encoding="utf-8").replace(
        "  - {name: iout_ratio, bounds: {min: 1.8, max: 2.2}}\n",
        "  - name: iout_ratio\n    bounds: {min: 1.8, max: 2.2}\n"
        + measure_extra + extra_measures)
    p.write_text(text, encoding="utf-8")


# passing()'s ten ratios have a sample 1-sigma of about 0.00303; with 60
# runs its 97.5%-confidence chi-square upper bound is about 0.0036.
@pytest.mark.parametrize("smax, code_want, verdict, kind", [
    (0.01, 0, "met", None),
    (0.001, 1, "above_spec", "sigma_above_spec"),
    (0.0032, 1, "unproven", "sigma_unproven"),
])
def test_spec_sigma_max_is_held_to_the_sample_sigma(
        tmp_path, monkeypatch, capsys, smax, code_want, verdict, kind):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    spec_with(ws, "    statistical: mismatch\n"
                  f"    sigma_max: {smax}\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == code_want, out
    checks = out["spec_sigmas"]
    assert checks and {c["verdict"] for c in checks} == {verdict}, checks
    assert all(c["sigma_upper"] >= c["sigma"] for c in checks)
    if kind:
        assert kind in kinds_of(out)


def test_spec_sigma_max_no_bench_measures_it_is_a_finding(
        tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    spec_with(ws, "", "  - name: vin_offset\n"
                      "    bounds: {min: -0.06, max: 0.06}\n"
                      "    sigma_max: 0.02\n")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert code == 1, out
    assert "sigma_unmeasured" in kinds_of(out)


def test_nominal_only_measure_is_left_out_of_mc(tmp_path, monkeypatch, capsys):
    # A second bench whose only bound is a `statistical: none` measure: the
    # fake eda never prints it, so judging it would fail every sample. The
    # gate must leave it out and name the bench as not scored.
    ws = make_ws(tmp_path, "mc: {enabled: true, runs: 60, yield_min: 0.9}\n")
    spec_with(ws, "", "  - name: decision\n"
                      "    bounds: {min: 2.5}\n"
                      "    statistical: none\n"
                      "    mc_waiver: a nominal 1 mV decision\n")
    shutil.copy(ws / "tb" / "mirror_tb.cir", ws / "tb" / "decide_tb.cir")
    (ws / "tb" / "decide_tb.bounds.json").write_text(json.dumps(
        [{"measure": "decision", "min": 2.5}]), encoding="utf-8")
    monkeypatch.setattr(sim_run, "EDA_BIN", make_fake_eda(tmp_path, passing()))
    code, out = run_mc(ws, capsys)
    assert out["nominal_only"] == ["decision"], out
    assert "decide_tb.cir" in out["not_scored"]
    assert {r["bench"] for r in out["results"]} == {"mirror_tb.cir"}


# --- resistor mismatch (the PDK's Xyce A_R carried into the ngspice copy) ---

RES_NETLIST = """\
.subckt leg a
XRL a 0 0 ppolyf_u r_width=2e-6 r_length=10e-6
.ends
XRP a b 0 ppolyf_u r_width={wid} r_length=10e-6 m=2
XRN a b 0 nplus_u r_width=1e-6 r_length=4e-6 s=2
XRM a b 0 rm1 r_width=1e-6 r_length=1e-6
XRH a b 0 ppolyf_u_1k r_width=1e-6 r_length=1e-6
XRO a b 0 npolyf_u r_width=1e-6 r_length=1e-6 mis_r=0.01
XRW a b 0 pplus_u w=1e-6 l=1e-6
XL a leg m=4
"""


def test_resistor_mismatch_term_is_written_from_each_instance_size():
    new, par, res = check_mc.mc_rewrite(RES_NETLIST)
    by = {d["instance"]: d for d in res}
    assert set(by) == {"XRL", "XRP", "XRN", "XRM", "XRH", "XRO", "XRW"}
    assert "no_term" in by["XRM"] and "no_term" in by["XRH"]
    assert "unhandled" in by["XRO"] and "unhandled" in by["XRW"]
    assert not any(k in by[n] for n in ("XRL", "XRP", "XRN")
                   for k in ("no_term", "unhandled"))
    assert by["XRP"]["a_r_pct_um"] == 2.1
    # m=2 gets par=2 first, and the term reads it: sigma/sqrt(2)
    assert ("XRP a b 0 ppolyf_u r_width={wid} r_length=10e-6 m=2 par=2 "
            "mis_r='agauss(0,0.7071*0.021*1e-06/sqrt((2)*1*((10e-6)-2*(2e-11))"
            "*((wid)-2*(2.55e-08))),1)'\n") in new
    assert "*(2)*((4e-6)-2*(2e-11))*((1e-6)-2*(-5e-08))" in new
    # an m=4 subckt carries its multiplier down to the resistor's par
    assert ("XRL a 0 0 ppolyf_u r_width=2e-6 r_length=10e-6 par={mc_mult} "
            "mis_r='agauss(0,0.7071*0.021*1e-06/sqrt((mc_mult)*") in new
    assert [d["instance"] for d in par] == ["XRL", "XRP", "XL"]
    for keep in ("rm1 r_width=1e-6 r_length=1e-6\n", "mis_r=0.01\n",
                 "pplus_u w=1e-6 l=1e-6\n", "ppolyf_u_1k r_width=1e-6 "
                 "r_length=1e-6\n"):
        assert keep in new
    # idempotent: a second pass leaves the written terms alone
    again, _, res2 = check_mc.mc_rewrite(new)
    assert all("unhandled" in d for d in res2 if d["instance"] in
               ("XRL", "XRP", "XRN"))
    assert again.count("agauss") == new.count("agauss") == 3


def test_resistor_sigma_expr_is_the_xyce_pelgrom_term():
    import math
    for model, (a_r, dl, dw) in check_mc.RES_MISMATCH.items():
        expr = check_mc.resistor_sigma_expr(
            model, {"r_width": "2e-6", "r_length": "10e-6", "par": "4"})
        head, tail = "'agauss(0,", ",1)'"
        assert expr.startswith(head) and expr.endswith(tail)
        sigma = eval(expr[len(head):-len(tail)], {"sqrt": math.sqrt})
        want = 0.7071 * a_r * 1e-6 / math.sqrt(
            4 * (10e-6 - 2 * dl) * (2e-6 - 2 * dw))
        assert sigma == pytest.approx(want, rel=1e-12)
    # ppolyf_u: A_R 2.1 %*um over 4 x 2x10 um -> 0.7071*2.1/sqrt(80) %
    sigma = eval(check_mc.resistor_sigma_expr(
        "ppolyf_u", {"r_width": "2e-6", "r_length": "10e-6", "par": "4"}
    )[len("'agauss(0,"):-len(",1)'")], {"sqrt": math.sqrt})
    assert sigma == pytest.approx(0.007071 * 2.1 / math.sqrt(80), rel=0.02)


@pytest.mark.slow
@pytest.mark.parametrize("model", sorted(check_mc.RES_MISMATCH))
def test_real_resistor_mismatch_matches_the_xyce_a_r(tmp_path, model):
    # Known answer: one seeded op over 200 pairs of one 2x100 um resistor,
    # each driven by its own current source. Each resistor's sample sigma
    # (dR/R) must sit within the chi-square 99.9% band of the Xyce deck's
    # 0.7071*A_R/sqrt(W_eff*L_eff), and a pair's difference within that of
    # A_R/sqrt(W*L) (the draws are per instance). The end terminals (rt1,
    # rt2: one square each, no mismatch term) dilute it by under 3% at 50
    # squares. An m=4 instance next to them, rewritten to par=4, has half the
    # sigma. The planted fault - the netlist as written - has no spread at
    # all, and with mismatch off the rewrite leaves every R where it was.
    import math
    import re
    import statistics

    import corners as corners_mod
    import simlib

    a_r, dl, dw = check_mc.RES_MISMATCH[model]
    n, w, ln = 200, 2e-6, 100e-6
    lines = [f"* {model} known answer"]
    for i in range(n):
        for tag, mult, cur in (("a", "", "1e-5"), ("b", "", "1e-5"),
                               ("c", " m=4", "4e-5")):
            lines += [f"XR{tag}{i} {tag}{i} 0 0 {model} r_width={w:g} "
                      f"r_length={ln:g}{mult}",
                      f"I{tag}{i} 0 {tag}{i} {cur}"]
    net = tmp_path / "ka.cir"
    net.write_text("\n".join(lines) + "\n", encoding="utf-8")
    header = (CORPUS_BANDGAP / "tb" / "bandgap_tb.cir").read_text(
        encoding="utf-8").split(".temp {{TEMP_C}}")[0]
    header = header[header.index(".include '{{PDK}}"):]
    bench = (header + ".temp {{TEMP_C}}\n.include '{{NETLIST}}'\n"
             ".control\nset numdgt=10\nop\nprint all\n.endc\n.end\n")
    eda = sim_run.EDA_BIN
    root = sim_run.toolchain_root(eda)
    tt = corners_mod.corners_by_name(corners_mod.load(), ["tt"])[0]

    def volts(fixed: bool, mismatch: bool):
        out = tmp_path / f"{'mc' if fixed else 'asis'}_{int(mismatch)}"
        nl, _, res = (check_mc.mc_netlist(net, out) if fixed
                      else (net, [], []))
        assert len(res) == (3 * n if fixed else 0)
        assert not any("unhandled" in d or "no_term" in d for d in res)
        text = check_mc.inject_mc_params(bench, 11, False) if mismatch \
            else bench
        deck = out / "ka_tb.cir"
        out.mkdir(parents=True, exist_ok=True)
        deck.write_text(simlib.materialize(
            text, sim_run.build_subs(root, nl, tt, 3.3, {})), encoding="utf-8")
        stdout, _, _ = simlib.run_ngspice(eda, deck, out, 120)
        v = {k.lower(): float(x) for k, x in re.findall(
            r"^\s*(\S+)\s*=\s*([-+0-9.eE]+)\s*$", stdout, re.MULTILINE)}
        return {t: [v[f"{t}{i}"] for i in range(n)] for t in "abc"}

    got = volts(True, True)
    nom = volts(True, False)
    rel = {t: [x / y - 1 for x, y in zip(got[t], nom[t])] for t in "abc"}
    one = 0.7071 * a_r * 1e-6 / math.sqrt((ln - 2 * dl) * (w - 2 * dw))
    lo = math.sqrt(check_mc.chi2_quantile(0.0005, n - 1) / (n - 1))
    hi = math.sqrt(check_mc.chi2_quantile(0.9995, n - 1) / (n - 1))
    sd_a = statistics.stdev(rel["a"])
    sd_pair = statistics.stdev([x - y for x, y in zip(rel["a"], rel["b"])])
    sd_m4 = statistics.stdev(rel["c"])
    print(f"{model}: sd {sd_a:.3e} want {one:.3e}; pair {sd_pair:.3e} want "
          f"{one * math.sqrt(2):.3e}; m=4 {sd_m4:.3e} want {one / 2:.3e}")
    assert one * lo * 0.97 < sd_a < one * hi, (sd_a, one)
    assert one * math.sqrt(2) * lo * 0.97 < sd_pair < one * math.sqrt(2) * hi
    assert one / 2 * lo * 0.97 < sd_m4 < one / 2 * hi, (sd_m4, one / 2)
    # planted fault: the netlist as written, mis_r=0 - no spread at all
    asis = volts(False, True)
    assert len(set(asis["a"])) == 1
    # with mismatch off the rewrite changes no resistance
    assert nom == volts(False, False)

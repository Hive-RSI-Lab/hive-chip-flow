"""engine/scripts/optimise.py: the sizing optimise loop, `start` + `numeric`
(docs/design.md section 4, "### M8."). Fast tests fake `eda` with a simple,
deterministic function of the sizing parameter it was handed (y = x), so
scipy's real differential_evolution runs against a trivial but REAL
objective with no actual ngspice call. test_real_r2r_dac_optimise_reaches_
bounds_from_wrong_start (slow) runs the whole thing against the REAL
corpus/ade/r2r_dac rung and the real gf180mcuD toolchain - this milestone's
own done criterion.

The RTL loop (M7: `start`/`trial`/`finish`) is tested below the M8 tests:
fast ones over real git and hashing with the tools faked, and one slow
test on the real corpus UART with the real tools."""
from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
CORPUS_R2R = REPO / "corpus" / "ade" / "r2r_dac"
CORPUS_OTA = REPO / "corpus" / "ade" / "ota"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import optimise  # noqa: E402
import sim_run  # noqa: E402
from checklib import CheckError  # noqa: E402

import pytest  # noqa: E402

SPEC_YAML = "top: widget\nsupply: {vdd: 3.3}\ndevices: [xm1]\nrequirements: []\n"

BENCH_TEMPLATE = """\
.include '{{PDK}}/libs.tech/ngspice/design.spice'
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}
.temp {{TEMP_C}}
.include '{{NETLIST}}'
{{SIZING}}
.control
op
print v(vdd)
.endc
.end
"""


def make_ws(tmp_path: Path, start_value: float = -3.0) -> Path:
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb", "sizing"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
    (ws / "netlist" / "widget.cir").write_text("* netlist\n", encoding="utf-8")
    (ws / "tb" / "widget_tb.cir").write_text(BENCH_TEMPLATE, encoding="utf-8")
    (ws / "tb" / "widget_tb.bounds.json").write_text(
        json.dumps([{"measure": "y", "min": 0.9, "max": 1.1}]),
        encoding="utf-8")
    import yaml
    (ws / "sizing" / "sizing.yaml").write_text(yaml.safe_dump({
        "x": {"value": start_value, "min": -5.0, "max": 5.0}}), encoding="utf-8")
    return ws


def make_identity_fake_eda(tmp_path: Path) -> Path:
    """Reports measure y = the deck's own `.param x=...` value - a trivial,
    real (not mocked out) function scipy's differential_evolution optimizes
    against, with no ngspice call."""
    fake_root = tmp_path / "fake_toolchain"
    (fake_root / "foss" / "pdks" / "gf180mcuD").mkdir(parents=True)
    script = tmp_path / "fake_eda"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = --print-toolchain-root ]; then\n"
        f"  echo '{fake_root}'\n  exit 0\nfi\n"
        "deck=\"$3\"\n"
        "x=$(grep -oE '\\.param x=[-0-9.eE+]+' \"$deck\" | cut -d= -f2)\n"
        "echo\n"
        "echo '  Measurements for Transient Analysis'\n"
        "echo \"y                     =  $x\"\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_start_freezes_evaluator_and_writes_empty_tsv(tmp_path):
    ws = make_ws(tmp_path)
    payload, _ = optimise.run_start(
        ["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    assert payload["status"] == "pass"
    meta = json.loads((ws / optimise.META_PATH).read_text())
    assert meta["target"] == "sizing/sizing.yaml"
    tsv_lines = (ws / optimise.TSV_PATH).read_text().splitlines()
    assert tsv_lines == ["\t".join(optimise.TSV_FIELDS)]


def test_numeric_without_start_raises(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    eda = make_identity_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    with pytest.raises(CheckError):
        optimise.run_numeric(["--workspace", str(ws)])


def test_numeric_detects_evaluator_drift(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    # a bench edit after `start` must be caught, not silently trusted.
    (ws / "tb" / "widget_tb.cir").write_text(BENCH_TEMPLATE + "* edited\n",
                                             encoding="utf-8")
    eda = make_identity_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    with pytest.raises(CheckError):
        optimise.run_numeric(["--workspace", str(ws)])


def test_numeric_reaches_bounds_from_a_wrong_start(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, start_value=-3.0)  # deliberately wrong: -3 is
    # nowhere near [0.9, 1.1], the bound y=x must land inside.
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    eda = make_identity_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "8", "--maxiter", "20", "--seed", "1"])
    assert payload["status"] == "pass", payload
    assert payload["improved"] is True
    assert payload["best_score"] >= 0
    import yaml
    final = yaml.safe_load((ws / "sizing" / "sizing.yaml").read_text())
    assert 0.9 <= final["x"]["value"] <= 1.1

    tsv_lines = (ws / optimise.TSV_PATH).read_text().splitlines()
    assert len(tsv_lines) > 2  # header + start row + at least one DE trial
    assert tsv_lines[1].split("\t")[-1] == "starting sizing, full corner set"
    assert tsv_lines[2].split("\t")[-1] == "starting sizing"

    # the identity fake is corner-invariant (y = x whatever .lib section the
    # deck selects), so the winner passes sim_pvt's own run, which gates
    # the write and is run once more on the file as written
    assert payload["full_corner_pass"] is True
    assert set(payload["full_corner_corners"]) == {"tt", "ss", "ff", "sf", "fs"}
    assert payload["full_corner_violations"] == []
    last = tsv_lines[-1].split("\t")
    assert last[-1] == "on disk, full corner set" and last[-2] == "True"
    # the on-disk row names the file as written, not the start
    assert last[2] == optimise.target_sha(optimise.load_sizing_yaml(
        ws / "sizing" / "sizing.yaml"))
    assert last[2] != tsv_lines[1].split("\t")[2]


def test_numeric_leaves_file_untouched_when_nothing_improves(tmp_path, monkeypatch):
    # start already at the optimum (y = x = 1.0, comfortably inside
    # [0.9, 1.1]) - the search must not report an "improvement" that never
    # happened, and must never rewrite a file it did not actually beat.
    ws = make_ws(tmp_path, start_value=1.0)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    eda = make_identity_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "6", "--maxiter", "10", "--seed", "1"])
    assert payload["status"] == "pass", payload
    import yaml
    final = yaml.safe_load((ws / "sizing" / "sizing.yaml").read_text())
    assert final["x"]["value"] == 1.0


def test_tsv_rows_survive_a_crash_mid_search(tmp_path, monkeypatch):
    # trials.tsv used to be buffered in memory and written once, all at
    # the end, AFTER differential_evolution returned - a crash partway
    # through a real (minutes-long, hundreds-of-ngspice-calls) search lost
    # every trial that had already run. Each row must be flushed to disk
    # as its own trial completes, not batched.
    ws = make_ws(tmp_path)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    eda = make_identity_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)

    calls = {"n": 0}

    def crashing_de(objective, bounds, **kwargs):
        for _ in range(3):
            objective([calls["n"]])  # each call appends its own tsv row
            calls["n"] += 1
        raise RuntimeError("simulated crash mid-search")

    import scipy.optimize
    monkeypatch.setattr(scipy.optimize, "differential_evolution", crashing_de)

    with pytest.raises(RuntimeError):
        optimise.run_numeric(["--workspace", str(ws)])

    # despite the crash, every trial that ran before it (the start's
    # full-corner row and search row + the 3 objective() calls) is
    # already on disk.
    tsv_lines = (ws / optimise.TSV_PATH).read_text().splitlines()
    assert len(tsv_lines) == 1 + 2 + 3, tsv_lines


def make_corner_fake_eda(tmp_path: Path, shifts: dict[str, str]) -> Path:
    """Like make_identity_fake_eda, but at each corner in `shifts` the
    reported y is `x` run through that corner's awk expression (`x` bound)
    - a corner-sensitive circuit with no ngspice call."""
    fake_root = tmp_path / "fake_toolchain"
    (fake_root / "foss" / "pdks" / "gf180mcuD").mkdir(parents=True,
                                                         exist_ok=True)
    script = tmp_path / "corner_fake_eda"
    lines = ["#!/usr/bin/env bash",
             "if [ \"$1\" = --print-toolchain-root ]; then",
             f"  echo '{fake_root}'", "  exit 0", "fi",
             "deck=\"$3\"",
             "x=$(grep -oE '\\.param x=[-0-9.eE+]+' \"$deck\" | cut -d= -f2)",
             "y=\"$x\""]
    for corner, expr in shifts.items():
        lines.append(f"if grep -q \"sm141064.spice' {corner}$\" \"$deck\"; "
                     f"then y=$(awk -v x=\"$x\" 'BEGIN{{print {expr}}}'); fi")
    lines += ["echo", "echo '  Measurements for Transient Analysis'",
              "echo \"y                     =  $y\""]
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _x_on_disk(ws: Path) -> float:
    import yaml
    return yaml.safe_load(
        (ws / "sizing" / "sizing.yaml").read_text())["x"]["value"]


def test_planted_corner_fault_is_not_written(tmp_path, monkeypatch):
    # The planted fault: x = 1.0 is dead centre at tt, so a tt-only search
    # keeps it, but ss reads x + 0.15 = 1.15 > 1.1, so sim_pvt fails it.
    # Every bound holds at every corner only for x in [0.9, 0.95]; the
    # corner-aware loop must find that and write only a sizing that passes
    # sim_pvt's own run.
    ws = make_ws(tmp_path, start_value=1.0)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    monkeypatch.setattr(sim_run, "EDA_BIN",
                        make_corner_fake_eda(tmp_path, {"ss": "x+0.15"}))

    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "6", "--maxiter", "15", "--seed", "1"])

    assert payload["start_full_corner_pass"] is False
    assert payload["status"] == "pass", payload
    assert payload["written"] is True and payload["full_corner_pass"] is True
    assert 0.9 <= _x_on_disk(ws) <= 0.95
    # ss, the corner that failed the start, is in the search from round 1
    assert "ss" in payload["rounds"][0]["corners"]

    import check_sim_pvt
    result, _ = check_sim_pvt.sweep(ws)
    assert result["violations"] == []


def test_failed_winner_re_picks_corners(tmp_path, monkeypatch):
    # --bounding 1 searches tt + ss only (ss fails the start worst). The
    # tt+ss optimum (x ~ 0.925) then fails ff (x - 0.04 < 0.9), so round 2
    # adds ff and lands in the only all-corner window, [0.94, 0.95].
    ws = make_ws(tmp_path, start_value=1.0)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    monkeypatch.setattr(sim_run, "EDA_BIN", make_corner_fake_eda(
        tmp_path, {"ss": "x+0.15", "ff": "x-0.04"}))

    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--bounding", "1", "--popsize", "8",
         "--maxiter", "30", "--seed", "1"])

    rounds = payload["rounds"]
    assert set(rounds[0]["corners"]) == {"tt", "ss"}
    assert rounds[0]["winner_pass"] is False
    assert "ff" in rounds[1]["corners"] and rounds[1]["winner_pass"] is True
    assert payload["status"] == "pass", payload
    assert 0.94 <= _x_on_disk(ws) <= 0.95


def test_start_failing_typical_searches_typical_first(tmp_path, monkeypatch):
    # x = -3 misses [0.9, 1.1] even at tt, so the typical-only stage runs
    # first, scoring tt alone: its winner sits near 1.0, which ss
    # (x + 0.15) fails. At -3 every corner is equally far off, so the
    # start's one bounding corner is ff (the first name); the stage
    # winner's failure adds ss. The corner round searches those from the
    # stage's last population and lands in the only all-corner window,
    # [0.9, 0.95].
    ws = make_ws(tmp_path, start_value=-3.0)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    monkeypatch.setattr(sim_run, "EDA_BIN",
                        make_corner_fake_eda(tmp_path, {"ss": "x+0.15"}))
    import scipy.optimize
    real_de, inits = scipy.optimize.differential_evolution, []

    def recording_de(objective, bounds, **kwargs):
        inits.append(kwargs.get("init", "latinhypercube"))
        return real_de(objective, bounds, **kwargs)
    monkeypatch.setattr(scipy.optimize, "differential_evolution", recording_de)

    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--bounding", "1", "--popsize", "6",
         "--maxiter", "15", "--seed", "1"])

    stage = payload["typical_stage"]
    assert stage["start_typical_margin"] < 0
    assert stage["winner_pass"] is False
    assert 0.95 < stage["winner_sizing"]["x"] <= 1.1
    assert payload["rounds"][0]["corners"] == ["tt", "ff", "ss"]
    assert inits[0] == "latinhypercube" and not isinstance(inits[1], str)
    assert payload["status"] == "pass", payload
    assert 0.9 <= _x_on_disk(ws) <= 0.95

    # a start that passes typical goes straight to the corner rounds
    ws2 = make_ws(tmp_path / "b", start_value=1.0)
    optimise.run_start(["--workspace", str(ws2), "--target", "sizing/sizing.yaml"])
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws2), "--popsize", "6", "--maxiter", "15",
         "--seed", "1"])
    assert payload["typical_stage"] is None


def test_no_full_corner_pass_writes_nothing(tmp_path, monkeypatch):
    # ss reads 0.2 whatever x is: no sizing passes sim_pvt, so nothing is
    # written - the file stays byte-identical - and the run says so (status
    # violations, with a remediation), rather than writing a tt-only winner.
    ws = make_ws(tmp_path, start_value=1.0)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    before = (ws / "sizing" / "sizing.yaml").read_bytes()
    monkeypatch.setattr(sim_run, "EDA_BIN",
                        make_corner_fake_eda(tmp_path, {"ss": "0.2"}))

    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "6", "--maxiter", "8",
         "--rounds", "2", "--seed", "1"])

    assert payload["status"] == "violations"
    assert payload["written"] is False and payload["improved"] is False
    assert (ws / "sizing" / "sizing.yaml").read_bytes() == before
    assert payload["remediation"]
    corner_names = {"tt", "ss", "ff", "sf", "fs"}
    bad = {r for v in payload["full_corner_violations"]
           for r in v["refs"] if r in corner_names}
    assert bad == {"ss"}
    last = (ws / optimise.TSV_PATH).read_text().splitlines()[-1].split("\t")
    assert last[-1] == "on disk, full corner set" and last[-2] == "False"


def test_every_kth_kept_trial_runs_the_full_corner_set(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    monkeypatch.setattr(sim_run, "EDA_BIN", make_identity_fake_eda(tmp_path))
    optimise.run_numeric(["--workspace", str(ws), "--check-every", "1",
                          "--popsize", "6", "--maxiter", "6", "--seed", "1"])
    rows = [ln.split("\t") for ln in
            (ws / optimise.TSV_PATH).read_text().splitlines()[1:]]
    kept_search = [r for r in rows if r[-2] == "True"
                   and "full corner set" not in r[-1]]
    checks = [r for r in rows if r[-1] == "kept trial, full corner set"]
    # every kept search trial is followed by its check, bar the start's
    # own (trial 0 already ran it)
    assert len(checks) == len(kept_search) - 1 >= 1
    assert all(r[-1] != "kept trial, full corner set"
               for r in rows[:3]), rows[:3]


def test_bounding_corners_picks_the_least_margin(tmp_path):
    bench = Path("widget_tb.cir")
    benches = [(bench, "", [{"measure": "y", "min": 0.9, "max": 1.1}])]

    def res(corner, y):
        return {"bench": bench.name, "corner": corner, "names": [corner],
                "engine_errors": [], "measures": {"y": y}}
    results = [res("tt", 1.0), res("ss", 1.3), res("ff", 1.05),
               res("sf", 0.95), res("fs", 1.08)]
    assert optimise.bounding_corners(results, benches, "tt", 2) == ["ss", "fs"]


def test_workers_two_runs_and_three_is_refused(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    monkeypatch.setattr(sim_run, "EDA_BIN", make_identity_fake_eda(tmp_path))
    with pytest.raises(CheckError):
        optimise.run_numeric(["--workspace", str(ws), "--workers", "3"])
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--workers", "2", "--popsize", "6",
         "--maxiter", "10", "--seed", "1"])
    assert payload["status"] == "pass" and payload["written"] is True
    assert 0.9 <= _x_on_disk(ws) <= 1.1
    assert {p.name for p in (ws / "log" / "optimise").iterdir()
            if p.is_dir()} >= {"w0", "w1"}



# ------------------------------------------------- drawable sizes (snap)

def _spec(value, lo, hi, **rules):
    return {"value": value, "min": lo, "max": hi, **rules}


def test_snap_moves_each_value_to_its_grid_inside_its_range():
    out = optimise.snap({"a": _spec(1.234e-6, 1e-6, 2e-6, grid=5e-9),
                         "b": _spec(0.3, 0.0, 1.0),        # no rule: as is
                         "c": _spec(9.0, 0.0, 1.0, grid=0.3)})  # clamped
    assert out["a"]["value"] == 1.235e-6
    assert out["b"]["value"] == 0.3
    assert out["c"]["value"] == 0.9
    assert out["a"]["grid"] == 5e-9  # the rule rides along to the file


def test_snap_ties_a_ratio_to_the_snapped_base():
    # base 4.0012 snaps to 4.0; 9 is nearest 2 x (2 x 4.0); 3 is under one
    # step, so it takes the least whole multiple, 1 x (2 x 4.0)
    sizing = {"wb": _spec(4.0012, 1.0, 20.0, grid=0.005),
              "wt": _spec(9.0, 2.0, 40.0, ratio_of="wb", ratio_step=2),
              "ws": _spec(3.0, 2.0, 40.0, ratio_of="wb", ratio_step=2)}
    out = optimise.snap(sizing)
    assert out["wb"]["value"] == 4.0
    assert out["wt"]["value"] == 8.0 and out["ws"]["value"] == 8.0
    # a range holding no multiple of the base: nothing drawable here
    sizing["wt"]["max"] = 7.0
    assert optimise.snap(sizing) is None


@pytest.mark.parametrize("rules, words", [
    ({"grid": 0}, "positive number"),
    ({"grid": True}, "positive number"),
    ({"ratio_step": 2}, "no ratio_of"),
    ({"ratio_of": "nope"}, "not another parameter"),
    ({"ratio_of": "y", "grid": 0.1}, "both grid and ratio_of"),
    ({"ratio_of": "y", "ratio_step": 0}, "whole number"),
    ({"ratio_of": "z"}, "itself a ratio_of"),
])
def test_malformed_drawable_rule_is_refused(tmp_path, rules, words):
    import yaml
    path = tmp_path / "sizing.yaml"
    path.write_text(yaml.safe_dump({
        "x": _spec(1.0, 0.0, 2.0, **rules), "y": _spec(1.0, 0.0, 2.0),
        "z": _spec(1.0, 0.0, 2.0, ratio_of="y")}), encoding="utf-8")
    with pytest.raises(CheckError, match=words):
        optimise.load_sizing_yaml(path)


def test_numeric_scores_only_drawable_sizings(tmp_path, monkeypatch):
    """x on a 0.07 grid, w tied to whole multiples of 2x: every sizing
    the search hands the bench is drawable, so the winner is saved exactly
    as scored - x in [0.9, 1.1] on the grid, w an even multiple of it -
    with no rounding after the gate."""
    import yaml
    ws = make_ws(tmp_path, start_value=-3.0)
    (ws / "sizing" / "sizing.yaml").write_text(yaml.safe_dump({
        "x": _spec(-3.0, -5.0, 5.0, grid=0.07),
        "w": _spec(3.3, 0.0, 10.0, ratio_of="x", ratio_step=2)}),
        encoding="utf-8")
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    eda = make_identity_fake_eda(tmp_path)
    seen = tmp_path / "seen.txt"
    # x read anywhere on the .param line (w sorts before it)
    text = eda.read_text().replace("'\\.param x=", "' x=").replace(
        "echo\n", f"grep -oE '\\.param .*' \"$deck\" >> '{seen}'\necho\n", 1)
    eda.write_text(text)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "4", "--maxiter", "6", "--seed", "1"])
    assert payload["status"] == "pass", payload

    def drawable(x, b):
        return (abs(x / 0.07 - round(x / 0.07)) < 1e-6
                and round(b / (2 * x)) >= 1
                and abs(b / (2 * x) - round(b / (2 * x))) < 1e-6)

    import re as _re
    scored = []
    for line in seen.read_text().splitlines():
        vals = dict(_re.findall(r"(\w+)=([-0-9.eE+]+)", line))
        scored.append((float(vals["x"]), float(vals["w"])))
    searched = [(x, b) for x, b in scored if (x, b) != (-3.0, 3.3)]
    assert searched and all(drawable(x, b) for x, b in searched), searched
    final = yaml.safe_load((ws / "sizing" / "sizing.yaml").read_text())
    x, b = final["x"]["value"], final["w"]["value"]
    assert 0.9 <= x <= 1.1 and drawable(x, b)
    assert final["x"]["grid"] == 0.07 and final["w"]["ratio_of"] == "x"
    # some candidates (x <= 0) had no drawable w: logged, never simulated
    rows = (ws / optimise.TSV_PATH).read_text()
    assert "differential_evolution, not drawable" in rows

@pytest.mark.slow
def test_real_r2r_dac_optimise_reaches_bounds_from_wrong_start(tmp_path):
    """corpus/ade/r2r_dac ships with a deliberately wrong 1:1 R:2R ratio.
    Its bits are driven from a fixed reference, so the 2:1 ratio meets the
    absolute-volt bounds at every corner, supply corners included: the
    corner-aware loop must find that ratio and save it."""
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb", "sizing"):
        (ws / sub).mkdir(parents=True)
    shutil.copy2(CORPUS_R2R / "spec.yaml", ws / "spec" / "spec.yaml")
    shutil.copy2(CORPUS_R2R / "netlist" / "r2r_dac.cir",
                ws / "netlist" / "r2r_dac.cir")
    for f in (CORPUS_R2R / "tb").iterdir():
        shutil.copy2(f, ws / "tb" / f.name)
    shutil.copy2(CORPUS_R2R / "sizing" / "sizing.yaml",
                ws / "sizing" / "sizing.yaml")

    import yaml
    start = yaml.safe_load((ws / "sizing" / "sizing.yaml").read_text())
    assert start["r_length"]["value"] == start["r2_length"]["value"], (
        "the shipped corpus rung is supposed to start wrong (1:1) - if this "
        "ever fires, the corpus file itself changed under this test")

    before = (ws / "sizing" / "sizing.yaml").read_bytes()
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "8", "--maxiter", "20", "--seed", "1"])

    assert payload["status"] == "pass", payload
    assert payload["written"] is True
    assert (ws / "sizing" / "sizing.yaml").read_bytes() != before
    saved = yaml.safe_load((ws / "sizing" / "sizing.yaml").read_text())
    assert saved["r2_length"]["value"] / saved["r_length"]["value"] == pytest.approx(2.0, rel=0.05)



def _ota_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for sub in ("layout", "netlist", "sizing", "layout_ref", "tb"):
        (ws / sub).mkdir()
        for f in (CORPUS_OTA / sub).iterdir():
            if f.is_file() and f.suffix != ".gds":
                shutil.copy2(f, ws / sub / f.name)
    (ws / "spec").mkdir()
    shutil.copy2(CORPUS_OTA / "spec.yaml", ws / "spec" / "spec.yaml")
    (ws / "state.json").write_text(json.dumps(
        {"version": 3, "skill": "ade", "block": "ota"}), encoding="utf-8")
    return ws


def _assert_saved_ota_reaches_layout(ws: Path, tmp_path: Path) -> None:
    import check_analog_drc
    import check_analog_lvs
    import check_sim_pvt
    result, _ = check_sim_pvt.sweep(ws)
    assert result["violations"] == []
    out = tmp_path / "drc.json"
    code = check_analog_drc.main(["--workspace", str(ws), "--out", str(out)])
    drc = json.loads(out.read_text(encoding="utf-8"))
    assert (code, drc["status"]) == (0, "pass"), drc
    out = tmp_path / "lvs.json"
    code = check_analog_lvs.main(["--workspace", str(ws), "--out", str(out)])
    lvs = json.loads(out.read_text(encoding="utf-8"))
    assert (code, lvs["status"]) == (0, "pass"), lvs


@pytest.mark.slow
def test_real_ota_optimise_winner_from_wrong_start_reaches_layout(tmp_path):
    """corpus/ade/ota from a wrong start: w_t off its even w_b ratio (gen_ota
    refuses to draw it) and a comp cap too small for 60 degrees of phase
    margin. The search scores only drawable sizings, so the winner it saves
    passes sim_pvt as written and regenerates a layout that passes drc and
    lvs, with nothing rounded in between. The rest of the sizing is the
    corpus's passing one, held fixed (min = max) so the test spends its
    trials on the two values the wrong start broke: w_t, which must stay an
    even multiple of w_b, and cc_w."""
    import yaml
    ws = _ota_workspace(tmp_path)
    path = ws / "sizing" / "sizing.yaml"
    start = yaml.safe_load(path.read_text(encoding="utf-8"))
    start["w_t"]["value"] = 9.0e-6    # 2.25 units of w_b: not drawable
    start["cc_w"]["value"] = 12.0e-6  # 0.29 pF: phase margin under 60
    for name, spec in start.items():
        if name not in ("w_t", "cc_w"):
            spec["min"] = spec["max"] = spec["value"]
    path.write_text(yaml.safe_dump(start), encoding="utf-8")

    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "1", "--maxiter", "5", "--seed", "1"])
    assert payload["start_full_corner_pass"] is False
    assert payload["status"] == "pass" and payload["written"] is True, payload

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    units = saved["w_t"]["value"] / saved["w_b"]["value"]
    assert units == pytest.approx(round(units)) and round(units) % 2 == 0
    _assert_saved_ota_reaches_layout(ws, tmp_path)

@pytest.mark.slow
def test_real_ota_optimise_converges_from_a_random_start(tmp_path):
    """corpus/ade/ota with all 13 sizes free, from a random drawable start
    (seeded, so the run repeats): nothing of the corpus's passing sizing
    survives but the bounds. The start fails even at typical, so the
    typical-only stage runs first; the winner saved must pass sim_pvt and
    regenerate a layout that passes drc and lvs."""
    import random
    import yaml
    ws = _ota_workspace(tmp_path)
    path = ws / "sizing" / "sizing.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rng = random.Random(11)
    while True:
        start = optimise.snap({n: {**s, "value": rng.uniform(s["min"], s["max"])}
                               for n, s in optimise.load_sizing_yaml(path).items()})
        if start:
            break
    for name in raw:
        raw[name]["value"] = start[name]["value"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "2", "--maxiter", "10",
         "--rounds", "2", "--seed", "1"])
    assert payload["start_full_corner_pass"] is False
    assert payload["typical_stage"]["start_typical_margin"] < 0
    assert payload["status"] == "pass" and payload["written"] is True, payload
    _assert_saved_ota_reaches_layout(ws, tmp_path)


# ------------------------------------------------------------ RTL loop (M7)
#
# Fast tests: real git, real hashing, the real diff/revert and must_keep
# check; only the tools are faked. run_constraints fails `sim` when the RTL
# says SIMFAIL; run_metric's area is the RTL's line count and its netlist
# holds one cell per `cellname u_name (` instance line, so deleting an
# instance really does drop it from what must_keep_missing reads.

import re  # noqa: E402
import subprocess  # noqa: E402

RTL_SPEC = """\
top: blk
requirements:
  - {id: REQ-A, text: a, check: sim}
ports:
  clk: {dir: input, width: 1}
  q: {dir: output, width: 1}
clock: {period_ns: 20, domains: [clk]}
must_keep: [u_ring]
"""
RTL_BASE = """\
module blk (input wire clk, output reg q);
  keeper u_ring (.a(q));
  // pad 1
  // pad 2
  // pad 3
  always @(posedge clk) q <= ~q;
endmodule
"""


def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(ws), *args], check=True,
                          capture_output=True, text=True).stdout


def make_rtl_ws(tmp_path: Path, monkeypatch) -> Path:
    ws = tmp_path / "ws"
    for sub in ("spec", "rtl", "tb", "formal", "holdout"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(RTL_SPEC, encoding="utf-8")
    (ws / "rtl" / "blk.v").write_text(RTL_BASE, encoding="utf-8")
    (ws / "tb" / "test_blk.py").write_text("# req: REQ-A\n", encoding="utf-8")
    (ws / "formal" / "blk_formal.sv").write_text("// f\n", encoding="utf-8")
    (ws / "holdout" / "test_h.py").write_text("# h\n", encoding="utf-8")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")

    lib_root = tmp_path / "tc"
    cs = optimise_check_synth()
    lib = cs.liberty_path(lib_root, cs.DEFAULT_STD_CELL)
    lib.parent.mkdir(parents=True)
    lib.write_text("library (fake) {}\n", encoding="utf-8")
    monkeypatch.setattr(optimise_check_synth(), "toolchain_root",
                        lambda timeout=30.0: lib_root)

    def fake_constraints(ew, detail):
        text = (ew / "rtl" / "blk.v").read_text(encoding="utf-8")
        sim = "fail" if "SIMFAIL" in text else "pass"
        if sim == "fail":
            detail.append("check_sim: test_blk failed")
        return {"lint": "pass", "sim": sim,
                "formal": "pass" if sim == "pass" else "skipped"}

    def fake_metric(ew, ev, profile, with_power):
        text = (ew / "rtl" / "blk.v").read_text(encoding="utf-8")
        cells = {m: {} for m in re.findall(r"^\s*\w+ (u_\w+) \(", text, re.M)}
        nl = {"modules": {"blk": {"cells": cells, "netnames": {}, "ports": {
            "clk": {"direction": "input", "bits": [2]},
            "q": {"direction": "output", "bits": [3]}}}}}
        keep = json.loads((ev / "must_keep.json").read_text(encoding="utf-8"))
        missing = optimise.must_keep_missing(nl, "blk", keep)
        ports = json.loads((ev / "ports.json").read_text(encoding="utf-8"))
        return {"area": float(len(text.splitlines())), "slack": 5.0,
                "power": 1e-3, "synth": "pass",
                "ports": "fail" if optimise.port_mismatch(nl, "blk", ports)
                else "pass",
                "must_keep": "fail" if missing else "pass",
                "detail": [f"must_keep {n} removed" for n in missing]}

    monkeypatch.setattr(optimise, "run_constraints", fake_constraints)
    monkeypatch.setattr(optimise, "run_metric", fake_metric)
    return ws


def optimise_check_synth():
    import check_synth
    return check_synth


def rtl_start(ws: Path, **kw) -> dict:
    argv = ["--workspace", str(ws), "--target", "rtl/blk.v"]
    for k, v in kw.items():
        argv += [f"--{k}", str(v)]
    payload, _ = optimise.run_rtl_start(argv)
    return payload


def rtl_trial(ws: Path, note: str = "t") -> dict:
    payload, _ = optimise.run_trial(["--workspace", str(ws), "--note", note])
    return payload


def tsv_rows(ws: Path) -> list[dict]:
    import csv
    with open(ws / "optimise" / "trials.tsv", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def edit_target(ws: Path, old: str, new: str) -> None:
    p = ws / "rtl" / "blk.v"
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_rtl_start_freezes_evaluator_and_scores_the_baseline(tmp_path, monkeypatch):
    ws = make_rtl_ws(tmp_path, monkeypatch)
    payload = rtl_start(ws)
    ev = ws / "optimise" / "evaluator"
    for name in ("synth.ys", "design.sdc", "liberty.json", "ports.json",
                 "must_keep.json", "holdout.json", "spec.yaml",
                 "tb/test_blk.py", "formal/blk_formal.sv"):
        assert (ev / name).is_file(), name
    assert "create_clock -name clk -period 20" in (ev / "design.sdc").read_text()
    meta = payload["meta"]
    assert meta["evaluator_sha"] == optimise.hash_rtl_evaluator(ws)
    assert meta["baseline"]["area"] == 7.0
    rows = tsv_rows(ws)
    assert list(rows[0]) == optimise.RTL_TSV_FIELDS
    assert rows[0]["trial"] == "0" and rows[0]["kept"] == "True"


def test_rtl_start_refuses_a_dirty_tree(tmp_path, monkeypatch):
    ws = make_rtl_ws(tmp_path, monkeypatch)
    (ws / "tb" / "test_blk.py").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(CheckError, match="uncommitted changes"):
        rtl_start(ws)


def test_rtl_kept_trial_is_committed_and_a_worse_one_reverted(tmp_path, monkeypatch):
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    edit_target(ws, "  // pad 1\n", "")
    kept = rtl_trial(ws, "drop a pad line")
    assert kept["kept"] is True and kept["area"] == 6.0
    assert "optimise trial 1: drop a pad line" in git(ws, "log", "-1", "--format=%s")
    edit_target(ws, "  // pad 2\n", "  // pad 2\n  // more\n  // more\n")
    worse = rtl_trial(ws, "grow it")
    assert worse["kept"] is False
    assert "// more" not in (ws / "rtl" / "blk.v").read_text()
    rows = tsv_rows(ws)
    assert [r["kept"] for r in rows] == ["True", "True", "False"]


def test_rtl_evaluator_edit_mid_loop_aborts_and_reverts(tmp_path, monkeypatch):
    """M7 done-criterion: an evaluator edit mid-loop aborts and reverts."""
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    edit_target(ws, "  // pad 1\n", "")
    assert rtl_trial(ws)["kept"] is True
    kept_text = (ws / "rtl" / "blk.v").read_text()

    edit_target(ws, "  // pad 2\n", "")
    sdc = ws / "optimise" / "evaluator" / "design.sdc"
    sdc.write_text(sdc.read_text().replace("-period 20", "-period 200"))
    with pytest.raises(CheckError, match="evaluator changed"):
        rtl_trial(ws, "loosen the clock")
    assert (ws / "rtl" / "blk.v").read_text() == kept_text
    rows = tsv_rows(ws)
    assert rows[-1]["kept"] == "False" and "ABORTED" in rows[-1]["note"]
    with pytest.raises(CheckError, match="aborted"):
        rtl_trial(ws)


def test_rtl_edit_outside_target_is_reverted(tmp_path, monkeypatch):
    """M7 done-criterion: an edit outside the target is reverted."""
    ws = make_rtl_ws(tmp_path, monkeypatch)
    lock = ws / "state.json.lock"  # state.py's own lock: engine output
    lock.write_text("", encoding="utf-8")
    git(ws, "add", "state.json.lock")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "lock")
    rtl_start(ws)
    lock.write_text("held\n", encoding="utf-8")
    tb = ws / "tb" / "test_blk.py"
    tb.write_text("# req: REQ-A\nassert True  # weakened\n", encoding="utf-8")
    (ws / "spec" / "extra.yaml").write_text("x: 1\n", encoding="utf-8")
    (ws / "holdout" / "test_h.py").unlink()
    edit_target(ws, "  // pad 1\n", "")
    payload = rtl_trial(ws, "shrink, and touch the tests")
    assert sorted(payload["reverted_outside_target"]) == [
        "holdout/test_h.py", "spec/extra.yaml", "tb/test_blk.py"]
    assert tb.read_text() == "# req: REQ-A\n"
    assert lock.read_text() == "held\n"
    assert not (ws / "spec" / "extra.yaml").exists()
    assert (ws / "holdout" / "test_h.py").read_text() == "# h\n"
    # the target's own change was still scored, and kept
    assert payload["kept"] is True
    assert "tb/test_blk.py" not in git(ws, "show", "--stat", "HEAD")
    assert "reverted outside target" in tsv_rows(ws)[-1]["note"]


def test_rtl_trial_failing_sim_is_logged_not_kept(tmp_path, monkeypatch):
    """M7 done-criterion: a trial failing `sim` is logged as not kept -
    even though it is smaller."""
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    edit_target(ws, "  // pad 1\n  // pad 2\n", "  // SIMFAIL\n")
    payload = rtl_trial(ws, "smaller but wrong")
    assert payload["kept"] is False
    assert payload["constraints"]["sim"] == "fail"
    row = tsv_rows(ws)[-1]
    assert row["sim"] == "fail" and row["kept"] == "False"
    assert row["area"] == "" and "rejected (sim)" in row["note"]
    assert "SIMFAIL" not in (ws / "rtl" / "blk.v").read_text()


def test_rtl_trial_removing_must_keep_cell_is_rejected(tmp_path, monkeypatch):
    """M7 done-criterion: a trial removing a `must_keep` cell is rejected,
    even though it is smaller and passes sim."""
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    edit_target(ws, "  keeper u_ring (.a(q));\n", "")
    payload = rtl_trial(ws, "the ring does nothing the tests see")
    assert payload["kept"] is False
    assert payload["constraints"]["sim"] == "pass"
    assert payload["constraints"]["must_keep"] == "fail"
    assert payload["area"] < 7.0  # smaller, and still rejected
    row = tsv_rows(ws)[-1]
    assert row["must_keep"] == "fail" and row["kept"] == "False"
    assert "u_ring" in (ws / "rtl" / "blk.v").read_text()


def test_must_keep_missing_reads_flattened_names():
    nl = {"modules": {"top": {"cells": {"u_ring.inv0": {}, "$abc$1": {}},
                              "netnames": {"\\sync_q": {}}}}}
    assert optimise.must_keep_missing(nl, "top", ["u_ring", "sync_q"]) == []
    assert optimise.must_keep_missing(nl, "top", ["inv0"]) == []
    assert optimise.must_keep_missing(nl, "top", ["u_gone"]) == ["u_gone"]


def test_port_mismatch_catches_a_dropped_or_narrowed_port():
    ports = {"a": {"dir": "input", "width": 8}, "y": {"dir": "output"}}
    nl = {"modules": {"t": {"ports": {
        "a": {"direction": "input", "bits": list(range(4))}}}}}
    diffs = optimise.port_mismatch(nl, "t", ports)
    assert any(d.startswith("a:") for d in diffs)
    assert any(d.startswith("y:") for d in diffs)


def test_rtl_loop_stops_on_patience_and_trials(tmp_path, monkeypatch):
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws, trials=10, patience=2)
    rtl_trial(ws, "no change")
    payload = rtl_trial(ws, "no change again")
    assert payload["stop"] and payload["stop"].startswith("patience")
    with pytest.raises(CheckError, match="stopped"):
        rtl_trial(ws)


def test_rtl_head_moved_outside_the_loop_aborts(tmp_path, monkeypatch):
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    (ws / "tb" / "test_blk.py").write_text("# sneaky\n", encoding="utf-8")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qam", "x")
    with pytest.raises(CheckError, match="HEAD"):
        rtl_trial(ws)


def test_rtl_finish_discards_a_winner_failing_full_gates(tmp_path, monkeypatch):
    """section 4: a winner that fails holdout/formal/mutate is discarded
    and the last passing trial restored."""
    ws = make_rtl_ws(tmp_path, monkeypatch)
    rtl_start(ws)
    edit_target(ws, "  // pad 1\n", "")
    assert rtl_trial(ws, "first")["kept"]
    first = (ws / "rtl" / "blk.v").read_text()
    edit_target(ws, "  // pad 2\n", "  // DROPS_HOLDOUT\n")
    edit_target(ws, "  // pad 3\n", "")
    assert rtl_trial(ws, "second")["kept"]

    def fake_full(ws_, detail):
        bad = "DROPS_HOLDOUT" in (ws_ / "rtl" / "blk.v").read_text()
        if bad:
            detail.append("check_holdout: a held-out test failed")
        return {"holdout": "fail" if bad else "pass", "formal": "pass",
                "mutate": "pass"}

    monkeypatch.setattr(optimise, "full_gates", fake_full)
    payload, _ = optimise.run_finish(["--workspace", str(ws)])
    assert payload["status"] == "pass"
    assert payload["winner"]["trial"] == 1 and payload["discarded"] == [2]
    assert payload["tried"][0]["detail"] == ["check_holdout: a held-out test failed"]
    assert (ws / "rtl" / "blk.v").read_text() == first
    assert "restore trial 1" in git(ws, "log", "-1", "--format=%s")
    notes = [r["note"] for r in tsv_rows(ws)[-2:]]
    assert "holdout=fail" in notes[0] and "holdout=pass" in notes[1]


CORPUS_UART = REPO / "corpus" / "vde" / "uart"
# Drop the frame counter: the frame ends when only the stop bit is left in
# `shift`. Smaller, same frames - the kept area trial.
DROP_BITS_LEFT = [
    ("  localparam FRAME_BITS = 4'd11; // start + 8 data + parity + stop\n", ""),
    ("  reg [3:0]  bits_left;\n", ""),
    ("      bits_left <= 4'd0;\n", ""),
    ("        bits_left <= FRAME_BITS;\n", ""),
    ("        bits_left <= bits_left - 4'd1;\n", ""),
    ("        if (bits_left == 4'd1)\n", "        if (shift[10:1] == 10'd0)\n"),
]


@pytest.mark.slow
def test_real_uart_loop_keeps_area_win_rejects_sim_fail_and_winner_passes(tmp_path):
    """M7 on the real corpus UART and the real tools: a trial that breaks
    the start bit fails `sim` and is not kept, a trial that drops the frame
    counter is kept with less area and slack >= 0, and `finish` runs
    holdout, unbounded formal and mutate on that winner, which passes."""
    sys.path.insert(0, str(ENGINE / "lib"))
    import faults
    ws = faults.make_scratch_workspace(tmp_path, CORPUS_UART, "vde", "uart")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "init")
    target = ws / "rtl" / "uart_tx.v"

    start, _ = optimise.run_rtl_start(["--workspace", str(ws), "--target",
                                       "rtl/uart_tx.v", "--trials", "5"])
    base = start["baseline"]
    assert base["passed"] and base["slack"] >= 0, base

    text = target.read_text(encoding="utf-8")
    bad = "shift     <= {1'b1, ^data, data, 1'b0};"
    assert bad in text
    target.write_text(text.replace(bad, bad.replace("1'b0}", "1'b1}")),
                      encoding="utf-8")
    t1 = rtl_trial(ws, "start bit driven high")
    assert t1["kept"] is False and t1["constraints"]["sim"] == "fail", t1
    assert target.read_text(encoding="utf-8") == text

    for old, new in DROP_BITS_LEFT:
        assert old in text, old
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")
    t2 = rtl_trial(ws, "drop bits_left; end on shift[10:1] == 0")
    assert t2["kept"] is True, t2
    assert t2["area"] < base["area"] and t2["slack"] >= 0, t2

    done, _ = optimise.run_finish(["--workspace", str(ws)])
    assert done["status"] == "pass", done
    assert done["winner"]["trial"] == 2 and done["discarded"] == []
    assert done["tried"][0]["gates"] == {"holdout": "pass", "formal": "pass",
                                         "mutate": "pass"}
    assert "bits_left" not in target.read_text(encoding="utf-8")
    rows = tsv_rows(ws)
    assert [r["kept"] for r in rows[:3]] == ["True", "False", "True"]


def test_sizing_start_and_numeric_record_state_optimise(tmp_path, monkeypatch):
    # design.md 4: sizing "runs the same loop", and the loop records its
    # evaluator hash in state.optimise - a session resuming from state.json
    # alone must see that a sizing search ran, not optimise: null.
    import state as state_mod
    ws = make_ws(tmp_path, start_value=-3.0)
    state_mod.State.init(ws, "ade", "widget")
    optimise.run_start(["--workspace", str(ws), "--target", "sizing/sizing.yaml"])
    meta = json.loads((ws / optimise.META_PATH).read_text())
    rec = json.loads((ws / "state.json").read_text())["optimise"]
    assert rec == {"trials": 0, "evaluator_sha": meta["evaluator_sha"],
                   "best": {"trial": None, "score": None}}
    monkeypatch.setattr(sim_run, "EDA_BIN", make_identity_fake_eda(tmp_path))
    payload, _ = optimise.run_numeric(
        ["--workspace", str(ws), "--popsize", "6", "--maxiter", "5", "--seed", "1"])
    rec = json.loads((ws / "state.json").read_text())["optimise"]
    assert rec["trials"] == payload["trials"]
    assert rec["evaluator_sha"] == meta["evaluator_sha"]
    assert rec["best"]["score"] == pytest.approx(payload["best_score"], abs=1e-6)
    assert rec["best"]["trial"] > 0  # -3 is out of bounds; DE beat it


def test_eval_workspace_caps_both_formal_depths(tmp_path):
    # the fast screen never inherits a deep cover_depth meant for the real
    # formal gate (check_formal.formal_settings: cover runs at cover_depth)
    ws = tmp_path / "ws"
    ev = ws / optimise.EVALUATOR_SUBDIR
    ev.mkdir(parents=True)
    (ev / "spec.yaml").write_text(
        "top: t\nformal:\n  depth: 40\n  cover_depth: 1200\n",
        encoding="utf-8")
    ew = optimise._eval_workspace(ws, {"rtl": [], "fast_formal_depth": 10})
    import yaml
    spec = yaml.safe_load((ew / "spec" / "spec.yaml").read_text())
    assert spec["formal"] == {"depth": 10}


def test_rtl_sdc_is_refused_where_harden_runs_a_design_sdc(tmp_path):
    """harden and timing run on harden/constraints.sdc for a block with
    several clock domains or a design SDC; the evaluator's spec-only SDC
    would score slack against other constraints, so it refuses."""
    one = {"clock": {"period_ns": 20, "domains": ["clk"]},
           "ports": {"clk": {"dir": "input"}, "q": {"dir": "output"}}}
    assert "create_clock -name clk -period 20" in optimise.sdc_text(one,
                                                                    tmp_path)
    two = {**one, "clock": {"period_ns": 20, "domains": ["clk", "vco"],
                            "primary": "clk"}}
    with pytest.raises(CheckError, match="constraints.sdc absent"):
        optimise.sdc_text(two, tmp_path)
    (tmp_path / "harden").mkdir()
    (tmp_path / "harden" / "constraints.sdc").write_text("# x\n")
    with pytest.raises(CheckError, match="constraints.sdc present"):
        optimise.sdc_text(one, tmp_path)

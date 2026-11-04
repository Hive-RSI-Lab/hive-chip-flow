"""top_harden's max slew and max cap gate (#177), on the PLL's recorded top
harden: final/metrics.json trimmed to the per-corner slew/cap counts, and
each corner's report_check_types section of the signoff STA's checks.rpt.
That run has 14 max-slew violations at each ss corner and none elsewhere."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_harden  # noqa: E402
import check_top_harden  # noqa: E402
import speclib  # noqa: E402

RECORDED = REPO / "tests" / "fixtures" / "harden_signoff" / "pll_top"
STA = "56-openroad-stapostpnr"
SLEW = "design__max_slew_violation__count"
CAP = "design__max_cap_violation__count"
TOP = "tt_um_x"
SS = ("max_ss_125C_3v00", "min_ss_125C_3v00", "nom_ss_125C_3v00")
SS_PINS = ["ua[0]", "u_analog/vctrl", "ua[1]", "u_analog/bias_ref", "ua[2]",
           "ua[3]", "ua[4]", "ua[5]", "ua[6]", "ua[7]", "uo_out[0]",
           "_198_/Z", "uo_out[1]", "_199_/Z"]


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    shutil.copytree(RECORDED, d)
    return d


def recorded() -> dict:
    return json.loads((RECORDED / "final" / "metrics.json").read_text("utf-8"))


def judge(run_dir, signoff=None, metrics=None, analog_pads=frozenset()):
    return check_harden.corner_drv_metrics(
        recorded() if metrics is None else metrics, signoff or {}, run_dir, TOP,
        analog_pads=analog_pads)


def test_the_recorded_pll_top_fails_at_each_ss_corner_naming_the_pins(run_dir):
    table, violations = judge(run_dir)
    assert sorted(v["corner"] for v in violations) == sorted(SS)
    for v in violations:
        assert v["kind"] == "max_slew_violation" and v["severity"] == "error"
        assert v["refs"] == sorted(SS_PINS)
        assert v["value"] == 14 and v["limit"] == 0
        assert v["report"] == f"{STA}/{v['corner']}/checks.rpt"
    assert table[SLEW]["nom_tt_025C_3v30"]["status"] == "ok"
    assert table[SLEW]["max_ss_125C_3v00"]["status"] == "over"
    assert len(table[SLEW]) == 9
    assert {r["status"] for r in table[CAP].values()} == {"ok"}


def test_a_clean_run_passes(run_dir):
    metrics = {k: 0 for k in recorded()}
    table, violations = judge(run_dir, metrics=metrics)
    assert violations == []
    assert len(table[SLEW]) == len(table[CAP]) == 9


def test_a_planted_cap_violation_names_its_pin(run_dir):
    rpt = run_dir / STA / "nom_tt_025C_3v30" / "checks.rpt"
    # max capacitance as OpenSTA prints it, a blank line between rows
    rpt.write_text(rpt.read_text("utf-8") + (
        "max capacitance\n\n"
        "Pin                                        Limit         Cap       Slack\n"
        + "-" * 72 + "\n"
        "u_analog/vctrl                          0.200000    0.305864   -0.105864 (VIOLATED)\n\n"
        "_198_/Z                                 0.200000    0.205864   -0.005864 (VIOLATED)\n\n"),
        encoding="utf-8")
    metrics = {**recorded(), f"{CAP}__corner:nom_tt_025C_3v30": 2}
    table, violations = judge(run_dir, metrics=metrics)
    cap = [v for v in violations if v["kind"] == "max_cap_violation"]
    assert len(cap) == 1 and cap[0]["corner"] == "nom_tt_025C_3v30"
    assert cap[0]["refs"] == ["_198_/Z", "u_analog/vctrl"]
    assert "u_analog/vctrl" in cap[0]["msg"]
    assert table[CAP]["nom_tt_025C_3v30"]["status"] == "over"


def test_waived_pins_do_not_count_and_the_rest_still_fail(run_dir):
    pads = {p: "analog pad, not a digital net" for p in SS_PINS[:10]}
    _table, violations = judge(run_dir, {"drv_pins_waived": pads})
    assert len(violations) == 3
    assert all(v["refs"] == sorted(SS_PINS[10:]) for v in violations)
    every = {p: "owner's call" for p in SS_PINS}
    table, violations = judge(run_dir, {"drv_pins_waived": every})
    assert violations == []
    assert table[SLEW]["max_ss_125C_3v00"]["waived"] == sorted(SS_PINS)


def test_analog_pad_pins_are_listed_not_counted(run_dir):
    """The PLL's ua[k] pads and the macro pins on them are LibreLane's
    default drive and load on a pad no digital cell touches; its two
    buf_1 outputs into uo_out are real and still fail."""
    macros = [{"instance": "u_analog", "ua": {"vctrl": 0, "bias_ref": 1}}]
    pads = check_top_harden.analog_pad_pins(macros)
    table, violations = judge(run_dir, analog_pads=pads)
    assert sorted(v["corner"] for v in violations) == sorted(SS)
    assert all(v["refs"] == sorted(SS_PINS[10:]) for v in violations)
    assert table[SLEW]["max_ss_125C_3v00"]["analog_pad"] == \
        sorted(SS_PINS[:10])
    _table, violations = judge(
        run_dir, {"drv_pins_waived": {p: "x" for p in SS_PINS[10:]}},
        analog_pads=pads)
    assert violations == []


def test_a_count_the_report_does_not_name_is_refused_even_waived(run_dir):
    every = {"drv_pins_waived": {p: "x" for p in SS_PINS}}
    shutil.rmtree(run_dir / STA / "max_ss_125C_3v00")
    _table, violations = judge(run_dir, every)
    assert [v["corner"] for v in violations] == ["max_ss_125C_3v00"]
    assert "none can be excused" in violations[0]["msg"]
    metrics = {**recorded(), f"{SLEW}__corner:min_ss_125C_3v00": 20}
    _table, violations = judge(run_dir, every, metrics)
    assert sorted(v["corner"] for v in violations) == \
        ["max_ss_125C_3v00", "min_ss_125C_3v00"]
    assert "names only 14" in [v for v in violations
                               if v["corner"] == "min_ss_125C_3v00"][0]["msg"]


def test_the_last_signoff_sta_step_is_read(run_dir):
    later = run_dir / "70-openroad-stapostpnr" / "max_ss_125C_3v00"
    later.mkdir(parents=True)
    (later / "checks.rpt").write_text(
        "max slew\n\nPin Limit Slew Slack\n----\n"
        "late_pin   3.0  4.0  -1.0 (VIOLATED)\n", encoding="utf-8")
    metrics = {**recorded(), f"{SLEW}__corner:max_ss_125C_3v00": 1}
    _table, violations = judge(run_dir, metrics=metrics)
    late = [v for v in violations if v["corner"] == "max_ss_125C_3v00"]
    assert late[0]["refs"] == ["late_pin"]


@pytest.mark.parametrize("bad", [None, "0", True, float("nan")])
def test_a_corner_value_that_is_no_number_is_missing(run_dir, bad):
    metrics = {**recorded(), f"{CAP}__corner:nom_tt_025C_3v30": bad}
    table, violations = judge(run_dir, metrics=metrics)
    missing = [v for v in violations if v["kind"] == "signoff_metric_missing"]
    assert [v["metric"] for v in missing] == [f"{CAP}__corner:nom_tt_025C_3v30"]
    assert table[CAP]["nom_tt_025C_3v30"]["status"] == "missing"


def test_no_corner_numbers_at_all_is_missing_not_clean(run_dir):
    metrics = {SLEW: 0, CAP: 0}
    table, violations = judge(run_dir, metrics=metrics)
    assert sorted(v["metric"] for v in violations) == [CAP, SLEW]
    assert {v["kind"] for v in violations} == {"signoff_metric_missing"}
    assert table == {SLEW: {}, CAP: {}}


@pytest.mark.parametrize("value", [
    {"drv_pins_waived": ["ua[0]"]}, {"drv_pins_waived": {"ua[0]": " "}},
])
def test_malformed_drv_waiver_is_refused(value):
    assert speclib.harden_signoff({"harden_signoff": value}) is None
    assert any(f["kind"] == "bad_harden_signoff"
               for f in speclib.lint_spec({"harden_signoff": value}))


def test_top_harden_fails_a_finished_harden_on_its_slew(tmp_path, monkeypatch,
                                                        capsys):
    """run()'s wiring: a finished inner harden is judged per corner; one
    that did not finish (no signoff table) is not."""
    run = tmp_path / "top" / "harden" / "runs" / "run"
    shutil.copytree(RECORDED, run)
    spec = {"macros": [{"cell": "pll_analog", "instance": "u_analog",
                        "location": [10, 10], "size_um": [5, 5], "pins": {}}],
            "harden_signoff": {}}
    monkeypatch.setattr(check_top_harden, "assemble",
                        lambda ws: (tmp_path / "top", spec))
    monkeypatch.setattr(check_top_harden, "unpowered_supplies", lambda m: [])
    monkeypatch.setattr(check_top_harden.ttlib, "spec_tiles", lambda s: "1x2")
    inner = {"top": TOP, "run_dir": str(run), "violations": [],
             "signoff_metrics": {}}
    monkeypatch.setattr(check_harden, "run", lambda argv: (dict(inner), None))
    monkeypatch.setattr(check_top_harden.ttlib, "wrapper_name", lambda s: TOP)

    code = check_top_harden.main(["--workspace", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert sorted(v["corner"] for v in out["violations"]) == sorted(SS)
    assert out["drv_metrics"][SLEW]["max_ss_125C_3v00"]["status"] == "over"

    inner["signoff_metrics"] = None
    code = check_top_harden.main(["--workspace", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert "drv_metrics" not in out

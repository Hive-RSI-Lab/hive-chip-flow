"""engine/lib/padlib.py and its use in sim_run (issue #112): the Tiny Tapeout
ua pad model, put on every ua pin of the top's instance, and the
"pad"/"no_pad" conditions every bench of a spec with ua_pins runs in.

Fast tests fake `eda` (tests/test_sim_run.py's make_fake_eda) and read the
decks sim_run wrote. The `slow` ones run real ngspice: an RC step settling
into the pad, whose tau must land within 1 % of the known answer."""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import padlib  # noqa: E402
import sim_run  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_sim_run import make_fake_eda  # noqa: E402

EDA = REPO / "bin" / "eda"
R_SRC = 1000.0

NETLIST = """\
* a source behind R_SRC driving `out`, its only load the pad
.subckt drv in out vss
rsrc in out 1k
.ends drv
"""

SPEC = """\
top: drv
supply: {vdd: 3.3}
devices: [rsrc]
ua_pins: [out]
measures:
  - {name: tau, bounds: {min: 0, max: 1}, corners: [tt]}
"""

# 0 -> 1 V at 10 ns: tau is the time from the edge to 1 - 1/e of the step
BENCH = """\
* rc step into the ua pin
.include '{{PDK}}/libs.tech/ngspice/design.spice'
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}
.temp {{TEMP_C}}
.include '{{NETLIST}}'
{{SIZING}}
vin in 0 pulse(0 1 10n 100p 100p 1 2)
xdut in out 0 drv
.tran 10p 120n 0 4p
.meas tran tau trig v(in) val=0.5 rise=1 targ v(out) val=0.6321206 rise=1
.end
"""


def make_ws(tmp_path: Path, spec: str = SPEC, bench: str = BENCH,
            bounds=None) -> Path:
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb", "sizing"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(spec, encoding="utf-8")
    (ws / "netlist" / "drv.cir").write_text(NETLIST, encoding="utf-8")
    (ws / "tb" / "step_tb.cir").write_text(bench, encoding="utf-8")
    (ws / "tb" / "step_tb.bounds.json").write_text(json.dumps(
        bounds or [{"measure": "tau", "min": 0, "max": 1,
                    "corners": ["tt"]}]), encoding="utf-8")
    return ws


TAU_STDOUT = "\n  Measurements for Transient Analysis\n\ntau = 1.0e-08\n"


# ------------------------------------------------------------ the model

def test_the_figures_are_tt_s_published_maxima():
    assert padlib.R_SERIES_OHM == 500.0
    assert padlib.C_SHUNT_F == 5e-12
    assert "tinytapeout.com/specs/analog" in padlib.SOURCE
    assert "asig_5p0" in padlib.SOURCE


def test_subckt_puts_r_on_the_die_side_and_c_and_esd_on_the_pad():
    text = padlib.pad_subckt("/pdk", "ff", 3.3, {"board_c": 1e-11})
    assert ".lib '/pdk/libs.tech/ngspice/sm141064.spice' diode_ff" in text
    assert "rroute die pad 500" in text
    assert "croute pad 0 5e-12" in text
    assert "desd0 0 pad diode_nd2ps_06v0 m=4 area=1.5e-10 pj=0.000106" in text
    assert "desd1 pad dvdd diode_pd2nw_06v0 m=4" in text
    assert "vdvdd dvdd 0 3.3" in text
    assert "cboard pad 0 1e-11" in text
    assert padlib.diode_corner("sf") == "typical"


def test_insert_moves_the_bench_s_node_to_the_board_side():
    text, pads = padlib.insert_pads(
        "vin in 0 1\nxdut in\n+ out 0 drv\ncl out 0 2p\n", "drv",
        ["in", "out", "vss"], ["out"], ".subckt tt_ua_pad die pad\n.ends\n")
    assert pads == [{"instance": "xdut", "port": "out", "node": "out"}]
    assert "xdut in out__ua_die_xdut_out 0 drv" in text
    assert "xuapad_xdut_out out__ua_die_xdut_out out tt_ua_pad" in text
    assert "cl out 0 2p" in text          # the bench's load: board side
    assert text.index(".subckt tt_ua_pad") < text.index("xdut")


def test_a_bench_with_no_top_instance_is_refused():
    with pytest.raises(padlib.PadRefused) as exc:
        padlib.insert_pads("vin out 0 1\nr1 out 0 1k\n", "drv",
                           ["in", "out", "vss"], ["out"], "")
    assert exc.value.kind == "ua_pad_not_inserted"


def test_an_instance_whose_node_count_is_not_the_port_count_is_refused():
    with pytest.raises(padlib.PadRefused):
        padlib.insert_pads("xdut in out drv\n", "drv", ["in", "out", "vss"],
                           ["out"], "")


@pytest.mark.parametrize("spec, err", [
    ({"ua_pins": {"out": 0}}, "interface.yaml"),
    ({"ua_pins": []}, "non-empty"),
    ({"ua_pins": ["out", "OUT"]}, "twice"),
    ({"ua_pins": ["out"], "ua_pad": {"r_series": 100}}, "board load"),
    ({"ua_pins": ["out"], "ua_pad": {"board_c": -1}}, "> 0"),
    ({"ua_pad": {"board_c": 1e-12}}, "no 'ua_pins'"),
])
def test_malformed_ua_fields_are_refused(spec, err):
    with pytest.raises(ValueError, match=re.escape(err)):
        padlib.ua_pins_of(spec)
        padlib.board_of(spec)


def test_scored_in_follows_the_bound_s_pad_scope():
    assert padlib.scored_in({}, "pad") and padlib.scored_in({}, "no_pad")
    assert not padlib.scored_in({"pad": "bare"}, "pad")
    assert padlib.scored_in({"pad": "bare"}, "no_pad")
    assert not padlib.scored_in({"pad": "pad"}, "no_pad")
    assert padlib.scored_in({"pad": "bare"}, None)


# ------------------------------------------------------------- in sim_run

def test_a_ua_pin_spec_runs_every_bench_bare_and_through_the_pad(tmp_path):
    ws = make_ws(tmp_path)
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    assert [r["condition"] for r in result["results"]] == ["no_pad", "pad"]
    bare, pad = (Path(r["deck"]).read_text() for r in result["results"])
    assert "tt_ua_pad" not in bare
    assert "xdut in out__ua_die_xdut_out 0 drv" in pad
    assert "xuapad_xdut_out out__ua_die_xdut_out out tt_ua_pad" in pad
    assert "diode_typical" in pad and "{{" not in pad
    assert not result["violations"]


def test_a_failure_names_its_pad_condition(tmp_path):
    ws = make_ws(tmp_path, bounds=[{"measure": "tau", "max": 1e-9,
                                    "corners": ["tt"]}])
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    fails = result["violations"]
    assert {v["condition"] for v in fails} == {"pad", "no_pad"}
    assert all("pad" in " ".join(v["refs"]) for v in fails)
    assert any(v["msg"].startswith("[pad] tau @ tt") for v in fails)


def test_a_bare_pin_bound_is_scored_without_the_pad_only(tmp_path):
    ws = make_ws(tmp_path, bounds=[{"measure": "tau", "max": 1e-9,
                                    "corners": ["tt"], "pad": "bare"}])
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    assert [r["condition"] for r in result["results"]] == ["no_pad"]
    assert [v["condition"] for v in result["violations"]] == ["no_pad"]
    assert result["not_scored"][0]["condition"] == "pad"


def test_a_planted_bench_with_a_ua_pin_and_no_pad_is_refused(tmp_path):
    # the bench drives the pin with no instance of the top: the pad model
    # has nowhere to go, so the "pad" condition cannot run - refused, and
    # never passed off as a second bare run
    bench = BENCH.replace("xdut in out 0 drv", "rplant in out 1k\ncout out 0 5p")
    ws = make_ws(tmp_path, bench=bench)
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    kinds = [v["kind"] for v in result["violations"]]
    assert kinds == ["ua_pad_not_inserted"]
    assert result["violations"][0]["severity"] == "error"
    assert [r["condition"] for r in result["results"]] == ["no_pad"]


def test_a_top_the_netlist_does_not_declare_is_refused(tmp_path):
    ws = make_ws(tmp_path, spec=SPEC.replace("top: drv", "top: other"))
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    assert "ua_pad_not_inserted" in [v["kind"] for v in result["violations"]]
    assert all(r["condition"] == "no_pad" for r in result["results"])


def test_a_spec_without_ua_pins_runs_once_as_before(tmp_path):
    ws = make_ws(tmp_path, spec=SPEC.replace("ua_pins: [out]\n", ""))
    result = sim_run.run_workspace_benches(
        ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT), corner_names=["tt"])
    assert len(result["results"]) == 1
    assert "condition" not in result["results"][0]


def test_a_malformed_ua_pins_field_is_an_error(tmp_path):
    ws = make_ws(tmp_path, spec=SPEC.replace("ua_pins: [out]",
                                             "ua_pins: {out: 0}"))
    with pytest.raises(CheckError):
        sim_run.run_workspace_benches(
            ws, eda_bin=make_fake_eda(tmp_path, TAU_STDOUT),
            corner_names=["tt"])


# ------------------------------------------------------------------- lint

def _lint(spec_text: str) -> list[str]:
    import yaml
    return [v["kind"] for v in speclib.lint_spec_ade(yaml.safe_load(spec_text))]


def test_spec_lint_refuses_bad_ua_fields():
    assert "bad_ua_pins" in _lint(SPEC.replace("[out]", "{out: 0}"))
    assert "bad_ua_pad" in _lint(SPEC + "ua_pad: {r_series: 1}\n")
    assert "measure_bad_pad" in _lint(SPEC.replace(
        "corners: [tt]}", "corners: [tt], pad: never}"))
    assert "measure_bad_pad" in _lint(SPEC.replace(
        "ua_pins: [out]\n", "").replace("corners: [tt]}",
                                        "corners: [tt], pad: bare}"))
    assert not {"bad_ua_pins", "bad_ua_pad", "measure_bad_pad"} & set(
        _lint(SPEC))


def test_spec_lint_cross_checks_the_bench_s_pad_scope():
    import yaml
    out = speclib.lint_measures_vs_bench_bounds(
        yaml.safe_load(SPEC), {"step_tb.cir": [
            {"measure": "tau", "min": 0, "max": 1, "corners": ["tt"],
             "pad": "bare"}]})
    assert [v["kind"] for v in out] == ["bench_bound_pad_mismatch"]


def test_spec_lint_refuses_a_ua_pin_that_is_not_a_top_port(tmp_path):
    import check_spec_lint_ade
    ws = make_ws(tmp_path, spec=SPEC.replace("[out]", "[outp]"))
    v = check_spec_lint_ade.ua_pin_violations(
        ws, speclib.load_spec(ws / "spec" / "spec.yaml"), [])
    assert [x["kind"] for x in v] == ["ua_pin_not_a_port"]
    assert v[0]["refs"] == ["outp"]


def test_bounds_sidecar_refuses_an_unknown_pad_scope(tmp_path):
    import simlib
    p = tmp_path / "x.bounds.json"
    p.write_text(json.dumps([{"measure": "a", "max": 1, "pad": "nope"}]))
    with pytest.raises(CheckError, match="pad must be"):
        simlib.load_bounds(p)


# --------------------------------------------- the known answer, real tools

def _ngspice(tmp_path: Path, deck: str) -> str:
    path = tmp_path / "deck.cir"
    path.write_text(deck, encoding="utf-8")
    proc = subprocess.run([str(EDA), "ngspice", "-b", str(path)],
                          cwd=tmp_path, capture_output=True, text=True,
                          timeout=300)
    return proc.stdout + proc.stderr


def _tau(out: str) -> float:
    m = re.search(r"^tau\s*=\s*([-+0-9.eE]+)", out, re.M)
    assert m, out[-2000:]
    return float(m.group(1))


@pytest.mark.slow
def test_rc_settling_into_the_pad_has_the_known_tau(tmp_path):
    # ESD out: the pad is 500 ohm then 5 pF, so a 1 V step through R_SRC
    # charges it with tau = (R_SRC + 500) * 5 pF, exactly, to 1 %
    pad = padlib.pad_subckt("unused", "typical", 3.3, esd=False)
    deck, _ = padlib.insert_pads(
        "* known answer\nvin in 0 pulse(0 1 10n 100p 100p 1 2)\n"
        "xdut in out 0 drv\n" + NETLIST
        + ".tran 10p 120n 0 4p\n.meas tran tau trig v(in) val=0.5 rise=1 "
        "targ v(out) val=0.6321206 rise=1\n.end\n",
        "drv", ["in", "out", "vss"], ["out"], pad)
    want = padlib.tau_s(R_SRC)
    assert want == pytest.approx(7.5e-9)
    got = _tau(_ngspice(tmp_path, deck))
    assert got == pytest.approx(want, rel=0.01), (got, want)


@pytest.mark.slow
def test_the_full_pad_through_sim_run_adds_only_the_esd_s_junction_c(tmp_path):
    # the whole path, real ngspice: sim_run's pad run settles slower than
    # TT's RC alone by the ESD's junction C, which at most is its zero-bias
    # cj0 (both diodes reverse biased on a 0..1 V pin under a 3.3 V DVDD)
    ws = make_ws(tmp_path)
    result = sim_run.run_workspace_benches(ws, eda_bin=EDA,
                                           corner_names=["tt"])
    by = {r["condition"]: r for r in result["results"]}
    assert not by["pad"]["engine_errors"], by["pad"]
    bare, pad = by["no_pad"]["measures"]["tau"], by["pad"]["measures"]["tau"]
    assert bare < 1e-10                    # no load at all on a bare pin
    cj0 = sum(m * (area * (0.00095 if "nd2ps" in model else 0.000912)
                   + m * pj * (1.33e-10 if "nd2ps" in model else 1.4649e-10)
                   / m) for model, _, m, area, pj in padlib.ESD_DIODES)
    lo, hi = padlib.tau_s(R_SRC), padlib.tau_s(R_SRC, c_extra=cj0)
    assert lo < pad < hi, (pad, lo, hi)
    assert math.isclose(cj0, 1.24e-12, rel_tol=0.05)


def test_two_ua_ports_on_one_node_meet_on_the_board_through_two_pads():
    # a follower's `xdut inp out out ...`: inn and out stay apart on the
    # die, each behind its own pad, joined at the board node `out`
    text, pads = padlib.insert_pads(
        "xdut inp out out vdd vss ibias ota\n", "ota",
        ["inp", "inn", "out", "vdd", "vss", "ibias"], ["inn", "out"], "")
    assert "xdut inp out__ua_die_xdut_inn out__ua_die_xdut_out vdd vss " \
        "ibias ota" in text
    assert "xuapad_xdut_inn out__ua_die_xdut_inn out tt_ua_pad" in text
    assert "xuapad_xdut_out out__ua_die_xdut_out out tt_ua_pad" in text
    assert len(pads) == 2

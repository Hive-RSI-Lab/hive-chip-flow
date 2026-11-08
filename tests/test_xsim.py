"""engine/scripts/xsim.py: the on-demand ngspice-vs-Xyce cross-check
(issue #105, docs/xsim.md). Fast tests cover the translation and the
comparison as pure functions; the `slow` ones run both real engines on the
known-answer RC benches in tests/fixtures/xsim, the corpus mirror, and a
planted ngspice-only default (a cap `ic=` with no `.ic`) that must show up
as a difference."""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "engine" / "scripts"
FIXTURE = REPO / "tests" / "fixtures" / "xsim"
CORPUS_MIRROR = REPO / "corpus" / "ade" / "mirror"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import pytest  # noqa: E402

import xsim  # noqa: E402
from checklib import CheckError  # noqa: E402

PLAIN = """\
* a bench
* xsim: true
.include '{{NETLIST}}'
{{SIZING}}
vin in 0 dc 0 ac 1
r1 in out 1k
c1 out 0 1n
.tran 1n 5u
.measure tran t50 TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1
.measure tran v1u FIND v(out) AT=1u
.control
run
.endc
.end
"""


# ---- refusals: only plain SPICE is translated ----

def test_plain_bench_is_not_refused():
    assert xsim.refusals(PLAIN) == []


def test_control_block_doing_more_than_run_is_refused():
    text = PLAIN.replace("run\n", "run\nmeas tran x find v(out) at=2u\n")
    assert any(".control" in w for w in xsim.refusals(text))


def test_two_analyses_are_refused():
    text = PLAIN.replace(".tran 1n 5u", ".tran 1n 5u\n.ac dec 10 1k 1meg")
    assert any("more than one analysis" in w for w in xsim.refusals(text))


def test_no_analysis_is_refused():
    text = PLAIN.replace(".tran 1n 5u\n", "")
    assert any("no .tran" in w for w in xsim.refusals(text))


def test_noise_measure_is_refused():
    text = PLAIN.replace(".tran 1n 5u", ".noise v(out) vin dec 10 10 1meg") \
        .replace(".measure tran v1u", ".measure noise n1")
    assert any(".measure noise" in w for w in xsim.refusals(text))


def test_param_defined_twice_is_named_but_sizing_is_exempt():
    bench = ".param a=1\n.param b=2\n"
    netlist = ".param a=3\n.subckt x p\n.param b=9\n.ends\n"
    assert xsim.redefined_params(bench, netlist, {}) == ["a"]
    assert xsim.redefined_params(bench, netlist, {"a": 1}) == []


# ---- the measure table ----

def test_measure_table_kinds():
    text = PLAIN + ".measure ac ph FIND vp(out) AT=1k\n" \
        ".measure tran tw WHEN v(out)=0.5 RISE=1\n"
    t = xsim.measure_table(text)
    assert t["t50"]["timing"] and t["tw"]["timing"]
    assert not t["v1u"]["timing"]
    assert t["ph"]["phase"] and t["ph"]["analysis"] == "ac"


# ---- translation ----

def test_ngspice_deck_issues_the_analysis_as_a_command():
    deck = xsim.ngspice_deck(PLAIN)
    assert ".tran 1n 5u" not in deck
    ctl = deck[deck.index(".control"):deck.index(".endc")]
    assert "tran 1n 5u" in ctl and "run" not in ctl
    assert deck.rstrip().endswith(".end")


def test_ngspice_deck_echoes_the_noise_integral():
    text = PLAIN.replace(".tran 1n 5u", ".noise v(out) vin dec 10 10 1meg")
    deck = xsim.ngspice_deck(text)
    assert "setplot noise2" in deck
    assert 'echo "onoise_rms = $&onoise_total"' in deck


def test_xyce_deck_translation():
    text = PLAIN.replace(
        ".include '{{NETLIST}}'",
        ".lib /pdk/libs.tech/ngspice/sm141064.spice typical\n"
        ".include '{{NETLIST}}'")
    deck = xsim.xyce_deck(text, ".param w1=2u")
    lines = deck.splitlines()
    assert lines[2] == ".param w1=2u"           # hoisted: Xyce keeps the first
    assert "$" not in lines[1] + lines[2]       # Xyce rejects `$` in a .param
    assert "/libs.tech/xyce/sm141064.xyce typical" in deck
    assert "ngspice" not in deck.split("\n", 1)[1].lower()
    assert ".control" not in deck
    assert "TRIG v(in)=0.5 RISE=1 TARG v(out)=0.5 RISE=1" in deck


def test_xyce_deck_adds_the_noise_integral():
    text = PLAIN.replace(".tran 1n 5u", ".noise v(out) vin dec 10 10 1meg")
    deck = xsim.xyce_deck(text, "")
    assert ".measure noise onoise_rms_sq INTEG onoise FROM=10 TO=1meg" in deck
    assert deck.rstrip().endswith(".end")


def test_parse_xyce_reads_every_measure_file_and_roots_the_noise(tmp_path):
    deck = tmp_path / "b.cir"
    (tmp_path / "b.cir.mt0").write_text("T50 = 6.9e-07\nV1U = FAILED\n")
    (tmp_path / "b.cir.ms0").write_text("ONOISE_RMS_SQ = 4e-12\n")
    vals, failed = xsim.parse_xyce(deck)
    assert vals["t50"] == 6.9e-07
    assert vals["onoise_rms"] == pytest.approx(2e-6)
    assert failed == {"v1u"}


# ---- comparison ----

TABLE = {"v": {"analysis": "tran", "kind": "find", "phase": False,
               "timing": False},
         "t": {"analysis": "tran", "kind": "trig", "phase": False,
               "timing": True},
         "p": {"analysis": "ac", "kind": "find", "phase": True,
               "timing": False}}


def rows(ng, xy, ng_failed=(), xy_failed=()):
    return {r["measure"]: r for r in xsim.compare(
        TABLE, ng, set(ng_failed), xy, set(xy_failed), 0.02, 0.05)}


def test_values_hold_2_percent_and_timing_5_percent():
    r = rows({"v": 1.0, "t": 1.0, "p": 0.0}, {"v": 1.03, "t": 1.03, "p": 0.0})
    assert not r["v"]["ok"] and r["t"]["ok"]
    r = rows({"v": 1.0, "t": 1.0, "p": 0.0}, {"v": 1.01, "t": 1.06, "p": 0.0})
    assert r["v"]["ok"] and not r["t"]["ok"]


def test_ngspice_phase_radians_meet_xyce_degrees_and_wrap():
    r = rows({"v": 1, "t": 1, "p": -math.pi / 4}, {"v": 1, "t": 1, "p": -45.0})
    assert r["p"]["ok"] and r["p"]["ngspice"] == pytest.approx(-45.0)
    r = rows({"v": 1, "t": 1, "p": math.radians(179.9)},
             {"v": 1, "t": 1, "p": -179.9})
    assert r["p"]["ok"]


def test_measure_failed_on_one_engine_is_a_difference():
    r = rows({"t": 1, "p": 0}, {"v": 1.0, "t": 1, "p": 0}, ng_failed={"v"})
    assert not r["v"]["ok"] and "ngspice failed" in r["v"]["why"]
    r = rows({"v": 1.0, "t": 1, "p": 0}, {"t": 1, "p": 0})
    assert not r["v"]["ok"] and "Xyce gave nothing" in r["v"]["why"]


def test_measure_failed_on_both_engines_agrees():
    r = rows({"t": 1, "p": 0}, {"t": 1, "p": 0}, {"v"}, {"v"})
    assert r["v"]["ok"]


def test_no_marked_bench_is_an_error(tmp_path):
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "a.cir").write_text(PLAIN.replace("* xsim: true\n", ""))
    with pytest.raises(CheckError):
        xsim.find_marked(tmp_path, None)


def test_marked_benches_are_found_in_tb_and_xsim(tmp_path):
    for sub in ("tb", "xsim"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / f"{sub}.cir").write_text(PLAIN)
    (tmp_path / "tb" / "plain.cir").write_text(PLAIN.replace("* xsim: true\n", ""))
    names = [p.name for p in xsim.find_marked(tmp_path, None)]
    assert names == ["tb.cir", "xsim.cir"]
    with pytest.raises(CheckError):
        xsim.find_marked(tmp_path, ["plain.cir"])


# ---- the real engines ----

def fixture_ws(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE, ws)
    return ws


def run_real(ws, capsys, *benches):
    argv = ["--workspace", str(ws)]
    for b in benches:
        argv += ["--bench", b]
    code = xsim.main(argv)
    out = json.loads(capsys.readouterr().out)
    got = {r["measure"]: r for res in out["results"]
           for r in res.get("measures", [])}
    return code, out, got


@pytest.mark.slow
def test_real_rc_known_answers_agree(tmp_path, capsys):
    code, out, got = run_real(fixture_ws(tmp_path), capsys,
                              "rc_tran.cir", "rc_ac.cir", "rcn.cir")
    assert code == 0, out
    rc = 1e3 * 1e-9
    for eng in ("ngspice", "xyce"):
        assert got["t50"][eng] == pytest.approx(rc * math.log(2), rel=0.01)
        assert got["v1u"][eng] == pytest.approx(1 - math.exp(-1), rel=0.01)
        assert got["f3db"][eng] == pytest.approx(1 / (2 * math.pi * rc), rel=0.01)
        assert got["ph3db"][eng] == pytest.approx(-45.0, abs=0.5)
        # 4kTR * fc * atan(f2/fc), 10 Hz..1 MHz, at 27 C
        kt = 1.380649e-23 * 300.15
        fc = 1 / (2 * math.pi * rc)
        want = math.sqrt(4 * kt * 1e3 * fc * math.atan(1e6 / fc))
        assert got["onoise_rms"][eng] == pytest.approx(want, rel=0.01)


@pytest.mark.slow
def test_real_planted_cap_ic_without_dot_ic_differs(tmp_path, capsys):
    code, out, got = run_real(fixture_ws(tmp_path), capsys, "plant.cir")
    assert code == 1, out
    assert {v["kind"] for v in out["violations"]} == {"xsim_differs"}
    assert got["v0"]["ngspice"] == pytest.approx(0.0, abs=1e-3)
    assert got["v0"]["xyce"] == pytest.approx(1.0, rel=0.02)


@pytest.mark.slow
def test_real_plant_with_dot_ic_agrees(tmp_path, capsys):
    code, out, got = run_real(fixture_ws(tmp_path), capsys, "plant_ic.cir")
    assert code == 0, out
    assert got["v1u"]["xyce"] == pytest.approx(math.exp(-1), rel=0.01)


@pytest.mark.slow
def test_real_corpus_mirror_agrees(tmp_path, capsys):
    ws = tmp_path / "mirror"
    shutil.copytree(CORPUS_MIRROR, ws)
    code, out, got = run_real(ws, capsys)
    assert code == 0, out
    assert 18e-6 <= got["iout_raw"]["xyce"] <= 32e-6

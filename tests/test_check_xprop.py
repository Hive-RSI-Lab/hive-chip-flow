"""engine/scripts/check_xprop.py: the xprop gate (#84 c, docs/design.md
1.5). The settings, wrapper, sby script and log parsing are tested on their
own; the slow tests prove corpus counter8 clean, show the planted unreset
register gives a counterexample naming `count`, and show a bogus
formal.x_init_ok name is refused."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(REPO / "corpus" / "vde" / "counter8" / "faults"))

import check_xprop as xp  # noqa: E402
import faults  # noqa: E402
import plant_reset_indep  # noqa: E402
import yaml  # noqa: E402
from checklib import CheckError  # noqa: E402

RUNG = REPO / "corpus" / "vde" / "counter8"


def _spec(**formal):
    return {"top": "c", "formal": formal,
            "ports": {"clk": {"dir": "input"}, "rst": {"dir": "input"},
                      "d": {"dir": "input", "width": 4},
                      "q": {"dir": "output", "width": 4}}}


def test_settings_defaults_and_depth_floor():
    s = xp.settings({})
    assert s["reset_cycles"] == xp.DEFAULT_RESET_CYCLES
    assert s["depth"] == xp.DEFAULT_DEPTH and s["x_init_ok"] == {}
    # induction must reach past the reset window
    assert xp.settings(_spec(reset_cycles=8, depth=3))["depth"] == 10


@pytest.mark.parametrize("formal", [
    {"reset_cycles": 0}, {"depth": True}, {"timeout_s": -1},
    {"x_init_ok": ["mem"]}, {"x_init_ok": {"mem": ""}},
    {"x_init_ok": {"bad name": "why"}}])
def test_settings_refuse_bad_values(formal):
    with pytest.raises(CheckError):
        xp.settings(_spec(**formal))


def test_settings_keep_x_init_ok_reasons():
    s = xp.settings(_spec(x_init_ok={"u.mem": " unloaded RAM "}))
    assert s["x_init_ok"] == {"u.mem": "unloaded RAM"}


def test_wrapper_holds_reset_then_asserts_each_watched_port():
    plan = {"clock": "clk", "reset": "rst", "active_high": True,
            "inputs": {"clk": 1, "rst": 1, "d": 4}, "outputs": {"q": 4},
            "watch": ["q"]}
    text, asserts = xp.wrapper("c", plan, 3)
    lines = text.splitlines()
    assert "cf_n < 16'd3" in text
    assert "wire rst = cf_armed ? cf_rst_free : 1'b1;" in text
    assert "(* anyseq *) wire [3:0] d;" in text
    assert ".q_x(q_x)" in text
    assert asserts == {n: "q" for n, l in enumerate(lines, 1)
                       if "assert (q_x == 0)" in l}
    assert len(asserts) == 1


def test_init_script_asserts_each_name_then_anyinits_them():
    assert xp.init_script([]) == ""
    s = xp.init_script(["mem", "rf"])
    assert s.count("select -assert-min 1 ") == 2
    assert "formalff -clk2ff -ff2anyinit" in s
    assert xp.init_selection("rf") in s


def test_sby_text_unmaps_ffs_before_init_and_xprop():
    t = xp.sby_text("c", [Path("/x/a.v")], 9, 60, ["mem"])
    order = [t.index(k) for k in ("async2sync", "dffunmap", "formalff",
                                  "opt_clean", "xprop -split-outputs")]
    assert order == sorted(order)
    assert "depth 9" in t and "timeout 60" in t


def _log(status, *fails):
    lines = [f"  failed assertion {xp.WRAP}.$assert at {xp.WRAP}.sv:{ln}.3-9"
             f" step {step}" for ln, step in fails]
    return "\n".join(lines + [f"DONE ({status}, rc=0)"])


def test_findings_pass_has_no_violations():
    assert xp.findings(_log("PASS"), {9: "q"}, 2, 20) == ("PASS", [])


def test_findings_fail_names_the_port_and_clocks_after_reset():
    status, vs = xp.findings(_log("FAIL", (9, 3), (9, 5)), {9: "q"}, 2, 20)
    assert status == "FAIL" and len(vs) == 1
    v = vs[0]
    assert (v["kind"], v["severity"], v["clocks_after_reset"]) == (
        "output_x_after_reset", "error", 1)
    assert "q" in v["msg"]


def test_findings_fail_without_a_watched_assert_is_an_error():
    with pytest.raises(CheckError):
        xp.findings(_log("FAIL", (4, 3)), {9: "q"}, 2, 20)


@pytest.mark.parametrize("status", ["UNKNOWN", "TIMEOUT"])
def test_findings_unproven(status):
    _, vs = xp.findings(_log(status), {9: "q"}, 2, 20)
    assert [v["kind"] for v in vs] == ["xprop_unproven"]


def test_findings_error_status_is_a_refusal_not_a_finding():
    with pytest.raises(CheckError):
        xp.findings(_log("ERROR"), {9: "q"}, 2, 20)


def _run(ws, capsys):
    code = xp.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.slow
def test_clean_counter8_is_proven(tmp_path, capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    code, out = _run(ws, capsys)
    assert (code, out["status"], out["sby_status"]) == (0, "pass", "PASS"), out
    assert out["proven"] and "count" in out["watched"]


@pytest.mark.slow
def test_unreset_register_gives_a_counterexample_naming_count(tmp_path,
                                                              capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    plant_reset_indep.plant(ws)
    code, out = _run(ws, capsys)
    assert code == 1, out
    errs = [v for v in out["violations"] if v["severity"] == "error"]
    assert [(v["kind"], v["module"]) for v in errs] == [
        ("output_x_after_reset", "count")]


@pytest.mark.slow
def test_x_init_ok_name_matching_nothing_is_refused(tmp_path, capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    p = ws / "spec" / "spec.yaml"
    spec = yaml.safe_load(p.read_text())
    spec.setdefault("formal", {})["x_init_ok"] = {"no_such_mem": "test"}
    p.write_text(yaml.safe_dump(spec))
    code, out = _run(ws, capsys)
    assert code == 2 and "x_init_ok" in out["error"], out

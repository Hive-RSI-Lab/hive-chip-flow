"""The cosim gate's corner runs (#108): which corners and views it owes,
what each run is told to apply, and how each run's outcome reaches the
verdict. Hermetic: the simulator is faked. The real runs are the `slow` tests
at the bottom, on a two-corner ring oscillator with an extracted netlist."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
CORPUS_RUNG = REPO / "corpus" / "msde" / "ring_osc_div"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402

import check_cosim  # noqa: E402
import cosim_corners as cc  # noqa: E402
from checklib import CheckError  # noqa: E402

CORNERS = [
    {"name": "tt", "process": "typical", "temp_c": 27, "supply_pct": 0},
    {"name": "ss", "process": "ss", "temp_c": 125, "supply_pct": -10},
    {"name": "ff", "process": "ff", "temp_c": -40, "supply_pct": 10},
]
MEASURED = {"tt": {"gain": 10.0, "pm": 60.0},
            "ss": {"gain": 8.0, "pm": 70.0},
            "ff": {"gain": 12.0, "pm": 50.0}}
DIGITAL_SPEC = "top: blk\nports: {}\n"


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_ws(tmp_path: Path, name="ws", kind="transistor", corners=None,
            spec=True, record=True, extracted=True, netlist_gl=False) -> Path:
    """A workspace with what plan_corners reads; each argument removes or
    adds one piece of it."""
    ws = tmp_path / name
    bench = {"top": "top", "bounds": "bounds.json", "analog_netlist": "a.sp",
             "analog_kind": kind}
    if corners is not None:
        bench["corners"] = corners
    _w(ws / "tb" / "cosim_bench.json", json.dumps(bench))
    _w(ws / "tb" / "bounds.json", json.dumps({"m": {"min": 0, "max": 1}}))
    _w(ws / "tb" / "a.sp", "* wrapper\n")
    _w(ws / "tb" / "top.v", "module top; endmodule\n")
    _w(ws / "tb" / "test_top.py", "SPICE = 'a.sp'\n")
    if spec:
        _w(ws / "analog" / "spec" / "spec.yaml",
           "top: blk_a\nsupply: {vdd: 3.3}\n")
        _w(ws / "analog" / "netlist" / "blk_a.cir", ".subckt blk_a\n.ends\n")
    if record:
        _w(ws / "analog" / "reports" / "recorded" / "gate-pex_sim.json",
           json.dumps({"facts": {"measured_by_corner": MEASURED}}))
    if extracted:
        _w(ws / "analog" / "log" / "pex_sim" / "blk_a.pex.ordered.spice",
           ".subckt blk_a\n.ends\n")
    _w(ws / "digital" / "spec" / "spec.yaml", DIGITAL_SPEC)
    if netlist_gl:
        _w(ws / check_cosim.HARDEN_FINAL / "nl" / "tt_um_blk.nl.v",
           "module tt_um_blk; endmodule\n")
    return ws


@pytest.fixture
def sweep(monkeypatch):
    """The analog spec's sweep without a netlist parse or a corners.yaml."""
    monkeypatch.setattr(check_cosim.sim_run, "spec_sweep",
                        lambda analog: (CORNERS, []))
    monkeypatch.setattr(check_cosim.sim_run, "load_sizing", lambda analog: {})


def plan(ws: Path) -> dict:
    return check_cosim.plan_corners(ws, check_cosim.load_bench_meta(ws / "tb"))


# ------------------------------------------------------- which corners
def test_bounding_corners_default_takes_both_ends_of_every_measure():
    picked = cc.bounding_corners(MEASURED)
    assert set(picked) == {"ss", "ff"}
    assert len(picked["ss"]) == 2 and len(picked["ff"]) == 2


def test_bounding_corners_named_ends_only():
    assert set(cc.bounding_corners(MEASURED, [["gain", "max"]])) == {"ff"}


def test_bounding_corners_refuses_a_measure_nobody_measured():
    with pytest.raises(CheckError, match="nope"):
        cc.bounding_corners(MEASURED, [["nope", "max"]])


def test_bounding_corners_ignores_a_corner_missing_the_measure():
    table = {"a": {"m": 1.0}, "b": {"m": None}, "c": {"other": 9.0}}
    assert set(cc.bounding_corners(table, [["m", "min"], ["m", "max"]])) == {"a"}


def test_pex_record_takes_the_sweep_with_more_corners(tmp_path):
    analog = tmp_path
    _w(analog / "reports/recorded/gate-pex_sim.json",
       json.dumps({"facts": {"measured_by_corner": {"tt": {"m": 1}}}}))
    _w(analog / "reports/recorded/pex_pvt.json",
       json.dumps({"measured": {"tt": {"m": 1}, "ss": {"m": 2}}}))
    rec = cc.pex_record(analog)
    assert rec["log_dir"] == "log/pex_pvt" and set(rec["measured"]) == {"tt", "ss"}


def test_pex_record_tie_goes_to_the_gate_record(tmp_path):
    _w(tmp_path / "reports/recorded/gate-pex_sim.json",
       json.dumps({"measured_by_corner": {"tt": {"m": 1}}}))
    _w(tmp_path / "reports/recorded/pex_pvt.json",
       json.dumps({"measured": {"tt": {"m": 1}}}))
    assert cc.pex_record(tmp_path)["log_dir"] == "log/pex_sim"


def test_pex_record_tie_on_corners_takes_the_one_with_more_measures(tmp_path):
    _w(tmp_path / "reports/recorded/gate-pex_sim.json",
       json.dumps({"measured_by_corner": {"tt": {"m": 1}}}))
    _w(tmp_path / "reports/recorded/pex_pvt.json",
       json.dumps({"measured": {"tt": {"m": 1, "fmax": 2}}}))
    assert cc.pex_record(tmp_path)["log_dir"] == "log/pex_pvt"


def test_pex_record_none_when_absent(tmp_path):
    assert cc.pex_record(tmp_path) is None


# --------------------------------------------------------- which SDF
@pytest.mark.parametrize("process,temp,want", [
    ("typical", 27, "nom_tt_025C_3v30"),
    ("ss", 125, "nom_ss_125C_3v00"),
    ("ss", -40, "nom_ss_n40C_3v00"),
    ("ff", 125, "nom_ff_125C_3v60"),
    ("ff", -40, "nom_ff_n40C_3v60"),
    ("sf", 125, "nom_tt_025C_3v30"),
    ("ss", 27, "nom_ss_n40C_3v00"),
])
def test_sdf_corner_follows_process_and_temperature(process, temp, want):
    assert cc.sdf_corner_for({"name": "c", "process": process,
                              "temp_c": temp}) == want


def test_sdf_corner_refuses_an_unknown_process():
    with pytest.raises(CheckError, match="bogus"):
        cc.sdf_corner_for({"name": "c", "process": "bogus", "temp_c": 27})


def test_sdf_file_prefers_the_harden_own(tmp_path):
    c = "nom_ss_125C_3v00"
    assert cc.sdf_file(tmp_path, "d", c) is None
    _w(tmp_path / "sdf" / c / f"d__{c}.gen.sdf", "G")
    assert cc.sdf_file(tmp_path, "d", c).name.endswith(".gen.sdf")
    _w(tmp_path / "sdf" / c / f"d__{c}.sdf", "S")
    assert cc.sdf_file(tmp_path, "d", c).name == f"d__{c}.sdf"


# ------------------------------------------------------- the bench's key
@pytest.mark.parametrize("raw,match", [
    ("x", "mapping"),
    ({"nope": 1}, "unknown keys"),
    ({"bound_by": [["m", "min"]], "list": ["tt"]}, "not both"),
    ({"bound_by": []}, "bound_by"),
    ({"bound_by": [["m", "mid"]]}, "bound_by"),
    ({"list": []}, "corners.list"),
    ({"list": ["tt", "tt"]}, "corners.list"),
    ({"digital": ["sdf"]}, "corners.digital"),
    ({"digital": ["rtl", "rtl"]}, "corners.digital"),
    ({"env": {"A": 1}}, "corners.env"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"timeout_s": True}, "timeout_s"),
    ({"jobs": 0}, "jobs"),
    ({"jobs": 1.5}, "jobs"),
])
def test_validate_config_refuses(raw, match):
    with pytest.raises(CheckError, match=match):
        cc.validate_config(raw, "b.json")


def test_validate_config_defaults():
    assert cc.validate_config(None, "b.json") is None
    assert cc.validate_config({}, "b.json") == {
        "bound_by": None, "list": None, "digital": None, "env": {},
        "timeout_s": cc.DEFAULT_TIMEOUT_S, "jobs": cc.DEFAULT_JOBS}


# ------------------------------------------------------- who owes corners
def test_ideal_bench_owes_none(tmp_path, sweep):
    out = plan(make_ws(tmp_path, kind="ideal"))
    assert out["owed"] is False and "ideal" in out["reason"]


def test_released_v1_block_owes_none_unless_it_declares(tmp_path, sweep):
    ws = make_ws(tmp_path / "blocks", name="pll")
    out = plan(ws)
    assert out["owed"] is False and "v1" in out["reason"]


def test_a_block_with_no_pex_sweep_owes_none(tmp_path, sweep):
    out = plan(make_ws(tmp_path, record=False))
    assert out["owed"] is False and "pex" in out["reason"]


def test_transistor_bench_with_a_sweep_owes_the_bounding_corners(tmp_path, sweep):
    ws = make_ws(tmp_path)
    out = plan(ws)
    assert out["owed"] is True
    assert set(out["corners"]) == {"ss", "ff"}
    assert out["netlist_kind"] == "pex"
    assert out["netlist"] == "analog/log/pex_sim/blk_a.pex.ordered.spice"
    assert out["views"] == ["rtl"]
    assert out["source"] == "analog/reports/recorded/gate-pex_sim.json"


def test_gl_is_the_default_view_once_the_digital_side_is_hardened(tmp_path, sweep):
    assert plan(make_ws(tmp_path, netlist_gl=True))["views"] == ["gl"]


def test_a_sweep_whose_extraction_is_gone_is_refused(tmp_path, sweep):
    with pytest.raises(CheckError, match="pex_sim"):
        plan(make_ws(tmp_path, extracted=False))


def test_gl_named_with_no_netlist_is_refused(tmp_path, sweep):
    with pytest.raises(CheckError, match="harden"):
        plan(make_ws(tmp_path, corners={"digital": ["gl"]}))


def test_declared_corners_with_no_analog_spec_are_refused(tmp_path, sweep):
    with pytest.raises(CheckError, match="spec"):
        plan(make_ws(tmp_path, corners={}, spec=False))


def test_picking_from_a_sweep_that_is_not_there_is_refused(tmp_path, sweep):
    with pytest.raises(CheckError, match="none"):
        plan(make_ws(tmp_path, corners={"bound_by": [["pm", "min"]]},
                     record=False))


def test_named_corners_without_a_sweep_run_the_schematic(tmp_path, sweep):
    out = plan(make_ws(tmp_path, corners={"list": ["ss"]}, record=False))
    assert out["owed"] is True and out["netlist_kind"] == "schematic"
    assert out["netlist"] == "analog/netlist/blk_a.cir"
    assert set(out["corners"]) == {"ss"}


def test_a_corner_the_spec_does_not_sweep_is_refused(tmp_path, sweep):
    with pytest.raises(CheckError, match="zz"):
        plan(make_ws(tmp_path, corners={"list": ["zz"]}))


def test_a_spec_with_no_numeric_supply_is_refused(tmp_path, sweep):
    ws = make_ws(tmp_path)
    _w(ws / "analog/spec/spec.yaml", "top: blk_a\nsupply: {vdd: vdd33}\n")
    with pytest.raises(CheckError, match="vdd"):
        plan(ws)


# ------------------------------------------------- what a corner run reads
def test_corner_setup_applies_the_corner_and_the_netlist(tmp_path):
    setup = check_cosim.corner_setup(
        tmp_path, CORNERS[1], 3.3, "analog/x.spice", tmp_path / "x.spice",
        "* no sizing", tmp_path / "m.json")
    assert setup["corner"] == "ss" and setup["netlist"] == "analog/x.spice"
    assert setup["vdd"] == pytest.approx(2.97)
    text = "\n".join(setup["lines"])
    assert "sm141064.spice' ss\n" in text
    assert f".include '{tmp_path / 'x.spice'}'" in text
    assert text.endswith(".temp 125")


def test_corner_not_applied_names_what_is_wrong():
    setup = {"corner": "ss", "netlist": "n"}
    assert cc.corner_not_applied(None, setup) == []
    assert cc.corner_not_applied({"cosim_corner": "ss", "cosim_netlist": "n"},
                                 setup) == []
    why = cc.corner_not_applied({"cosim_corner": "tt"}, setup)
    assert len(why) == 2 and "'tt'" in why[0]


def test_generate_sdf_refuses_when_its_inputs_are_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(check_cosim.layoutlib, "pdk_root", lambda: tmp_path)
    with pytest.raises(CheckError, match="re-run"):
        check_cosim.generate_sdf(tmp_path, tmp_path / "final", "d",
                                 "nom_ss_n40C_3v00")


def _job(ws: Path, gl=None) -> dict:
    root = ws / check_cosim.CORNER_SUBDIR / "rtl" / "ss"
    shutil.copytree(ws / "tb", root / "tb")
    return {"ws": str(ws), "root": str(root), "label": "ss/rtl", "view": "rtl",
            "condition": None, "env": {}, "timeout_s": 10.0, "gl": gl,
            "setup": {"corner": "ss", "netlist": "n"}, "sources": []}


def test_a_bench_that_ignored_the_setup_is_a_finding(tmp_path, monkeypatch):
    # its typical run would otherwise score at every corner
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_cosim, "run_condition", lambda *a, **k: {
        "measures": {"m": 0.5}, "tests_run": 1, "signatures": [],
        "ic_ignored": []})
    monkeypatch.setattr(check_cosim.cocotblib, "test_modules", lambda tb: [])
    result = check_cosim.run_corner_job(_job(ws))
    assert [v["kind"] for v in result["violations"]] == \
        ["cosim_corner_not_applied"]
    assert "ss/rtl" in result["violations"][0]["refs"]


def test_a_bench_that_applied_it_is_clean(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_cosim, "run_condition", lambda *a, **k: {
        "measures": {"cosim_corner": "ss", "cosim_netlist": "n"},
        "tests_run": 1, "signatures": [], "ic_ignored": []})
    monkeypatch.setattr(check_cosim.cocotblib, "test_modules", lambda tb: [])
    assert check_cosim.run_corner_job(_job(ws))["violations"] == []


def test_a_worker_that_breaks_still_leaves_a_refusal(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    job = root / "job.json"
    job.write_text(json.dumps({"ws": str(tmp_path), "root": str(root),
                               "label": "ss/rtl", "env": {}, "setup": {}}))
    assert check_cosim.corner_worker(str(job)) == 0
    assert "refused" in json.loads((root / "result.json").read_text())


def test_a_netlist_the_bench_never_reaches_is_refused(tmp_path, monkeypatch):
    # the bench still builds the RTL frame: the run would pass on the RTL
    ws = make_ws(tmp_path)
    nl, sdf = tmp_path / "n.v", tmp_path / "s.sdf"
    nl.write_text("n"), sdf.write_text("s")
    for fn, value in (("escaped_dot_renames", lambda *a: {}),
                      ("sdf_for_icarus", lambda *a: None),
                      ("netlist_for_icarus", lambda *a: None)):
        monkeypatch.setattr(check_cosim.check_glsim, fn, value)
    monkeypatch.setattr(check_cosim.ttlib, "generate_glsim_harness",
                        lambda *a, **k: "")
    monkeypatch.setattr(check_cosim, "elaborate", lambda *a, **k: "vvp")
    monkeypatch.setattr(check_cosim, "scope_parents",
                        lambda text: [("x", "tt_frame_blk", None)])
    gl = {"netlist": str(nl), "sdf": str(sdf), "bench": [], "cells": [],
          "module": None, "dut": "tt_um_blk"}
    with pytest.raises(CheckError, match="not_instantiated"):
        check_cosim.gl_sources(ws, tmp_path / "root", gl, "top")


def test_gl_build_log_names_the_cells_icarus_refused(tmp_path, monkeypatch):
    # sim.log only holds the test step, so the SDF waiver needs this log
    ws = make_ws(tmp_path)
    nl, sdf, cell = tmp_path / "n.v", tmp_path / "s.sdf", tmp_path / "cell.v"
    nl.write_text("n"), sdf.write_text("s")
    cell.write_text("""module xor_cell(input A, output Z);
  assign Z = A;
  specify
    ifnone (posedge A => (Z : A)) = (1, 1);
  endspecify
endmodule
module top; wire z; xor_cell u(.A(1'b0), .Z(z)); endmodule
""")
    for fn, value in (("escaped_dot_renames", lambda *a: {}),
                      ("sdf_for_icarus", lambda *a: None),
                      ("netlist_for_icarus", lambda *a: None)):
        monkeypatch.setattr(check_cosim.check_glsim, fn, value)
    monkeypatch.setattr(check_cosim.ttlib, "generate_glsim_harness",
                        lambda *a, **k: "")
    gl = {"netlist": str(tmp_path / "empty.v"), "sdf": str(sdf),
          "bench": [str(cell)], "cells": [], "module": None, "dut": "top"}
    (tmp_path / "empty.v").write_text("")
    root = tmp_path / "root"
    monkeypatch.setattr(check_cosim.check_glsim, "netlist_for_icarus",
                        lambda _in, out, _r: Path(out).write_text(""))
    check_cosim.gl_sources(ws, root, gl, "top")
    text = (root / "gl" / check_cosim.GL_BUILD_LOG).read_text()
    assert check_cosim.check_glsim.unsupported_cells(text) == {"xor_cell"}


# -------------------------------------------------------- the verdict
RESULT = {"violations": [], "wall_s": 3.0, "tests_run": 1, "signatures": [],
          "sdf_waived": [], "measures": {"m": 0.5, "edge_times_ns": [1.0],
                                         "cosim_corner": "ss"}}


def test_aggregate_keeps_numbers_and_drops_lists():
    found: list = []
    row = check_cosim.aggregate_corner("ss/rtl", "log", dict(RESULT), None, found)
    assert row["status"] == "pass" and not found
    assert row["measures"] == {"m": 0.5, "cosim_corner": "ss"}


def test_aggregate_a_failed_corner_fails_and_says_which():
    bad = {"kind": "measure_out_of_bounds"}
    found: list = []
    row = check_cosim.aggregate_corner(
        "ss/gl", "log", {**RESULT, "violations": [bad]}, "nom_ss", found)
    assert row["status"] == "fail" and found == [bad]
    assert row["violations"] == ["measure_out_of_bounds"]
    assert row["sdf"] == "nom_ss"


def test_aggregate_no_result_is_a_finding_never_a_pass():
    found: list = []
    row = check_cosim.aggregate_corner("ss/rtl", "log", {"refused": "died"},
                                       None, found)
    assert row["status"] == "no_result"
    assert [v["kind"] for v in found] == ["cosim_corner_no_result"]


def test_one_dead_corner_keeps_the_others(tmp_path, monkeypatch, sweep):
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_cosim.layoutlib, "pdk_root", lambda: tmp_path)
    seen = []

    def launch(job):
        seen.append(job)
        return {"refused": "died"} if job["label"] == "ss/rtl" else dict(RESULT)

    monkeypatch.setattr(check_cosim, "launch_corner_job", launch)
    found: list = []
    out = check_cosim.run_corners(ws, ws / "tb", plan(ws), [ws / "tb/top.v"],
                                  None, None, found)
    assert out["runs"]["ss/rtl"]["status"] == "no_result"
    assert out["runs"]["ff/rtl"]["status"] == "pass"
    assert [v["kind"] for v in found] == ["cosim_corner_no_result"]
    # each runs from its own copy of tb/, writing its own measures
    job = next(j for j in seen if j["label"] == "ff/rtl")
    assert Path(job["root"], "tb", "test_top.py").is_file()
    assert job["setup"]["out"] == str(Path(job["root"]) / check_cosim.MEASURES_REL)
    assert job["setup"]["netlist"].endswith(".pex.ordered.spice")
    assert not Path(job["setup"]["netlist"]).is_absolute()


def test_gl_jobs_get_their_corner_sdf(tmp_path, monkeypatch, sweep):
    ws = make_ws(tmp_path, netlist_gl=True, corners={"digital": ["gl"]})
    final = ws / check_cosim.HARDEN_FINAL
    for c in ("nom_ss_125C_3v00", "nom_ff_n40C_3v60"):
        _w(final / "sdf" / c / f"tt_um_blk__{c}.sdf", "(DELAYFILE)")
    _w(ws / "digital" / "rtl" / "blk.v", "module blk; endmodule\n")
    monkeypatch.setattr(check_cosim.layoutlib, "pdk_root", lambda: tmp_path)
    monkeypatch.setattr(check_cosim.check_glsim, "_pdk_root", lambda: tmp_path)
    monkeypatch.setattr(check_cosim.check_glsim, "_cell_sources",
                        lambda root: [tmp_path / "cells.v"])
    seen = {}
    monkeypatch.setattr(check_cosim, "launch_corner_job",
                        lambda job: seen.setdefault(job["label"], job)
                        and dict(RESULT))
    out = check_cosim.run_corners(ws, ws / "tb", plan(ws), [], None, None, [])
    assert out["runs"]["ss/gl"]["sdf"] == "nom_ss_125C_3v00"
    assert seen["ff/gl"]["gl"]["sdf"].endswith("nom_ff_n40C_3v60.sdf")
    assert seen["ss/gl"]["gl"]["netlist"].endswith("tt_um_blk.nl.v")
    assert [Path(p).name for p in seen["ss/gl"]["gl"]["bench"]] == ["top.v"]


def _fake_typical(monkeypatch, ws, value):
    def fake_run_cocotb(build_dir, test_dir, sources, hdl_toplevel,
                        test_modules_, results_xml, **kwargs):
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "sim.log").write_text("", encoding="utf-8")
        results_xml.parent.mkdir(parents=True, exist_ok=True)
        results_xml.write_text(
            '<testsuites><testsuite><testcase name="t.t"/>'
            '</testsuite></testsuites>', encoding="utf-8")
        _w(ws / "reports" / "cosim_measures.json", json.dumps({
            "m": value, "digital_toggles": 1, "edge_times_ns": [1.0],
            "spice_time_reached_ns": 2.0}))
        return results_xml

    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb", fake_run_cocotb)
    monkeypatch.setattr(check_cosim, "ensure_ngspice_env", lambda root: None)
    monkeypatch.setattr(check_cosim, "toolchain_root", lambda: Path("/t"))
    _w(ws / "interface.yaml", "signals: []\n")
    (ws / "log").mkdir(exist_ok=True)


def test_a_failing_typical_run_skips_the_corners(tmp_path, monkeypatch, capsys,
                                                 sweep):
    ws = make_ws(tmp_path)
    _fake_typical(monkeypatch, ws, 5.0)
    monkeypatch.setattr(check_cosim, "run_corners",
                        lambda *a, **k: pytest.fail("ran the corners"))
    assert check_cosim.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["corners"]["owed"] is True and "skipped" in out["corners"]


def test_a_failed_corner_fails_the_gate(tmp_path, monkeypatch, capsys, sweep):
    ws = make_ws(tmp_path)
    _fake_typical(monkeypatch, ws, 0.5)

    def corners(ws_, tb, plan_, sources, condition, framed, violations):
        violations.append({"kind": "measure_out_of_bounds", "severity": "error",
                           "gate": "cosim", "file": "x", "name": None,
                           "refs": [], "msg": "m", "source": "check_cosim"})
        return {"owed": True, "runs": {}}

    monkeypatch.setattr(check_cosim, "run_corners", corners)
    assert check_cosim.main(["--workspace", str(ws)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "violations"


def test_a_refusal_comes_before_any_simulation(tmp_path, monkeypatch, capsys,
                                               sweep):
    ws = make_ws(tmp_path, extracted=False)
    _fake_typical(monkeypatch, ws, 0.5)
    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb",
                        lambda *a, **k: pytest.fail("simulated first"))
    assert check_cosim.main(["--workspace", str(ws)]) == 2
    assert "pex_sim" in json.loads(capsys.readouterr().out)["remediation"]


def test_cosim_hashes_every_post_layout_view(tmp_path):
    import statelib
    imap = statelib.load_map()
    kinds = imap["gate_inputs"]["msde"]["cosim"]
    files = {"analog_pex_netlists": "analog/log/pex_pvt/a.pex.ordered.spice",
             "analog_pex_pvt": "analog/reports/recorded/pex_pvt.json",
             "analog_pex_sim": "analog/reports/recorded/gate-pex_sim.json",
             "digital_final_nl": f"{check_cosim.HARDEN_FINAL}/nl/d.nl.v",
             "digital_final_sdf": f"{check_cosim.HARDEN_FINAL}/sdf/c/d.sdf",
             "digital_final_spef": f"{check_cosim.HARDEN_FINAL}/spef/nom/d.spef",
             "digital_final_sdc": f"{check_cosim.HARDEN_FINAL}/sdc/d.sdc"}
    for kind, rel in files.items():
        assert kind in kinds
        _w(tmp_path / rel, '{"a": 1}\n')
        before = statelib.hash_kind(tmp_path, kind, imap)[1]
        _w(tmp_path / rel, '{"a": 2}\n')
        assert statelib.hash_kind(tmp_path, kind, imap)[1] != before, kind


# --------------------------------------------------------------------- slow
RING_CORE = (CORPUS_RUNG / "tb" / "ring5.sp").read_text(encoding="utf-8") \
    .replace(".subckt ring5 ", ".subckt ring5_core ") \
    .replace(".ends ring5", ".ends ring5_core")
RING_WRAPPER = ".subckt ring5 osc_out vdd vss\n" \
    "x0 osc_out vdd vss ring5_core\n.ends ring5\n"


def ring_ws(tmp_path: Path, corners: dict) -> Path:
    """ring_osc_div with its cell moved under analog/, a thin wrapper in
    tb/ and an extracted netlist the corner runs read."""
    ws = tmp_path / "ws"
    (ws / "log").mkdir(parents=True)
    shutil.copytree(CORPUS_RUNG / "tb", ws / "tb")
    shutil.copy2(CORPUS_RUNG / "interface.yaml", ws / "interface.yaml")
    (ws / "tb" / "ring5.sp").write_text(RING_WRAPPER, encoding="utf-8")
    bench = json.loads((ws / "tb/cosim_bench.json").read_text())
    (ws / "tb/cosim_bench.json").write_text(json.dumps(
        {**bench, "corners": corners}))
    test = ws / "tb" / "test_ring_osc_div.py"
    text = test.read_text(encoding="utf-8")
    text = text.replace("v(x1.", "v(x1.x0.").replace(
        "extra_lines=list(RING_IC),",
        "extra_lines=list(RING_IC) + (SETUP['lines'] if SETUP else "
        "[f\".include '{HERE.parent / 'analog/netlist/ring5_core.cir'}'\"]),"
    ).replace(
        'HERE = Path(__file__).resolve().parent\n',
        'HERE = Path(__file__).resolve().parent\n'
        'import cosimlib\nSETUP = cosimlib.corner_setup()\n'
    ).replace(
        '"divided_freq_hz": divided_freq_hz,\n',
        '"divided_freq_hz": divided_freq_hz,\n'
        '            **cosimlib.corner_evidence(SETUP),\n')
    test.write_text(text, encoding="utf-8")
    _w(ws / "analog/spec/spec.yaml",
       "top: ring5_core\nsupply: {vdd: 3.3}\ncorners: default\n")
    _w(ws / "analog/netlist/ring5_core.cir", RING_CORE)
    _w(ws / "analog/log/pex_sim/ring5_core.pex.ordered.spice", RING_CORE)
    _w(ws / "analog/reports/recorded/gate-pex_sim.json", json.dumps(
        {"facts": {"measured_by_corner": {
            "ss": {"f": 1.0}, "tt": {"f": 2.0}, "ff": {"f": 3.0}}}}))
    return ws


def test_corner_bench_finds_the_block_from_its_own_file(tmp_path):
    ws = tmp_path / "block"
    (ws / "tb").mkdir(parents=True)
    (ws / "tb" / "bench.py").write_text("# bench\n", encoding="utf-8")
    (ws / "tb" / "__pycache__").mkdir()
    (ws / "analog" / "sizing").mkdir(parents=True)
    (ws / "analog" / "sizing" / "sizing.yaml").write_text("w: 1\n", encoding="utf-8")
    (ws / "log" / "other").mkdir(parents=True)
    (ws / "reports").mkdir()
    root = ws / "log" / "cosim_corners" / "rtl" / "ss"

    check_cosim.stage_corner_bench(ws, ws / "tb", root)

    bench_dir = root / "tb"
    assert not bench_dir.is_symlink() and (bench_dir / "bench.py").is_file()
    assert not (bench_dir / "__pycache__").exists()
    assert (bench_dir.parent / "analog" / "sizing" / "sizing.yaml").read_text(
        encoding="utf-8") == "w: 1\n"
    assert not (root / "log").exists()
    assert not (root / "reports").exists(), "each corner writes its own measures"


@pytest.mark.slow
def test_ring_corner_runs_read_the_extracted_netlist(tmp_path, capsys):
    ws = ring_ws(tmp_path, {"digital": ["rtl"], "jobs": 2})
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    corners = out["corners"]
    assert corners["netlist_kind"] == "pex"
    assert set(corners["runs"]) == {"ss/rtl", "ff/rtl"}
    for row in corners["runs"].values():
        assert row["status"] == "pass"
        assert row["measures"]["cosim_netlist"] == corners["netlist"]


@pytest.mark.slow
def test_ring_added_cap_in_the_extraction_fails_at_the_corner(tmp_path, capsys):
    # schematic passes at typical; only the post-layout view is slow
    ws = ring_ws(tmp_path, {"list": ["ss"], "digital": ["rtl"]})
    path = ws / "analog/log/pex_sim/ring5_core.pex.ordered.spice"
    extra = "".join(f"cx{i} l{i} 0 5p\n" for i in range(5))
    path.write_text(RING_CORE.replace("eout ", extra + "eout "), encoding="utf-8")
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert out["measures"]["divided_freq_hz"] > 1e7
    found = {(v["kind"], tuple(v["refs"])[-1]) for v in out["violations"]}
    assert ("measure_out_of_bounds", "ss/rtl") in found


@pytest.mark.slow
def test_ring_bench_that_ignores_the_setup_fails(tmp_path, capsys):
    ws = ring_ws(tmp_path, {"list": ["ss"], "digital": ["rtl"]})
    test = ws / "tb" / "test_ring_osc_div.py"
    text = test.read_text(encoding="utf-8").replace(
        "            **cosimlib.corner_evidence(SETUP),\n", "")
    test.write_text(text, encoding="utf-8")
    assert check_cosim.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert "cosim_corner_not_applied" in {v["kind"] for v in out["violations"]}

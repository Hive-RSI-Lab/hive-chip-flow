"""engine/scripts/check_pex_sim.py: the ade `pex_sim` gate (docs/design.md
1.5, 5, "### M9."). Real magic parasitic extraction (every capacitor, and
extresist's wire resistance) then ngspice at the block's corners against the
corpus's own layout_ref/ post-layout bench + .bounds.json."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_pex_sim  # noqa: E402
import gate  # noqa: E402
import yaml as _yaml  # noqa: E402
from checklib import CheckError  # noqa: E402

CORPUS = REPO / "corpus" / "ade"
GATES_YAML = ENGINE / "reference" / "gates.yaml"


def _pex_sim_gate_row():
    rows = _yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))["gates"]["ade"]
    return rows["pex_sim"]


def make_ws(tmp_path: Path, block: str) -> Path:
    """A workspace holding the rung's own generator, M8 netlist, sizing,
    spec and post-layout bench - what faults.py's scratch copy carries."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    for sub in ("layout", "netlist", "sizing", "layout_ref"):
        src = CORPUS / block / sub
        if src.is_dir():
            (ws / sub).mkdir()
            for f in src.iterdir():
                if f.is_file() and f.suffix != ".gds":
                    shutil.copy2(f, ws / sub / f.name)
    (ws / "spec").mkdir()
    shutil.copy2(CORPUS / block / "spec.yaml", ws / "spec" / "spec.yaml")
    ws.joinpath("state.json").write_text(
        json.dumps({"version": 3, "skill": "ade", "block": block}),
        encoding="utf-8")
    return ws


def test_find_bench_refuses_with_neither_source(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        check_pex_sim.find_bench(ws, "mirror")
        assert False, "expected CheckError"
    except CheckError as exc:
        assert "no pex_sim bench" in str(exc)


def test_find_bench_prefers_tb_dir(tmp_path):
    ws = tmp_path / "ws"
    (ws / "tb").mkdir(parents=True)
    (ws / "layout_ref").mkdir(parents=True)
    canon_cir = ws / "tb" / "mirror_pex_tb.cir"
    canon_bounds = ws / "tb" / "mirror_pex_tb.bounds.json"
    canon_cir.write_text("* canonical\n", encoding="utf-8")
    canon_bounds.write_text("{}", encoding="utf-8")
    (ws / "layout_ref" / "mirror_pex_tb.cir").write_text("* standin\n",
                                                         encoding="utf-8")
    (ws / "layout_ref" / "mirror_pex_tb.bounds.json").write_text("{}",
                                                                 encoding="utf-8")
    cir, bounds = check_pex_sim.find_bench(ws, "mirror")
    assert cir == canon_cir
    assert bounds == canon_bounds


def run_json(args, tmp_path):
    # --out, not capsys - see test_check_analog_drc.py's own run_json for
    # why (a vendor GF180 pymacros import prints a stray line the first
    # time any test in the session imports it).
    out_path = tmp_path / "result.json"
    code = check_pex_sim.main([*args, "--out", str(out_path)])
    return code, json.loads(out_path.read_text(encoding="utf-8"))

def run_err(args, capsys):
    """An exit-2 refusal goes to stdout, never to --out (checklib.cli_wrap).
    Only for runs that never import the GF180 pymacros, whose first import
    prints a stray line."""
    code = check_pex_sim.main(args)
    return code, json.loads(capsys.readouterr().out)



def test_reorder_pins_puts_extracted_pins_in_reference_order():
    text = (".subckt current_mirror iref_node iout vss vdd\n"
            "X0 iout iref_node vss vss nfet_03v3 w=8u l=0.5u\n.ends\n")
    out = check_pex_sim.reorder_pins(
        text, "current_mirror", ["iref_node", "iout", "vdd", "vss"])
    assert out.splitlines()[0] == ".subckt current_mirror iref_node iout vdd vss"
    assert out.splitlines()[1] == text.splitlines()[1]


def test_reorder_pins_consumes_continuation_lines():
    body = "X0 iout iref_node vss vss nfet_03v3 w=8u l=0.5u\n.ends\n"
    text = (".subckt current_mirror iref_node iout\n"
            "+ vss vdd\n" + body)
    out = check_pex_sim.reorder_pins(
        text, "current_mirror", ["iref_node", "iout", "vdd", "vss"])
    assert out == (".subckt current_mirror iref_node iout vdd vss\n" + body)


def test_reorder_pins_refuses_a_different_pin_set():
    text = ".subckt blk a b\n.ends\n"
    with pytest.raises(CheckError, match="pins"):
        check_pex_sim.reorder_pins(text, "blk", ["a", "c"])


def test_pins_of_skips_subckt_parameters():
    text = ".subckt r2r_dac bmsb blsb vout r_width=2e-6\n.ends\n"
    assert check_pex_sim.pins_of(text, "r2r_dac") == ["bmsb", "blsb", "vout"]


def test_unlaunchable_magic_is_a_refusal(unlaunchable_eda, tmp_path, capsys):
    code, out = run_err(["--workspace", str(unlaunchable_eda)], capsys)
    assert code == 2, out
    assert out["status"] == "error"
    assert "failed to launch" in out["remediation"]


def test_netlist_with_no_parasitics_is_a_refusal(unlaunchable_eda, tmp_path,
                                                 monkeypatch, capsys):
    # what the review found: `ext2spice lvs` after the thresholds left the
    # "parasitic" netlist identical to the LVS one, devices only
    import layoutlib
    ws = unlaunchable_eda

    def lvs_only(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(".subckt blk a b\nX0 a b rm1 r_width=2u r_length=10u\n"
                     ".ends\n", encoding="utf-8")
        return p, ""

    monkeypatch.setattr(layoutlib, "run_magic_extract", lvs_only)
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2, out
    assert "no R and no C" in out["remediation"]


def test_measure_never_met_is_a_finding_not_a_refusal(unlaunchable_eda,
                                                      tmp_path, monkeypatch):
    # the comparator's pex bench on a layout that decided the wrong way: the
    # sim ran to the end, but outn never fell, so ngspice's `meas` for the
    # decision delay reported "out of interval ... failed!". That is the
    # design failing its bench (exit 1), not a gate that did not run.
    import layoutlib
    ws = unlaunchable_eda
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        '{"measures": {"x": {"min": 0, "max": 1}, '
        '"tdelay": {"min": 1e-10, "max": 5e-10}}}', encoding="utf-8")

    def extracted(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(".subckt blk a b\nR0 a a.t0 12.5\nC0 a b 0.1f\n"
                     "r1 a.t0 b 1k\n.ends\n", encoding="utf-8")
        return p, ""

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ("x = 5.0e-01\n"
                  "Error: measure  tdelay  trig(TARG) : out of interval\n"
                  " meas tran tdelay trig v(clk) val=1.4 rise=1 targ v(outn)"
                  " val=1.4 fall=1 failed!\n")

    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: Proc())
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert [(v["kind"], v["refs"][0]) for v in out["violations"]] == [
        ("measure_missing", "tdelay")]
    assert "never met" in out["violations"][0]["msg"]
    assert out["measured"] == {"x": 0.5}


# r2r_dac as a real ws hit it: the reference returns the ladder and every
# poly body to global 0 with no port for it, and magic named the layout's
# ground w_n352_n240#, so the bench ran with ground floating
R2R_REF = (".subckt r2r_dac bmsb blsb vout\n"
           "xrmsb bmsb vout 0 ppolyf_u_1k r_length={r_length} r_width=2e-6\n"
           "xr2lsb n1 0 0 ppolyf_u_1k r_length={r2_length} r_width=2e-6\n"
           ".ends r2r_dac\n")
R2R_EXT_FLOATING = (".subckt r2r_dac bmsb blsb vout\n"
                    "X0 vout bmsb w_n352_n240# ppolyf_u_1k r_width=2u "
                    "r_length=20u\n"
                    "X2 w_n352_n240# a_7214_0# w_n352_n240# ppolyf_u_1k "
                    "r_width=2u r_length=36u\n"
                    "C2 vout w_n352_n240# 1.08f\n.ends\n")


def test_element_nodes_reads_nodes_not_values_or_models():
    text = ("* comment 0\n.subckt blk a b\nX1 a 0 b\n+ rm1 w=1u\n"
            "C1 a b 0\nR2 b gnd 1k\nM1 d g s b nfet l=0\n.ends\n"
            ".control\nlet v = 0\n.endc\n")
    assert check_pex_sim.element_nodes(text) == {
        "a", "b", "0", "gnd", "d", "g", "s"}


def test_ground_finding_when_reference_uses_global_0_and_layout_does_not():
    v = check_pex_sim.ground_finding(
        R2R_REF, ["bmsb", "blsb", "vout"], R2R_EXT_FLOATING, "r2r_dac",
        "layout/r2r_dac.gds")
    assert v["kind"] == "ground_unlabelled"
    assert v["refs"] == ["0"]
    assert "label" in v["msg"] and "layout-fixer" in v["msg"]


def test_ground_labelled_0_in_the_layout_is_no_finding():
    ext = R2R_EXT_FLOATING.replace("w_n352_n240#", "0")
    assert check_pex_sim.ground_finding(
        R2R_REF, ["bmsb", "blsb", "vout"], ext, "r2r_dac", "g") is None


def test_pin_grounded_reference_is_unchanged():
    # the mirror: ground is the vss pin, so the extraction owes no node 0
    ref = (".subckt current_mirror iref_node iout vdd vss\n"
           "xm1 iref_node iref_node vss vss nfet_03v3 w=8u l=0.5u\n.ends\n")
    ext = (".subckt current_mirror iref_node iout vss vdd\n"
           "X0 iout iref_node vss vss nfet_03v3 w=8u l=0.5u\n"
           "C0 iout vss 0.2f\n.ends\n")
    assert check_pex_sim.ground_finding(
        ref, ["iref_node", "iout", "vdd", "vss"], ext, "current_mirror",
        "g") is None


def test_floating_ground_is_a_finding_and_no_bench_runs(unlaunchable_eda,
                                                        tmp_path,
                                                        monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nr1 a n1 1k\nr2 n1 0 1k\nr3 n1 b 1k\n.ends\n",
        encoding="utf-8")

    def extracted(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(".subckt blk a b\nR0 a n1 1k\nR1 n1 a_5_5# 1k\n"
                     "R2 n1 b 1k\nC0 a b 0.1f\n.ends\n", encoding="utf-8")
        return p, ""

    def no_sim(*a, **k):
        raise AssertionError("the bench ran on a floating ground")

    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)
    monkeypatch.setattr(layoutlib, "run_eda", no_sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert [(v["kind"], v["refs"]) for v in out["violations"]] == [
        ("ground_unlabelled", ["0"])]
    assert out["measured"] == {}


# ring_osc_div as #44 found it: the bench reads the stage nets inside the
# extracted cell, and a stage net with no layout label came back a_..#
RING_REF = (".subckt blk a b\nr1 a s1 1k\nr2 s1 s2 1k\nr3 s2 b 1k\n"
            ".ends\n")
RING_BENCH = ("* {pdk} {extracted}\nv1 in 0 dc 1\nxdut in 0 blk\n"
              ".control\nop\nprint v(xdut.s1)\n"
              "meas tran t1 when v(xdut.s2, in)=0.5\n+ rise=1\n"
              "print vdb(in) v(xdut.xinv.q) v(xother.s9)\n.endc\n.end\n")


def test_probed_nets_reads_one_level_inside_the_top_cell_only():
    assert check_pex_sim.probed_nets(RING_BENCH, "blk") == {"s1", "s2"}


def test_probe_finding_names_each_unlabelled_net():
    ext = ".subckt blk a b\nR0 a s1 1k\nR1 s1 a_5_5# 1k\nR2 a_5_5# b 1k\n.ends\n"
    found = check_pex_sim.probe_findings(RING_BENCH, RING_REF, ext, "blk",
                                         "layout/blk.gds")
    assert [(v["kind"], v["refs"]) for v in found] == [
        ("probe_unlabelled", ["s2"])]
    assert "label" in found[0]["msg"] and "layout-fixer" in found[0]["msg"]


def test_probe_of_a_net_the_reference_lacks_is_no_finding():
    # a bench typo is the bench's bug, not the layout's: ngspice refuses it
    ext = ".subckt blk a b\nR0 a a_1_1# 1k\nR1 a_1_1# b 1k\n.ends\n"
    bench = RING_BENCH.replace("xdut.s1", "xdut.s7").replace("xdut.s2", "xdut.s8")
    assert check_pex_sim.probe_findings(bench, RING_REF, ext, "blk", "g") == []


def _ring_ws(ws, monkeypatch, ext_text):
    import layoutlib
    (ws / "netlist" / "blk.cir").write_text(RING_REF, encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        RING_BENCH, encoding="utf-8")

    def extracted(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(ext_text, encoding="utf-8")
        return p, ""

    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)


def test_unlabelled_probe_is_a_finding_and_no_bench_runs(unlaunchable_eda,
                                                         tmp_path,
                                                         monkeypatch):
    import layoutlib
    _ring_ws(unlaunchable_eda, monkeypatch,
             ".subckt blk a b\nR0 a a_5_5# 1k\nR1 a_5_5# s2 1k\n"
             "R2 s2 b 1k\nC0 a b 0.1f\n.ends\n")

    def no_sim(*a, **k):
        raise AssertionError("the bench ran with a probe that is not there")

    monkeypatch.setattr(layoutlib, "run_eda", no_sim)
    code, out = run_json(["--workspace", str(unlaunchable_eda)], tmp_path)
    assert code == 1, out
    assert [(v["kind"], v["refs"]) for v in out["violations"]] == [
        ("probe_unlabelled", ["s1"])]
    assert out["measured"] == {}


def test_labelled_probes_run_the_bench(unlaunchable_eda, tmp_path,
                                       monkeypatch):
    import layoutlib
    _ring_ws(unlaunchable_eda, monkeypatch,
             ".subckt blk a b\nR0 a s1 1k\nR1 s1 s2 1k\nR2 s2 b 1k\n"
             "C0 a b 0.1f\n.ends\n")

    class Proc:
        returncode = 0
        stderr = ""
        stdout = "x = 5.0e-01\n"

    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: Proc())
    code, out = run_json(["--workspace", str(unlaunchable_eda)], tmp_path)
    assert code == 0, out
    assert out["measured"] == {"x": 0.5}


@pytest.mark.slow
def test_clean_mirror_pex_sim_passes(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert out["violations"] == []
    assert out["parasitics"]["r"] > 0 and out["parasitics"]["c"] > 0
    assert set(out["measured"]) == {"vdrop", "f3db"}
    # no sim_pvt record in a scratch ws: the whole sweep, and it passes
    assert out["corners"] == ["tt", "ss", "ff", "sf", "fs"]
    assert set(out["measured_by_corner"]) == set(out["corners"])
    gate_result = gate.evaluate("pex_sim", _pex_sim_gate_row(), out)
    assert gate_result["status"] == "pass"


@pytest.mark.slow
def test_clean_r2r_dac_pex_sim_passes(tmp_path):
    ws = make_ws(tmp_path, "r2r_dac")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert out["parasitics"]["c"] > 0
    m = out["measured"]
    # the three codes of a 1:1 ladder, the rung's current sizing
    assert abs(m["vout_lsb_frac"] - 0.2) < 0.006
    assert abs(m["vout_msb_frac"] - 0.6) < 0.018
    assert abs(m["vout_both_frac"] - 0.8) < 0.024


@pytest.mark.slow
def test_long_output_line_fails_pex_sim(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    path = CORPUS / "mirror" / "faults" / "plant_pex_long_output_line.py"
    spec = importlib.util.spec_from_file_location("plant_pex_long", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.plant(ws)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert {"f3db", "tt"} <= {r for v in out["violations"] for r in v["refs"]}
    gate_result = gate.evaluate("pex_sim", _pex_sim_gate_row(), out)
    assert gate_result["status"] == "fail"


def _plant(ws, name):
    path = CORPUS / "mirror" / "faults" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.plant(ws)


@pytest.mark.slow
def test_a_fault_only_slow_hot_shows_fails_and_names_the_corner(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    _plant(ws, "plant_pex_line_slow_corner_only.py")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    failed = {tuple(sorted(v["refs"])) for v in out["violations"]}
    assert failed == {("f3db", "ss"), ("f3db", "sf")}, out["violations"]
    assert all("@ s" in v["msg"] for v in out["violations"])
    assert 5e8 <= out["measured_by_corner"]["tt"]["f3db"]
    assert gate.evaluate("pex_sim", _pex_sim_gate_row(), out)["status"] == "fail"


@pytest.mark.slow
def test_typical_only_pass_on_that_fault_is_what_the_sweep_catches(tmp_path):
    # what the gate did before corners: typical alone passes this layout
    ws = make_ws(tmp_path, "mirror")
    _plant(ws, "plant_pex_line_slow_corner_only.py")
    spec = ws / "spec" / "spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "pex_corners: [tt]\n",
                    encoding="utf-8")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert out["corners"] == ["tt"]


@pytest.mark.slow
def test_default_selection_with_a_sim_pvt_record_still_runs_slow_hot(
        tmp_path, monkeypatch):
    # sim_pvt never measured f3db and saw no wire C: slow/hot is in anyway
    ws = make_ws(tmp_path, "mirror")
    _plant(ws, "plant_pex_line_slow_corner_only.py")
    monkeypatch.setattr(check_pex_sim, "fresh_sim_pvt_results",
                        lambda _ws: [{"corner": "ff",
                                      "measures": {"iout_raw": 3e-5}}])
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert out["corners"] == ["tt", "ss"]
    assert {r for v in out["violations"] for r in v["refs"]} == {"f3db", "ss"}


SWEEP = [{"name": "tt", "process": "typical", "temp_c": 27, "supply_pct": 0},
         {"name": "ss", "process": "ss", "temp_c": 125, "supply_pct": -10},
         {"name": "ff", "process": "ff", "temp_c": -40, "supply_pct": 10},
         {"name": "sf", "process": "sf", "temp_c": 125, "supply_pct": -10},
         {"name": "fs", "process": "fs", "temp_c": -40, "supply_pct": 10}]
GRID = [{"name": "tt_m40c", "process": "typical", "temp_c": -40, "supply_pct": 0},
        {"name": "tt_27c", "process": "typical", "temp_c": 27, "supply_pct": 0},
        {"name": "ss_27c", "process": "ss", "temp_c": 27, "supply_pct": 0},
        {"name": "ss_125c_vm10", "process": "ss", "temp_c": 125,
         "supply_pct": -10},
        {"name": "ss_pff", "process": "ss", "temp_c": 125, "supply_pct": -10,
         "passive": "ff"}]


def _names(corners):
    return [c["name"] for c in corners]


def test_typical_and_slow_are_found_in_a_grid():
    assert check_pex_sim.typical_corner(GRID)["name"] == "tt_27c"
    assert check_pex_sim.slow_corner(GRID)["name"] == "ss_125c_vm10"
    assert check_pex_sim.slow_corner(SWEEP[:1]) is None


def test_worst_corner_is_least_margin_to_the_pex_bound_per_measure():
    bounds = [{"measure": "f", "min": 100.0}, {"measure": "G", "min": 0.0,
                                               "max": 10.0},
              {"measure": "not_in_sim_pvt", "max": 1.0}]
    results = [{"corner": "tt", "measures": {"f": 150.0, "g": 5.0}},
               {"corner": "ff", "measures": {"f": 300.0, "g": 9.5}},
               {"corner": "sf", "measures": {"f": 90.0, "g": 4.0}}]
    assert check_pex_sim.worst_corners(SWEEP, bounds, results) == ["sf", "ff"]


def test_worst_corner_honours_a_bounds_scope():
    bounds = [{"measure": "f", "min": 100.0, "corners": ["tt", "ff"]}]
    results = [{"corner": "tt", "measures": {"f": 150.0}},
               {"corner": "ff", "measures": {"f": 300.0}},
               {"corner": "ss", "measures": {"f": 1.0}}]
    assert check_pex_sim.worst_corners(SWEEP, bounds, results) == ["tt"]


@pytest.mark.parametrize("field, results, want, source", [
    (None, None, ["tt", "ss", "ff", "sf", "fs"], "whole sweep"),
    (None, [], ["tt", "ss"], "sim_pvt"),
    (None, [{"corner": "fs", "measures": {"f": 1.0}}], ["tt", "ss", "fs"],
     "sim_pvt"),
    ("all", None, ["tt", "ss", "ff", "sf", "fs"], "all"),
    (["ff"], None, ["tt", "ff"], "pex_corners"),   # typical is never dropped
    (["tt"], [{"corner": "fs", "measures": {"f": 1.0}}], ["tt"], "pex_corners"),
])
def test_select_corners(field, results, want, source):
    picked, got, unswept = check_pex_sim.select_corners(
        field, SWEEP, [{"measure": "f", "min": 0.0}], results)
    assert _names(picked) == want and source in got and unswept == []


def test_a_bounds_own_corner_joins_the_set_or_is_unswept():
    in_sweep = {"measure": "f", "min": 0.0, "corners": ["ff"]}
    picked, _, unswept = check_pex_sim.select_corners(
        ["tt"], SWEEP, [in_sweep], None)
    assert _names(picked) == ["tt", "ff"] and unswept == []
    outside = {"measure": "f", "min": 0.0, "corners": ["tt_pss"]}
    _, _, unswept = check_pex_sim.select_corners(["tt"], SWEEP, [outside], None)
    assert unswept == [outside]


@pytest.mark.parametrize("field", ["ss", [], [1], ["nope"], {"grid": 1}])
def test_bad_pex_corners_refuses(field):
    with pytest.raises(CheckError, match="pex_corners"):
        check_pex_sim.select_corners(field, SWEEP, [], None)


def test_a_grid_name_selects_by_any_of_its_names():
    picked, _, _ = check_pex_sim.select_corners(
        ["tt_27c", "ss_125c_vm10"], GRID, [], None)
    assert _names(picked) == ["tt_27c", "ss_125c_vm10"]


def test_unaware_tokens_names_what_the_corners_move():
    typ = SWEEP[:1]
    bare = "* typical only\n"
    assert check_pex_sim.unaware_tokens(bare, typ, ["resistor"]) == []
    assert check_pex_sim.unaware_tokens(bare, SWEEP, []) == [
        "corner", "temp_c", "vdd"]
    assert check_pex_sim.unaware_tokens(bare, SWEEP, ["resistor", "mim_cap"]) == [
        "corner", "temp_c", "vdd", "res_corner", "mim_corner"]
    aware = "{corner} {temp_c} {vdd}"
    assert check_pex_sim.unaware_tokens(aware, SWEEP, []) == []
    # a single non-typical corner moves them too
    assert check_pex_sim.unaware_tokens(bare, SWEEP[1:2], []) == [
        "corner", "temp_c", "vdd"]
    # a sweep whose resistors are pinned by a scope-out owes no res_corner
    pinned = [dict(c, pinned={"resistor": "typical"}) for c in SWEEP]
    assert "res_corner" not in check_pex_sim.unaware_tokens(
        bare, pinned, ["resistor"])


def test_a_typical_only_bench_under_a_corner_sweep_is_a_finding_and_no_sim_runs(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")

    def no_sim(*a, **k):
        raise AssertionError("a bench ran")
    monkeypatch.setattr(layoutlib, "run_eda", no_sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert {v["kind"] for v in out["violations"]} == {
        "pex_bench_not_corner_aware"}
    assert {r for v in out["violations"] for r in v["refs"]} == {
        "corner", "temp_c", "vdd"}


def _spec(ws, text):
    (ws / "spec").mkdir(exist_ok=True)
    (ws / "spec" / "spec.yaml").write_text(text, encoding="utf-8")


def _aware_bench(ws, bounds):
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        ".lib x {corner}\n.temp {temp_c}\nv1 a 0 {vdd}\n.end\n",
        encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        json.dumps({"measures": bounds}), encoding="utf-8")


def test_each_corner_gets_its_own_deck_values_and_bound(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\npex_corners: [ss]\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    _aware_bench(ws, {"x": [{"min": 1, "corners": ["tt"]},
                            {"min": 3, "corners": ["ss"]}]})
    decks = {}

    def sim(bench, cwd, timeout, failed_measures):
        decks[bench.name] = bench.read_text(encoding="utf-8")
        return "x = 2.0\n"
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    ss = decks["blk_pex_tb__ss.cir"]
    assert ".lib x ss\n" in ss and ".temp 125\n" in ss and "v1 a 0 2.97\n" in ss
    tt = decks["blk_pex_tb__tt.cir"]
    assert ".lib x typical\n" in tt and "v1 a 0 3.3\n" in tt
    [v] = out["violations"]
    assert v["kind"] == "measure_out_of_bounds" and "x @ ss = 2" in v["msg"]
    assert out["measured"] == {"x": 2.0}  # typical's
    assert out["measured_by_corner"] == {"tt": {"x": 2.0}, "ss": {"x": 2.0}}


def test_a_corner_with_no_scored_bound_is_skipped_and_listed(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\npex_corners: [ss]\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    _aware_bench(ws, {"x": {"min": 1, "corners": ["tt"]}})
    monkeypatch.setattr(layoutlib, "run_ngspice",
                        lambda *a, **k: "x = 2.0\n")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert list(out["measured_by_corner"]) == ["tt"]
    assert [n["corner"] for n in out["not_scored"]] == ["ss"]


def test_a_stale_sim_pvt_record_is_no_record(tmp_path, monkeypatch):
    import statelib
    ws = tmp_path
    rec = ws / "reports" / "recorded" / "gate-sim_pvt.json"
    assert check_pex_sim.fresh_sim_pvt_results(ws) is None
    rec.parent.mkdir(parents=True)
    results = [{"corner": "ff", "measures": {"f": 1.0}}]
    rec.write_text(json.dumps({"inputs_at_start": {"netlist": "a"},
                               "facts": {"results": results}}),
                   encoding="utf-8")
    monkeypatch.setattr(statelib, "gate_input_hashes",
                        lambda *a, **k: {"netlist": "a"})
    assert check_pex_sim.fresh_sim_pvt_results(ws) == results
    # a record from before pdk_* were inputs is judged on what it holds
    monkeypatch.setattr(statelib, "gate_input_hashes",
                        lambda *a, **k: {"netlist": "a", "pdk_models": "x"})
    assert check_pex_sim.fresh_sim_pvt_results(ws) == results
    monkeypatch.setattr(statelib, "gate_input_hashes",
                        lambda *a, **k: {"netlist": "b"})
    assert check_pex_sim.fresh_sim_pvt_results(ws) is None


def _unlabelled_mirror(tmp_path):
    """The mirror with iref_node's label taken out: magic names that net
    itself (a_38_n132#), which extresist used to strip of its couplings."""
    ws = make_ws(tmp_path, "mirror")
    gen = ws / "layout" / "gen_mirror.py"
    text = gen.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines(keepends=True)
                if 'labels.append(("iref_node"' in ln)
    gen.write_text(text.replace(line, ""), encoding="utf-8")
    import layout_gen
    gds, top, _p, _a = layout_gen.build(ws, "mirror")
    (ws / "log").mkdir()
    return ws, gds.resolve(), top


@pytest.mark.slow
def test_unlabelled_net_keeps_its_coupling_through_extresist(tmp_path):
    import layoutlib
    ws, gds, top = _unlabelled_mirror(tmp_path)
    out, _log = layoutlib.run_magic_extract(ws / "log", gds, top,
                                            parasitics=True)
    text = out.read_text(encoding="utf-8")
    assert any(ln.split()[1:3] in (["a_38_n132", "iout"],
                                   ["iout", "a_38_n132"])
               for ln in text.splitlines() if ln.startswith("C"))


@pytest.mark.slow
def test_unnamed_net_losing_its_coupling_is_a_refusal(tmp_path, monkeypatch):
    # the planted fault: the naming pass off, extresist drops the net's C
    import layoutlib
    ws, gds, top = _unlabelled_mirror(tmp_path)
    monkeypatch.setattr(layoutlib, "name_unlabelled_nets", lambda ext: [])
    with pytest.raises(layoutlib.LayoutError, match="a_38_n132-iout"):
        layoutlib.run_magic_extract(ws / "log", gds, top, parasitics=True)


def _rc_extract(monkeypatch):
    import layoutlib

    def extracted(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(".subckt blk a b\nR0 a b 12.5\nC0 a b 0.1f\n.ends\n",
                     encoding="utf-8")
        return p, ""

    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)


def test_scale_parasitics_moves_each_kind_and_keeps_the_suffix():
    import layoutlib
    text = (".subckt blk a b\nR0 a b 12.5\nC0 a b 4.89611f\nC1 a 0 1e-15\n"
            "Xm a b c d nfet w=1u\n.ends\n")
    got = layoutlib.scale_parasitics(text, 1.2, 2).splitlines()
    assert got == [".subckt blk a b", "R0 a b 25", "C0 a b 5.875332f",
                   "C1 a 0 1.2e-15", "Xm a b c d nfet w=1u", ".ends"]
    # a factor of 1 leaves that kind's lines as extracted
    assert "R0 a b 12.5" in layoutlib.scale_parasitics(text, 1.2, 1)
    assert "C0 a b 4.89611f" in layoutlib.scale_parasitics(text, 1, 1.2)


@pytest.mark.parametrize("line", [
    "R0 a b rm1 l=1", "R0 a b", "C0 a b 1f w=2", "C0 a b tbd",
])
def test_scale_parasitics_refuses_a_line_it_cannot_read(line):
    import layoutlib
    with pytest.raises(layoutlib.LayoutError, match="cannot scale"):
        layoutlib.scale_parasitics(f"{line}\nR1 a b 1\nC1 a b 1f\n", 1.2, 1.2)


@pytest.mark.parametrize("c, r", [(1.2, 1), (1, 1.2)])
def test_scale_parasitics_refuses_a_factor_with_nothing_to_move(c, r):
    # a c_high run on a netlist with no C line would be the nominal run again
    import layoutlib
    only = "R0 a b 1\n" if c != 1 else "C0 a b 1f\n"
    with pytest.raises(layoutlib.LayoutError, match="no [RC] line"):
        layoutlib.scale_parasitics(only, c, r)


def test_fast_corner_is_the_mirror_of_slow():
    fast = [{"name": "ff_27c", "process": "ff", "temp_c": 27, "supply_pct": 0},
            {"name": "ff_cold", "process": "ff", "temp_c": -40, "supply_pct": 10},
            {"name": "ff_pss", "process": "ff", "temp_c": -40, "supply_pct": 10,
             "passive": "ss"}]
    assert check_pex_sim.fast_corner(GRID) is None
    assert check_pex_sim.fast_corner(SWEEP[:1]) is None
    assert check_pex_sim.fast_corner(fast)["name"] == "ff_cold"


def test_rc_knob_runs_pair_high_with_slow_and_low_with_fast():
    runs = check_pex_sim.rc_knob_runs(["c_low", "rc_high"], SWEEP)
    assert [(k["knob"], k["corner"]["name"], k["c"], k["r"]) for k in runs] == [
        ("c_low", "ff", 0.84, 1.0), ("rc_high", "ss", 1.20, 1.156)]
    assert check_pex_sim.rc_knob_runs(None, SWEEP) == []
    assert len(check_pex_sim.rc_knob_runs("all", SWEEP)) == 6


@pytest.mark.parametrize("field", ["c_high", [], ["nope"], [1]])
def test_rc_knob_runs_refuses_a_bad_field(field):
    with pytest.raises(CheckError, match="pex_rc_knobs"):
        check_pex_sim.rc_knob_runs(field, SWEEP)


def test_rc_knob_runs_refuses_a_sweep_without_the_paired_process():
    # a grid must span ss and ff today, so only a table change gets here: a
    # knob with no corner to run at is refused, not dropped
    with pytest.raises(CheckError, match="c_low needs ff"):
        check_pex_sim.rc_knob_runs(["c_low"], GRID)


def _rc_knob_block(ws, field, bounds, bench=None):
    _spec(ws, f"top: blk\nsupply: {{vdd: 3.3}}\npex_corners: [ss]\n"
              f"pex_rc_knobs: {field}\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        bench or ".lib x {corner}\n.temp {temp_c}\nv1 a 0 {vdd}\n"
        ".include {extracted}\n.end\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        json.dumps({"measures": bounds}), encoding="utf-8")


def test_knob_runs_its_scaled_netlist_and_the_finding_names_both(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _rc_knob_block(ws, "[rc_high]", {"x": {"min": 1}})
    decks = {}

    def sim(bench, cwd, timeout, failed_measures):
        decks[bench.name] = bench.read_text(encoding="utf-8")
        # the measure drops under its floor only on the scaled netlist
        return "x = 0.5\n" if ".rc_high.spice" in decks[bench.name] else "x = 2\n"
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert set(decks) == {"blk_pex_tb__tt.cir", "blk_pex_tb__ss.cir",
                          "blk_pex_tb__ss__rc_high.cir"}
    assert all(".rc_high.spice" not in decks[n] for n in
               ("blk_pex_tb__tt.cir", "blk_pex_tb__ss.cir"))
    assert ".temp 125" in decks["blk_pex_tb__ss__rc_high.cir"]
    [v] = out["violations"]
    assert v["kind"] == "measure_out_of_bounds"
    assert set(v["refs"]) == {"x", "ss", "rc_high"}
    assert "x @ ss / rc_high (C x1.2, R x1.156) = 0.5" in v["msg"]
    assert out["rc_knobs"] == [{"knob": "rc_high", "corner": "ss", "c": 1.2,
                                "r": 1.156, "measured": {"x": 0.5}}]
    assert set(out["measured_by_corner"]) == {"tt", "ss"}
    scaled = (ws / "log" / "pex_sim" / "blk.pex.ordered.rc_high.spice").read_text(
        encoding="utf-8")
    assert "R0 a b 14.45" in scaled and "C0 a b 0.12f" in scaled


def test_no_knob_field_adds_no_run_and_no_report_field(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\npex_corners: [ss]\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    _aware_bench(ws, {"x": {"min": 1}})
    ran = []

    def sim(bench, cwd, timeout, failed_measures):
        ran.append(bench.name)
        return "x = 2\n"
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert sorted(ran) == ["blk_pex_tb__ss.cir", "blk_pex_tb__tt.cir"]
    assert "rc_knobs" not in out


def test_knob_on_a_bench_without_extracted_is_a_finding_and_runs_nothing(
        unlaunchable_eda, tmp_path, monkeypatch):
    # a bench that hard-codes its netlist would run every knob on nominal wires
    import layoutlib
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _rc_knob_block(ws, "[c_high]", {"x": {"min": 1}},
                   bench=".lib x {corner}\n.temp {temp_c}\nv1 a 0 {vdd}\n.end\n")

    def sim(*a, **k):
        raise AssertionError("no bench may run")
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    [v] = out["violations"]
    assert v["kind"] == "pex_bench_not_corner_aware" and v["refs"] == ["extracted"]


def test_knob_corner_needs_its_tokens_even_when_outside_pex_corners(
        unlaunchable_eda, tmp_path, monkeypatch):
    # typical-only corners ask no token of the bench, and c_high's slow corner
    # does: without {corner} it would simulate typical devices and pass unseen
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\npex_corners: [tt]\n"
              "pex_rc_knobs: [c_high]\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        ".include {extracted}\n.end\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        json.dumps({"measures": {"x": {"min": 1}}}), encoding="utf-8")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert {v["refs"][0] for v in out["violations"]} == {"corner", "temp_c", "vdd"}


def test_knob_with_no_bound_at_its_corner_is_a_refusal(
        unlaunchable_eda, tmp_path, monkeypatch, capsys):
    ws = unlaunchable_eda
    _rc_extract(monkeypatch)
    _rc_knob_block(ws, "[c_low]", {"x": {"min": 1, "corners": ["ss"]}})
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2 and "c_low" in out["error"] and "ff" in out["error"]


def test_knob_refuses_a_netlist_it_cannot_scale(
        unlaunchable_eda, tmp_path, monkeypatch, capsys):
    import layoutlib
    ws = unlaunchable_eda

    def extracted(work_dir, gds, cell, parasitics):
        p = work_dir / f"{cell}.pex.spice"
        p.write_text(".subckt blk a b\nR0 a b rm1 l=1\nC0 a b 0.1f\n.ends\n",
                     encoding="utf-8")
        return p, ""
    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)
    _rc_knob_block(ws, "[r_high]", {"x": {"min": 1}})

    def sim(*a, **k):
        raise AssertionError("no bench may run")
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2
    assert "cannot scale" in out["error"] and "r_high" in out["error"]


# #160: "Test: the mirror with a line that passes every PVT corner at nominal C
# and fails at C x(1+dc): the gate must fail and name both the PVT corner and
# the RC knob."
@pytest.mark.slow
def test_line_passing_every_corner_fails_at_c_high_and_names_both(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    _plant(ws, "plant_pex_line_fails_at_c_high.py")
    spec_path = ws / "spec" / "spec.yaml"
    with_knob = spec_path.read_text(encoding="utf-8")
    spec_path.write_text(with_knob.replace("pex_rc_knobs: [c_high]\n", "")
                         + "pex_corners: all\n", encoding="utf-8")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out["violations"]
    assert out["corners"] == ["tt", "ss", "ff", "sf", "fs"]

    spec_path.write_text(with_knob, encoding="utf-8")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    [v] = out["violations"]
    assert v["kind"] == "measure_out_of_bounds"
    assert set(v["refs"]) == {"f3db", "ss", "c_high"}
    assert "f3db @ ss / c_high (C x1.2, R x1)" in v["msg"]
    assert out["measured_by_corner"]["ss"]["f3db"] >= 5e8


@pytest.mark.slow
def test_pinned_knobs_bound_the_pdk_files_they_cite():
    """pex_rc_knobs.yaml says where its numbers come from: read those files."""
    import corners as corners_mod
    import layoutlib
    pdk = layoutlib.pdk_root()
    knobs = corners_mod.rc_knobs()

    def rpersq(which):
        tlef = (pdk / "libs.ref" / "gf180mcu_fd_sc_mcu7t5v0" / "techlef" /
                f"gf180mcu_fd_sc_mcu7t5v0__{which}.tlef").read_text(encoding="utf-8")
        block = tlef[tlef.index("LAYER Metal1"):]
        return float(re.search(r"RESISTANCE RPERSQ\s+([\d.]+)", block)[1])
    assert rpersq("max") / rpersq("nom") == pytest.approx(knobs["r_high"]["r"], abs=1e-3)
    assert rpersq("min") / rpersq("nom") == pytest.approx(knobs["r_low"]["r"], abs=1e-3)

    def rows(which):
        out, section = {}, None
        text = (pdk / "libs.tech" / "librelane" /
                f"rules.openrcx.gf180mcuD.{which}").read_text(encoding="utf-8")
        for line in text.splitlines():
            t = line.split()
            if t[:1] == ["Metal"] and len(t) >= 3:
                section = tuple(t[1:])
            elif section and len(t) == 4 and all(
                    re.fullmatch(r"[-+0-9.eE]+", x) for x in t):
                out.setdefault(section, []).append([float(x) for x in t])
        return out
    nom = rows("nom")
    assert nom
    for which, knob, pick in (("max", "c_high", max), ("min", "c_low", min)):
        other = rows(which)
        ratios = [(a[1] + a[2]) / (b[1] + b[2]) for key, row in nom.items()
                  for a, b in zip(other[key], row) if b[1] + b[2] > 0]
        extreme = pick(ratios)
        assert knobs[knob]["c"] == pytest.approx(extreme, abs=0.005)
        assert pick(extreme, knobs[knob]["c"]) == knobs[knob]["c"], extreme


# brief: "let a block's spec set the simulation time limit in a place the
# gate reads ... the CLI --timeout flag overrides it. Keep the current
# defaults when the spec says nothing."
@pytest.mark.parametrize("spec, flag, want", [
    (None, [], 120.0),
    ("top: blk\npex_corners: [tt]\n", [], 120.0),
    ("top: blk\npex_corners: [tt]\nsim_timeout_s: {pex_sim: 900}\n", [], 900.0),
    ("top: blk\npex_corners: [tt]\nsim_timeout_s: {sim_tt: 900}\n", [], 120.0),
    ("top: blk\npex_corners: [tt]\nsim_timeout_s: {pex_sim: 900}\n",
     ["--timeout", "30"], 30.0),
    (None, ["--timeout", "30"], 30.0),
])
def test_pex_sim_timeout_comes_from_spec_then_flag(unlaunchable_eda, tmp_path,
                                                   monkeypatch, spec, flag,
                                                   want):
    import layoutlib
    ws = unlaunchable_eda
    if spec is not None:
        (ws / "spec").mkdir()
        (ws / "spec" / "spec.yaml").write_text(spec, encoding="utf-8")
    _rc_extract(monkeypatch)
    seen = []

    class Proc:
        returncode = 0
        stderr = ""
        stdout = "x = 5.0e-01\n"

    def fake_eda(args, cwd=None, timeout=0.0, stdin_text=None):
        seen.append(timeout)
        return Proc()

    monkeypatch.setattr(layoutlib, "run_eda", fake_eda)
    code, out = run_json(["--workspace", str(ws), *flag], tmp_path)
    assert code == 0, out
    assert seen == [want]


def test_pex_sim_timeout_is_a_refusal_naming_the_spec_knob(unlaunchable_eda,
                                                           monkeypatch,
                                                           capsys):
    # a synthetic slow bench: a stand-in ngspice that outlives the spec's
    # limit, run through the real subprocess timeout
    import layoutlib
    ws = unlaunchable_eda
    (ws / "spec").mkdir()
    (ws / "spec" / "spec.yaml").write_text(
        "top: blk\npex_corners: [tt]\nsim_timeout_s: {pex_sim: 0.3}\n", encoding="utf-8")
    _rc_extract(monkeypatch)
    slow = ws.parent / "slow_eda"
    slow.write_text("#!/usr/bin/env bash\nexec sleep 5\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setattr(layoutlib, "EDA_BIN", slow)
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2, out
    assert "0.3 s limit" in out["remediation"]
    assert "sim_timeout_s: {pex_sim: <seconds>}" in out["remediation"]
    assert "at corner tt" in out["remediation"]


def test_pex_sim_refuses_a_bad_spec_timeout(unlaunchable_eda, monkeypatch,
                                            capsys):
    ws = unlaunchable_eda
    (ws / "spec").mkdir()
    (ws / "spec" / "spec.yaml").write_text(
        "top: blk\nsim_timeout_s: {pex_sim: 9000}\n", encoding="utf-8")
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2, out
    assert "sim_timeout_s" in out["remediation"]

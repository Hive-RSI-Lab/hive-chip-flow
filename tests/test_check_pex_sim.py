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


REAL_CROSSCHECK = check_pex_sim.extractor_crosscheck


@pytest.fixture(autouse=True)
def _no_kpex_outside_slow_tests(request, monkeypatch):
    """The fast tests stub magic, so there is no GDS for kpex to extract:
    they stub the cross-check too (its own tests below call
    REAL_CROSSCHECK). The slow tests run the real one."""
    if request.node.get_closest_marker("slow") is None:
        monkeypatch.setattr(check_pex_sim, "extractor_crosscheck",
                            lambda *a, **k: ([], {"stubbed": True}))


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


def _crosscheck_agrees(out, nets):
    """magic and kpex 2.5D agree on the clean block, on these nets at least."""
    assert not [v for v in out["violations"]
                if v["kind"] == "pex_extractors_disagree"], out["violations"]
    assert nets <= {r["net"] for r in out["extractor_crosscheck"]["nets"]}


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
    _crosscheck_agrees(out, {"iout", "iref_node", "vss"})


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
    _crosscheck_agrees(out, {"blsb", "bmsb", "vout"})


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


@pytest.mark.slow
def test_clean_comparator_symmetric_nets_pass(tmp_path):
    ws = make_ws(tmp_path, "comparator")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert not [v for v in out["violations"] if v["kind"] == "symmetry_violated"]
    pairs = {tuple(e["nets"]): e for e in out["symmetry"]}
    assert set(pairs) == {("outp", "outn"), ("inp", "inn")}
    for e in pairs.values():
        assert {c["quantity"] for c in e["checked"]} == {"C", "R", "C to clk"}
        assert all(c["mismatch_pct"] <= 5 for c in e["checked"])
    _crosscheck_agrees(out, {"clk", "inn", "inp", "outn", "outp"})


@pytest.mark.slow
def test_asymmetric_output_detour_fails_before_the_bench(tmp_path):
    ws = make_ws(tmp_path, "comparator")
    path = CORPUS / "comparator" / "faults" / "plant_pex_asymmetric_output.py"
    spec = importlib.util.spec_from_file_location("plant_pex_asym", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.plant(ws)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    sym = [v for v in out["violations"] if v["kind"] == "symmetry_violated"]
    assert sym and all({"outp", "outn"} <= set(v["refs"]) for v in sym)
    # both values are in the message, and the pair is reported as measured
    assert any("outn" in v["msg"] and "outp" in v["msg"] for v in sym)
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
    itself (a_638_n132#), which extresist used to strip of its couplings."""
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
    assert any(ln.split()[1:3] in (["a_638_n132", "iout"],
                                   ["iout", "a_638_n132"])
               for ln in text.splitlines() if ln.startswith("C"))


@pytest.mark.slow
def test_unnamed_net_losing_its_coupling_is_a_refusal(tmp_path, monkeypatch):
    # the planted fault: the naming pass off, extresist drops the net's C
    import layoutlib
    ws, gds, top = _unlabelled_mirror(tmp_path)
    monkeypatch.setattr(layoutlib, "name_unlabelled_nets", lambda ext: [])
    with pytest.raises(layoutlib.LayoutError, match="a_638_n132-iout"):
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


def test_variant_table_pairs_each_magic_variant_with_one_process():
    import corners as corners_mod
    table = corners_mod.rc_variants()
    assert {k: v["pair"] for k, v in table.items()} == {
        "hrhc": "ss", "lrlc": "ff", "hrlc": "sf", "lrhc": "fs"}
    assert not set(table) & set(corners_mod.rc_knobs())


def test_variant_runs_pair_from_the_table_and_refuse_a_grid_without_the_pair():
    runs = check_pex_sim.rc_variant_runs("all", SWEEP)
    assert [(r["variant"], r["pair"], r["corner"]["name"]) for r in runs] == [
        ("hrhc", "ss", "ss"), ("lrlc", "ff", "ff"),
        ("hrlc", "sf", "sf"), ("lrhc", "fs", "fs")]
    assert check_pex_sim.rc_variant_runs(None, GRID) == []
    # GRID sweeps typical and ss: hrhc has its pair, the others do not.
    assert [r["variant"] for r in
            check_pex_sim.rc_variant_runs(["hrhc"], GRID)] == ["hrhc"]
    for unpaired in ("lrlc", "hrlc", "lrhc"):
        with pytest.raises(CheckError, match=unpaired):
            check_pex_sim.rc_variant_runs([unpaired], GRID)


def _rc_variant_extract(monkeypatch, seen=None):
    """A fake extraction whose R and C differ per variant, as magic's do."""
    import layoutlib
    numbers = {None: ("12.5", "0.1f"), "hrhc": ("40", "0.2f"),
               "lrlc": ("1", "0.05f"), "hrlc": ("40", "0.05f"),
               "lrhc": ("1", "0.2f")}

    def extracted(work_dir, gds, cell, parasitics, variant=None):
        if seen is not None:
            seen.append(variant)
        p = work_dir / f"{cell}.pex.spice"
        r, c = numbers[variant]
        p.write_text(f".subckt blk a b\nR0 a b {r}\nC0 a b {c}\n.ends\n",
                     encoding="utf-8")
        return p, ""

    monkeypatch.setattr(layoutlib, "run_magic_extract", extracted)


def _rc_variant_block(ws, field, bounds, bench=None):
    _spec(ws, f"top: blk\nsupply: {{vdd: 3.3}}\npex_corners: [ss]\n"
              f"pex_rc_corners: {field}\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        bench or ".lib x {corner}\n.temp {temp_c}\nv1 a 0 {vdd}\n"
        ".include {extracted}\n.end\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        json.dumps({"measures": bounds}), encoding="utf-8")


def test_variant_extracts_its_own_netlist_and_the_finding_names_it(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    import statelib
    ws = unlaunchable_eda
    seen = []
    _rc_variant_extract(monkeypatch, seen)
    _rc_variant_block(ws, "[hrhc]", {"x": {"min": 1}})
    decks = {}

    def sim(bench, cwd, timeout, failed_measures):
        decks[bench.name] = bench.read_text(encoding="utf-8")
        return "x = 0.5\n" if ".hrhc.spice" in decks[bench.name] else "x = 2\n"
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert seen == [None, "hrhc"]
    assert set(decks) == {"blk_pex_tb__tt.cir", "blk_pex_tb__ss.cir",
                          "blk_pex_tb__ss__hrhc.cir"}
    assert ".temp 125" in decks["blk_pex_tb__ss__hrhc.cir"]
    [v] = out["violations"]
    assert v["kind"] == "measure_out_of_bounds"
    assert set(v["refs"]) == {"x", "ss", "hrhc"}
    assert "x @ ss / hrhc = 0.5" in v["msg"]
    [run] = out["rc_variants"]
    assert (run["variant"], run["corner"], run["pair"]) == ("hrhc", "ss", "ss")
    assert run["measured"] == {"x": 0.5}
    assert run["netlist"] == "log/pex_sim/blk.pex.ordered.hrhc.spice"
    netlist = ws / run["netlist"]
    assert "R0 a b 40" in netlist.read_text(encoding="utf-8")
    assert run["sha256"] == statelib.hash_artifact(netlist, "text_eol")
    # the nominal netlist is still the nominal extraction
    assert "R0 a b 12.5" in (ws / "log" / "pex_sim" / "blk.pex.ordered.spice"
                             ).read_text(encoding="utf-8")


def test_no_variant_asked_means_no_variant_extraction_and_no_report_field(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    seen = []
    _rc_variant_extract(monkeypatch, seen)
    _rc_variant_block(ws, "[hrhc]", {"x": {"min": 1}})
    spec = ws / "spec" / "spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8").replace(
        "pex_rc_corners: [hrhc]\n", ""), encoding="utf-8")
    monkeypatch.setattr(layoutlib, "run_ngspice",
                        lambda bench, cwd, timeout, failed_measures: "x = 2\n")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    assert seen == [None]
    assert "rc_variants" not in out


def test_variant_bench_that_hardcodes_the_netlist_is_not_corner_aware(
        unlaunchable_eda, tmp_path, monkeypatch):
    ws = unlaunchable_eda
    _rc_variant_extract(monkeypatch)
    _rc_variant_block(ws, "[hrhc]", {"x": {"min": 1}},
                      bench=".lib x {corner}\n.temp {temp_c}\nv1 a 0 {vdd}\n"
                            ".include blk.pex.spice\n.end\n")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert {v["kind"] for v in out["violations"]} == {"pex_bench_not_corner_aware"}
    assert any("extracted" in v["refs"] for v in out["violations"])


def test_spec_asking_a_variant_its_sweep_lacks_is_refused_before_extracting_it(
        unlaunchable_eda, tmp_path, monkeypatch, capsys):
    ws = unlaunchable_eda
    _rc_variant_block(ws, "[hrlc]", {"x": {"min": 1}})
    spec = ws / "spec" / "spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8").replace(
                        "pex_corners: [ss]", "pex_corners: all")
                    + "corners: {grid: {process: [typical, ss, ff], "
                      "temp_c: [-40, 25, 125], supply_pct: [-10, 0, 10]}}\n",
                    encoding="utf-8")
    seen = []
    _rc_variant_extract(monkeypatch, seen)
    code, out = run_err(["--workspace", str(ws)], capsys)
    assert code == 2, out
    assert "hrlc needs sf" in out["error"]
    # The nominal extraction comes first, as it always has; the variant is not run.
    assert seen == [None]


def test_view_is_stale_when_its_file_or_its_pair_changes(tmp_path):
    import statelib
    view = tmp_path / "log" / "b.hrhc.spice"
    view.parent.mkdir()
    view.write_text("R0 a b 40\n", encoding="utf-8")
    facts = {"rc_variants": [{
        "variant": "hrhc", "pair": "ss", "corner": "ss",
        "netlist": "log/b.hrhc.spice",
        "sha256": statelib.hash_artifact(view, "text_eol")}]}
    views = statelib.views_of(facts)
    assert views == {"hrhc": {"path": "log/b.hrhc.spice", "pair": "ss",
                              "sha256": facts["rc_variants"][0]["sha256"]}}
    assert statelib.views_of({}) == {}
    entry = {"last": {"inputs": {"a": "1"}, "views": views}}
    assert statelib.view_changes(entry, tmp_path) == []
    fresh = statelib.gate_freshness(entry, {"a": "1"}, "pex_sim", 0,
                                    statelib.view_changes(entry, tmp_path))
    assert fresh["fresh"] is True

    view.write_text("R0 a b 41\n", encoding="utf-8")
    changed = statelib.view_changes(entry, tmp_path)
    assert changed == ["view:hrhc"]
    verdict = statelib.gate_freshness(entry, {"a": "1"}, "pex_sim", 0, changed)
    assert verdict["fresh"] is False
    assert verdict["changed_inputs"] == ["view:hrhc"]

    # a clean checkout has no log/: the gate's inputs vouch for it
    view.unlink()
    assert statelib.view_changes(entry, tmp_path) == []

    # the table moved the pairing under the record
    moved = {"last": {"views": {"hrhc": dict(views["hrhc"], pair="ff")}}}
    assert statelib.view_changes(moved, tmp_path) == ["view:hrhc"]


def test_record_gate_keeps_the_views_a_result_carries(tmp_path):
    import state
    st = state.State.init(tmp_path / "ws", "ade", "blk")
    st.record_gate("pex_sim", {"status": "pass", "facts": {"rc_variants": [{
        "variant": "lrlc", "pair": "ff", "corner": "ff",
        "netlist": "log/b.lrlc.spice", "sha256": "text_eol:abc"}]}})
    assert st.data["gates"]["pex_sim"]["last"]["views"] == {"lrlc": {
        "path": "log/b.lrlc.spice", "sha256": "text_eol:abc", "pair": "ff"}}


@pytest.mark.slow
def test_unknown_variant_is_refused_since_magic_only_complains(tmp_path):
    """magic carries on at nominal after an unknown style: the .ext must say."""
    import layoutlib
    ws, gds, top = _unlabelled_mirror(tmp_path)
    with pytest.raises(layoutlib.LayoutError, match="hxhx"):
        layoutlib.run_magic_extract(ws / "log", gds, top, parasitics=True,
                                    variant="hxhx")


# #254: the 50um mirror line, first posted on #160 and re-measured on the
# two-tap row (#118). hrhc and lrlc are the PDK's own variants, against the
# C and R the knobs only approximate.
@pytest.mark.slow
def test_fifty_um_line_at_the_real_variants_matches_the_posted_numbers(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    gen = ws / "layout" / "gen_mirror.py"
    gen.write_text(gen.read_text(encoding="utf-8").replace(
        '    d = iout_pad\n    labels.append(("iout", (d[0] + d[2]) / 2, '
        '(d[1] + d[3]) / 2, L1LBL))\n',
        '    d = iout_pad\n    y = (d[1] + d[3]) / 2\n'
        '    layoutlib.rect(top, d[0], y - 0.115, d[2] + 50.0, y + 0.115, L1)\n'
        '    layoutlib.rect(top, d[2] + 50.0, y - 0.5, d[2] + 51.0, y + 0.5, L1)\n'
        '    labels.append(("iout", d[2] + 50.5, y, L1LBL))\n'), encoding="utf-8")
    assert "d[2] + 50.0" in gen.read_text(encoding="utf-8")
    _spec(ws, (ws / "spec" / "spec.yaml").read_text(encoding="utf-8")
          + "pex_corners: all\npex_rc_corners: all\n")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    by = {r["variant"]: r for r in out["rc_variants"]}
    assert {k: v["corner"] for k, v in by.items()} == {
        "hrhc": "ss", "lrlc": "ff", "hrlc": "sf", "lrhc": "fs"}
    # lrlc and lrhc were 521.0 / 487.6 MHz until #298: at low R extresist
    # leaves iref_node whole, and ext2spice wrote it no C to vss at all;
    # it now carries its .ext 1.70 / 1.95 fF, as the split hr variants do
    posted = {"hrhc": 425.7e6, "lrlc": 519.9e6, "hrlc": 451.1e6, "lrhc": 486.5e6}
    for name, want in posted.items():
        assert by[name]["measured"]["f3db"] == pytest.approx(want, abs=0.1e6), name
    assert out["measured_by_corner"]["ss"]["f3db"] == pytest.approx(434.2e6, abs=0.1e6)
    assert out["measured_by_corner"]["ff"]["f3db"] == pytest.approx(508.3e6, abs=0.1e6)


@pytest.mark.slow
def test_line_failing_only_at_hrhc_names_the_variant(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    _plant(ws, "plant_pex_line_fails_at_hrhc.py")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    [v] = out["violations"]
    assert v["kind"] == "measure_out_of_bounds"
    assert set(v["refs"]) == {"f3db", "ss", "hrhc"}
    assert "f3db @ ss / hrhc" in v["msg"]
    assert all(m["f3db"] >= 5e8 for m in out["measured_by_corner"].values())
    assert {r["knob"] for r in out["rc_knobs"]} == {"r_high"}


# A plain line and a via chain with nearly the same nominal pole: the plain line
# passes hrhc and the chain fails it, so the vias decide, and r_high misses it.
@pytest.mark.slow
def test_via_chain_that_r_high_misses_fails_at_hrhc_and_a_plain_line_does_not(
        tmp_path):
    chain = make_ws(tmp_path / "chain", "mirror")
    _plant(chain, "plant_pex_via_chain_fails_at_hrhc.py")
    code, out = run_json(["--workspace", str(chain)], tmp_path)
    assert code == 1, out
    [v] = out["violations"]
    assert set(v["refs"]) == {"f3db", "ss", "hrhc"}
    [knob] = out["rc_knobs"]
    assert (knob["knob"], knob["corner"]) == ("r_high", "ss")
    assert knob["measured"]["f3db"] >= 5e8
    chain_ss = out["measured_by_corner"]["ss"]["f3db"]

    line = make_ws(tmp_path / "line", "mirror")
    _plant(line, "plant_pex_line_fails_at_c_high.py")
    spec_path = line / "spec" / "spec.yaml"
    spec_path.write_text(spec_path.read_text(encoding="utf-8").replace(
        "pex_rc_knobs: [c_high]\n", "pex_corners: [ss]\npex_rc_corners: [hrhc]\n"),
        encoding="utf-8")
    code, out = run_json(["--workspace", str(line)], tmp_path)
    assert code == 0, out["violations"]
    line_ss = out["measured_by_corner"]["ss"]["f3db"]
    assert abs(line_ss - chain_ss) < 2e6
    [run] = out["rc_variants"]
    assert run["variant"] == "hrhc" and run["measured"]["f3db"] >= 5e8


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


# ---- symmetric_nets (#121) ---------------------------------------------------
def _two_wires(len_a_um: float, len_b_um: float, seg_um: float = 10.0) -> str:
    """A hand-built extraction of two parallel metal1 wires, each from its
    labelled pin node to a gate at its far end, cut into seg_um segments
    with extresist-style sub-nodes: every segment adds R_SEG ohm and splits
    its ground C between its ends; the wires couple C_CPL per um over their
    common length, and wire a alone runs past clk for 5 um."""
    lines = [".subckt pair a b clk vss"]
    k = 0

    def wire(net, length):
        nonlocal k
        n_seg = round(length / seg_um)
        nodes = [net] + [f"{net}.n{i}" for i in range(1, n_seg + 1)]
        for i in range(n_seg):
            k += 1
            lines.append(f"R{k} {nodes[i]} {nodes[i + 1]} {R_SEG}")
            for end in (nodes[i], nodes[i + 1]):
                k += 1
                lines.append(f"C{k} {end} vss {C_GND * seg_um / 2}f")
        lines.append(f"X{net} vss {nodes[-1]} vss vss nfet_03v3 w=1u l=1u")
        return nodes

    na, nb = wire("a", len_a_um), wire("b", len_b_um)
    for x, y in zip(na[1:], nb[1:]):
        k += 1
        lines.append(f"C{k} {x} {y} {C_CPL * seg_um}f")
    lines.append(f"C{k + 1} a.n1 clk {C_CLK * 5}f")
    lines.append(".ends")
    return "\n".join(lines) + "\n"


R_SEG, C_GND, C_CPL, C_CLK = 1.5, 0.04, 0.02, 0.01  # ohm, fF/um, fF/um, fF/um


def test_net_parasitics_known_answer_two_wires():
    # a: 60um, b: 20um -> the coupling runs the 20um they share
    par = check_pex_sim.net_parasitics(_two_wires(60, 20), ["a", "b", "clk"])
    ca = C_GND * 60 + C_CPL * 20 + C_CLK * 5
    cb = C_GND * 20 + C_CPL * 20
    assert par["a"]["c_ff"] == pytest.approx(ca, rel=1e-6)
    assert par["b"]["c_ff"] == pytest.approx(cb, rel=1e-6)
    assert par["a"]["c_ff"] / par["b"]["c_ff"] == pytest.approx(2.85 / 1.2)
    assert par["a"]["r_ohm"] == pytest.approx(6 * R_SEG)
    assert par["b"]["r_ohm"] == pytest.approx(2 * R_SEG)
    assert par["a"]["c_to"]["b"] == pytest.approx(C_CPL * 20)
    assert par["a"]["c_to"]["clk"] == pytest.approx(C_CLK * 5)
    assert "clk" not in par["b"]["c_to"]


def test_net_parasitics_solves_parallel_paths_not_the_sum():
    text = (".subckt t a vss\nR1 a a.n1 10\nR2 a a.n1 10\nR3 a.n1 a.n2 4\n"
            "R4 a.n1 a.n2 12\nM1 vss a.n2 vss vss nfet_03v3 w=1u l=1u\n"
            "C1 a vss 1f\n.ends\n")
    par = check_pex_sim.net_parasitics(text, ["a"])
    assert par["a"]["r_ohm"] == pytest.approx(5 + 3)
    assert par["a"]["c_ff"] == pytest.approx(1.0)


def test_net_parasitics_unsplit_net_has_zero_r():
    text = ".subckt t a vss\nX1 vss a vss vss nfet_03v3 w=1u l=1u\nC1 a vss 2f\n.ends\n"
    assert check_pex_sim.net_parasitics(text, ["a"])["a"]["r_ohm"] == 0.0


def test_net_parasitics_refuses_a_device_cut_off_from_the_label():
    text = (".subckt t a vss\nR1 a a.n1 10\nR2 a.n2 a.n3 10\n"
            "X1 vss a.n3 vss vss nfet_03v3 w=1u l=1u\n.ends\n")
    with pytest.raises(CheckError):
        check_pex_sim.net_parasitics(text, ["a"])


PAIR_REF = (".subckt pair a b clk vss\nX1 vss a vss vss nfet_03v3 w=1u l=1u\n"
            "X2 vss b vss vss nfet_03v3 w=1u l=1u\nC1 a clk 1f\n.ends\n")


def _rows(**kw):
    row = {"nets": ["a", "b"], "max_dc_pct": 5.0, "max_dr_pct": 5.0,
           "min_dc_ff": 0.0, "min_dr_ohm": 0.0, "aggressors": []}
    return [{**row, **kw}]


def test_symmetric_pair_passes_and_reports_both_values():
    found, rep = check_pex_sim.symmetry_findings(
        _rows(aggressors=["clk"]), PAIR_REF, _two_wires(20, 20).replace(
            ".ends", f"C99 b.n1 clk {C_CLK * 5}f\n.ends"), "pair", "g",
        "spec/spec.yaml")
    assert found == []
    assert rep[0]["a"]["c_ff"] == rep[0]["b"]["c_ff"]
    assert [c["quantity"] for c in rep[0]["checked"]] == ["C", "R", "C to clk"]


def test_asymmetric_pair_is_symmetry_violated_with_both_values():
    found, rep = check_pex_sim.symmetry_findings(
        _rows(), PAIR_REF, _two_wires(60, 20), "pair", "g", "spec/spec.yaml")
    kinds = {(v["kind"], tuple(v["refs"])) for v in found}
    assert kinds == {("symmetry_violated", ("a", "b"))}
    msgs = " ".join(v["msg"] for v in found)
    assert "2.85 fF" in msgs and "1.2 fF" in msgs   # C, both values
    assert "9 ohm" in msgs and "3 ohm" in msgs      # R, both values
    assert "layout-fixer" in msgs


def test_aggressor_coupling_on_one_side_only_is_a_finding():
    found, _ = check_pex_sim.symmetry_findings(
        _rows(aggressors=["clk"], max_dc_pct=50.0, max_dr_pct=None),
        PAIR_REF, _two_wires(20, 20), "pair", "g", "spec/spec.yaml")
    assert [v["refs"] for v in found] == [["a", "b", "clk"]]


def test_floor_spares_a_large_percentage_of_a_tiny_difference():
    found, _ = check_pex_sim.symmetry_findings(
        _rows(min_dc_ff=5.0, min_dr_ohm=10.0), PAIR_REF, _two_wires(60, 20),
        "pair", "g", "spec/spec.yaml")
    assert found == []


def test_a_pair_net_the_reference_lacks_is_a_refusal():
    with pytest.raises(CheckError):
        check_pex_sim.symmetry_findings(
            _rows(nets=["a", "nope"]), PAIR_REF, _two_wires(20, 20), "pair",
            "g", "spec/spec.yaml")


def test_a_pair_net_the_extraction_never_names_is_unlabelled():
    ext = _two_wires(20, 20).replace(" b ", " w_1_2# ").replace(" b.", " w_1_2#.")
    found, _ = check_pex_sim.symmetry_findings(
        _rows(), PAIR_REF, ext, "pair", "g", "spec/spec.yaml")
    assert [(v["kind"], v["refs"]) for v in found] == [
        ("symmetry_net_unlabelled", ["b"])]


# ---- the second extractor (kpex 2.5D, issue #116) ----------------------------
XC_REF = (".subckt blk a b vdd vss\nM1 a b vss vss nfet_03v3 L=1u W=1u\n"
          "M2 a b vdd vdd pfet_03v3 L=1u W=1u\n.ends blk\n")
XC_EXT = ('substrate "vss" 1 0 0 0 m1 0 0 100 10\n'
          'node "vdd" 1 10 0 0 m1 5000 300 0 0\n'
          'node "a" 1 10 0 0 m1 0 0 0 0\n'
          'node "b" 1 10 0 0 m1 0 0 0 0\n')
XC_BENCH = ("xdut na nb nvdd 0 blk\n.control\ntran 1n 1u\n"
            "meas tran x find v(na) at=1u\nprint v(xdut.b)\n.endc\n")
XC_MAGIC = (".subckt blk a b vdd vss\nM1 a b vss vss nfet_03v3 L=1u W=1u\n"
            "M2 a b vdd vdd pfet_03v3 L=1u W=1u\nC0 a vss 1f\nC1 b vss 1f\n"
            "C2 a b 0.2f\nC3 vdd vss 3f\n.ends\n")


def _xc_kpex(kpex_caps):
    def fake(work_dir, gds_path, topcell, timeout=None):
        work_dir = Path(work_dir)
        out = work_dir / "out" / f"{topcell}__{topcell}"
        out.mkdir(parents=True)
        netlist = out / f"{topcell}_k25d_pex_netlist.spice"
        netlist.write_text(
            f".SUBCKT {topcell} a b vss\n"
            "M$1 a b vss vss nfet_03v3 L=1U W=1U\n"
            "M$2 a b vdd vdd pfet_03v3 L=1U W=1U\n" + kpex_caps
            + f".ENDS {topcell}\n", encoding="utf-8")
        log = work_dir / "kpex.log"
        log.write_text("", encoding="utf-8")
        return netlist, log
    return fake


def _xc_run(tmp_path, monkeypatch, kpex_caps):
    ws = tmp_path / "ws"
    ws.mkdir()
    ext = tmp_path / "blk.ext"
    ext.write_text(XC_EXT, encoding="utf-8")
    monkeypatch.setattr(check_pex_sim.kpexlib, "run_kpex", _xc_kpex(kpex_caps))
    return REAL_CROSSCHECK(ws, tmp_path / "blk.gds", "blk", "layout/blk.gds",
                           XC_REF, ["a", "b", "vdd", "vss"], XC_BENCH,
                           XC_MAGIC, ext)


def test_probed_dut_nets_reads_inner_probes_and_bound_pins():
    assert check_pex_sim.probed_dut_nets(XC_BENCH, "blk",
                                         ["a", "b", "vdd", "vss"]) == {"a", "b"}


def test_crosscheck_agreeing_extractors_is_no_finding(tmp_path, monkeypatch):
    found, report = _xc_run(tmp_path, monkeypatch,
                            "Cext_1 a b 0.21f\nCext_2 VSUBS a 1.05f\n"
                            "Cext_3 VSUBS b 1.0f\nCext_4 VSUBS vdd 9f\n")
    assert found == []
    assert {r["net"] for r in report["nets"]} == {"a", "b"}
    assert report["couplings"][0]["nets"] == ["a", "b"]
    # the well's net and, with a well in the block, the substrate
    assert {r["net"] for r in report["not_compared"]} == {"vdd", "vss"}
    assert report["substrate"] == "vss" and report["probed"] == ["a", "b"]


def test_crosscheck_a_net_past_tolerance_is_a_finding_with_both_values(
        tmp_path, monkeypatch):
    found, _ = _xc_run(tmp_path, monkeypatch,
                       "Cext_1 a b 0.21f\nCext_2 VSUBS a 1.5f\n"
                       "Cext_3 VSUBS b 1.0f\n")
    assert [(v["kind"], v["refs"]) for v in found] == [
        ("pex_extractors_disagree", ["a"])]
    assert found[0]["magic_ff"] == pytest.approx(1.2)
    assert found[0]["kpex_ff"] == pytest.approx(1.71)


def test_crosscheck_kpex_failing_is_a_refusal(tmp_path, monkeypatch):
    ext = tmp_path / "blk.ext"
    ext.write_text(XC_EXT, encoding="utf-8")

    def broken(*a, **k):
        raise check_pex_sim.kpexlib.KpexError("no netlist")
    monkeypatch.setattr(check_pex_sim.kpexlib, "run_kpex", broken)
    with pytest.raises(CheckError, match="second extractor"):
        REAL_CROSSCHECK(tmp_path, tmp_path / "blk.gds", "blk", "layout/blk.gds",
                        XC_REF, ["a", "b", "vdd", "vss"], XC_BENCH, XC_MAGIC,
                        ext)


@pytest.mark.slow
def test_a_dropped_label_is_caught_by_kpex_with_lost_couplings_off(
        tmp_path, monkeypatch):
    """The plant: the comparator's tail net loses the label
    name_unlabelled_nets gives it, and lost_couplings is off, so only the
    cross-check is left to see magic's extresist drop the couplings onto
    the unnamed net: inn and inp come out ~30% below kpex."""
    import layoutlib
    orig = layoutlib.name_unlabelled_nets

    def dropped(ext_text):
        cmds, out, i = orig(ext_text), [], 0
        while i < len(cmds):
            if (cmds[i].startswith("box values") and i + 1 < len(cmds)
                    and " a_n1350_n934 " in cmds[i + 1] + " "):
                i += 2
                continue
            out.append(cmds[i])
            i += 1
        assert len(out) < len(cmds), "the tail net's label was not found"
        return out
    monkeypatch.setattr(layoutlib, "name_unlabelled_nets", dropped)
    monkeypatch.setattr(layoutlib, "lost_couplings", lambda *a, **k: [])
    ws = make_ws(tmp_path, "comparator")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    bad = {r for v in out["violations"]
           if v["kind"] == "pex_extractors_disagree" for r in v["refs"]}
    assert {"inn", "inp"} <= bad


def test_kpex_loses_each_poly_bodys_model_c_before_the_comparison():
    # kpex reads a poly resistor body as a wire with the tech's full C;
    # magic's netlist has only the residual, the model the rest - so the
    # model's cj x r_w x r_l comes off kpex, half at each end
    pk = {"n1": {"c_ff": 40.0, "c_to": {"vss": 30.0}},
          "mb1": {"c_ff": 50.0, "c_to": {"vss": 35.0}},
          "vss": {"c_ff": 100.0, "c_to": {"n1": 30.0, "mb1": 35.0}}}
    bodies = [{"device": "X29", "ends": ["n1", "mb1"], "body": "vss.t0",
               "model_c_ff": 25.59}]
    taken = check_pex_sim.subtract_model_c(pk, bodies)
    assert taken == {"mb1": 12.795, "n1": 12.795, "vss": 25.59}
    assert pk["n1"]["c_ff"] == pytest.approx(27.205)
    assert pk["n1"]["c_to"]["vss"] == pytest.approx(17.205)
    assert pk["vss"]["c_ff"] == pytest.approx(74.41)
    assert pk["vss"]["c_to"]["mb1"] == pytest.approx(22.205)


def test_fill_drc_findings_are_errors_and_density_only_reported(
        tmp_path, monkeypatch):
    """pex_fill (#136): the filled tile's DRC (all decks but antenna and
    density) is a fill_drc error per rule; the whole-die density deck is
    reported, never a finding. Real tools stubbed: what fill_view does with
    their answers."""
    import filllib
    import layoutlib

    ws = tmp_path / "ws"
    work = ws / "log" / "pex_sim"
    work.mkdir(parents=True)
    gds = ws / "layout" / "blk.gds"
    gds.parent.mkdir()
    gds.write_bytes(b"")
    pex = (".subckt blk a b vss\nm1 a b vss vss n\nC1 a f1 2f\n"
           "C2 f1 b 2f\nC3 f2 vss 1f\n.ends\n")

    def drc(filled, top, rdb, timeout, decks):
        rdb.write_text(decks, encoding="utf-8")

    def parse(rdb):
        if rdb.read_text() == filllib.FILL_DRC_DECKS:
            return [{"category": "M1.2a", "description": "M1 space"}] * 2
        return [{"category": "M1.4", "description": "Metal1 coverage >30%"}]

    def extract(fdir, xgds, top, parasitics, timeout):
        raw = fdir / "blk.spice"
        raw.write_text(pex, encoding="utf-8")
        return raw, ""

    filled = ws / "log" / "pex_sim" / "fill" / "blk.filled.gds"
    monkeypatch.setattr(filllib, "shuttle_fill", lambda *a: filled)
    monkeypatch.setattr(filllib, "fill_summary", lambda *a: {})
    monkeypatch.setattr(filllib, "extraction_copy", lambda *a: 2)
    monkeypatch.setattr(layoutlib, "run_klayout_drc", drc)
    monkeypatch.setattr(layoutlib, "parse_drc_rdb", parse)
    monkeypatch.setattr(layoutlib, "run_magic_extract", extract)
    ref = ".subckt blk a b vss\nm1 a b vss vss n\n.ends\n"
    out = check_pex_sim.fill_view(
        ws, gds, "blk", "layout/blk.gds", work, ref, ["a", "b", "vss"],
        ".subckt blk a b vss\nm1 a b vss vss n\n.ends\n", "", [],
        ["floating", "grounded"])
    errors = [v for v in out["violations"] if v["kind"] == "fill_drc"]
    assert len(errors) == 1 and errors[0]["severity"] == "error"
    assert errors[0]["refs"] == ["M1.2a", "fill"] and "x2" in errors[0]["msg"]
    assert not [v for v in out["violations"] if "M1.4" in v["refs"]]
    facts = out["facts"]
    assert facts["drc"]["findings"] == 2 and facts["fill_nets"] == 2
    assert facts["density_deck"] == ["M1.4: Metal1 coverage >30%"]
    assert [v["mode"] for v in facts["views"]] == ["floating", "grounded"]
    floating = (ws / facts["views"][0]["netlist"]).read_text()
    assert "f1" not in floating and "Cfill0 a b 1e-15" in floating
    grounded = (ws / facts["views"][1]["netlist"]).read_text()
    assert "C1 a vss 2f" in grounded and "f2" not in grounded


@pytest.mark.slow
def test_clean_ota_post_fill_view_passes(tmp_path):
    """#136 on the real tools: corpus ota asks for pex_fill: all, so the
    tile is filled with the PDK's fill_all.rb, DRC-clean after fill, and
    both fill views are benched at every scored corner and pass."""
    ws = make_ws(tmp_path, "ota")
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 0, out
    fill = out["fill"]
    assert fill["drc"]["findings"] == 0 and fill["fill_nets"] > 0
    assert all(d["density_filled"] > d["density_drawn"]
               for d in fill["density"].values())
    assert [v["mode"] for v in fill["views"]] == ["floating", "grounded"]
    for view in fill["views"]:
        assert set(view["measured_by_corner"]) == set(out["corners"])
    # fill adds coupling to the sensitive nets, never removes it
    for net in ("out", "d2", "inp", "inn"):
        c = fill["coupling"][net]
        assert c["c_ff_filled"] >= c["c_ff"]


# ---- the ua pad model (#112) ------------------------------------------------

def _pad_ws(ws, monkeypatch, bench_line="xd a b blk", bounds=None):
    _rc_extract(monkeypatch)
    _spec(ws, "top: blk\nsupply: {vdd: 3.3}\npex_corners: [tt]\n"
          "ua_pins: [b]\n")
    (ws / "netlist" / "blk.cir").write_text(
        ".subckt blk a b\nR1 a b 1k\n.ends\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.cir").write_text(
        f".lib x {{corner}}\n.temp {{temp_c}}\nv1 a 0 {{vdd}}\n{bench_line}\n"
        ".end\n", encoding="utf-8")
    (ws / "layout_ref" / "blk_pex_tb.bounds.json").write_text(
        json.dumps({"measures": bounds or {"x": {"min": 1}}}),
        encoding="utf-8")


def test_a_ua_pin_spec_runs_the_pex_bench_bare_and_through_the_pad(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _pad_ws(ws, monkeypatch, bounds={"x": {"min": 3}})
    decks = {}

    def sim(bench, cwd, timeout, failed_measures):
        decks[bench.name] = bench.read_text(encoding="utf-8")
        return "x = 2.0\n"
    monkeypatch.setattr(layoutlib, "run_ngspice", sim)
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert "tt_ua_pad" not in decks["blk_pex_tb__tt.cir"]
    pad = decks["blk_pex_tb__tt__pad.cir"]
    assert "xd a b__ua_die_xd_b blk" in pad
    assert "xuapad_xd_b b__ua_die_xd_b b tt_ua_pad" in pad
    assert "vdvdd dvdd 0 3.3" in pad
    assert out["pad_runs"] == [{"run": "tt", "corner": "tt",
                                "measured": {"x": 2.0}}]
    assert sorted(v["msg"] for v in out["violations"]) == [
        "x @ tt / pad = 2, outside bound [3, None]",
        "x @ tt = 2, outside bound [3, None]"]


def test_a_pex_bench_the_pad_cannot_go_in_is_refused(
        unlaunchable_eda, tmp_path, monkeypatch):
    import layoutlib
    ws = unlaunchable_eda
    _pad_ws(ws, monkeypatch, bench_line="r1 a b 1k")
    monkeypatch.setattr(layoutlib, "run_ngspice",
                        lambda *a, **k: pytest.fail("no bench may run"))
    code, out = run_json(["--workspace", str(ws)], tmp_path)
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == ["ua_pad_not_inserted"]

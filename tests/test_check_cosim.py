"""engine/scripts/check_cosim.py: the cosim gate (docs/design.md 1.5's msde
cosim row, "### M10.").

CLAUDE.md's rule for this gate specifically: "ngspice exits 0 on many
failures, so parse the measures... Prove with tests that a non-converging
run, a bench where the digital side never toggled, or a missing measure
fails cosim." The three fast tests below exercise exactly those three
shapes against `evaluate_measures`/`scan_nonconvergence` directly - no
workspace, no subprocess, no real ngspice, so they are not `slow` and run
in `tests/check.sh`'s own budget. The slow tests at the bottom run the real
corpus/msde/ring_osc_div bench end to end (needs the eda image - cocotb,
Icarus, ngspice's shared library)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
CORPUS_RUNG = REPO / "corpus" / "msde" / "ring_osc_div"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402

import check_cosim  # noqa: E402

BOUNDS = {"divided_freq_hz": {"min": 10_000_000, "max": 20_000_000}}


# --------------------------------------------------------------------- fast
def test_missing_measures_file_fails():
    violations = check_cosim.evaluate_measures(None, BOUNDS)
    assert violations
    assert violations[0]["kind"] == "measures_missing"


def test_digital_side_never_toggled_fails():
    measures = {"divided_freq_hz": None, "digital_toggles": 0,
                "edge_times_ns": []}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "digital_side_never_toggled" in kinds


def test_missing_measure_key_fails():
    measures = {"digital_toggles": 8, "edge_times_ns": list(range(8))}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "measure_missing" in kinds


def test_measure_out_of_bounds_fails():
    measures = {"divided_freq_hz": 1.0, "digital_toggles": 8,
                "edge_times_ns": list(range(8))}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "measure_out_of_bounds" in kinds


def test_clean_measures_pass():
    measures = {"divided_freq_hz": 15_151_515.0, "digital_toggles": 8,
                "edge_times_ns": list(range(8)),
                "spice_time_reached_ns": 700.0}
    assert check_cosim.evaluate_measures(measures, BOUNDS) == []


def test_spice_time_missing_fails():
    # digital_toggles/edge_times_ns claim a toggled digital side, but
    # nothing records how far the analog bridge itself got - exactly what a
    # Python-only toggle (a counter with no real analog block behind it)
    # would leave behind.
    measures = {"divided_freq_hz": 15_151_515.0, "digital_toggles": 8,
                "edge_times_ns": list(range(8))}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "spice_time_missing" in kinds


def test_spice_time_behind_last_edge_fails():
    # the analog bridge only reached t=3ns, but the digital side claims an
    # edge at t=7ns - the analog side never actually got there.
    measures = {"divided_freq_hz": 15_151_515.0, "digital_toggles": 8,
                "edge_times_ns": list(range(8)), "spice_time_reached_ns": 3.0}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "spice_time_behind_last_edge" in kinds


def test_digital_toggles_unbacked_by_edges_fails():
    # digital_toggles is a bare literal - "8" with no matching edge times
    # recorded behind it.
    measures = {"divided_freq_hz": 15_151_515.0, "digital_toggles": 8,
                "edge_times_ns": [], "spice_time_reached_ns": 700.0}
    violations = check_cosim.evaluate_measures(measures, BOUNDS)
    kinds = {v["kind"] for v in violations}
    assert "digital_toggles_unbacked" in kinds


def test_scan_nonconvergence_catches_timestep_collapse():
    log = ('doAnalyses: TRAN:  Timestep too small; time = 5e-08, '
          'timestep = 0: trouble with node "x1.mid"')
    hits = check_cosim.scan_nonconvergence(log)
    assert hits, "a real ngspice non-convergence signature must be caught"


def test_scan_nonconvergence_clean_log_finds_nothing():
    log = "TESTS=1 PASS=1 FAIL=0\n"
    assert check_cosim.scan_nonconvergence(log) == []


def test_scan_nonconvergence_gmin_failure_counts_only_unrecovered():
    recovered = ("Warning: Dynamic gmin stepping failed\n"
                 "Note: Starting true gmin stepping\n"
                 "Note: True gmin stepping completed\n")
    assert check_cosim.scan_nonconvergence(recovered) == []
    assert check_cosim.scan_nonconvergence(
        "Warning: Dynamic gmin stepping failed\n")


def test_non_convergence_signature_fails_even_with_good_measures():
    # the brief's exact case: a run that logged a real non-convergence
    # signature must fail even though the measures it happened to record
    # look clean - "ngspice exits 0 on many failures" means the measures
    # alone are not the whole story either.
    log = 'doAnalyses: TRAN:  Timestep too small; trouble with node "x1.mid"'
    measures = {"divided_freq_hz": 15_151_515.0, "digital_toggles": 8,
                "edge_times_ns": list(range(8)),
                "spice_time_reached_ns": 700.0}
    assert check_cosim.scan_nonconvergence(log)
    assert check_cosim.evaluate_measures(measures, BOUNDS) == []
    # check_cosim.run() is what combines the two - see the slow tests below
    # for that combination exercised end to end.


def test_scan_ic_ignored_catches_non_existent_node():
    # docs/spikes/dcosim.md's own bug: an .ic naming a node without the
    # wrapper netlist's "x1." instance prefix - ngspice does not error, it
    # logs this and silently drops the initial condition.
    log = "Warning: IC on non-existent node - mid, ignored\n"
    hits = check_cosim.scan_ic_ignored(log)
    assert hits == ["mid"]


def test_scan_ic_ignored_clean_log_finds_nothing():
    log = "TESTS=1 PASS=1 FAIL=0\n"
    assert check_cosim.scan_ic_ignored(log) == []


def test_run_detects_ngspice_non_convergence_via_monkeypatched_run_cocotb(
        tmp_path, monkeypatch, capsys):
    # Nothing drove run()'s own combination of a monkeypatched cocotb result
    # with the raw sim log scan before this - the three fast tests above
    # exercise evaluate_measures/scan_nonconvergence directly, never run()
    # itself, so a bug in how run() wires the two together (e.g. reading the
    # wrong sim.log path, or never scanning it at all) had no test to catch
    # it. cocotblib.run_cocotb is monkeypatched to skip cocotb/Icarus/ngspice
    # entirely: it writes a sim.log carrying a real non-convergence
    # signature alongside a results.xml AND a cosim_measures.json that both
    # look clean, so the only way this test can fail is if run() itself
    # never reads the sim log.
    ws = tmp_path / "ws"
    tb = ws / "tb"
    tb.mkdir(parents=True)
    (ws / "interface.yaml").write_text("signals: []\n", encoding="utf-8")
    (tb / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (tb / "test_top.py").write_text("", encoding="utf-8")
    (tb / "ideal.sp").write_text("* stub netlist, never simulated\n",
                                 encoding="utf-8")
    (tb / "bounds.json").write_text(
        json.dumps({"m": {"min": 0, "max": 1}}), encoding="utf-8")
    (tb / "cosim_bench.json").write_text(json.dumps({
        "top": "top", "bounds": "bounds.json",
        "analog_netlist": "ideal.sp", "analog_kind": "ideal",
    }), encoding="utf-8")

    def fake_run_cocotb(build_dir, test_dir, sources, hdl_toplevel,
                        test_modules_, results_xml, **kwargs):
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "sim.log").write_text(
            'doAnalyses: TRAN:  Timestep too small; time = 5e-08, '
            'timestep = 0: trouble with node "x1.mid"\n', encoding="utf-8")
        results_xml.parent.mkdir(parents=True, exist_ok=True)
        results_xml.write_text(
            '<testsuites><testsuite>'
            '<testcase name="test_top.test_top_case"/>'
            '</testsuite></testsuites>', encoding="utf-8")
        reports_dir = ws / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "cosim_measures.json").write_text(json.dumps({
            "m": 0.5, "digital_toggles": 1, "edge_times_ns": [1.0],
            "spice_time_reached_ns": 2.0,
        }), encoding="utf-8")
        return results_xml

    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb", fake_run_cocotb)

    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert "ngspice_non_convergence" in kinds


# ------------------------------------------------------- the ua pad (#112)
PAD_NETLIST = """* block on a ua pad
.subckt leaf a b
r1 a b 1k
.ends leaf
.subckt blk code_0 vout vdd vss
.param ru=5k
x1 code_0 vout leaf
cp vout vss 1p
.ends blk
"""


def _pad_cosim_ws(tmp_path, netlist=PAD_NETLIST, bench_names_netlist=True,
                  bounds=None):
    ws = tmp_path / "ws"
    tb = ws / "tb"
    tb.mkdir(parents=True)
    (ws / "interface.yaml").write_text("signals: []\nua_pins:\n  vout: 0\n",
                                       encoding="utf-8")
    spec = ws / "analog" / "spec"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text("top: blk\nsupply: {vdd: 3.3}\n",
                                    encoding="utf-8")
    (tb / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (tb / "test_top.py").write_text(
        "SPICE = 'ideal.sp'\n" if bench_names_netlist else "", encoding="utf-8")
    (tb / "ideal.sp").write_text(netlist, encoding="utf-8")
    (tb / "bounds.json").write_text(json.dumps(
        bounds or {"m": {"min": 0, "max": 1}}), encoding="utf-8")
    (tb / "cosim_bench.json").write_text(json.dumps({
        "top": "top", "bounds": "bounds.json",
        "analog_netlist": "ideal.sp", "analog_kind": "ideal",
    }), encoding="utf-8")
    return ws


def _fake_cocotb(ws, values, seen):
    """run_cocotb stand-in: records which tb/ ran and which netlist it saw,
    writes the measures beside that tb/ (as a bench does, HERE.parent)."""
    def fake(build_dir, test_dir, sources, hdl_toplevel, test_modules_,
             results_xml, **kwargs):
        text = (test_dir / "ideal.sp").read_text(encoding="utf-8")
        cond = "pad" if "tt_ua_pad" in text else "no_pad"
        seen.append((cond, test_dir))
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "sim.log").write_text("", encoding="utf-8")
        results_xml.parent.mkdir(parents=True, exist_ok=True)
        results_xml.write_text(
            '<testsuites><testsuite><testcase name="test_top.case"/>'
            '</testsuite></testsuites>', encoding="utf-8")
        rep = test_dir.parent / "reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / "cosim_measures.json").write_text(json.dumps({
            **values[cond], "digital_toggles": 1, "edge_times_ns": [1.0],
            "spice_time_reached_ns": 2.0}), encoding="utf-8")
        return results_xml
    return fake


def _no_toolchain(monkeypatch):
    monkeypatch.setattr(check_cosim, "toolchain_root", lambda: Path("/tc"))
    monkeypatch.setattr(check_cosim.layoutlib, "pdk_root",
                        lambda: Path("/pdk"))


def test_wrap_subckt_pads_puts_the_block_behind_the_pad():
    import padlib
    text, block, ins = padlib.wrap_subckt_pads(
        PAD_NETLIST, ["vout"], padlib.pad_subckt("/pdk", "tt", 3.3))
    assert block == "blk"
    assert ins == [{"instance": "blk", "port": "vout", "node": "vout"}]
    assert ".subckt blk__core code_0 vout vdd vss" in text
    assert ".ends blk__core" in text
    assert ".subckt leaf a b" in text and ".ends leaf" in text
    assert ("xcore code_0 vout__ua_die_vout vdd vss blk__core"
            in text.splitlines())
    assert ("xuapad_vout vout__ua_die_vout vout tt_ua_pad"
            in text.splitlines())
    # the wrapper keeps the block's name and ports: the bench loads it as is
    assert ".subckt blk code_0 vout vdd vss" in text.splitlines()


def test_wrap_subckt_pads_refuses_a_netlist_with_no_block_on_the_pin():
    import padlib
    with pytest.raises(padlib.PadRefused) as exc:
        padlib.wrap_subckt_pads(PAD_NETLIST, ["vref"], "")
    assert exc.value.kind == "ua_pad_not_inserted"
    two = PAD_NETLIST + ".subckt other vout\nr1 vout 0 1k\n.ends other\n"
    with pytest.raises(padlib.PadRefused):
        padlib.wrap_subckt_pads(two, ["vout"], "")


def test_cosim_runs_bare_and_padded(tmp_path, monkeypatch, capsys):
    ws = _pad_cosim_ws(tmp_path)
    seen = []
    _no_toolchain(monkeypatch)
    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb", _fake_cocotb(
        ws, {"no_pad": {"m": 0.5}, "pad": {"m": 1.5}}, seen))
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert [c for c, _ in seen] == ["no_pad", "pad"]
    assert seen[0][1] == ws / "tb"
    # the bench's own tb/ is never rewritten
    assert "tt_ua_pad" not in (ws / "tb" / "ideal.sp").read_text()
    assert code == 1, out
    bad = [v for v in out["violations"]]
    assert len(bad) == 1 and bad[0]["kind"] == "measure_out_of_bounds"
    assert "pad" in bad[0]["refs"] and bad[0]["msg"].startswith("[pad] ")
    assert out["measures"]["m"] == 0.5
    assert out["pad"]["measures"]["m"] == 1.5
    assert out["pad"]["block"] == "blk"


def test_cosim_bare_scoped_bound_is_not_scored_through_the_pad(
        tmp_path, monkeypatch, capsys):
    ws = _pad_cosim_ws(tmp_path, bounds={"m": {"min": 0, "max": 1,
                                               "pad": "bare"}})
    _no_toolchain(monkeypatch)
    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb", _fake_cocotb(
        ws, {"no_pad": {"m": 0.5}, "pad": {"m": 1.5}}, []))
    assert check_cosim.main(["--workspace", str(ws)]) == 0, \
        capsys.readouterr().out


def test_cosim_refuses_a_bench_with_a_ua_pin_and_no_pad_model(
        tmp_path, monkeypatch, capsys):
    # planted: the ua pin is on no subckt port, so no pad can go in
    ws = _pad_cosim_ws(tmp_path, netlist=PAD_NETLIST.replace("vout", "out"))
    seen = []
    _no_toolchain(monkeypatch)
    monkeypatch.setattr(check_cosim.cocotblib, "run_cocotb", _fake_cocotb(
        ws, {}, seen))
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and not seen, out
    assert [v["kind"] for v in out["violations"]] == ["ua_pad_not_inserted"]


def test_cosim_refuses_a_bench_that_never_loads_its_netlist(
        tmp_path, monkeypatch, capsys):
    ws = _pad_cosim_ws(tmp_path, bench_names_netlist=False)
    _no_toolchain(monkeypatch)
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == ["ua_pad_not_inserted"]


def test_cosim_bound_with_a_bad_pad_scope_is_an_error(tmp_path, capsys):
    ws = _pad_cosim_ws(tmp_path, bounds={"m": {"max": 1, "pad": "never"}})
    assert check_cosim.main(["--workspace", str(ws)]) == 2
    assert "pad" in json.loads(capsys.readouterr().out)["remediation"]


# --------------------------------------------------------------------- slow
def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "log").mkdir(parents=True)
    (ws / "reports").mkdir(parents=True)
    shutil.copytree(CORPUS_RUNG / "tb", ws / "tb")
    shutil.copy2(CORPUS_RUNG / "interface.yaml", ws / "interface.yaml")
    return ws


@pytest.mark.slow
def test_ring_osc_div_clean_bench_passes(tmp_path, capsys):
    ws = make_ws(tmp_path)
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert out["measures"]["digital_toggles"] >= 8
    assert not out["ngspice_signatures"]


@pytest.mark.slow
def test_ring_osc_div_never_toggled_fault_caught(tmp_path, capsys):
    ws = make_ws(tmp_path)
    sys.path.insert(0, str(CORPUS_RUNG / "faults"))
    import plant_never_toggled
    plant_never_toggled.plant(ws)
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert "digital_side_never_toggled" in kinds


@pytest.mark.slow
def test_ring_osc_div_wrong_divide_ratio_fault_caught(tmp_path, capsys):
    ws = make_ws(tmp_path)
    sys.path.insert(0, str(CORPUS_RUNG / "faults"))
    import plant_wrong_divide_ratio
    plant_wrong_divide_ratio.plant(ws)
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert "measure_out_of_bounds" in kinds


def test_msde_cosim_hashes_the_sides_its_bench_reads(tmp_path):
    # regression (msde H2 review): the cosim bench .includes the analog
    # netlist, applies the analog sizing and compiles the digital RTL, so an
    # edit to any of them must change cosim's input hashes
    import statelib
    imap = statelib.load_map()
    kinds = imap["gate_inputs"]["msde"]["cosim"]
    assert kinds[0] == "interface"
    ws = tmp_path
    files = {"analog_netlist": "analog/netlist/a.cir",
             "analog_sizing": "analog/sizing/sizing.yaml",
             "digital_rtl": "digital/rtl/d.v"}
    for rel in files.values():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("w: 1\n", encoding="utf-8")
    for kind, rel in files.items():
        assert kind in kinds
        before = statelib.hash_kind(ws, kind, imap)[1]
        (ws / rel).write_text("w: 2\n", encoding="utf-8")
        assert statelib.hash_kind(ws, kind, imap)[1] != before, kind
    # an unrelated side file stays out of cosim's inputs
    (ws / "analog" / "layout").mkdir(parents=True)
    assert "analog_layout" not in kinds


@pytest.mark.slow
def test_dac_tile_cosim_runs_through_the_ua_pad(tmp_path, capsys):
    # interface.yaml puts vout on ua[0]: the gate runs the bench bare and
    # with dac_tile_ideal behind the TT pad model, and both meet the bounds
    sys.path.insert(0, str(REPO / "tests"))
    import faults
    ws = faults.make_scratch_workspace(
        tmp_path, REPO / "corpus" / "msde" / "dac_tile", "msde", "dac_tile",
        block="dac_tile")
    code = check_cosim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["pad"]["block"] == "dac_tile_ideal"
    padded = (ws / out["pad"]["netlist"]).read_text(encoding="utf-8")
    assert "xuapad_vout vout__ua_die_vout vout tt_ua_pad" in padded
    # the pad run is a different circuit, not a second bare run
    assert out["pad"]["measures"]["vout_11"] != out["measures"]["vout_11"]

"""engine/scripts/check_timing.py: the timing gate (docs/design.md 1.5's
timing row, "### M4."). Fakes `eda sta`'s own output (never the real
OpenSTA - tests/smoke-harden.sh runs that) to prove the gate's failure
classification: a launcher that never produces a parseable "worst slack"
line is a refusal, a negative slack is a finding, never a silent pass.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_timing  # noqa: E402
import pytest  # noqa: E402
import ttlib  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "opensta"
# the clock uncertainty read-back (#73) as LibreLane's base.sdc leaves it:
# CLOCK_UNCERTAINTY_CONSTRAINT's 0.25 ns on the one clock
CLEAN_UNCERTAINTY = ("chipflow_clock_uncertainty_begin\nchipflow_clock clk\n"
                     "set_clock_uncertainty 0.2500 clk\n"
                     "chipflow_clock_uncertainty_end\n")
# check_setup and the min pulse width/min period/clock gating sections as a
# clean OpenSTA 3.1.0 run prints them (markers only, no findings).
CLEAN_SIGNOFF = ("chipflow_check_setup_begin\n"
                 + "".join(f"chipflow_section {s}\n"
                           for s in check_timing.SIGNOFF_SECTIONS)
                 + "chipflow_section end\n"
                 + "chipflow_ideal_clocks_begin\nchipflow_ideal_clocks_end\n"
                 + CLEAN_UNCERTAINTY)
CLEAN_COUNTS = ("chipflow_violation_counts slew 0 cap 0 fanout 0\n"
                + CLEAN_SIGNOFF)


def make_ws_with_harden(tmp_path: Path, corners=("nom_tt_025C_3v30",)) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "top: counter8\nrequirements: []\n"
        "ports: {clk: {dir: input, width: 1}}\n"
        "tt_pins: {clk: clk}\n", encoding="utf-8")
    final = ws / "harden" / "runs" / "run" / "final"
    top = "tt_um_counter8"
    for corner in corners:
        (final / "lib" / corner).mkdir(parents=True)
        (final / "nl").mkdir(parents=True, exist_ok=True)
        (final / "nl" / f"{top}.nl.v").write_text("x", encoding="utf-8")
        (final / "sdc").mkdir(parents=True, exist_ok=True)
        (final / "sdc" / f"{top}.sdc").write_text("x", encoding="utf-8")
        bucket = corner.split("_", 1)[0]
        (final / "spef" / bucket).mkdir(parents=True, exist_ok=True)
        (final / "spef" / bucket / f"{top}.{bucket}.spef").write_text("x", encoding="utf-8")
    # the run's resolved config, recorded where harden ran (here: a design
    # directory somewhere else, as when the workspace was copied since).
    (ws / "harden" / "flow.sdc").write_text("x", encoding="utf-8")
    (final.parent / "resolved.json").write_text(json.dumps({
        "meta": {"version": 1},
        "DESIGN_DIR": "/elsewhere/ws/harden",
        "SIGNOFF_SDC_FILE": "/elsewhere/ws/harden/flow.sdc",
        "PNR_SDC_FILE": "/elsewhere/ws/harden/flow.sdc",
        "CLOCK_PERIOD": 20.0,
        "CLOCK_PORT": "clk",
        "IO_DELAY_CONSTRAINT": 20,
        "RUN_MCSTA": True,
        "STA_CORNERS": ["nom_tt_025C_3v30", "max_ss_125C_3v00"],
        "SYNTH_DRIVING_CELL": "gf180mcu_fd_sc_mcu7t5v0__inv_1/ZN",
        "EXTRA_LIBS": None,
    }), encoding="utf-8")
    return ws


def _patch_common(monkeypatch, tmp_path):
    # a fake, always-existing liberty file - the real one lives in the PDK,
    # outside anything this test controls or needs.
    fake_lib = tmp_path / "fake.lib"
    fake_lib.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ttlib, "stdcell_liberty_path", lambda corner, pdk_root: fake_lib)

    def fake_pdk_root(*a, **kw):
        return Path("/fake/toolchain/foss/pdks")
    monkeypatch.setattr(check_timing, "_pdk_root", fake_pdk_root)
    scripts = tmp_path / "librelane_scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "base.sdc").write_text("x", encoding="utf-8")
    monkeypatch.setattr(check_timing, "_scripts_dir", lambda pdk_root: scripts)


def test_no_harden_output_refuses(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "top: counter8\nrequirements: []\nports: {}\ntt_pins: {}\n",
        encoding="utf-8")
    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "has not produced" in out["error"]


def test_sta_with_no_parseable_slack_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path)
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="Error: something broke\n", stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "no parseable worst-slack" in out["error"]


def test_negative_setup_slack_is_a_violation(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path, corners=("max_ss_125C_3v00",))
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="worst slack max -0.06\nworst slack min 2.23\n" + CLEAN_COUNTS, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert out["status"] == "violations"
    kinds = {v["kind"] for v in out["violations"]}
    assert "setup_violation" in kinds


def test_infinite_slack_is_a_clock_unconstrained_violation_not_a_pass(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path, corners=("nom_tt_025C_3v30",))
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="worst slack max INF\nworst slack min INF\n" + CLEAN_COUNTS, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert out["status"] == "violations"
    kinds = {v["kind"] for v in out["violations"]}
    assert kinds == {"clock_unconstrained"}
    assert "setup_violation" not in kinds and "hold_violation" not in kinds
    # never Infinity in the JSON payload
    corner = out["corners"]["nom_tt_025C_3v30"]
    assert corner["setup_ws"] is None and corner["hold_ws"] is None
    text = json.dumps(out)
    assert "Infinity" not in text


def test_all_corners_positive_slack_pass(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path, corners=("nom_tt_025C_3v30", "max_ss_125C_3v00"))
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="worst slack max 7.97\nworst slack min 2.23\n" + CLEAN_COUNTS, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert len(out["corners"]) == 2

    # Regression: the per-corner .sta_<corner>.tcl scratch file used to be
    # written straight into harden/runs/run/final/, the exact tree the
    # "harden" artifact-kind's dir_text hash recursively covers
    # (invalidation.yaml) - a write there staled every OTHER gate that also
    # reads "harden". It must land under ws/log/timing_work/ instead, and
    # final/ must stay exactly what harden itself produced (nothing new).
    final_dir = ws / "harden" / "runs" / "run" / "final"
    assert not list(final_dir.glob(".sta_*")), \
        "check_timing wrote a scratch file into final_dir"
    work_dir = ws / "log" / "timing_work"
    assert len(list(work_dir.glob(".sta_*.tcl"))) == 2, \
        "expected one .sta_<corner>.tcl per corner under ws/log/timing_work/"


def _check_types_with_good_slack() -> str:
    """The real OpenSTA 3.1.0 report_check_types -violators output (a tiny
    design with a forced slew, capacitance and fanout violation), with its
    slack lines swapped for positive ones so only the check-types section
    can fail the gate."""
    lines = (FIXTURES / "check_types_violators.txt").read_text(
        encoding="utf-8").splitlines()
    body = [ln for ln in lines if not ln.startswith("worst slack")]
    return ("worst slack max 7.97\nworst slack min 2.23\n" + "\n".join(body)
            + "\n" + CLEAN_SIGNOFF)


def test_parse_check_types_reads_the_real_opensta_format():
    parsed = check_timing.parse_check_types(_check_types_with_good_slack())
    assert parsed["slew"]["count"] == 25 and len(parsed["slew"]["pins"]) == 25
    assert parsed["cap"]["count"] == 8 and len(parsed["cap"]["pins"]) == 8
    assert parsed["fanout"] == {"count": 1, "pins": ["u0/ZN"]}
    assert "b0/ZN" in parsed["cap"]["pins"]


def test_parse_check_types_without_counts_line_refuses():
    text = (FIXTURES / "check_types_violators.txt").read_text(encoding="utf-8")
    text = "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith(check_timing.COUNTS_MARK))
    with pytest.raises(check_timing.CheckError, match="never ran"):
        check_timing.parse_check_types(text)


def test_cap_and_fanout_violators_fail_with_positive_slack(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path)
    _patch_common(monkeypatch, tmp_path)
    out_text = _check_types_with_good_slack()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=out_text, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    msgs = [v["msg"] for v in out["violations"]]
    assert any("max cap: 8" in m for m in msgs), msgs
    assert any("max fanout: 1" in m and "u0/ZN" in m for m in msgs), msgs
    assert any("max slew: 25" in m for m in msgs), msgs


def test_counts_line_missing_from_real_run_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path)
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="worst slack max 7.97\nworst slack min 2.23\n", stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)

    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert check_timing.COUNTS_MARK in out["error"]


TINY_NETLIST = """module tt_um_tiny(input clk, input a, output [7:0] y);
 wire n;
 gf180mcu_fd_sc_mcu7t5v0__inv_1 u0(.I(a), .ZN(n));
""" + "".join(f" gf180mcu_fd_sc_mcu7t5v0__inv_1 b{i}(.I(n), .ZN(y[{i}]));\n"
              for i in range(8)) + "endmodule\n"

TINY_SPEF = """*SPEF "IEEE 1481-1998"
*DESIGN "tt_um_tiny"
*DATE "x"
*VENDOR "x"
*PROGRAM "x"
*VERSION "x"
*DESIGN_FLOW "x"
*DIVIDER /
*DELIMITER :
*BUS_DELIMITER [ ]
*T_UNIT 1 NS
*C_UNIT 1 PF
*R_UNIT 1 OHM
*L_UNIT 1 HENRY

*D_NET n 0.001
*CONN
*I u0:ZN O
*I b0:I I
*END
"""

TINY_SDC = """create_clock -name clk -period 100 [get_ports clk]
set_input_delay 0 -clock clk [get_ports a]
set_output_delay 0 -clock clk [get_ports y]
"""


@pytest.mark.slow
@pytest.mark.parametrize("extra_sdc,expect", [
    ("", []),
    ("set_max_fanout 4 [current_design]\n", ["max fanout: 1"]),
    ("set_load 0.5 [get_ports y]\n", ["max cap: 8"]),
])
def test_real_opensta_catches_fanout_and_cap(tmp_path, extra_sdc, expect):
    """Real OpenSTA on an eight-way fanout: clean as written, and red on a
    fanout limit or an output load it breaks - with timing slack still
    positive, so only the check-types section can catch it."""
    corner = "nom_tt_025C_3v30"
    final = tmp_path / "final"
    for sub in ("nl", "sdc", "spef/nom"):
        (final / sub).mkdir(parents=True)
    (final / "nl" / "tt_um_tiny.nl.v").write_text(TINY_NETLIST, encoding="utf-8")
    (final / "sdc" / "tt_um_tiny.sdc").write_text(TINY_SDC + extra_sdc, encoding="utf-8")
    (final / "spef" / "nom" / "tt_um_tiny.nom.spef").write_text(TINY_SPEF, encoding="utf-8")
    r = check_timing.run_corner(final, "tt_um_tiny", corner,
                                check_timing._pdk_root(), tmp_path,
                                final / "sdc" / "tt_um_tiny.sdc", "")
    assert r["setup_ws"] > 0 and r["hold_ws"] > 0, r
    for want in expect:
        assert any(v.startswith(want) for v in r["violators"]), r
    if not expect:
        assert r["violators"] == [], r


# pll's harden/constraints.sdc shape: a clock period picked from the loaded
# std-cell liberty's name, so only an SDC read per corner gets it right.
PER_CORNER_SDC = """proc tiny_period {} {
    foreach lib [get_libs *] {
        if {[string match *__ss_* [get_name $lib]]} { return 100 }
    }
    return $::env(CLOCK_PERIOD)
}
create_clock -name clk -period [tiny_period] [get_ports clk]
set_input_delay 0 -clock clk [get_ports a]
set_output_delay 0 -clock clk [get_ports y]
"""


@pytest.mark.slow
def test_real_opensta_reads_a_per_corner_sdc_per_corner(tmp_path):
    """Real OpenSTA: an SDC whose clock period comes from the corner's
    liberty (100 ns at ss) and from ::env(CLOCK_PERIOD) (10 ns elsewhere)
    gets each corner's own period, with the run's config in ::env."""
    final = tmp_path / "ws" / "harden" / "runs" / "run" / "final"
    (final / "nl").mkdir(parents=True)
    (final / "nl" / "tt_um_tiny.nl.v").write_text(TINY_NETLIST, encoding="utf-8")
    for bucket in ("nom", "max"):
        (final / "spef" / bucket).mkdir(parents=True)
        (final / "spef" / bucket / f"tt_um_tiny.{bucket}.spef").write_text(
            TINY_SPEF, encoding="utf-8")
    harden = tmp_path / "ws" / "harden"
    (harden / "constraints.sdc").write_text(PER_CORNER_SDC, encoding="utf-8")
    (final.parent / "resolved.json").write_text(json.dumps({
        "DESIGN_DIR": "/elsewhere/harden",
        "SIGNOFF_SDC_FILE": "/elsewhere/harden/constraints.sdc",
        "CLOCK_PERIOD": 10, "IO_DELAY_CONSTRAINT": 20,
        "TIME_DERATING_CONSTRAINT": 0, "MAX_FANOUT_CONSTRAINT": 10,
        "CLOCK_UNCERTAINTY_CONSTRAINT": 0.25,
        "CLOCK_TRANSITION_CONSTRAINT": 0.15, "OUTPUT_CAP_LOAD": 5,
    }), encoding="utf-8")
    pdk_root = check_timing._pdk_root()
    sdc, env_tcl = check_timing.signoff_sdc(tmp_path / "ws", final.parent,
                                            pdk_root)
    assert sdc == harden / "constraints.sdc"
    tt = check_timing.run_corner(final, "tt_um_tiny", "nom_tt_025C_3v30",
                                 pdk_root, tmp_path, sdc, env_tcl)
    ss = check_timing.run_corner(final, "tt_um_tiny", "max_ss_125C_3v00",
                                 pdk_root, tmp_path, sdc, env_tcl)
    # one inverter pair's delay is well under a nanosecond: the slacks sit
    # near each corner's own period
    assert 9 < tt["setup_ws"] < 10, tt
    assert 98 < ss["setup_ws"] < 100, ss


def _sta_scripts(ws: Path) -> dict:
    return {p.name: p.read_text(encoding="utf-8")
            for p in (ws / "log" / "timing_work").glob(".sta_*.tcl")}


def _passing_sta(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="worst slack max 7.97\nworst slack min 2.23\n"
            + CLEAN_COUNTS, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)


def test_each_corner_reads_the_signoff_sdc_under_librelanes_env(
        tmp_path, monkeypatch, capsys):
    """The signoff SDC itself, not final/sdc (one step's write_sdc, frozen
    at whichever corner it had loaded), read once per corner with the run's
    config in ::env as LibreLane's STAPostPNR has it, so an SDC that picks a
    value per corner gets each corner's own."""
    ws = make_ws_with_harden(tmp_path, corners=("nom_tt_025C_3v30",
                                                "max_ss_125C_3v00"))
    _patch_common(monkeypatch, tmp_path)
    _passing_sta(monkeypatch)
    assert check_timing.main(["--workspace", str(ws)]) == 0
    scripts = _sta_scripts(ws)
    assert set(scripts) == {".sta_nom_tt_025C_3v30.tcl",
                            ".sta_max_ss_125C_3v00.tcl"}
    harden = ws / "harden"
    for name, tcl in scripts.items():
        corner = name[len(".sta_"):-len(".tcl")]
        assert "final/sdc" not in tcl
        assert f"read_sdc {harden / 'flow.sdc'}\n" in tcl
        # DESIGN_DIR and the paths under it rebased onto this workspace
        assert f"set ::env(DESIGN_DIR) {harden}\n" in tcl
        assert f"set ::env(SIGNOFF_SDC_FILE) {harden / 'flow.sdc'}\n" in tcl
        assert "/elsewhere" not in tcl
        assert "set ::env(CLOCK_PERIOD) 20.0\n" in tcl
        assert "set ::env(RUN_MCSTA) 1\n" in tcl
        assert ("set ::env(STA_CORNERS) "
                "\"nom_tt_025C_3v30 max_ss_125C_3v00\"\n") in tcl
        assert "EXTRA_LIBS" not in tcl and "::env(meta)" not in tcl
        assert "set ::env(OPENLANE_SDC_IDEAL_CLOCKS) 0\n" in tcl
        assert f"set ::env(_CURRENT_CORNER_NAME) {corner}\n" in tcl
        assert "set ::env(IO_PCT)" in tcl
        # everything set before the SDC is read
        assert tcl.index("::env(DESIGN_DIR)") < tcl.index("read_sdc")
        assert tcl.index("read_liberty") < tcl.index("read_sdc")


def test_no_design_sdc_reads_librelanes_base_sdc(tmp_path, monkeypatch,
                                                  capsys):
    ws = make_ws_with_harden(tmp_path)
    resolved = ws / "harden" / "runs" / "run" / "resolved.json"
    config = json.loads(resolved.read_text(encoding="utf-8"))
    config["SIGNOFF_SDC_FILE"] = config["PNR_SDC_FILE"] = None
    config["FALLBACK_SDC"] = "/an/older/tree/librelane/scripts/base.sdc"
    resolved.write_text(json.dumps(config), encoding="utf-8")
    _patch_common(monkeypatch, tmp_path)
    _passing_sta(monkeypatch)
    assert check_timing.main(["--workspace", str(ws)]) == 0
    tcl = next(iter(_sta_scripts(ws).values()))
    base = tmp_path / "librelane_scripts" / "base.sdc"
    assert f"read_sdc {base}\n" in tcl
    assert f"set ::env(SCRIPTS_DIR) {base.parent}\n" in tcl


def test_no_resolved_config_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path)
    (ws / "harden" / "runs" / "run" / "resolved.json").unlink()
    _patch_common(monkeypatch, tmp_path)
    _passing_sta(monkeypatch)
    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "resolved.json" in out["error"]


def test_missing_signoff_sdc_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws_with_harden(tmp_path)
    (ws / "harden" / "flow.sdc").unlink()
    _patch_common(monkeypatch, tmp_path)
    _passing_sta(monkeypatch)
    code = check_timing.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "signoff SDC" in out["error"]


def test_value_to_tcl_matches_librelane():
    assert check_timing.value_to_tcl(True) == "1"
    assert check_timing.value_to_tcl(3) == "3"
    assert check_timing.value_to_tcl(["a b", "c"]) == '"a b" c'
    assert check_timing.value_to_tcl({"k": [1, 2]}) == 'k "1 2"'
    assert check_timing.tcl_escape("") == '""'
    assert check_timing.tcl_escape('x $y [z] "q"') == r'"x \$y \[z] \"q\""'



# ------------------------------------------------- check_setup (issue #70)
# and min pulse width / min period / clock gating (issue #72). Fixtures are
# real OpenSTA 3.1.0 transcripts of run_corner's own script on the tiny
# designs below (TINY2_*).

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _good_slack(text: str) -> str:
    body = [ln for ln in text.splitlines() if not ln.startswith("worst slack")]
    return ("worst slack max 7.97\nworst slack min 2.23\n"
            "chipflow_ideal_clocks_begin\nchipflow_ideal_clocks_end\n"
            + CLEAN_UNCERTAINTY + "\n".join(body) + "\n")


def test_parse_check_setup_reads_unclocked_and_unconstrained():
    found = check_timing.parse_check_setup(_fixture("check_setup_unclocked.txt"))
    assert found == {"unclocked_register": ["f2/CLK"],
                     "unconstrained_endpoint": ["z", "f2/D"]}


def test_parse_check_setup_reads_a_loop_and_multiple_clocks():
    found = check_timing.parse_check_setup(
        _fixture("check_setup_loop_multiclock.txt"))
    assert found["multiple_clock"] == ["f0/CLK"]
    # the loop listing's "| loop cut point" and dashed rule are not items
    assert found["combinational_loop"] == ["x1/ZN", "x2/I", "x2/ZN", "x1/A2"]


def test_parse_check_setup_keeps_an_unknown_heading():
    text = ("chipflow_check_setup_begin\n"
            "Warning: There are 2 frobbed widgets.\n  u1\n  u2\n"
            "chipflow_section min_pulse_width\n")
    assert check_timing.parse_check_setup(text) == {
        "frobbed widgets": ["u1", "u2"]}


def test_parse_check_setup_without_marker_refuses():
    with pytest.raises(check_timing.CheckError, match="check_setup never ran"):
        check_timing.parse_check_setup("worst slack max 1\n")


def test_parse_signoff_reads_min_period_and_pulse_width():
    found = check_timing.parse_signoff_checks(_fixture("signoff_min_period.txt"))
    assert found["min_period"] == ["f0/CLK", "f1/CLK", "f3/CLK", "fd/CLK"]
    assert found["min_pulse_width"] == ["f3/CLK (high)"]
    assert found["clock_gating_setup"] == [] == found["clock_gating_hold"]


def test_parse_signoff_reads_clock_gating():
    found = check_timing.parse_signoff_checks(_fixture("signoff_clock_gating.txt"))
    assert found["clock_gating_hold"] == ["gated clock (slack -10.00)"]
    assert found["clock_gating_setup"] == []


def test_parse_signoff_reads_recovery_and_removal():
    found = check_timing.parse_signoff_checks(
        _fixture("signoff_recovery_removal.txt"))
    assert found["recovery"] == ["f0 (slack -0.70)"]
    assert found["removal"] == ["f0 (slack -1.46)"]
    assert found["min_period"] == [] and found["clock_gating_hold"] == []


@pytest.mark.parametrize("drop", ["chipflow_section end",
                                  "chipflow_section min_period",
                                  "chipflow_section recovery"])
def test_parse_signoff_with_a_missing_marker_refuses(drop):
    text = "\n".join(ln for ln in CLEAN_SIGNOFF.splitlines() if ln != drop)
    with pytest.raises(check_timing.CheckError):
        check_timing.parse_signoff_checks(text)


def _gate(tmp_path, monkeypatch, capsys, sta_out, spec_extra=""):
    ws = make_ws_with_harden(tmp_path)
    if spec_extra:
        spec = ws / "spec" / "spec.yaml"
        spec.write_text(spec.read_text(encoding="utf-8") + spec_extra,
                        encoding="utf-8")
    _patch_common(monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=sta_out, stderr="")
    monkeypatch.setattr(check_timing.checklib, "run_tool", fake_run)
    code = check_timing.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_unclocked_bank_fails_with_finite_positive_slack(tmp_path, monkeypatch, capsys):
    """#70's fault: one register bank clocked by a divider flop with no
    create_generated_clock. The rest of the design has finite, positive
    slack - the old gate passed it."""
    code, out = _gate(tmp_path, monkeypatch, capsys,
                      _good_slack(_fixture("check_setup_unclocked.txt")))
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert kinds == {"unclocked_register", "unconstrained_endpoint"}, kinds
    unclocked = next(v for v in out["violations"]
                     if v["kind"] == "unclocked_register")
    assert unclocked["refs"] == ["f2/CLK"] and "f2/CLK" in unclocked["msg"]


def test_unconstrained_ok_waives_named_endpoints_only(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys,
                      _good_slack(_fixture("check_setup_unclocked.txt")),
                      "timing: {unconstrained_ok: [z, 'f2/*']}\n")
    assert code == 1, out
    # the endpoints are waived; the unclocked register is not
    assert {v["kind"] for v in out["violations"]} == {"unclocked_register"}


def test_unconstrained_ok_not_a_list_refuses(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys, _good_slack(
        "chipflow_violation_counts slew 0 cap 0 fanout 0\n" + CLEAN_SIGNOFF),
        "timing: {unconstrained_ok: z}\n")
    assert code == 2, out
    assert "unconstrained_ok" in out["error"]


def test_multiple_clocks_are_a_fact_not_a_failure(tmp_path, monkeypatch, capsys):
    text = _fixture("check_setup_loop_multiclock.txt")
    code, out = _gate(tmp_path, monkeypatch, capsys, _good_slack(text))
    assert code == 1, out
    assert {v["kind"] for v in out["violations"]} == {"combinational_loop"}
    corner = out["corners"]["nom_tt_025C_3v30"]
    assert corner["multiple_clock"] == ["f0/CLK"]


@pytest.mark.parametrize("fixture,kinds", [
    ("signoff_min_period.txt", {"min_period_violation",
                                "min_pulse_width_violation"}),
    ("signoff_clock_gating.txt", {"clock_gating_violation"}),
    # #195: a reset released too late and too early for the flop
    ("signoff_recovery_removal.txt", {"recovery_violation",
                                      "removal_violation"}),
])
def test_signoff_checks_fail_with_positive_slack(tmp_path, monkeypatch, capsys,
                                                 fixture, kinds):
    code, out = _gate(tmp_path, monkeypatch, capsys,
                      _good_slack(_fixture(fixture)))
    assert code == 1, out
    assert {v["kind"] for v in out["violations"]} == kinds


TINY2_NETLIST = """module tt_um_tiny2(input clk, input a, input en, output y, output z, output w);
 wire q0, qn0, div, g;
 gf180mcu_fd_sc_mcu7t5v0__dffq_1 f0(.D(a), .CLK(clk), .Q(q0));
 gf180mcu_fd_sc_mcu7t5v0__dffq_1 f1(.D(q0), .CLK(clk), .Q(y));
 gf180mcu_fd_sc_mcu7t5v0__dffq_1 fd(.D(qn0), .CLK(clk), .Q(div));
 gf180mcu_fd_sc_mcu7t5v0__inv_1 i0(.I(div), .ZN(qn0));
 gf180mcu_fd_sc_mcu7t5v0__dffq_1 f2(.D(z), .CLK(div), .Q(z));
 gf180mcu_fd_sc_mcu7t5v0__and2_1 ga(.A1(clk), .A2(en), .Z(g));
 gf180mcu_fd_sc_mcu7t5v0__dffq_1 f3(.D(q0), .CLK(g), .Q(w));
endmodule
"""
TINY2_SPEF = (TINY_SPEF.split("*D_NET")[0].replace("tt_um_tiny", "tt_um_tiny2")
              + "*D_NET q0 0.001\n*CONN\n*I f0:Q O\n*I f1:D I\n*END\n")


def tiny2_sdc(period=20, waveform="", source="clk", generated=True,
              en_delay=15, outputs="y w z"):
    return (f"create_clock -name clk -period {period} {waveform}[get_ports clk]\n"
            + (f"create_generated_clock -name div -source [get_ports {source}] "
               "-divide_by 2 [get_pins fd/Q]\n" if generated else "")
            + "set_input_delay 1 -clock clk [get_ports a]\n"
            f"set_input_delay {en_delay} -clock clk [get_ports en]\n"
            f"set_output_delay 0 -clock clk [get_ports {{{outputs}}}]\n")


@pytest.mark.slow
@pytest.mark.parametrize("sdc,setup,signoff", [
    # known-clean: a /2 divider with its generated clock, a gated clock
    # whose enable settles while the clock is low.
    (tiny2_sdc(), {}, {}),
    # #70: the divider's create_generated_clock forgotten
    (tiny2_sdc(generated=False), {"unclocked_register": ["f2/CLK"]}, {}),
    # #70: the generated clock keyed on a source that reaches nothing
    (tiny2_sdc(source="en"), {"generated_clock_unreachable": ["div"]}, {}),
    # #70: an output with no output delay
    (tiny2_sdc(outputs="y w"), {"unconstrained_endpoint": ["z"]}, {}),
    # #72: 0.3 ns high phase, setup/hold still met
    (tiny2_sdc(waveform="-waveform {0 0.3} "), {},
     {"min_pulse_width": "f0/CLK (high)"}),
    # #72: period under dffq_1's liberty min_period (1.54 ns at tt)
    (tiny2_sdc(period=1.52, en_delay=1.0), {}, {"min_period": "f0/CLK"}),
    # #72: the clock gate's enable changes while the clock is high
    (tiny2_sdc(en_delay=0), {}, {"clock_gating_hold": "gated clock (slack"}),
])
def test_real_opensta_catches_check_setup_and_signoff_faults(
        tmp_path, sdc, setup, signoff):
    corner = "nom_tt_025C_3v30"
    final = tmp_path / "final"
    for sub in ("nl", "sdc", "spef/nom"):
        (final / sub).mkdir(parents=True)
    (final / "nl" / "tt_um_tiny2.nl.v").write_text(TINY2_NETLIST, encoding="utf-8")
    (final / "sdc" / "t.sdc").write_text(sdc, encoding="utf-8")
    (final / "spef" / "nom" / "tt_um_tiny2.nom.spef").write_text(
        TINY2_SPEF, encoding="utf-8")
    r = check_timing.run_corner(final, "tt_um_tiny2", corner,
                                check_timing._pdk_root(), tmp_path,
                                final / "sdc" / "t.sdc", "")
    for kind, items in setup.items():
        assert set(items) <= set(r["check_setup"].get(kind, [])), r
    if not setup:
        assert r["check_setup"] == {}, r
    for section in check_timing.SIGNOFF_SECTIONS:
        if section in signoff:
            assert any(i.startswith(signoff[section])
                       for i in r["signoff"][section]), r
        else:
            assert r["signoff"][section] == [], (section, r)
    if not setup and not signoff:
        assert r["setup_ws"] > 0 and r["hold_ws"] > 0, r
        assert check_timing.signoff_violations(r, "tt_um_tiny2", []) == []


def _tiny2_corner(tmp_path, sdc, name="t"):
    final = tmp_path / name / "final"
    for sub in ("nl", "sdc", "spef/nom"):
        (final / sub).mkdir(parents=True)
    (final / "nl" / "tt_um_tiny2.nl.v").write_text(TINY2_NETLIST, encoding="utf-8")
    (final / "sdc" / "t.sdc").write_text(sdc, encoding="utf-8")
    (final / "spef" / "nom" / "tt_um_tiny2.nom.spef").write_text(
        TINY2_SPEF, encoding="utf-8")
    return check_timing.run_corner(final, "tt_um_tiny2", "nom_tt_025C_3v30",
                                   check_timing._pdk_root(), tmp_path / name,
                                   final / "sdc" / "t.sdc", "")


@pytest.mark.slow
def test_real_opensta_propagates_an_sdc_with_no_propagation_line(tmp_path):
    # #145: read_current_sdc's fallback - no set_/unset_propagated_clock in
    # the SDC means every clock is propagated, so the divider's and the
    # clock gate's insertion delay shows in hold slack exactly as with an
    # explicit set_propagated_clock, and not as with the clocks ideal.
    plain = _tiny2_corner(tmp_path, tiny2_sdc(), "plain")
    prop = _tiny2_corner(
        tmp_path, tiny2_sdc() + "set_propagated_clock [all_clocks]\n", "prop")
    ideal = _tiny2_corner(
        tmp_path, tiny2_sdc() + "unset_propagated_clock [all_clocks]\n", "ideal")
    assert plain["ideal_clocks"] == [] and prop["ideal_clocks"] == [], plain
    assert plain["hold_ws"] == prop["hold_ws"], (plain, prop)
    assert plain["hold_ws"] != ideal["hold_ws"], (plain, ideal)
    assert check_timing.signoff_violations(plain, "tt_um_tiny2", []) == []


@pytest.mark.slow
def test_real_opensta_flags_an_ideal_clock_after_cts(tmp_path):
    # #145: an SDC that leaves its clocks ideal is timed without skew
    r = _tiny2_corner(tmp_path,
                      tiny2_sdc() + "unset_propagated_clock [all_clocks]\n")
    assert sorted(r["ideal_clocks"]) == ["clk", "div"], r
    found = [v for v in check_timing.signoff_violations(r, "tt_um_tiny2", [])
             if v["kind"] == "ideal_clock"]
    assert len(found) == 1 and "clk" in found[0]["msg"], found


@pytest.mark.slow
def test_real_opensta_skips_a_virtual_clock(tmp_path):
    # #145: a virtual IO clock has no source pin and is ideal on purpose
    r = _tiny2_corner(tmp_path, tiny2_sdc()
                      + "create_clock -name v -period 10\n"
                      + "set_propagated_clock [get_clocks {clk div}]\n")
    assert r["ideal_clocks"] == [], r
    assert not [v for v in check_timing.signoff_violations(r, "tt_um_tiny2", [])
                if v["kind"] == "ideal_clock"]


def test_parse_ideal_clocks_needs_its_closing_marker():
    text = ("chipflow_ideal_clocks_begin\nchipflow_ideal_clock clk\n"
            "chipflow_ideal_clock div\nchipflow_ideal_clocks_end\n")
    assert check_timing.parse_ideal_clocks(text) == ["clk", "div"]
    with pytest.raises(check_timing.CheckError):
        check_timing.parse_ideal_clocks(text.replace(
            "chipflow_ideal_clocks_end\n", ""))


def test_an_ideal_clock_fails_the_gate(tmp_path, monkeypatch, capsys):
    out_text = CLEAN_COUNTS.replace(
        "chipflow_ideal_clocks_end",
        "chipflow_ideal_clock clk\nchipflow_ideal_clocks_end")
    code, out = _gate(tmp_path, monkeypatch, capsys,
                      "worst slack max 7.97\nworst slack min 2.23\n" + out_text)
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == ["ideal_clock"]
    assert out["violations"][0]["refs"] == ["clk"]


# #195: one flop with an asynchronous active-low reset straight from a pad,
# as counter8 is with its reset synchroniser removed.
TINY3_NETLIST = """module tt_um_tiny3(input clk, input rst_n, input a, output y);
 gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 f0(.D(a), .RN(rst_n), .CLK(clk), .Q(y));
endmodule
"""
TINY3_SPEF = (TINY_SPEF.split("*D_NET")[0].replace("tt_um_tiny", "tt_um_tiny3")
              + "*D_NET y 0.001\n*CONN\n*I f0:Q O\n*P y O\n*END\n")
TINY3_SDC = """create_clock -name clk -period 20 [get_ports clk]
set_input_delay 1 -clock clk [get_ports a]
set_output_delay 0 -clock clk [get_ports y]
"""


@pytest.mark.slow
@pytest.mark.parametrize("rst_delay,section", [
    # known-clean: the reset releases 1 ns into the cycle
    ("set_input_delay 1 -clock clk [get_ports rst_n]\n", None),
    # released 21 ns into a 20 ns cycle: past the next edge's recovery time
    ("set_input_delay 21 -clock clk [get_ports rst_n]\n", "recovery"),
    # released 1 ns before the edge that launched it: inside removal time
    ("set_input_delay -max 1 -clock clk [get_ports rst_n]\n"
     "set_input_delay -min -1 -clock clk [get_ports rst_n]\n", "removal"),
])
def test_real_opensta_names_recovery_and_removal(tmp_path, rst_delay, section):
    """A recovery/removal miss is named as such, and the worst setup/hold
    slack stays the data paths' own - before #195 OpenSTA's worst slack
    folded the recovery path in and the gate called it a setup violation."""
    corner = "nom_tt_025C_3v30"
    final = tmp_path / "final"
    for sub in ("nl", "sdc", "spef/nom"):
        (final / sub).mkdir(parents=True)
    (final / "nl" / "tt_um_tiny3.nl.v").write_text(TINY3_NETLIST, encoding="utf-8")
    (final / "sdc" / "t.sdc").write_text(TINY3_SDC + rst_delay, encoding="utf-8")
    (final / "spef" / "nom" / "tt_um_tiny3.nom.spef").write_text(
        TINY3_SPEF, encoding="utf-8")
    r = check_timing.run_corner(final, "tt_um_tiny3", corner,
                                check_timing._pdk_root(), tmp_path,
                                final / "sdc" / "t.sdc", "")
    assert r["setup_ws"] > 0 and r["hold_ws"] > 0, r
    for name in check_timing.SIGNOFF_SECTIONS:
        if name == section:
            assert [i.split()[0] for i in r["signoff"][name]] == ["f0"], r
        else:
            assert r["signoff"][name] == [], (name, r)
    kinds = {v["kind"] for v in
             check_timing.signoff_violations(r, "tt_um_tiny3", [])}
    assert kinds == ({f"{section}_violation"} if section else set()), kinds


# --- #73: clock uncertainty from a declared jitter budget -----------------

import clock_budget  # noqa: E402
import speclib  # noqa: E402


def test_budget_known_answer_is_the_plls_hand_derivation():
    """#73's known answer: 12 ps rms over 2 correlated cycles at 7 sigma is
    0.168 ns (0.17 ns); random cycle-to-cycle jitter grows as sqrt(N)."""
    assert clock_budget.required_uncertainty_ns(12, 2) == pytest.approx(0.168)
    assert clock_budget.required_uncertainty_ns(
        12, 2, "random") == pytest.approx(0.084 * 2 ** 0.5)
    assert clock_budget.required_uncertainty_ns(50) == pytest.approx(0.35)
    assert clock_budget.required_uncertainty_ns(10, 1, sigma=3) == pytest.approx(0.03)


@pytest.mark.parametrize("jitter,kind", [
    ({"clk": {"ps_rms": 0}}, "clock_jitter_bad_entry"),
    ({"clk": {"ps_rms": 5, "cycles": 0}}, "clock_jitter_bad_entry"),
    ({"clk": {"ps_rms": 5, "cycles": 1.5}}, "clock_jitter_bad_entry"),
    ({"clk": {"ps_rms": 5, "accumulation": "linear"}}, "clock_jitter_bad_entry"),
    ({"clk": {"ps_rms": 5, "sigma": -1}}, "clock_jitter_bad_entry"),
    ({"clk": {"ps_rms": 5, "jitter_ps": 1}}, "clock_jitter_bad_entry"),
    ({"clk": 5}, "clock_jitter_bad_entry"),
    ([], "clock_jitter_not_a_mapping"),
    ({"clk": {"ps_rms": 5, "source": "analog/jitter.json"}},
     "clock_jitter_bad_source"),
    ({"clk": {"ps_rms": 5, "source": "spec/jitter/../spec.yaml"}},
     "clock_jitter_bad_source"),
    ({"clk": {"ps_rms": 5, "source": "spec/jitter"}}, "clock_jitter_bad_source"),
])
def test_spec_lint_names_a_bad_jitter_budget(jitter, kind):
    spec = {"top": "x", "requirements": [],
            "clock": {"period_ns": 20, "domains": ["clk"], "jitter": jitter}}
    assert kind in {v["kind"] for v in speclib.lint_spec(spec)}


def test_spec_lint_takes_a_good_jitter_budget():
    spec = {"top": "x", "requirements": [],
            "clock": {"period_ns": 20, "domains": ["clk"], "jitter": {
                "clk_pre": {"ps_rms": 12, "cycles": 2, "sigma": 7,
                            "accumulation": "correlated",
                            "source": "spec/jitter/vco.json"}}}}
    assert not [v for v in speclib.lint_spec(spec)
                if v["kind"].startswith("clock_")]


def test_parse_clock_uncertainty_reads_write_sdcs_forms():
    """The forms OpenSTA 3.1.0's write_sdc prints (see the slow real-tool
    test): a bare clock name, -setup/-hold splits, inter-clock lines that
    take precedence over the clock's own value, and pin/port values."""
    text = ("chipflow_clock_uncertainty_begin\n"
            "chipflow_clock clk\nchipflow_clock vco_out\nchipflow_clock div\n"
            "chipflow_clock clk/div\nchipflow_clock bus[0]\n"
            "set_clock_uncertainty -setup 0.2000 clk\n"
            "set_clock_uncertainty -hold 0.9000 clk\n"
            "set_clock_uncertainty 0.2500 vco_out\n"
            "set_clock_uncertainty 0.3000 clk/div\n"
            "set_clock_uncertainty 0.3000 bus[0]\n"
            "set_clock_uncertainty 0.4000 div\n"
            "set_clock_uncertainty -rise_from [get_clocks {vco_out}] "
            "-rise_to [get_clocks {div}]  -setup 0.1000\n"
            "set_clock_uncertainty -fall_from [get_clocks {vco_out}] "
            "-fall_to [get_clocks {div}]  -hold 0.0000\n"
            "set_clock_uncertainty 0.0100 [get_ports {clk}]\n"
            "set_clock_uncertainty -hold 0.0000 [get_pins {u1/CLK}]\n"
            "chipflow_clock_uncertainty_end\n")
    clocks, pins, via = check_timing.parse_clock_uncertainty(text)
    assert clocks == {"clk": 0.2, "vco_out": 0.1, "div": 0.1,
                      "clk/div": 0.3, "bus[0]": 0.3}
    assert pins == [("clk", 0.01)]
    assert via == ["div", "vco_out"]
    with pytest.raises(check_timing.CheckError):
        check_timing.parse_clock_uncertainty(
            text.replace("chipflow_clock_uncertainty_end\n", ""))


def test_inter_clock_value_on_a_clock_with_none_of_its_own_counts_zero():
    text = ("chipflow_clock_uncertainty_begin\nchipflow_clock a\n"
            "chipflow_clock b\n"
            "set_clock_uncertainty -from [get_clocks {a}] -to "
            "[get_clocks {b}] -setup 0.5000\n"
            "chipflow_clock_uncertainty_end\n")
    assert check_timing.parse_clock_uncertainty(text)[0] == {"a": 0.0, "b": 0.0}


def test_uncertainty_compare_rounds_to_write_sdcs_four_places():
    assert not check_timing._below(0.1680, 12 * 7 * 2 / 1000.0)
    assert check_timing._below(0.1679, 0.168)
    # a need of 0.16804 ns rounds up to 0.1681, not down to 0.1680
    assert check_timing._below(0.1680, 0.16804)


def test_a_pin_uncertainty_under_the_budget_fails(tmp_path, monkeypatch, capsys):
    sta = _clean_sta().replace(
        "set_clock_uncertainty 0.2500 clk\n",
        "set_clock_uncertainty 0.4000 clk\n"
        "set_clock_uncertainty 0.0100 [get_ports {clk}]\n")
    code, out = _gate(tmp_path, monkeypatch, capsys, sta, _jitter_spec(30))
    assert code == 1, out
    assert [(v["kind"], v["refs"]) for v in out["violations"]] == [
        ("clock_uncertainty_below_budget", ["clk"])]
    assert "pin/port" in out["violations"][0]["msg"]


def _jitter_spec(ps_rms, clock="clk", extra=""):
    return (f"clock: {{period_ns: 20, domains: [clk], jitter: "
            f"{{{clock}: {{ps_rms: {ps_rms}{extra}}}}}}}\n")


def _clean_sta():
    return "worst slack max 7.97\nworst slack min 2.23\n" + CLEAN_COUNTS


def test_uncertainty_below_the_jitter_budget_fails(tmp_path, monkeypatch, capsys):
    """#73's planted fault, faked: 50 ps rms at 7 sigma needs 0.35 ns and
    the SDC applies LibreLane's 0.25 ns - with every slack positive."""
    code, out = _gate(tmp_path, monkeypatch, capsys, _clean_sta(),
                      _jitter_spec(50))
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == [
        "clock_uncertainty_below_budget"]
    v = out["violations"][0]
    assert v["refs"] == ["clk"] and "0.3500" in v["msg"] and "0.2500" in v["msg"]
    assert out["clock_budgets"]["clk"]["required_ns"] == 0.35


def test_uncertainty_covering_the_budget_passes(tmp_path, monkeypatch, capsys):
    # 30 ps rms x 7 = 0.21 ns, under the 0.25 ns applied
    code, out = _gate(tmp_path, monkeypatch, capsys, _clean_sta(),
                      _jitter_spec(30))
    assert code == 0, out
    corner = next(iter(out["corners"].values()))
    assert corner["clock_uncertainty_ns"] == {"clk": 0.25}


def test_no_jitter_declared_keeps_the_generic_uncertainty(tmp_path, monkeypatch, capsys):
    sta = _clean_sta().replace("set_clock_uncertainty 0.2500 clk\n", "")
    code, out = _gate(tmp_path, monkeypatch, capsys, sta)
    assert code == 0, out
    assert "clock_budgets" not in out


def test_a_clock_with_no_uncertainty_counts_zero(tmp_path, monkeypatch, capsys):
    sta = _clean_sta().replace("set_clock_uncertainty 0.2500 clk\n", "")
    code, out = _gate(tmp_path, monkeypatch, capsys, sta, _jitter_spec(1))
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == [
        "clock_uncertainty_below_budget"]


def test_a_budget_on_a_clock_the_sdc_never_makes_fails(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys, _clean_sta(),
                      _jitter_spec(1, clock="clk_pre"))
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == [
        "clock_jitter_unknown_clock"]


def test_a_missing_jitter_source_fails(tmp_path, monkeypatch, capsys):
    extra = ", source: spec/jitter/vco.json"
    code, out = _gate(tmp_path, monkeypatch, capsys, _clean_sta(),
                      _jitter_spec(1, extra=extra))
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == [
        "clock_jitter_source_missing"]


def test_a_bad_jitter_budget_refuses(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys, _clean_sta(),
                      _jitter_spec(-3))
    assert code == 2, out
    assert "clock_jitter_bad_entry" in out["error"]


def test_uncertainty_read_back_missing_refuses(tmp_path, monkeypatch, capsys):
    sta = _clean_sta().replace("chipflow_clock_uncertainty_end\n", "")
    code, out = _gate(tmp_path, monkeypatch, capsys, sta)
    assert code == 2, out
    assert check_timing.UNCERTAINTY_END in out["error"]


@pytest.mark.slow
@pytest.mark.parametrize("extra_sdc,want,pins", [
    ("", {"clk": None}, []),
    ("set_clock_uncertainty 0.25 [get_clocks clk]\n", {"clk": 0.25}, []),
    ("set_clock_uncertainty 0.25 [get_clocks clk]\n"
     "set_clock_uncertainty -setup 0.4 [get_clocks clk]\n"
     "set_clock_uncertainty -hold 0.1 [get_clocks clk]\n", {"clk": 0.4}, []),
    # an inter-clock value overrides the clock's own in OpenSTA
    ("set_clock_uncertainty 0.4 [get_clocks clk]\n"
     "set_clock_uncertainty -from [get_clocks clk] -to [get_clocks clk] "
     "-setup 0.0\n", {"clk": 0.0}, []),
    ("set_clock_uncertainty 0.4 [get_clocks clk]\n"
     "set_clock_uncertainty 0.01 [get_ports clk]\n", {"clk": 0.4},
     [("clk", 0.01)]),
    # the SDC's own units do not leak into the ns read-back
    ("set_cmd_units -time ps\nset_clock_uncertainty 400 [get_clocks clk]\n",
     {"clk": 0.4}, []),
    ("create_generated_clock -name {clk/div} -source [get_ports clk] "
     "-divide_by 2 [get_ports a]\n"
     "set_clock_uncertainty 0.3 [get_clocks clk/div]\n"
     "set_clock_uncertainty 0.2 [get_clocks clk]\n",
     {"clk": 0.2, "clk/div": 0.3}, []),
])
def test_real_opensta_reads_back_each_clocks_uncertainty(tmp_path, extra_sdc,
                                                         want, pins):
    """Real OpenSTA: the read-back gives the setup uncertainty the SDC left
    on each clock (none, one value for both sides, a later -setup
    overriding it, an inter-clock value taking precedence, a generated
    clock, ps command units) and any pin/port value."""
    final = tmp_path / "final"
    for sub in ("nl", "sdc", "spef/nom"):
        (final / sub).mkdir(parents=True)
    (final / "nl" / "tt_um_tiny.nl.v").write_text(TINY_NETLIST, encoding="utf-8")
    (final / "sdc" / "tt_um_tiny.sdc").write_text(TINY_SDC + extra_sdc, encoding="utf-8")
    (final / "spef" / "nom" / "tt_um_tiny.nom.spef").write_text(TINY_SPEF, encoding="utf-8")
    r = check_timing.run_corner(final, "tt_um_tiny", "nom_tt_025C_3v30",
                                check_timing._pdk_root(), tmp_path,
                                final / "sdc" / "tt_um_tiny.sdc", "")
    assert r["clock_uncertainty"] == want, r
    assert r["pin_uncertainty"] == pins, r
    # with ps units the slack line must still read in ns
    assert 1.0 < r["setup_ws"] < 100.0, r

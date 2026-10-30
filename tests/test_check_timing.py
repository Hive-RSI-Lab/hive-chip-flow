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
CLEAN_COUNTS = "chipflow_violation_counts slew 0 cap 0 fanout 0\n"


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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    return "worst slack max 7.97\nworst slack min 2.23\n" + "\n".join(body) + "\n"


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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)

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
    monkeypatch.setattr(check_timing.subprocess, "run", fake_run)


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

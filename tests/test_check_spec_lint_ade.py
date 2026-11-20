"""engine/scripts/check_spec_lint_ade.py: the CLI/report contract around
speclib.lint_spec_ade plus this script's own tb/*.bounds.json reconciliation
(docs/design.md 1.1's script contract, 1.5's ade spec_lint row). Hermetic:
no eda/ngspice call, tb/*.bounds.json is read straight off disk."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_spec_lint_ade  # noqa: E402

GOOD_YAML = """\
top: current_mirror
supply: {vdd: 3.3}
devices: [xmref, xmout]
corners: default
measures:
  - name: iout_ua
    bounds: {min: 1.8, max: 2.2}
"""


def make_ws(tmp_path: Path, spec_text: str = GOOD_YAML) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(spec_text, encoding="utf-8")
    return ws


def write_bounds(ws: Path, bench_stem: str, entries: list[dict]) -> None:
    (ws / "tb").mkdir(parents=True, exist_ok=True)
    (ws / "tb" / f"{bench_stem}.cir").write_text("* bench\n", encoding="utf-8")
    (ws / "tb" / f"{bench_stem}.bounds.json").write_text(
        json.dumps(entries), encoding="utf-8")


def test_clean_spec_with_no_tb_yet_passes(tmp_path, capsys):
    # right after spec-writer runs, before any bench exists - nothing to
    # reconcile, and no error either.
    ws = make_ws(tmp_path)
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pass"


def test_matching_bench_bounds_passes(tmp_path, capsys):
    ws = make_ws(tmp_path)
    write_bounds(ws, "mirror_tb", [{"measure": "iout_ua", "min": 1.8, "max": 2.2}])
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pass"


def test_spec_measure_with_no_bench_bound_fails(tmp_path, capsys):
    ws = make_ws(tmp_path)
    write_bounds(ws, "mirror_tb", [{"measure": "some_other_measure", "min": 0, "max": 1}])
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    kinds = {v["kind"] for v in out["violations"]}
    assert "measure_no_bench_bound" in kinds


def test_bench_bound_with_no_spec_measure_fails(tmp_path, capsys):
    ws = make_ws(tmp_path)
    write_bounds(ws, "mirror_tb", [
        {"measure": "iout_ua", "min": 1.8, "max": 2.2},
        {"measure": "extra_measure", "min": 0, "max": 1},
    ])
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    kinds = {v["kind"] for v in out["violations"]}
    assert "bench_bound_no_spec_measure" in kinds


def test_missing_spec_yaml_is_an_error(tmp_path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"


def test_spec_grid_that_spans_the_defaults_passes(tmp_path, capsys):
    ws = make_ws(tmp_path, GOOD_YAML.replace(
        "corners: default",
        "corners: {grid: {process: [typical, ff, ss], temp_c: [-40, 25, 125]}}"))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 0


def test_spec_grid_missing_ss_is_bad_corners(tmp_path, capsys):
    ws = make_ws(tmp_path, GOOD_YAML.replace(
        "corners: default",
        "corners: {grid: {process: [typical, ff], temp_c: [-40, 25, 125]}}"))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == ["bad_corners"]


def test_measure_scoped_to_a_corner_the_grid_never_runs_is_refused(tmp_path, capsys):
    grid = "corners: {grid: {process: [typical, ff, ss], temp_c: [-40, 25, 125]}}"
    scoped = "    bounds: {min: 1.8, max: 2.2}\n    corners: [tt]\n"
    ws = make_ws(tmp_path, GOOD_YAML.replace("corners: default", grid)
                 .replace("    bounds: {min: 1.8, max: 2.2}\n", scoped))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == ["measure_bad_corners"]

    ws2 = make_ws(tmp_path / "b", GOOD_YAML.replace(
        "    bounds: {min: 1.8, max: 2.2}\n", scoped))  # default five hold tt
    assert check_spec_lint_ade.main(["--workspace", str(ws2)]) == 0


def _scoped(corners: str) -> str:
    return GOOD_YAML.replace("    bounds: {min: 1.8, max: 2.2}\n",
                             "    bounds: {min: 1.8, max: 2.2}\n"
                             f"    corners: {corners}\n")


def test_measure_scoped_to_a_passive_corner_passes(tmp_path, capsys):
    """Red before: the lint expanded the spec with no passives, so a PLL
    measure scoped to [tt_pff] (a resistor netlist sweeps it) was refused
    and its bound went unscored. The lint reads the spec alone; a netlist
    that does not sweep tt_pff is sim_pvt's sim_bound_not_swept."""
    ws = make_ws(tmp_path, _scoped("[tt_pff]"))
    (ws / "netlist").mkdir()
    (ws / "netlist" / "pll.cir").write_text(
        "xr1 a b vss ppolyf_u_1k r_width=2e-6 r_length=1e-5\n",
        encoding="utf-8")
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 0, \
        capsys.readouterr().out
    grid = ("corners: {grid: {process: [typical, ff, ss], "
            "temp_c: [-40, 25, 125]}}")
    ws2 = make_ws(tmp_path / "b", _scoped("[tt_pss]").replace(
        "corners: default", grid))
    assert check_spec_lint_ade.main(["--workspace", str(ws2)]) == 0, \
        capsys.readouterr().out


def test_measure_names_a_corner_by_its_alias(tmp_path, capsys):
    # names_of: the default tt corner is also tt_27c, as sim_pvt scores it
    ws = make_ws(tmp_path, _scoped("[tt_27c]"))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 0, \
        capsys.readouterr().out


@pytest.mark.parametrize("corners", ["[ss_pff]", "[tt_25c]", "[nosuch]"])
def test_measure_scoped_to_a_corner_no_sweep_runs_is_refused(
        tmp_path, capsys, corners):
    # ss_pff is swept only when the spec names it; tt_25c is a grid name
    # the default five never answer to
    ws = make_ws(tmp_path, _scoped(corners))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == ["measure_bad_corners"]


def test_positive_footprint_passes(tmp_path, capsys):
    ws = make_ws(tmp_path, GOOD_YAML + "footprint_um: {width: 60, height: 60.5}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize("fp", [
    "{width: 60}",                       # height missing
    "{width: 0, height: 60}",            # not positive
    "{width: -5, height: 60}",
    "{width: '60', height: 60}",         # not a number
    "{width: true, height: 60}",
    "{width: 60, height: 60, depth: 1}",  # unknown key
    "[60, 60]",                           # not a mapping
])
def test_bad_footprint_is_refused(tmp_path, capsys, fp):
    ws = make_ws(tmp_path, GOOD_YAML + f"footprint_um: {fp}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == ["bad_footprint"]


def test_spec_grid_with_extra_passes_and_bad_extra_is_bad_corners(tmp_path, capsys):
    grid = "{process: [typical, ff, ss], temp_c: [-40, 25, 125]}"
    ws = make_ws(tmp_path, GOOD_YAML.replace(
        "corners: default", f"corners: {{grid: {grid}, extra: [ss_pff, ff_pss]}}"))
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 0
    capsys.readouterr()
    for i, extra in enumerate(("[nope]", "1")):
        ws = make_ws(tmp_path / f"b{i}", GOOD_YAML.replace(
            "corners: default", f"corners: {{grid: {grid}, extra: {extra}}}"))
        assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 1
        out = json.loads(capsys.readouterr().out)
        assert [v["kind"] for v in out["violations"]] == ["bad_corners"]


@pytest.mark.parametrize("field, ok", [
    ("all", True), ("[tt, ss]", True), ("[tt_27c]", True),
    ("ss", False), ("[]", False), ("[1]", False), ("[nope]", False),
])
def test_pex_corners_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"pex_corners: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_pex_corners" in out, out


@pytest.mark.parametrize("field, ok", [
    ("all", True), ("[c_high]", True), ("[rc_high, rc_low]", True),
    ("c_high", False), ("[]", False), ("[1]", False), ("[nope]", False),
    ("null", False),
])
def test_pex_rc_knobs_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"pex_rc_knobs: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_pex_rc_knobs" in out, out


@pytest.mark.parametrize("field, ok", [
    ("all", True), ("[hrhc]", True), ("[lrlc, hrlc, lrhc]", True),
    ("hrhc", False), ("[]", False), ("[1]", False), ("[nope]", False),
    ("[c_high]", False), ("null", False),
])
def test_pex_rc_corners_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"pex_rc_corners: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_pex_rc_corners" in out, out


def test_sim_timeout_within_the_cap_passes(tmp_path, capsys):
    ws = make_ws(tmp_path, GOOD_YAML + "sim_timeout_s: {pex_sim: 900, "
                 "sim_tt: 120.5, sim_pvt: 3600}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize("field", [
    "{pex_sim: 0}",                      # not positive
    "{pex_sim: -10}",
    "{pex_sim: 3601}",                   # past the cap
    "{pex_sim: '600'}",                  # not a number
    "{pex_sim: true}",
    "{analog_drc: 600}",                 # runs no ngspice
    "{}",                                # empty
    "600",                               # not a mapping
])
def test_bad_sim_timeout_is_refused(tmp_path, capsys, field):
    ws = make_ws(tmp_path, GOOD_YAML + f"sim_timeout_s: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == ["bad_sim_timeout"]


# --- matching-sensitive measures need Monte Carlo (issue #97) ---

MISMATCH_YAML = """\
top: current_mirror
supply: {vdd: 3.3}
devices: [xmref, xmout]
measures:
  - name: iout_ratio
    bounds: {min: 1.8, max: 2.2}
"""


def _lint(tmp_path, capsys, text):
    ws = make_ws(tmp_path, text)
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_matching_measure_without_mc_is_refused(tmp_path, capsys):
    # planted: a ratio checked only at zero mismatch
    code, out = _lint(tmp_path, capsys, MISMATCH_YAML)
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == ["mismatch_measure_no_mc"]
    assert out["violations"][0]["refs"] == ["iout_ratio"]


def test_matching_measure_with_mc_passes(tmp_path, capsys):
    code, out = _lint(tmp_path, capsys, MISMATCH_YAML + "mc: {enabled: true}\n")
    assert code == 0, out
    assert out["mc_waived"] == []


def test_mc_disabled_is_no_mc(tmp_path, capsys):
    code, out = _lint(tmp_path, capsys, MISMATCH_YAML + "mc: {enabled: false}\n")
    assert code == 1, out


@pytest.mark.parametrize("name", ["cp_updn_mismatch_pct", "vos_offset_mv",
                                  "inl_max_abs", "dnl_lsb", "pair_matching"])
def test_name_heuristic(tmp_path, capsys, name):
    code, out = _lint(tmp_path, capsys,
                      MISMATCH_YAML.replace("iout_ratio", name))
    assert code == 1, out


@pytest.mark.parametrize("name", ["iout_ua", "ratiometric_gain", "vref"])
def test_name_heuristic_reads_whole_tokens(tmp_path, capsys, name):
    code, out = _lint(tmp_path, capsys,
                      MISMATCH_YAML.replace("iout_ratio", name))
    assert code == 0, out


def test_statistical_mismatch_marks_any_name(tmp_path, capsys):
    text = MISMATCH_YAML.replace("iout_ratio", "iout_ua") \
        + "    statistical: mismatch\n"
    code, out = _lint(tmp_path, capsys, text)
    assert [v["kind"] for v in out["violations"]] == ["mismatch_measure_no_mc"]


def test_waiver_needs_a_reason_and_is_reported(tmp_path, capsys):
    code, out = _lint(tmp_path, capsys,
                      MISMATCH_YAML + "    statistical: none\n")
    assert code == 1 and out["violations"][0]["kind"] == "bad_statistical"
    text = MISMATCH_YAML.replace("iout_ratio", "presc_div2_ratio") \
        + "    statistical: none\n    mc_waiver: a digital divide ratio\n"
    code, out = _lint(tmp_path / "w", capsys, text)
    assert code == 0, out
    assert out["mc_waived"] == [{"measure": "presc_div2_ratio",
                                 "why": "a digital divide ratio"}]


def test_bad_statistical_value(tmp_path, capsys):
    code, out = _lint(tmp_path, capsys,
                      MISMATCH_YAML + "    statistical: global\n")
    assert [v["kind"] for v in out["violations"]] == ["bad_statistical"]


def test_matching_measure_scoped_away_from_tt_is_refused(tmp_path, capsys):
    text = MISMATCH_YAML.replace("bounds: {min: 1.8, max: 2.2}",
                                 "bounds: {min: 1.8, max: 2.2}\n    corners: [ss, ff]") \
        + "mc: {enabled: true}\n"
    code, out = _lint(tmp_path, capsys, text)
    assert [v["kind"] for v in out["violations"]] == ["mismatch_measure_not_at_tt"]
    code, out = _lint(tmp_path / "t", capsys, text.replace("[ss, ff]", "[tt, ss]"))
    assert code == 0, out


@pytest.mark.parametrize("field, ok", [
    ("[{nets: [outp, outn], max_dc_pct: 5, max_dr_pct: 5}]", True),
    ("[{nets: [outp, outn], max_dr_pct: 5, min_dr_ohm: 2}]", True),
    ("[{nets: [outp, outn], max_dc_pct: 5, aggressors: [clk]}]", True),
    ("[{nets: [outp], max_dc_pct: 5}]", False),
    ("[{nets: [outp, OUTP], max_dc_pct: 5}]", False),
    ("[{nets: [outp, outn]}]", False),
    ("[{nets: [outp, outn], min_dc_ff: 1}]", False),
    ("[{nets: [outp, outn], max_dc_pct: -1}]", False),
    ("[{nets: [outp, outn], max_dc_pct: true}]", False),
    ("[{nets: [outp, outn], max_dc_pct: 5, aggressors: [outp]}]", False),
    ("[{nets: [outp, outn], max_dr_pct: 5, aggressors: [clk]}]", False),
    ("[{nets: [outp, outn], max_dc_pct: 5, max_pct: 5}]", False),
    ("{nets: [outp, outn]}", False),
])
def test_symmetric_nets_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"symmetric_nets: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_symmetric_nets" in out, out


@pytest.mark.parametrize("field, ok", [
    ("[{devices: [xm1, xm2], style: common_centroid}]", True),
    ("[{devices: [xm1, xm2, xm3], style: interdigitate, tol_um: 0.1}]", True),
    ("[{devices: [xm1, xm2], style: common_centroid},"
     " {devices: [xm3, xm4], style: interdigitate}]", True),
    ("[{devices: [xm1], style: common_centroid}]", False),
    ("[{devices: [xm1, XM1], style: common_centroid}]", False),
    ("[{devices: [xm1, xm2], style: common_centroid},"
     " {devices: [xm2, xm3], style: common_centroid}]", False),
    ("[{devices: [xm1, xm2]}]", False),
    ("[{devices: [xm1, xm2], style: abba}]", False),
    ("[{devices: [xm1, xm2], style: common_centroid, tol_um: 0}]", False),
    ("[{devices: [xm1, xm2], style: common_centroid, tol_um: true}]", False),
    ("[{devices: [xm1, xm2], style: common_centroid, dummies: 2}]", False),
    ("{devices: [xm1, xm2], style: common_centroid}", False),
])
def test_matching_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"matching: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_matching" in out, out


@pytest.mark.parametrize("field, ok", [
    ("all", True), ("[floating]", True), ("[grounded, floating]", True),
    ("floating", False), ("[]", False), ("[floating, floating]", False),
    ("[nope]", False), ("null", False),
])
def test_pex_fill_field(tmp_path, capsys, field, ok):
    ws = make_ws(tmp_path, GOOD_YAML + f"pex_fill: {field}\n")
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    out = capsys.readouterr().out
    if ok:
        assert code == 0, out
    else:
        assert code == 1 and "bad_pex_fill" in out, out


# ---- #159: post_layout_bounds against the pex_sim sidecar ------------------
PLB_YAML = GOOD_YAML + """\
post_layout_bounds:
  iout_ua: {min: 1.8, max: 2.2}
  tdelay:
    - {min: 1.0e-10, max: 5.0e-10, corners: [tt, ff]}
    - {min: 1.0e-10, max: 7.5e-10, corners: [ss]}
"""
PEX_MEASURES = {
    "iout_ua": {"min": 1.8, "max": 2.2},
    "tdelay": [
        {"min": 1.0e-10, "max": 7.5e-10, "corners": ["ss"]},
        {"min": 1.0e-10, "max": 5.0e-10, "corners": ["ff", "tt"]},
    ],
}


def write_pex(ws: Path, measures: dict, base: str = "layout_ref",
              stem: str = "mirror_pex_tb") -> None:
    d = ws / base
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.cir").write_text("* pex bench\n", encoding="utf-8")
    (d / f"{stem}.bounds.json").write_text(
        json.dumps({"measures": measures}), encoding="utf-8")


def lint(ws: Path, capsys) -> tuple[int, list[dict]]:
    code = check_spec_lint_ade.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out).get("violations", [])


def test_post_layout_bounds_equal_to_the_sidecar_pass(tmp_path, capsys):
    # list order and corner order inside a bound do not matter
    ws = make_ws(tmp_path, PLB_YAML)
    write_pex(ws, PEX_MEASURES)
    assert lint(ws, capsys) == (0, [])


def test_spec_with_post_layout_bounds_and_no_pex_bench_yet_passes(
        tmp_path, capsys):
    assert lint(make_ws(tmp_path, PLB_YAML), capsys) == (0, [])


def test_a_planted_floor_in_the_sidecar_fails_naming_the_measure(
        tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    write_pex(ws, {**PEX_MEASURES, "iout_ua": {"min": 1.5, "max": 2.2}})
    code, vs = lint(ws, capsys)
    assert code == 1
    assert [(v["kind"], v["refs"]) for v in vs] == [
        ("bench_bound_value_mismatch",
         ["iout_ua", "layout_ref/mirror_pex_tb.bounds.json"])]
    assert "min=1.8" in vs[0]["msg"] and "min=1.5" in vs[0]["msg"]


def test_a_changed_per_corner_bound_fails(tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    pex = json.loads(json.dumps(PEX_MEASURES))
    pex["tdelay"][0]["max"] = 9.0e-10
    write_pex(ws, pex)
    code, vs = lint(ws, capsys)
    assert code == 1
    assert [v["kind"] for v in vs] == ["bench_bound_value_mismatch"]
    assert "tdelay" in vs[0]["refs"]


def test_a_moved_corner_scope_is_a_corner_mismatch(tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    pex = json.loads(json.dumps(PEX_MEASURES))
    pex["tdelay"][0]["corners"] = ["ss", "sf"]
    write_pex(ws, pex)
    code, vs = lint(ws, capsys)
    assert [v["kind"] for v in vs] == ["bench_bound_corner_mismatch"]


def test_a_pad_condition_that_differs_is_a_pad_mismatch(tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    write_pex(ws, {**PEX_MEASURES,
                   "iout_ua": {"min": 1.8, "max": 2.2, "pad": "pad"}})
    code, vs = lint(ws, capsys)
    assert [v["kind"] for v in vs] == ["bench_bound_pad_mismatch"]


def test_a_key_on_one_side_only_fails_both_ways(tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    pex = {k: v for k, v in PEX_MEASURES.items() if k != "tdelay"}
    pex["vreset"] = {"min": 2.6}
    write_pex(ws, pex)
    code, vs = lint(ws, capsys)
    assert code == 1
    assert sorted((v["kind"], v["refs"][0]) for v in vs) == [
        ("bench_bound_no_spec_measure", "layout_ref/mirror_pex_tb.bounds.json"),
        ("measure_no_bench_bound", "layout_ref/mirror_pex_tb.bounds.json"),
    ]
    assert {r for v in vs for r in v["refs"]} >= {"tdelay", "vreset"}


def test_a_pex_sidecar_with_no_post_layout_bounds_fails(tmp_path, capsys):
    ws = make_ws(tmp_path)
    write_pex(ws, PEX_MEASURES)
    code, vs = lint(ws, capsys)
    assert [v["kind"] for v in vs] == ["no_post_layout_bounds"]


def test_a_string_bound_in_post_layout_bounds_is_refused(tmp_path, capsys):
    # YAML 1.1 reads 52e-3 (no dot) as a string, not a number
    ws = make_ws(tmp_path, GOOD_YAML + "post_layout_bounds:\n"
                 "  iout_ua: {min: 1.8, max: 22e-1}\n")
    write_pex(ws, {"iout_ua": {"min": 1.8, "max": 2.2}})
    code, vs = lint(ws, capsys)
    assert code == 1
    assert "bad_post_layout_bounds" in [v["kind"] for v in vs]


def test_the_block_in_state_json_picks_its_pex_bench(tmp_path, capsys):
    ws = make_ws(tmp_path, PLB_YAML)
    write_pex(ws, PEX_MEASURES, stem="mirror_pex_tb")
    write_pex(ws, {"other": {"min": 0}}, stem="other_pex_tb")
    (ws / "state.json").write_text(json.dumps({"block": "mirror"}),
                                   encoding="utf-8")
    assert lint(ws, capsys) == (0, [])


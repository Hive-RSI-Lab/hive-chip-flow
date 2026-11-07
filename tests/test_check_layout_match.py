"""engine/scripts/check_layout_match.py: the ade `layout_match` gate (#118).

Fast tests build unit lists by hand and run assign()/check() on them, and
parse a magic .ext excerpt from a real extraction of corpus mirror. The
slow test regenerates and extracts corpus mirror for real; its three
planted rows (AABB, end dummies removed, one unit rotated) run in
test_corpus_faults.py from the rung's manifest."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_layout_match as lm  # noqa: E402
import faults  # noqa: E402
import gate  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_check_analog_lvs import make_ws  # noqa: E402

# five lines of corpus mirror's own extraction (one unit rotated)
EXT = """\
tech gf180mcuD
scale 1000 1 0.5
device msubckt nfet_03v3 2438 0 2439 1 l=100 w=800 "vss" "vss" 200 0 "vss" 0 0 "vss" 1600 97600,1844
device msubckt nfet_03v3 1838 0 1839 1 l=100 w=800 "vss" "iref_node" 200 0 "iout" 800 97600,1844 "vss" 800 97600,1844
device msubckt nfet_03v3 1238 0 1239 1 l=100 w=800 "vss" "iref_node" 200 0 "vss" 800 97600,1844 "iref_node" 800 97600,1844
device msubckt nfet_03v3 638 0 639 1 l=100 w=800 "vss" "iref_node" 200 0 "vss" 800 97600,1844 "iout" 800 97600,1844
device msubckt nfet_03v3 38 0 39 1 l=100 w=800 "vss" "vss" 200 0 "vss" 0 0 "vss" 1600 97600,1844
"""

CIR = """\
.subckt current_mirror iref_node iout vdd vss
xmref iref_node iref_node vss vss nfet_03v3 w=4e-6 l=5e-7
xmout iout iref_node vss vss nfet_03v3 w=8e-6
+ l=5e-7
xmdum1 vss vss vss vss nfet_03v3 w=4e-6 l=5e-7
.ends
"""

GROUP = [{"devices": ["xmref", "xmout"], "style": "common_centroid",
          "tol_um": 0.05}]
SCHEM = lm.schematic_devices(CIR)


def unit(x, kind, y=0.0, axis="x", flip=False, w=4.0):
    """A placed unit: kind A = xmref, B = xmout, D = a vss dummy."""
    net = {"A": "iref_node", "B": "iout", "D": "vss"}[kind]
    terms = ["vss", net] if kind != "D" else ["vss", "vss"]
    if flip:
        terms.reverse()
    gate = "vss" if kind == "D" else "iref_node"
    return {"model": "nfet_03v3", "gate": gate, "terms": terms, "w": w,
            "l": 0.5, "centre": (x, y), "axis": axis}


def row(pattern, over=None, pitch=3.0):
    units = [unit(i * pitch, k) for i, k in enumerate(pattern)]
    for i, kw in (over or {}).items():
        units[i] = unit(i * pitch, pattern[i], **kw)
    return units


def run(units, groups=GROUP):
    lm.assign(units, SCHEM, groups)
    violations, facts = lm.check(units, groups, "current_mirror")
    return [(v["kind"], sorted(v["refs"])) for v in violations], facts, violations


def test_parse_ext_reads_units_in_um_and_terminal_order():
    units = lm.parse_ext(EXT)
    assert len(units) == 5
    u = units[1]
    assert u["model"] == "nfet_03v3" and u["gate"] == "iref_node"
    assert u["terms"] == ["iout", "vss"]
    assert (u["w"], u["l"]) == (4.0, 0.5)
    assert u["at"] == pytest.approx((9.19, 0.0))


def test_parse_ext_refuses_a_hierarchical_extraction():
    with pytest.raises(CheckError, match="hierarchical"):
        lm.parse_ext("use fet fet_0 1 0 0 0 1 0\n" + EXT)


def test_schematic_devices_reads_continuations_and_lowercases():
    assert SCHEM["xmout"] == {"kind": "fet", "model": "nfet_03v3",
                              "d": "iout", "g": "iref_node", "s": "vss"}
    assert set(SCHEM) == {"xmref", "xmout", "xmdum1"}


def test_the_corpus_row_passes():
    kinds, facts, _ = run(row("DBABD"))
    assert kinds == []
    g = facts["groups"][0]
    assert g["units"] == {"xmref": 1, "xmout": 2}
    assert g["centroid_offset_um"] == 0.0
    assert facts["dummies"] == 2


def test_aabb_reports_the_centroid_offset_in_um():
    kinds, _facts, v = run(row("DABBD"))
    assert kinds == [("match_centroid_offset", ["xmout", "xmref"])]
    assert v[0]["offset_um"] == pytest.approx(4.5)
    assert (v[0]["dx_um"], v[0]["dy_um"]) == (pytest.approx(4.5), 0.0)
    assert v[0]["tol_um"] == 0.05


def test_a_wider_tolerance_is_the_specs_call():
    group = [dict(GROUP[0], tol_um=5.0)]
    kinds, _facts, _ = run(row("DABBD"), group)
    assert kinds == []


def test_removed_dummies_leave_both_end_units_open():
    kinds, _facts, v = run(row("BAB"))
    assert kinds == [("match_end_no_dummy", ["xmout"])] * 2
    assert sorted(x["side"] for x in v) == ["the left", "the right"]


def test_a_dummy_of_another_size_is_not_a_dummy():
    units = row("DBABD")
    units[0]["w"] = 2.0
    kinds, _facts, _ = run(units)
    assert kinds == [("match_end_no_dummy", ["xmout"])]


def test_one_unit_drawn_drain_first_is_an_orientation_mismatch():
    kinds, _facts, v = run(row("DBABD", {3: {"flip": True}}))
    assert kinds == [("match_orientation", ["xmout", "xmref"])]
    assert v[0]["orientations"] == {"xmref": {"x+": 1.0},
                                    "xmout": {"x+": 0.5, "x-": 0.5}}


def test_one_unit_rotated_90_degrees_is_an_orientation_mismatch():
    kinds, _facts, _ = run(row("DBABD", {1: {"axis": "y"}}))
    assert ("match_orientation", ["xmout", "xmref"]) in kinds


def test_units_of_two_sizes():
    kinds, _facts, v = run(row("DBABD", {2: {"w": 3.0}}))
    assert ("match_unit_size", ["xmout", "xmref"]) in kinds
    hit = next(x for x in v if x["kind"] == "match_unit_size")
    assert hit["sizes"] == [{"w_um": 3.0, "l_um": 0.5}, {"w_um": 4.0, "l_um": 0.5}]


def test_a_2d_array_needs_dummies_across_the_row_too():
    units = row("DABD") + [unit(i * 3.0, k, y=6.0) for i, k in enumerate("DBAD")]
    kinds, _facts, _ = run(units)
    assert sorted(kinds) == [("match_end_no_dummy", ["xmout"])] * 2 \
        + [("match_end_no_dummy", ["xmref"])] * 2
    dummies = [unit(i * 3.0, "D", y=y) for y in (-6.0, 12.0) for i in range(4)]
    kinds, facts, _ = run(units + dummies)
    assert kinds == [] and facts["groups"][0]["centroid_offset_um"] == 0.0


def test_interdigitate_allows_runs_up_to_the_unit_ratio():
    group = [{"devices": ["xmref", "xmout"], "style": "interdigitate",
              "tol_um": 0.05}]
    assert run(row("DABABD"), group)[0] == []
    kinds, _facts, v = run(row("DAABBD"), group)
    assert kinds == [("match_not_interdigitated", ["xmout", "xmref"])]
    assert (v[0]["longest_run"], v[0]["allowed"]) == (2, 1)
    # 2:4 allows two of xmout in a row, not four
    assert run(row("DBABBABD"), group)[0] == []
    kinds, _facts, v = run(row("DAABBBBD"), group)
    assert kinds == [("match_not_interdigitated", ["xmout", "xmref"])]
    assert (v[0]["longest_run"], v[0]["allowed"]) == (4, 2)


def test_a_device_with_no_unit():
    kinds, _facts, _ = run(row("DBBD"))
    assert kinds == [("match_no_units", ["xmref"])]


def test_a_group_naming_no_netlist_device_refuses():
    with pytest.raises(CheckError, match="xmnope"):
        run(row("DBABD"), [{"devices": ["xmref", "xmnope"],
                            "style": "common_centroid", "tol_um": 0.05}])


def test_a_unit_fitting_two_groups_refuses():
    cir = CIR.replace(".ends", "xmref2 iref_node iref_node vss vss nfet_03v3 "
                               "w=4e-6 l=5e-7\nxmout2 iout iref_node vss vss "
                               "nfet_03v3 w=8e-6 l=5e-7\n.ends")
    groups = [{"devices": ["xmref", "xmout"], "style": "common_centroid", "tol_um": 0.05},
              {"devices": ["xmref2", "xmout2"], "style": "common_centroid", "tol_um": 0.05}]
    units = row("DBABD")
    with pytest.raises(CheckError, match="two matching groups"):
        lm.assign(units, lm.schematic_devices(cir), groups)


def _ws(tmp_path, spec_extra=""):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "block: current_mirror\nmeasures: []\n" + spec_extra, encoding="utf-8")
    return ws


def test_no_matching_is_not_applicable(tmp_path, capsys):
    import json

    ws = _ws(tmp_path)
    assert lm.main(["--workspace", str(ws)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applicable"] is False and out["violations"] == []
    assert lm.not_applicable_at_release(ws).startswith("not applicable")


def test_matching_owes_the_gate_at_release(tmp_path):
    ws = _ws(tmp_path, "matching:\n  - {devices: [a, b], style: common_centroid}\n")
    assert lm.not_applicable_at_release(ws) is None
    assert lm.not_applicable_at_release(tmp_path / "missing") is None


def test_a_malformed_matching_refuses(tmp_path, capsys):
    ws = _ws(tmp_path, "matching: [{devices: [a], style: common_centroid}]\n")
    assert lm.main(["--workspace", str(ws)]) == 2
    assert "malformed" in capsys.readouterr().out
    assert lm.not_applicable_at_release(ws) is None


@pytest.mark.slow
def test_mirror_passes_the_real_layout_match(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    gates = gate.load_gates(ENGINE / "reference" / "gates.yaml")
    report, result = faults.run_gate(ws, "layout_match", gates, "ade")
    assert result["status"] == "pass", report
    g = report["groups"][0]
    assert g["units"] == {"xmref": 1, "xmout": 2}
    assert g["centroid_offset_um"] == 0.0
    assert report["dummies"] == 2


# resistor units: two lines of r2r_dac's own extraction, and a ladder
# excerpt (one 2R branch's halves, a trim, a dummy, a buffer to skip)
REXT = """\
tech gf180mcuD
scale 1000 1 0.5
device rsubckt ppolyf_u 0 7272 1 7273 l=30000 w=400 "vss" "a_0_7272#" 0 0 "a_n132_7272#" 400 52800,1064 "vss" 400 52800,1064
device rsubckt ppolyf_u 0 8872 1 8873 l=29672 w=400 "vss" "a_0_8872#" 0 0 "a_n132_8072#" 400 52800,1064 "a_29672_8872#" 400 52800,1064
"""

RCIR = """\
.subckt ladder d0 vout vdd vss
xd0 code0 d0 vdd vss vdd vss buf_20
xra d0 mb vss ppolyf_u r_width=2e-6 r_length=150e-6
xrs vout mb vss ppolyf_u r_width=2e-6 r_length=150e-6
xrt mb nt vss ppolyf_u r_width=2e-6 r_length=148.36e-6
xrdum vss vss vss ppolyf_u r_width=2e-6 r_length=150e-6
.ends
"""
RSCHEM = lm.schematic_devices(RCIR)
RGROUP = [{"devices": ["xra", "xrs"], "style": "interdigitate"}]


def res_unit(y, kind, flip=False):
    """A resistor in a column at 4 um pitch: A = xra, S = xrs, T = the
    148.36 um trim, D = a vss dummy."""
    terms = {"A": ["d0", "mb"], "S": ["mb", "vout"], "T": ["mb", "nt"],
             "D": ["vss", "vss"]}[kind]
    if flip:
        terms = terms[::-1]
    return {"kind": "res", "model": "ppolyf_u", "gate": None, "terms": terms,
            "w": 2.0, "l": 148.36 if kind == "T" else 150.0,
            "centre": (75.0, y), "axis": "x"}


def res_run(pattern, extra=()):
    units = [res_unit(i * 4.0, k) for i, k in enumerate(pattern)] + list(extra)
    lm.assign(units, RSCHEM, RGROUP)
    violations, facts = lm.check(units, RGROUP, "ladder")
    return [(v["kind"], sorted(v["refs"])) for v in violations], facts, units


def test_parse_ext_reads_resistor_units():
    units = lm.parse_ext(REXT)
    assert [u["kind"] for u in units] == ["res", "res"]
    u, t = units
    assert u["gate"] is None and u["terms"] == ["a_n132_7272#", "vss"]
    assert (u["w"], u["l"]) == (2.0, 150.0)
    assert u["at"] == pytest.approx((0.0, 36.36))
    assert t["l"] == pytest.approx(148.36)


def test_schematic_devices_reads_resistors_and_skips_buffers():
    assert RSCHEM["xra"] == {"kind": "res", "model": "ppolyf_u", "a": "d0",
                             "b": "mb", "sub": "vss"}
    assert set(RSCHEM) == {"xra", "xrs", "xrt", "xrdum"}


def test_a_resistor_row_with_end_dummies_passes():
    kinds, facts, units = res_run("DASD")
    assert kinds == []
    assert [u["dev"] for u in units] == [None, "xra", "xrs", None]
    assert facts["dummies"] == 2


def test_a_resistor_fits_either_way_round():
    flipped = [res_unit(-4.0, "D"), res_unit(0.0, "A", flip=True),
               res_unit(4.0, "S"), res_unit(8.0, "D")]
    lm.assign(flipped, RSCHEM, RGROUP)
    assert [u["dev"] for u in flipped] == [None, "xra", "xrs", None]
    violations, _ = lm.check(flipped, RGROUP, "ladder")
    assert violations == []  # a resistor has no current direction


def test_a_resistor_row_without_dummies_fails_at_both_ends():
    kinds, _facts, _ = res_run("AS")
    assert sorted(kinds) == [("match_end_no_dummy", ["xra"]),
                             ("match_end_no_dummy", ["xrs"])]


def test_a_trim_length_neighbour_is_not_a_dummy():
    kinds, facts, _ = res_run("TAST")
    assert sorted(kinds) == [("match_end_no_dummy", ["xra"]),
                             ("match_end_no_dummy", ["xrs"])]
    assert facts["dummies"] == 0


def test_dummies_count_only_devices_equal_to_a_matched_unit():
    buffer_fet = {"kind": "fet", "model": "nfet_03v3", "gate": "code0",
                  "terms": ["vss", "d0"], "w": 4.0, "l": 0.5,
                  "centre": (10.0, -20.0), "axis": "x"}
    _kinds, facts, _ = res_run("DASD", extra=[buffer_fet])
    assert facts["dummies"] == 2

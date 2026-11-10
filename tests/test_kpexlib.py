"""engine/lib/kpexlib.py: klayout_pex 2.5D as pex_sim's second extractor
(issue #116). The comparison, the .ext readers and the GDS kpex is handed
are unit tests; the known answer (metal2 over metal1, eps*A/t) runs both
real extractors and is slow."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import kpexlib  # noqa: E402
import netlistlib  # noqa: E402

# the real .ext line shapes (magic 8.3, GF180): node name, R, C, x, y, type,
# then one area/perimeter pair per resist class, class 0 being nwell
EXT = (
    'substrate "vss" 32442 0 -1834 -774 m1 0 0 19409840 17880\n'
    'node "vdd" 3594 15765.1 1834 716 m1 5126784 10712 0 0 80000 1600\n'
    'node "inn" 145 1213.26 1200 -3386 m1 0 0 0 0 0 0\n'
    'node "a_n1350_n934#" 10 50 -1350 -934 p 0 0 0 0 0 0\n'
)


def test_substrate_and_well_nets_read_from_the_ext():
    assert kpexlib.substrate_net(EXT) == "vss"
    assert kpexlib.well_nets(EXT) == {"vdd"}


def test_no_substrate_line_is_none():
    assert kpexlib.substrate_net('node "a" 1 2 0 0 m1 0 0\n') is None


def test_rename_substrate_is_whole_word():
    text = "C1 VSUBS a 1f\nC2 VSUBSX a 1f\n"
    assert kpexlib.rename_substrate(text, "vss") == \
        "C1 vss a 1f\nC2 VSUBSX a 1f\n"


@pytest.mark.parametrize("a, b, want", [
    (1.0, 1.2, 0.2), (1.2, 1.0, 0.2), (0.0, 0.0, 0.0), (0.0, 1.0, None),
    (1.0, 0.0, None)])
def test_off_is_over_the_smaller(a, b, want):
    got = kpexlib._off(a, b)
    assert got == pytest.approx(want) if want is not None else got is None


def _p(c, **c_to):
    return {"c_ff": c, "c_to": c_to}


def test_compare_within_tolerance_is_no_disagreement():
    r = kpexlib.compare({"a": _p(1.0, b=0.2), "b": _p(2.0, a=0.2)},
                        {"a": _p(1.1, b=0.21), "b": _p(1.9, a=0.21)},
                        {"a", "b"}, {"a", "b"}, {})
    assert r["disagree"] == []
    assert [x["net"] for x in r["nets"]] == ["a", "b"]
    assert r["couplings"][0]["nets"] == ["a", "b"]
    assert r["couplings"][0]["off"] == pytest.approx(0.05)


def test_compare_over_tolerance_names_the_net_and_both_values():
    r = kpexlib.compare({"a": _p(1.0)}, {"a": _p(1.3)}, {"a"}, set(), {})
    assert r["disagree"] == [
        {"net": "a", "magic_ff": 1.0, "kpex_ff": 1.3, "off": 0.3}]


def test_compare_a_net_only_one_extraction_names_disagrees():
    r = kpexlib.compare({"a": _p(1.0)}, {}, {"a"}, set(), {})
    assert r["disagree"][0]["why"] == "only magic names it"
    r = kpexlib.compare({}, {"a": _p(1.0)}, {"a"}, set(), {})
    assert r["disagree"][0]["why"] == "only kpex names it"


def test_compare_lists_the_nets_it_does_not_compare():
    r = kpexlib.compare({"w": _p(1.0)}, {"w": _p(9.0)}, {"w", "x"}, {"w"},
                        {"w": "well"})
    assert r["disagree"] == [] and r["nets"] == [] and r["couplings"] == []
    assert {x["net"]: x["reason"][:7] for x in r["not_compared"]} == {
        "w": "well", "x": "neither"}


def test_compare_a_coupling_below_the_floor_in_both_is_not_compared():
    r = kpexlib.compare({"a": _p(1.0, b=0.01), "b": _p(1.0)},
                        {"a": _p(1.0, b=0.04), "b": _p(1.0)},
                        {"a", "b"}, {"a", "b"}, {})
    assert r["disagree"] == [] and r["couplings"][0]["off"] is None


def test_compare_a_coupling_over_tolerance_disagrees():
    r = kpexlib.compare({"a": _p(1.0, b=0.10), "b": _p(1.0, a=0.10)},
                        {"a": _p(1.0, b=0.15), "b": _p(1.0, a=0.15)},
                        {"a", "b"}, {"a", "b"}, {})
    assert [x["nets"] for x in r["disagree"]] == [["a", "b"]]


def test_spice_number_reads_atto():
    assert netlistlib.spice_number("227.552a") == pytest.approx(227.552e-18)


# ---- the GDS kpex is handed --------------------------------------------------
def _gds(path, shapes, texts=()):
    import klayout.db as kdb
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("blk")
    for (layer, dt), box in shapes:
        top.shapes(ly.layer(layer, dt)).insert(kdb.Box(*box))
    for (layer, dt), name, x, y in texts:
        top.shapes(ly.layer(layer, dt)).insert(kdb.Text(name, kdb.Trans(x, y)))
    ly.write(str(path))
    return path


def _read(path):
    import klayout.db as kdb
    ly = kdb.Layout()
    ly.read(str(path))
    top = ly.cell("blk")

    def layer(l, d):
        li = ly.find_layer(l, d)
        return [] if li is None else list(top.shapes(li).each())
    return layer


def test_prepare_gds_cuts_a_metal_resistor_and_copies_labels(tmp_path):
    pytest.importorskip("klayout.db")
    src = _gds(tmp_path / "a.gds",
               [((34, 0), (0, 0, 10000, 500)), ((110, 11), (3000, 0, 7000, 500)),
                ((110, 5), (3000, 0, 7000, 500))],
               [((34, 0), "a", 100, 100), ((34, 0), "b", 9900, 100)])
    dst = tmp_path / "k.gds"
    assert kpexlib.prepare_gds(src, dst, "blk") == {"res_cuts": 1}
    layer = _read(dst)
    metal = [s for s in layer(34, 0) if not s.is_text()]
    assert len(metal) == 2  # the body split in two at its middle
    assert sorted(s.text.string for s in layer(34, 10) if s.is_text()) == \
        ["a", "b"]
    assert layer(110, 11) == [] and layer(110, 5) == []


def test_prepare_gds_cuts_a_poly_resistor(tmp_path):
    pytest.importorskip("klayout.db")
    # a ppolyf_u-style body: res_mk 110/5 over poly2 alone, poly heads out
    # past both ends
    src = _gds(tmp_path / "a.gds",
               [((30, 0), (0, 0, 10000, 2000)), ((110, 5), (1000, 0, 9000, 2000))],
               [((30, 0), "a", 100, 100), ((30, 0), "b", 9900, 100)])
    dst = tmp_path / "k.gds"
    assert kpexlib.prepare_gds(src, dst, "blk") == {"res_cuts": 1}
    layer = _read(dst)
    poly = [s for s in layer(30, 0) if not s.is_text()]
    assert len(poly) == 2  # the body split in two at its middle
    assert sorted(s.text.string for s in layer(30, 10) if s.is_text()) == \
        ["a", "b"]
    assert layer(110, 5) == []


def test_prepare_gds_takes_a_high_rs_poly_resistor(tmp_path):
    pytest.importorskip("klayout.db")
    # a ppolyf_u_1k-style body: the ppolyf_u one inside the 62/0 implant
    src = _gds(tmp_path / "a.gds",
               [((30, 0), (0, 0, 10000, 2000)), ((110, 5), (1000, 0, 9000, 2000)),
                ((62, 0), (-500, -500, 10500, 2500))])
    dst = tmp_path / "k.gds"
    assert kpexlib.prepare_gds(src, dst, "blk") == {"res_cuts": 1}
    layer = _read(dst)
    assert len(layer(30, 0)) == 2
    assert layer(62, 0) == [] and layer(110, 5) == []


@pytest.mark.parametrize("shape", [((62, 0), (0, 0, 1000, 1000)),
                                   # res_mk over no poly: a diffusion
                                   # resistor, say
                                   ((110, 5), (0, 0, 1000, 1000))])
def test_prepare_gds_refuses_a_resistor_kpex_reads_otherwise(tmp_path, shape):
    pytest.importorskip("klayout.db")
    src = _gds(tmp_path / "a.gds", [((34, 0), (0, 0, 5000, 500)), shape])
    with pytest.raises(kpexlib.KpexError):
        kpexlib.prepare_gds(src, tmp_path / "k.gds", "blk")


def test_prepare_gds_refuses_a_missing_cell(tmp_path):
    pytest.importorskip("klayout.db")
    src = _gds(tmp_path / "a.gds", [((34, 0), (0, 0, 5000, 500))])
    with pytest.raises(kpexlib.KpexError, match="no cell"):
        kpexlib.prepare_gds(src, tmp_path / "k.gds", "other")


def _tech(m5_name="MetalTop"):
    """The parts of kpex 0.4's GF180 tech json kpex_runner patches."""
    def comp(name, gds, orig):
        return {"layer_info": {"name": name,
                               "drw_gds_pair": {"layer": gds, "datatype": 0}},
                "original_layer_name": orig}
    return {
        "layers": [],
        "lvs_computed_layers": [comp("poly_con", 30, "Poly2"),
                                comp("metal5_con", 81, m5_name)],
        "process_stack": {"layers": [{"name": "Poly2", "metal_layer": {}},
                                     {"name": "Metal5", "metal_layer": {}}]},
        "process_parasitics": {"capacitance": {
            "substrates": [], "sidewalls": [],
            "overlaps": [{"top_layer_name": "Metal1",
                          "bottom_layer_name": "Poly2", "capacitance": 1.0}],
            "sideoverlaps": [{"in_layer_name": "Metal1",
                              "out_layer_name": "Poly2",
                              "capacitance": 1.0}]}},
    }


def test_kpex_runner_files_metal5_under_the_name_its_cap_tables_use():
    # and as metal5 off the MIM top plate: over it, metal5 on the metal4
    # bottom plate is the MIM's own C, which the device already carries
    import kpex_runner
    t = kpex_runner.patched_tech(_tech())
    names = [x["layer_info"]["name"] for x in t["lvs_computed_layers"]]
    assert "metal5_con" not in names
    m5 = [x for x in t["lvs_computed_layers"]
          if x["layer_info"]["name"] == "metal5_ncap"]
    assert [x["original_layer_name"] for x in m5] == ["Metal5"]


def test_kpex_runner_stacks_the_gate_poly_just_below_poly2():
    # kpex shields a fringe only with the layers between its two ends in
    # the stack: a Poly2 edge's fringe to the substrate has to pass PolyGate,
    # or the wire/gate seam the split draws fringes over the channel (2 fF
    # on sensor_counted's osc_en that magic does not have)
    import kpex_runner
    t = kpex_runner.patched_tech(_tech())
    names = [x["name"] for x in t["process_stack"]["layers"]]
    assert names == ["PolyGate", "Poly2", "Metal5"]


def test_kpex_runner_refuses_a_tech_without_metal5_con():
    import kpex_runner
    t = _tech()
    t["lvs_computed_layers"].pop()
    with pytest.raises(SystemExit) as e:
        kpex_runner.patched_tech(t)
    assert e.value.code == 3


# ---- the known answer --------------------------------------------------------
EPS_OVER_T = 0.05903  # fF/um^2: GF180 metal2 over metal1, eps*A/t


@pytest.mark.slow
def test_metal2_over_metal1_plate_matches_eps_a_over_t_in_both(tmp_path):
    """A 40x40 um metal2 plate over a metal1 plate 2 um larger on each side:
    each extractor's top-to-bottom coupling is within TOLERANCE of eps*A/t
    (the edge fringe puts both a few % over)."""
    import check_pex_sim
    import layoutlib
    side = 40.0
    n = int(side * 1000)
    gds = _gds(tmp_path / "plate.gds",
               [((34, 0), (-2000, -2000, n + 2000, n + 2000)),
                ((36, 0), (0, 0, n, n))],
               [((34, 0), "bot", 1000, 1000), ((36, 0), "top", 1000, 1000)])
    want = EPS_OVER_T * side * side
    (tmp_path / "m").mkdir()
    magic, _ = layoutlib.run_magic_extract(tmp_path / "m", gds, "blk",
                                           parasitics=True)
    kpex, _ = kpexlib.run_kpex(tmp_path / "k", gds, "blk")
    got = {}
    for name, path in (("magic", magic), ("kpex", kpex)):
        text = path.read_text(encoding="utf-8")
        if name == "kpex":
            text = kpexlib.rename_substrate(text, "sub")
        p = check_pex_sim.net_parasitics(text, {"top", "bot"})
        got[name] = p["top"]["c_to"]["bot"]
    for name, c in got.items():
        assert kpexlib._off(c, want) <= kpexlib.TOLERANCE, (name, c, want)


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


# each kpex release's marks, as its GF180 deck and tech carry them: 0.4
# (IIC 2026.09) lists the deck's layer names and calls the poly poly_con on
# MetalTop's metal5; 0.6 (IIC 2026.10) names them itself and calls it
# poly2_con, with metal5 on Metal5
LVS_04 = "  poly2_con pgate_lv_base\n  metal5_con via4_n_cap\n"
LVS_06 = "# %include rule_decks/auto_name_layers.rb\nauto_name_layers!(binding)\n"


def _tech06():
    t = _tech("Metal5")
    t["lvs_computed_layers"][0]["layer_info"]["name"] = "poly2_con"
    return t


@pytest.mark.parametrize("lvs,tech,want", [
    (LVS_04, _tech(), "0.4"), (LVS_06, _tech06(), "0.6")],
    ids=["kpex-0.4", "kpex-0.6"])
def test_kpex_runner_tells_the_release(lvs, tech, want):
    import kpex_runner
    assert kpex_runner.release(lvs, tech) == want


@pytest.mark.parametrize("lvs,tech", [
    ("", _tech()),                # 0.4 tech, a deck of neither
    (LVS_06, _tech()),            # 0.6 deck, 0.4 tech
    (LVS_04 + LVS_06, _tech()),   # both decks' marks
    (LVS_06, _tech("Metal5")),    # 0.6 deck, poly_con with Metal5
    ("", {"lvs_computed_layers": []}),
], ids=["no-deck-mark", "mixed", "both-deck-marks", "half-0.6", "empty"])
def test_kpex_runner_refuses_a_deck_of_no_single_release(lvs, tech):
    import kpex_runner
    with pytest.raises(SystemExit) as e:
        kpex_runner.release(lvs, tech)
    assert e.value.code == 3


def test_kpex_runner_patch_edits_its_anchor(tmp_path):
    import kpex_runner
    (tmp_path / "d.lvs").write_text("a\nold\nb\n")
    kpex_runner.patch(str(tmp_path), "d.lvs", "old\n", "old\nnew\n")
    assert (tmp_path / "d.lvs").read_text() == "a\nold\nnew\nb\n"


@pytest.mark.parametrize("text", ["a\nb\n", "old\nold\n"],
                         ids=["gone", "twice"])
def test_kpex_runner_patch_refuses_an_anchor_not_there_once(tmp_path, text):
    import kpex_runner
    (tmp_path / "d.lvs").write_text(text)
    with pytest.raises(SystemExit) as e:
        kpex_runner.patch(str(tmp_path), "d.lvs", "old\n", "new\n")
    assert e.value.code == 3
    assert (tmp_path / "d.lvs").read_text() == text


def test_kpex_runner_leaves_mim_c_to_the_device_on_0_6():
    import kpex_runner
    args = ["--pdk", "gf180mcuD", "--2.5D"]
    assert kpex_runner.upstream_args(args) == [*args, "--blackbox", "true"]
    chosen = [*args, "--blackbox", "false"]
    assert kpex_runner.upstream_args(chosen) == chosen


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



# ---- kpex 0.6: the gate-poly share magic's tech leaves out -------------------
def _cpx():
    import check_pex_sim
    return check_pex_sim


def _pk(**c_to):
    out = {}
    for pair, ff in c_to.items():
        a, b = pair.split("__")
        for x, y in ((a, b), (b, a)):
            out.setdefault(x, {"c_ff": 0.0, "c_to": {}})
            out[x]["c_to"][y] = ff
            out[x]["c_ff"] += ff
    return out


CLK_OUTN = {"pairs": [{"nets": ["clk", "outn"], "ff": 0.0305, "terms": {}}]}


def test_clk_outn_agrees_once_the_gate_poly_share_is_off():
    """The comparator's clk-outn: magic 0.068 fF, kpex 0.6 0.103 fF, of which
    0.0305 fF is gate-poly sidewall and fringe magic's tech does not have."""
    magic = _pk(clk__outn=0.068)
    raw = kpexlib.compare(magic, _pk(clk__outn=0.103), [], ["clk", "outn"], {})
    assert [r["nets"] for r in raw["disagree"]] == [["clk", "outn"]]
    pk = _pk(clk__outn=0.103)
    taken = _cpx().subtract_gate_poly(pk, CLK_OUTN, "vss")
    assert taken == {"clk": {"outn": 0.0305}, "outn": {"clk": 0.0305}}
    got = kpexlib.compare(magic, pk, [], ["clk", "outn"], {})
    assert got["disagree"] == []
    assert got["couplings"][0]["kpex_ff"] == pytest.approx(0.0725)


def test_a_gap_in_a_counted_term_still_fails_after_the_gate_share():
    # 0.05 fF more metal1-to-gate coupling (counted by both) in kpex
    pk = _pk(clk__outn=0.153)
    _cpx().subtract_gate_poly(pk, CLK_OUTN, "vss")
    got = kpexlib.compare(_pk(clk__outn=0.068), pk, [], ["clk", "outn"], {})
    assert [r["nets"] for r in got["disagree"]] == [["clk", "outn"]]


def test_the_gate_share_to_vsubs_comes_off_the_substrate_net():
    pk = _pk(a__vss=1.0)
    _cpx().subtract_gate_poly(
        pk, {"pairs": [{"nets": ["VSUBS", "A"], "ff": 0.1}]}, "vss")
    assert pk["a"]["c_to"]["vss"] == pytest.approx(0.9)
    assert pk["vss"]["c_ff"] == pytest.approx(0.9)


def test_gate_poly_excluded_reads_the_file_and_refuses_without_one(tmp_path):
    import json
    with pytest.raises(kpexlib.KpexError, match="wrote no"):
        kpexlib.gate_poly_excluded(tmp_path)
    (tmp_path / "deck").mkdir()
    f = tmp_path / "deck" / "gate_poly_excluded.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(kpexlib.KpexError, match="unreadable"):
        kpexlib.gate_poly_excluded(tmp_path)
    f.write_text(json.dumps({"pairs": [{"nets": ["a"], "ff": 1}]}),
                 encoding="utf-8")
    with pytest.raises(kpexlib.KpexError, match="unreadable"):
        kpexlib.gate_poly_excluded(tmp_path)
    f.write_text(json.dumps(CLK_OUTN), encoding="utf-8")
    assert kpexlib.gate_poly_excluded(tmp_path) == CLK_OUTN


def _fake_kpex06(monkeypatch, drop=None):
    """Stand-ins for kpex 0.6's two hooked classes, as small as the hooks
    need: the cap adders keep what they get."""
    import types
    got = []

    class CellExtractionResults:
        def add_sidewall_cap(self, cap):
            got.append(cap)

        def add_sideoverlap_cap(self, cap):
            got.append(cap)

        def add_overlap_cap(self, cap):
            got.append(cap)

    class RCX25Extractor:
        def __init__(self, channels):
            self.pex_context = types.SimpleNamespace(mos_channels=channels)

        def extract_cell(self, results, report):
            return "extracted"
    if drop:
        delattr(CellExtractionResults, drop)
    er = types.ModuleType("klayout_pex.rcx25.extraction_results")
    er.CellExtractionResults = CellExtractionResults
    ex = types.ModuleType("klayout_pex.rcx25.extractor")
    ex.RCX25Extractor = RCX25Extractor
    pkg = types.ModuleType("klayout_pex.rcx25")
    pkg.extraction_results, pkg.extractor = er, ex
    for name, mod in (("klayout_pex", types.ModuleType("klayout_pex")),
                      ("klayout_pex.rcx25", pkg),
                      ("klayout_pex.rcx25.extraction_results", er),
                      ("klayout_pex.rcx25.extractor", ex)):
        monkeypatch.setitem(sys.modules, name, mod)
    return CellExtractionResults, RCX25Extractor, got


def _cap(key, value, **geo):
    import types
    return types.SimpleNamespace(key=types.SimpleNamespace(**key),
                                 cap_value=value, **geo)


def test_kpex_runner_measures_the_gate_share_of_each_poly_cap(monkeypatch):
    import klayout.db as kdb
    import kpex_runner
    Results, Extractor, got = _fake_kpex06(monkeypatch)
    share = kpex_runner.GatePolyShare()
    share.install()
    # one channel, x 0..1000, y 0..2000 (DBU)
    assert Extractor(kdb.Region(kdb.Box(0, 0, 1000, 2000))).extract_cell(
        None, None) == "extracted"
    r = Results()
    E = kdb.Edge
    # a gate edge half along the channel (y 1000..3000 on x=1000)
    r.add_sideoverlap_cap(_cap(
        {"layer_inside": "Poly2", "net_inside": "clk",
         "layer_outside": "Metal1", "net_outside": "outn"}, 0.02,
        inside_edge=E(1000, 1000, 1000, 3000), outside_edge=None))
    # a sidewall whose far side runs the whole channel: all of it
    r.add_sidewall_cap(_cap(
        {"layer": "Poly2", "net1": "x", "net2": "clk"}, 0.01,
        inside_edge=E(1500, 0, 1500, 2000), outside_edge=E(1000, 0, 1000, 2000)))
    # metal1's fringe onto the gate below is counted by magic: not taken
    r.add_sideoverlap_cap(_cap(
        {"layer_inside": "Metal1", "net_inside": "outn",
         "layer_outside": "Poly2", "net_outside": "clk"}, 0.05,
        inside_edge=E(0, 0, 0, 2000), outside_edge=None))
    # poly over the channel's well: a quarter of its area is channel
    r.add_overlap_cap(_cap(
        {"layer_top": "Poly2", "net_top": "clk", "layer_bot": "nwell",
         "net_bot": "vdd"}, 0.04,
        area=kdb.Region(kdb.Box(-1000, 0, 1000, 4000))))
    assert len(got) == 4 and share.errors == []
    rep = share.report("0.6")
    by = {tuple(p["nets"]): p for p in rep["pairs"]}
    assert by[("clk", "outn")]["ff"] == pytest.approx(0.01, rel=1e-2)
    assert by[("clk", "x")]["ff"] == pytest.approx(0.01, rel=1e-2)
    assert by[("clk", "vdd")]["ff"] == pytest.approx(0.01, rel=1e-2)
    assert set(by) == {("clk", "outn"), ("clk", "x"), ("clk", "vdd")}


def test_kpex_runner_records_a_cap_with_no_edge_as_an_error(monkeypatch):
    import klayout.db as kdb
    import kpex_runner
    Results, Extractor, _ = _fake_kpex06(monkeypatch)
    share = kpex_runner.GatePolyShare()
    share.install()
    Extractor(kdb.Region(kdb.Box(0, 0, 10, 10))).extract_cell(None, None)
    Results().add_sideoverlap_cap(_cap(
        {"layer_inside": "Poly2", "net_inside": "a", "layer_outside": "Metal1",
         "net_outside": "b"}, 0.01, inside_edge=None, outside_edge=None))
    assert share.errors and "no edge" in share.errors[0]


def test_kpex_runner_refuses_a_kpex_without_its_hook_points(monkeypatch):
    import kpex_runner
    _fake_kpex06(monkeypatch, drop="add_overlap_cap")
    with pytest.raises(SystemExit) as e:
        kpex_runner.GatePolyShare().install()
    assert e.value.code == 3

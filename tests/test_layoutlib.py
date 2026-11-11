"""engine/lib/layoutlib.py: pure-Python pieces (docs/design.md 1.5, 5,
"### M9.") - the klayout .lyrdb parser, the DRC layer-name heuristic, the
ngspice print-line parser, and the refusal paths that do not need the eda
image at all. Real-tool coverage (run_klayout_drc, run_magic_extract,
run_netgen_lvs, run_ngspice actually launching bin/eda) lives in
tests/test_check_analog_drc.py etc., marked `slow`."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))

import layoutlib  # noqa: E402


def test_drc_layer_of_known_prefixes():
    assert layoutlib.drc_layer_of("DF.14_LV") == "comp"
    assert layoutlib.drc_layer_of("CO.10") == "contact"
    assert layoutlib.drc_layer_of("PL.6") == "poly2"
    assert layoutlib.drc_layer_of("NP.5a") == "nplus"
    assert layoutlib.drc_layer_of("PP.3d") == "pplus"
    assert layoutlib.drc_layer_of("NW.2b") == "nwell"
    assert layoutlib.drc_layer_of("metal1_OFFGRID") == "offgrid"
    assert layoutlib.drc_layer_of("MET1.1") == "metal1"
    assert layoutlib.drc_layer_of("something_unknown") == "unknown"


RDB_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <categories>
  <category>
   <name>'DF.14_LV'</name>
   <description>DF.14_LV : some rule text</description>
  </category>
 </categories>
 <cells>
  <cell><name>'mirror'</name></cell>
 </cells>
 <items>
  <item>
   <category>'DF.14_LV'</category>
   <cell>'mirror'</cell>
   <values>
    <value>polygon: (0,0;0,1;1,1;1,0)</value>
   </values>
  </item>
 </items>
</report-database>
"""


def test_parse_drc_rdb_reads_items_and_categories(tmp_path):
    rdb = tmp_path / "report.lyrdb"
    rdb.write_text(RDB_TEMPLATE, encoding="utf-8")
    items = layoutlib.parse_drc_rdb(rdb)
    assert len(items) == 1
    assert items[0]["category"] == "DF.14_LV"
    assert items[0]["cell"] == "mirror"
    assert items[0]["description"] == "DF.14_LV : some rule text"
    assert items[0]["value"].startswith("polygon:")


EMPTY_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <categories>
  <category>
   <name>'DF.14_LV'</name>
   <description>DF.14_LV : some rule text</description>
  </category>
 </categories>
 <cells>
  <cell><name>'mirror'</name></cell>
 </cells>
 <items>
 </items>
</report-database>
"""


def test_parse_drc_rdb_zero_items_is_a_real_clean_report(tmp_path):
    rdb = tmp_path / "clean.lyrdb"
    rdb.write_text(EMPTY_RDB, encoding="utf-8")
    assert layoutlib.parse_drc_rdb(rdb) == []


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


PNP_FIXED = (" device msubcircuit pnp_10p00x10p00 pnp *pdiff pwell,space/w"
             " a1>99.0 a1<101.0\n")
PNP_TYPO = (" device msubcircuit pnp_10p00x00p42 pnp pwell,space/w *pdiff error"
            " a2>4.1 a2<4.3\n device msubcircuit pnp_10p00x10p00 pnp"
            " pwell,space/w *pdiff error a2>99.0 a2<101.0\n")


# the well diode and Schottky fixes as a PDK that already has them
DIODES_FIXED = (" device ndiode diode_dw2ps dnwdiode space/w,pwell\n"
                " device pdiode diode_pw2dw *pwdiode dnwell\n"
                " device ndiode diode_nw2ps_03v3 nwdiode space/w,pwell\n"
                " device ndiode diode_nw2ps_06v0 mvnwdiode space/w,pwell\n"
                + layoutlib._NSD_FIXED + layoutlib._SC_ANODE_FIXED)
# the stock tech's lines the diode fixes edit
DIODES_STOCK = ("".join(old for old, _ in layoutlib._WELL_DIODE_EDITS)
                + layoutlib._NSD_RULE + layoutlib._SC_ANODE)
# the P-varactor fix as a PDK that has it, and the stock lines it edits
PVAR_FIXED = layoutlib._PFET_EXPAND_FIXED + layoutlib._MVPFET_EXPAND_FIXED
PVAR_STOCK = layoutlib._PFET_EXPAND + layoutlib._MVPFET_EXPAND


def fake_pdk(tmp_path, monkeypatch, tech=" calma MET1RES 110 11\n",
             pnp=PNP_FIXED, diodes=DIODES_FIXED, pvar=PVAR_FIXED):
    pdk = tmp_path / "pdk" / "gf180mcuD"
    deck = pdk / layoutlib.DRC_DECK_REL
    deck.parent.mkdir(parents=True)
    deck.write_text("# fake deck\n", encoding="utf-8")
    magic = pdk / "libs.tech" / "magic" / "gf180mcuD.tech"
    magic.parent.mkdir(parents=True)
    magic.write_text(tech + pnp + diodes + pvar + POLY_TECH,
                     encoding="utf-8")
    models = pdk / "libs.tech" / "ngspice" / "sm141064.spice"
    models.parent.mkdir(parents=True)
    models.write_text(POLY_MODEL, encoding="utf-8")
    setup = pdk / "libs.tech" / "netgen" / "gf180mcuD_setup.tcl"
    setup.parent.mkdir(parents=True)
    setup.write_text("# fake setup\n", encoding="utf-8")
    monkeypatch.setattr(layoutlib, "pdk_root", lambda: pdk)
    return pdk


def test_run_eda_missing_launcher_is_a_layout_error(tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "EDA_BIN", tmp_path / "no-such-eda")
    with pytest.raises(layoutlib.LayoutError, match="failed to launch"):
        layoutlib.run_eda(["klayout", "-v"])


def test_run_klayout_drc_refuses_when_no_drc_result_banner(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        "some unrelated klayout output, no banner"))
    with pytest.raises(layoutlib.LayoutError, match="DRC RESULT"):
        layoutlib.run_klayout_drc(tmp_path / "x.gds", "top", tmp_path / "out.lyrdb")


def test_run_klayout_drc_refuses_when_deck_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "pdk_root", lambda: tmp_path)
    with pytest.raises(layoutlib.LayoutError, match="no klayout GF180 DRC deck"):
        layoutlib.run_klayout_drc(tmp_path / "x.gds", "top", tmp_path / "out.lyrdb")


def test_run_klayout_drc_nonzero_exit_refuses_despite_banner(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    rdb = tmp_path / "out.lyrdb"

    def crashed(*a, **k):
        rdb.write_text(EMPTY_RDB, encoding="utf-8")
        return FakeProc("DRC RESULT: SUCCESS (0 violations)", "NameError", 1)

    monkeypatch.setattr(layoutlib, "run_eda", crashed)
    with pytest.raises(layoutlib.LayoutError, match="exited 1"):
        layoutlib.run_klayout_drc(tmp_path / "x.gds", "top", rdb)


def test_run_klayout_drc_does_not_reread_a_stale_report(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    rdb = tmp_path / "out.lyrdb"
    rdb.write_text(EMPTY_RDB, encoding="utf-8")  # an earlier clean run's
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        "DRC RESULT: SUCCESS (0 violations)"))
    with pytest.raises(layoutlib.LayoutError, match="no report"):
        layoutlib.run_klayout_drc(tmp_path / "x.gds", "top", rdb)


def test_klayout_drc_variant_is_the_pdk_name(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    seen = {}
    rdb = tmp_path / "out.lyrdb"

    def fake(args, **k):
        seen["args"] = args
        rdb.write_text(EMPTY_RDB, encoding="utf-8")
        return FakeProc("DRC RESULT: SUCCESS (0 violations)")

    monkeypatch.setattr(layoutlib, "run_eda", fake)
    layoutlib.run_klayout_drc(tmp_path / "x.gds", "top", rdb)
    assert "variant=gf180mcuD" in seen["args"]
    assert f"decks={layoutlib.DRC_DECKS}" in seen["args"]


def test_run_magic_extract_refuses_when_no_spice_written(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    (tmp_path / "top.spice").write_text(".subckt top\n.ends\n")  # stale
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        "magic ran, wrote nothing useful"))
    with pytest.raises(layoutlib.LayoutError, match="top.spice"):
        layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                    parasitics=False)
    assert not (tmp_path / "top.spice").exists()


def test_run_magic_extract_nonzero_exit_refuses(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)

    def died(*a, **k):
        (tmp_path / "top.pex.spice").write_text(".subckt top\n.ends\n")
        return FakeProc("", "segfault", 139)

    monkeypatch.setattr(layoutlib, "run_eda", died)
    with pytest.raises(layoutlib.LayoutError, match="exited 139"):
        layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                    parasitics=True)


def test_parasitic_extraction_sets_thresholds_after_ext2spice_lvs(tmp_path, monkeypatch):
    # `ext2spice lvs` resets cthresh/rthresh to infinity: set after it, or
    # the "parasitic" netlist is the LVS one byte for byte (the review's
    # finding)
    fake_pdk(tmp_path, monkeypatch)
    seen = {}

    def fake(args, cwd=None, timeout=None, stdin_text=None):
        seen["tcl"] = stdin_text.splitlines()
        (tmp_path / "top.ext").write_text("tech gf180mcuD\n")
        (tmp_path / "top.res.ext").write_text("scale 1000 1 0.5\n")
        (tmp_path / "top.pex.spice").write_text(".subckt top\n.ends\n")
        return FakeProc()

    monkeypatch.setattr(layoutlib, "run_eda", fake)
    layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                parasitics=True)
    tcl = seen["tcl"]
    lvs = tcl.index("ext2spice lvs")
    assert tcl.index("ext2spice cthresh 0") > lvs
    assert tcl.index("ext2spice extresist on") > lvs
    assert tcl.index("extresist all") < lvs


PEX_FIXTURES = REPO / "tests" / "fixtures" / "pex_unlabelled"


def fixture_text(name):
    return (PEX_FIXTURES / name).read_text(encoding="utf-8")


def test_ext_nodes_reads_each_net_once():
    ext = ('node "vdd" 0 129.8 1756 1904 m1 0 0\n'
           'node "vdd" 0 129.8 1756 1904 m1 0 0\n'
           'node "a_38_n132#" 273 1347.82 38 -132 p 0 0\n')
    assert layoutlib.ext_nodes(ext) == [
        {"name": "vdd", "cap": 129.8, "x": 1756, "y": 1904, "type": "m1"},
        {"name": "a_38_n132#", "cap": 1347.82, "x": 38, "y": -132,
         "type": "p"}]


def test_name_unlabelled_nets_labels_each_made_up_name_on_its_own_tile():
    cmds = layoutlib.name_unlabelled_nets(fixture_text("unlabelled.ext"))
    assert cmds == ["snap internal", "box values 38 -132 39 -131",
                    "label a_38_n132 c p", "snap lambda"]


def test_name_unlabelled_nets_leaves_a_labelled_layout_alone():
    assert layoutlib.name_unlabelled_nets(fixture_text("named.ext")) == []


def test_name_unlabelled_nets_never_reuses_a_label():
    ext = ('node "a_1_2" 0 1 0 0 m1\n'
           'node "a_1_2#" 0 1 5 5 m1\n')
    assert "label a_1_2_1 c m1" in layoutlib.name_unlabelled_nets(ext)


# the planted fault: the net magic named itself lost its coupling to iout
# through extresist; the same layout with the net labelled kept it
def test_lost_couplings_catches_the_unlabelled_net():
    lost = layoutlib.lost_couplings(fixture_text("unlabelled.ext"),
                                    fixture_text("unlabelled.pex.spice"))
    assert lost == [{"nets": ["a_38_n132", "iout"], "ext_ff": 0.09435,
                     "pex_ff": 0.0}]


def test_lost_couplings_passes_the_named_net():
    assert layoutlib.lost_couplings(fixture_text("named.ext"),
                                    fixture_text("named.pex.spice")) == []


def test_lost_couplings_sums_a_nets_sub_nodes():
    ext = 'cap "a" "b" 100\n'
    pex = ("X0 a.t0 b.t0 c nfet\nC0 a.t0 b 0.06f\nC1 a.n1 b.t0 0.05f\n")
    assert layoutlib.lost_couplings(ext, pex) == []
    assert layoutlib.lost_couplings(ext, "X0 a b c nfet\nC0 a b 0.05f\n")


def test_lost_couplings_skips_a_device_body_the_netlist_never_names():
    # an rm1's body node: magic folds its C into the resistor's terminals
    ext = 'cap "m1_0_0" "vout" 100\n'
    assert layoutlib.lost_couplings(ext, "X0 bmsb vout rm1\n") == []


def c_to(pex_text, net, other):
    """fF of every C line between net (or its .tN/.nN) and other."""
    import re
    total = 0.0
    for line in pex_text.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("C"):
            continue
        ends = sorted(re.sub(r"\.[tn]\d+$", "", n) for n in parts[1:3])
        if ends == sorted([net, other]):
            total += float(parts[3].rstrip("f"))
    return total


# extresist's rnode C on the mirror's split net sums to its coupling to iout
# (94.35 aF), and ext2spice writes it to vss besides the coupling itself
def test_rnode_c_is_the_coupling_c_again():
    import re
    rnode = sum(float(m.group(1)) for m in re.finditer(
        r'^rnode "a_38_n132[^"]*" \S+ (\S+)',
        fixture_text("named.res.ext"), re.MULTILINE))
    assert rnode == pytest.approx(94.3498, abs=1e-3)


def test_uncount_rnode_caps_brings_a_split_nets_c_back_to_the_ext():
    pex = fixture_text("named.pex.spice")
    assert c_to(pex, "a_38_n132", "vss") == pytest.approx(1.40705)
    out, changed = layoutlib.uncount_rnode_caps(
        fixture_text("named.ext"), fixture_text("named.res.ext"), pex)
    # its .ext substrate C, 1347.82 aF; the coupling and the net no
    # extresist split are left as they were
    assert changed == {"a_38_n132": [1.40705, 1.34782]}
    assert c_to(out, "a_38_n132", "vss") == pytest.approx(1.34782, abs=1e-5)
    assert c_to(out, "a_38_n132", "iout") == pytest.approx(0.09435)
    assert c_to(out, "iout", "vss") == pytest.approx(0.55748)
    assert layoutlib.lost_couplings(fixture_text("named.ext"), out) == []


def test_uncount_rnode_caps_leaves_a_netlist_with_no_split_net_alone():
    pex = "C0 a vss 1f\n"
    assert layoutlib.uncount_rnode_caps(
        'substrate "vss" 0 0 0 0 m1\nnode "a" 0 500 0 0 m1\n',
        "scale 1000 1 0.5\n", pex) == (pex, {})


def test_uncount_rnode_caps_refuses_without_one_substrate():
    with pytest.raises(layoutlib.LayoutError, match="substrate"):
        layoutlib.uncount_rnode_caps(
            'node "a" 0 500 0 0 m1\n', 'rnode "a.t0" 0 10 0 0 0\n',
            "C0 a.t0 vss 1f\n")


UNSPLIT_EXT = ('substrate "vss" 0 0 0 0 m1\n'
               'node "d2" 0 3000 0 0 m1\n'
               'node "dz" 0 8764 0 0 m2\n'
               'cap "dz" "vss" 236\n')
UNSPLIT_RES = 'rnode "d2.t0" 0 1000 0 0 0\n'
UNSPLIT_PEX = (".subckt top d2 dz out vss\n"
               "X1 dz out cap_mim_2f0_m4m5_noshield c_width=26u c_length=26u\n"
               "X2 dz d2.t0 vss ppolyf_u_1k r_width=2u r_length=9u\n"
               "C0 d2.t0 vss 3f\n"
               "C1 dz out 2.5f\n"
               ".ends\n")


def test_restore_unsplit_node_caps_gives_a_whole_net_its_ext_c():
    # dz holds no transistor, so extresist leaves it whole and ext2spice
    # writes it no C to vss: it gets its .ext node C plus its coupling to
    # vss; split d2 and the dz-out coupling are left as they were
    out, changed = layoutlib.restore_unsplit_node_caps(
        UNSPLIT_EXT, UNSPLIT_RES, UNSPLIT_PEX)
    assert changed == {"dz": [0.0, 9.0]}
    assert c_to(out, "dz", "vss") == pytest.approx(9.0)
    assert c_to(out, "d2", "vss") == pytest.approx(3.0)
    assert c_to(out, "dz", "out") == pytest.approx(2.5)
    assert out.splitlines()[-1] == ".ends"


def test_restore_unsplit_node_caps_takes_a_split_nets_share_off():
    # ext2spice can file the rnode share of the split net on a device's
    # other terminal (d2.t0's 16 fF on dz): dz is set back to its .ext 9 fF
    pex = UNSPLIT_PEX.replace(".ends", "C2 dz vss 25f\n.ends")
    out, changed = layoutlib.restore_unsplit_node_caps(
        UNSPLIT_EXT, UNSPLIT_RES, pex)
    assert changed == {"dz": [25.0, 9.0]}
    assert c_to(out, "dz", "vss") == pytest.approx(9.0)
    assert "C2 dz vss" not in out
    assert c_to(out, "d2", "vss") == pytest.approx(3.0)


def test_restore_unsplit_node_caps_leaves_a_net_at_its_ext_c_alone():
    pex = UNSPLIT_PEX.replace(".ends", "C2 dz vss 9f\n.ends")
    assert layoutlib.restore_unsplit_node_caps(
        UNSPLIT_EXT, UNSPLIT_RES, pex) == (pex, {})


R2R_EXT = ('substrate "0" 8596 0 -200 0 pw\n'
           'node "body1" 0 1374.7 0 0 rm1\n'
           'node "body2" 0 1374.7 2200 0 rm1\n'
           'node "vout" 0 137.47 2100 200 m1\n'
           'node "bmsb" 0 216.332 -100 200 m1\n'
           'node "mid" 0 333.548 4200 0 m1\n'
           'device rsubckt rm1 0 0 1 1 l=2000 w=400 "None" "body1" 0 0 '
           '"bmsb" 400 80000,1200 "vout" 400 40000,600\n'
           'device rsubckt rm1 2200 0 2201 1 l=2000 w=400 "None" "body2" 0 0 '
           '"vout" 400 40000,600 "mid" 400 80000,666\n')
R2R_PEX = (".subckt r2r bmsb vout\n"
           "X0 bmsb vout rm1 r_width=2u r_length=10u\n"
           "X1 vout mid rm1 r_width=2u r_length=10u\n"
           "C0 vout 0 1.51217f\n"
           "C1 bmsb 0 0.90368f\n"
           "C2 mid 0 1.0209f\n"
           ".ends\n")


def test_restore_unsplit_node_caps_keeps_the_resistor_body_shares():
    # ext2spice writes no rm1 body node as a net: it splits each body's C
    # equally over the resistor's two ends, so r2r_dac's vout carries half
    # of each of its two bodies on top of its own 0.137 fF. That is the
    # .ext's C too, and nothing is changed.
    assert layoutlib.restore_unsplit_node_caps(
        R2R_EXT, "scale 1000 1 0.5\n", R2R_PEX) == (R2R_PEX, {})
    out, changed = layoutlib.restore_unsplit_node_caps(
        R2R_EXT, "scale 1000 1 0.5\n",
        R2R_PEX.replace("C0 vout 0 1.51217f\n", ""))
    assert changed == {"vout": [0.0, 1.51217]}
    assert c_to(out, "vout", "0") == pytest.approx(1.51217)


def test_restore_unsplit_node_caps_refuses_without_one_substrate():
    with pytest.raises(layoutlib.LayoutError, match="substrate"):
        layoutlib.restore_unsplit_node_caps(
            UNSPLIT_EXT.replace('substrate "vss"', 'node "vss"'),
            UNSPLIT_RES, UNSPLIT_PEX)


def test_parasitic_extraction_labels_unlabelled_nets_before_extresist(
        tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    runs = []

    def fake(args, cwd=None, timeout=None, stdin_text=None):
        runs.append(stdin_text.splitlines())
        name = "unlabelled.ext" if len(runs) == 1 else "named.ext"
        (tmp_path / "top.ext").write_text(fixture_text(name))
        if len(runs) == 2:
            (tmp_path / "top.res.ext").write_text(
                fixture_text("named.res.ext"))
            (tmp_path / "top.pex.spice").write_text(
                fixture_text("named.pex.spice"))
        return FakeProc()

    monkeypatch.setattr(layoutlib, "run_eda", fake)
    layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                parasitics=True)
    assert len(runs) == 2
    assert "extresist all" not in runs[0]
    second = runs[1]
    assert second.index("label a_38_n132 c p") < second.index("extract all")
    # the netlist written is the one with the double count taken off
    assert c_to(
        (tmp_path / "top.pex.spice").read_text(), "a_38_n132", "vss") == \
        pytest.approx(1.34782, abs=1e-4)


def test_parasitic_extraction_refuses_a_netlist_that_lost_couplings(
        tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)

    def fake(args, cwd=None, timeout=None, stdin_text=None):
        (tmp_path / "top.ext").write_text(fixture_text("unlabelled.ext"))
        (tmp_path / "top.res.ext").write_text(
            fixture_text("unlabelled.res.ext"))
        (tmp_path / "top.pex.spice").write_text(
            fixture_text("unlabelled.pex.spice"))
        return FakeProc()

    monkeypatch.setattr(layoutlib, "run_eda", fake)
    with pytest.raises(layoutlib.LayoutError,
                       match=r"a_38_n132-iout \(0 of 0.09435 fF\)"):
        layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                    parasitics=True)


def test_parasitic_extraction_refuses_when_the_naming_pass_wrote_no_ext(
        tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc())
    with pytest.raises(layoutlib.LayoutError, match="naming pass"):
        layoutlib.run_magic_extract(tmp_path, tmp_path / "x.gds", "top",
                                    parasitics=True)


def test_magic_tech_fixes_the_rm1_input_typo(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch,
             tech=" calma MET1 34 0\n calma MET2RES 110 11\n"
                  " calma MET2RES 110 12\n")
    out = layoutlib.magic_tech(tmp_path)
    text = out.read_text(encoding="utf-8")
    assert " calma MET1RES 110 11\n" in text
    assert " calma MET2RES 110 12\n" in text
    assert " calma MET2RES 110 11\n" not in text


def test_magic_tech_leaves_a_fixed_pdk_alone(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    assert layoutlib.magic_tech(tmp_path) is None


def test_magic_tech_refuses_an_unknown_rm1_mapping(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch, tech=" calma MET1 34 0\n")
    with pytest.raises(layoutlib.LayoutError, match="110/11"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_selects_each_pnp_on_its_emitter_area(tmp_path, monkeypatch):
    # the PDK's rules give *pdiff as the substrate, so a2 is 0 and the
    # first rule names every pnp (#285); the fix makes the emitter
    # terminal 1 (a1) and the collector terminal 2, with no substrate
    fake_pdk(tmp_path, monkeypatch, pnp=PNP_TYPO)
    text = layoutlib.magic_tech(tmp_path).read_text(encoding="utf-8")
    assert (" device msubcircuit pnp_10p00x00p42 pnp *pdiff pwell,space/w"
            " a1>4.1 a1<4.3\n") in text
    assert (" device msubcircuit pnp_10p00x10p00 pnp *pdiff pwell,space/w"
            " a1>99.0 a1<101.0\n") in text
    assert "a2>" not in text


def test_magic_tech_fixes_pnp_rules_and_rm1_together(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch, tech=" calma MET2RES 110 11\n",
             pnp=PNP_TYPO)
    text = layoutlib.magic_tech(tmp_path).read_text(encoding="utf-8")
    assert " calma MET1RES 110 11\n" in text and "a2>" not in text


def test_magic_tech_refuses_an_unknown_pnp_rule(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch,
             pnp=" device msubcircuit pnp_10p00x10p00 pnp nwell *pdiff a3>1\n")
    with pytest.raises(layoutlib.LayoutError, match="pnp rule"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_refuses_a_tech_with_no_pnp_rule(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch, pnp="")
    with pytest.raises(layoutlib.LayoutError, match="no `device msubcircuit pnp_"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_extracts_each_well_diode(tmp_path, monkeypatch):
    # the PDK has no rule for its well diodes and does not read their
    # WELL_DIODE marker (#286 #287 #291); the fix reads 153/51 into a
    # well type per diode and adds a rule for each
    fake_pdk(tmp_path, monkeypatch, diodes=DIODES_STOCK)
    text = layoutlib.magic_tech(tmp_path).read_text(encoding="utf-8")
    assert " calma WELLDIODE 153 51\n" in text
    for rule in ("diode_pw2dw      *pwdiode dnwell,dnwdiode",
                 "diode_dw2ps      dnwdiode space/w,pwell",
                 "diode_nw2ps_03v3 nwdiode space/w,pwell",
                 "diode_nw2ps_06v0 mvnwdiode space/w,pwell"):
        assert text.count(rule) == 1
    # the plain wells give up the marked area, which the diode types take
    assert (" layer nwell NWELL,NWELLTXT\n and-not BJTDEF,BJTDRC\n"
            " and-not WELLDIODE\n") in text
    assert " layer dnwdiode DNWELL\n and dnwdmk\n" in text
    # and connect, draw and write out as the plain wells do
    assert "nbase,dnwell,nwdiode,mvnwdiode,dnwdiode " in text
    assert " layer DNWELL\tdnwell,dnwdiode\n" in text


def test_magic_tech_extracts_the_schottky_anode_as_drawn(tmp_path, monkeypatch):
    # nsd took the anode's edge (three sc_diodes) and the anode-cathode
    # gap counted as anode (area 5.12p for 4p drawn) (#289)
    fake_pdk(tmp_path, monkeypatch, diodes=DIODES_STOCK)
    text = layoutlib.magic_tech(tmp_path).read_text(encoding="utf-8")
    assert " and nsdexpand\n and-not SCHOTTKY\n" in text
    assert " and-not sccathode\n and DIFF\n" in text
    assert " layer nsd SCHOTTKY\n" in text


def test_magic_tech_refuses_a_partly_extracted_well_diode_set(tmp_path,
                                                              monkeypatch):
    fake_pdk(tmp_path, monkeypatch, diodes=DIODES_STOCK
             + " device ndiode diode_dw2ps dnwell space/w,pwell\n")
    with pytest.raises(layoutlib.LayoutError, match="some well diodes"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_refuses_well_diodes_it_cannot_fix(tmp_path, monkeypatch):
    stock = DIODES_STOCK.replace(" calma DNWELL 12 0\n", "")
    fake_pdk(tmp_path, monkeypatch, diodes=stock)
    with pytest.raises(layoutlib.LayoutError, match="WELL_DIODE"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_refuses_an_unknown_schottky_rule(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch,
             diodes=DIODES_FIXED.replace(layoutlib._SC_ANODE_FIXED, ""))
    with pytest.raises(layoutlib.LayoutError, match="Schottky anode"):
        layoutlib.magic_tech(tmp_path)


def test_magic_tech_reads_the_p_varactor_terminals(tmp_path, monkeypatch):
    # cap_pmos_*_b extracted no device: its gate grew pfetexpand (and a
    # forced nwell) over its own source and drain, so they read as pfet
    # diffusion, not the *ppdiff its device rule names (#288)
    fake_pdk(tmp_path, monkeypatch, pvar=PVAR_STOCK)
    text = layoutlib.magic_tech(tmp_path).read_text(encoding="utf-8")
    assert (" templayer pvararea pfetarea\n and MOSCAP\n and PPLUS\n"
            " and-not nwelldef\n") in text
    assert " templayer pfetexpand pfetarea\n and-not pvararea\n" in text
    assert " layer nwell pfetarea\n and-not pvararea\n grow 310\n" in text
    assert (" templayer mvpfetexpand mvpfetarea\n and-not mvpvararea\n"
            in text)
    assert layoutlib._PFET_EXPAND not in text


def test_magic_tech_refuses_an_unknown_pfet_area_rule(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch, pvar=layoutlib._PFET_EXPAND_FIXED)
    with pytest.raises(layoutlib.LayoutError, match="6 V pfet area"):
        layoutlib.magic_tech(tmp_path)


def test_count_parasitics():
    text = (".subckt top a b\nX0 a b rm1\nR0 a a.t0 1.5\nC0 a b 1f\n"
            "C1 b 0 2f\n.ends\n")
    assert layoutlib.count_parasitics(text) == {"r": 1, "c": 2}


def test_run_netgen_lvs_refuses_when_no_log_written(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc("netgen ran"))
    with pytest.raises(layoutlib.LayoutError, match="no log"):
        layoutlib.run_netgen_lvs(tmp_path, "a.spice", "a", "b.spice", "b",
                                 "out.log")


def test_run_netgen_lvs_does_not_reread_a_stale_match(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    (tmp_path / "out.log").write_text(
        "Final result: Circuits match uniquely.\n", encoding="utf-8")
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc())
    with pytest.raises(layoutlib.LayoutError, match="no log"):
        layoutlib.run_netgen_lvs(tmp_path, "a.spice", "a", "b.spice", "b",
                                 "out.log")


def test_run_netgen_lvs_uses_the_pdk_setup(tmp_path, monkeypatch):
    pdk = fake_pdk(tmp_path, monkeypatch)
    seen = {}

    def fake(args, **k):
        seen["cmd"] = args[-1]
        (tmp_path / "out.log").write_text(
            "Final result: Circuits match uniquely.\n", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(layoutlib, "run_eda", fake)
    layoutlib.run_netgen_lvs(tmp_path, "a.spice", "a", "b.spice", "b", "out.log")
    assert str(pdk / "libs.tech" / "netgen" / "gf180mcuD_setup.tcl") in seen["cmd"]
    assert "{}" not in seen["cmd"]


def netgen_writing(tmp_path, text, rc=0):
    def fake(*a, **k):
        (tmp_path / "out.log").write_text(text, encoding="utf-8")
        return FakeProc("", "", rc)
    return fake


def test_run_netgen_lvs_property_errors_are_not_a_match(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", netgen_writing(
        tmp_path, "Netlists match uniquely with port and property errors.\n"
        "Final result: Circuits match uniquely.\n"
        "The following cells had property errors:\n"))
    matched, _text = layoutlib.run_netgen_lvs(
        tmp_path, "a.spice", "a", "b.spice", "b", "out.log")
    assert matched is False


def test_run_netgen_lvs_final_line_decides(tmp_path, monkeypatch):
    # a subcell's "Circuits match uniquely" above a failing top cell
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", netgen_writing(
        tmp_path, "Final result: Circuits match uniquely.\n"
        "Final result: Top level cell failed pin matching.\n"))
    matched, _text = layoutlib.run_netgen_lvs(
        tmp_path, "a.spice", "a", "b.spice", "b", "out.log")
    assert matched is False


def test_run_netgen_lvs_nonzero_exit_refuses(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", netgen_writing(
        tmp_path, "Final result: Circuits match uniquely.\n", rc=1))
    with pytest.raises(layoutlib.LayoutError, match="exited 1"):
        layoutlib.run_netgen_lvs(tmp_path, "a.spice", "a", "b.spice", "b",
                                 "out.log")


def test_run_netgen_lvs_clean_match(tmp_path, monkeypatch):
    fake_pdk(tmp_path, monkeypatch)
    monkeypatch.setattr(layoutlib, "run_eda", netgen_writing(
        tmp_path, "Final result: Circuits match uniquely.\n"))
    matched, _text = layoutlib.run_netgen_lvs(
        tmp_path, "a.spice", "a", "b.spice", "b", "out.log")
    assert matched is True


def test_run_ngspice_nonzero_exit_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        "v(iout) = 2.8\n", "Error: unknown subckt", 1))
    with pytest.raises(layoutlib.LayoutError, match="exited 1"):
        layoutlib.run_ngspice(tmp_path / "tb.cir")


def test_run_ngspice_engine_failure_with_exit_zero_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        "vout = 0.66\n", "Warning: singular matrix:  check node x.n1\n"))
    with pytest.raises(layoutlib.LayoutError, match="singular_matrix"):
        layoutlib.run_ngspice(tmp_path / "tb.cir")


def test_parse_ngspice_prints_reads_name_equals_value_lines():
    output = """\
Note: some banner
v(iref) = 1.215070e+00
v(iout) = 2.825339e+00
not a measure line
"""
    values = layoutlib.parse_ngspice_prints(output)
    assert values["v(iref)"] == 1.21507
    assert values["v(iout)"] == 2.825339


def test_psub_tap_pad_center_matches_pad_center_helper():
    # gf180_cells()'s own import activates a gdsfactory PDK as a side
    # effect - psub_tap() is always called after it in real generator code
    # (gen_mirror.py etc.), never standalone; matched here so this stays a
    # test of psub_tap()'s own geometry, not of PDK activation order.
    layoutlib.gf180_cells()
    tap = layoutlib.psub_tap(size=1.0)
    cx, cy = layoutlib.pad_center(1.0)
    assert (cx, cy) == (0.5, 0.5)
    polys = tap.get_polygons(by_spec=True)
    assert layoutlib.GF180_LAYER["metal1"] in polys
    assert layoutlib.GF180_LAYER["contact"] in polys


class _Recorder:
    """Stands in for a gf.Component: rect() only calls add_polygon."""

    def __init__(self):
        self.polys = []

    def add_polygon(self, pts, layer):
        self.polys.append((layer, pts))


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def test_via1_is_a_centred_cut_enclosed_on_both_metals():
    top = _Recorder()
    layoutlib.via1(top, 1.0, 2.0)
    by_layer = {layer: _bbox(pts) for layer, pts in top.polys}
    L = layoutlib.GF180_LAYER
    assert set(by_layer) == {L["via1"], L["metal1"], L["metal2"]}
    h = layoutlib.VIA1_SIZE / 2
    assert by_layer[L["via1"]] == pytest.approx((1 - h, 2 - h, 1 + h, 2 + h))
    e = h + layoutlib.VIA1_ENC
    for m in ("metal1", "metal2"):
        assert by_layer[L[m]] == pytest.approx((1 - e, 2 - e, 1 + e, 2 + e))


def test_nwell_tap_is_nplus_and_draws_no_well():
    pytest.importorskip("gdsfactory")
    L = layoutlib.GF180_LAYER
    ntap = layoutlib.nwell_tap(1.0)
    layers = {tuple(li) for li in ntap.layers}
    assert L["nplus"] in layers and L["pplus"] not in layers
    assert L["nwell"] not in layers
    assert {L["comp"], L["contact"], L["metal1"]} <= layers


# what this box's ngspice printed for a .control `meas` whose target never
# happened (corpus/ade/comparator's pex bench: outn never fell)
MEAS_NEVER_MET = (
    "vdiff_pos           =  -3.30000e+00\n"
    "\nError: measure  tdelay  trig(TARG) : out of interval\n"
    " meas tran tdelay trig v(clk) val=1.4 rise=1 targ v(outn) val=1.4 "
    "fall=1 failed!\n\n"
    "Warning from checkvalid: vector tdelay is not available or has zero "
    "length.\n")


def test_run_ngspice_measure_never_met_is_still_a_refusal_by_default(
        tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "run_eda",
                        lambda *a, **k: FakeProc(MEAS_NEVER_MET))
    with pytest.raises(layoutlib.LayoutError, match="ngspice_error"):
        layoutlib.run_ngspice(tmp_path / "tb.cir")


def test_run_ngspice_hands_a_measure_never_met_back_by_name(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(layoutlib, "run_eda",
                        lambda *a, **k: FakeProc(MEAS_NEVER_MET))
    failed = set()
    out = layoutlib.run_ngspice(tmp_path / "tb.cir", failed_measures=failed)
    assert failed == {"tdelay"}
    assert layoutlib.parse_ngspice_prints(out)["vdiff_pos"] == -3.3


def test_run_ngspice_measure_never_met_does_not_hide_an_engine_error(
        tmp_path, monkeypatch):
    monkeypatch.setattr(layoutlib, "run_eda", lambda *a, **k: FakeProc(
        MEAS_NEVER_MET + "Error: unknown subckt: x1 a b foo\n"))
    with pytest.raises(layoutlib.LayoutError, match="unknown_subckt"):
        layoutlib.run_ngspice(tmp_path / "tb.cir", failed_measures=set())


def test_finalize_keeps_holes():
    # shakedown breakage 18: the PDK's snap_to_grid rebuilt each polygon
    # from its outline, so a ring came back as a solid block.
    pytest.importorskip("gdsfactory")
    import gdsfactory as gf
    import klayout.db as kdb

    layoutlib.gf180_cells()
    L = layoutlib.GF180_LAYER
    ring = gf.Component()
    outer = kdb.DBox(0, 0, 4, 4)
    hole = kdb.DBox(1, 1, 3, 3)
    poly = kdb.DPolygon(outer)
    poly.insert_hole(list(kdb.DPolygon(hole).each_point_hull()))
    li = ring.kcl.layer(*L["nplus"])
    ring.kdb_cell.shapes(li).insert(poly)
    fin = layoutlib.finalize(ring, "ring_fin", [])
    got = kdb.Region(fin.kdb_cell.begin_shapes_rec(fin.kcl.layer(*L["nplus"])))
    want = kdb.Region(ring.kdb_cell.begin_shapes_rec(li))
    assert got.area() == want.area() == 12 / fin.kcl.dbu ** 2
    assert (got ^ want).is_empty()
    # and the snap still moves an off-grid vertex onto the 5nm grid
    off = gf.Component()
    off.add_polygon([(0, 0), (1.002, 0), (1.002, 1), (0, 1)], layer=L["metal1"])
    fin = layoutlib.finalize(off, "off_fin", [])
    box = fin.kdb_cell.dbbox()
    assert (box.right, box.top) == (pytest.approx(1.0), pytest.approx(1.0))


def test_std_cell_subckts_adds_only_undefined_pdk_cells(tmp_path, monkeypatch):
    pdk = tmp_path / "pdk"
    import netlistlib
    lib = pdk / netlistlib.PDK_STDCELL_FILES[0]
    lib.parent.mkdir(parents=True)
    lib.write_text(
        ".SUBCKT lib__buf_1 I Z VDD VNW VPW VSS\n"
        "X_i0 Z I VSS VPW nfet_05v0 W=8.2e-07 L=6e-07\n.ENDS\n"
        ".SUBCKT lib__inv_1 I ZN VDD VNW VPW VSS\n.ENDS\n", encoding="utf-8")
    monkeypatch.setattr(layoutlib, "pdk_root", lambda: pdk)
    text, cells = layoutlib.std_cell_subckts(
        ".subckt top a y vdd vss\n"
        "x1 a y vdd vdd vss vss lib__buf_1\n"
        "x2 a m vss ppolyf_u r_width=2u r_length=10u\n.ends\n")
    assert cells == ["lib__buf_1"]
    assert text.startswith(".SUBCKT lib__buf_1") and "inv_1" not in text
    # a netlist that defines the cell itself keeps its own definition
    _text, cells = layoutlib.std_cell_subckts(
        ".subckt lib__buf_1 I Z VDD VNW VPW VSS\n.ends\n"
        ".subckt top a y vdd vss\nx1 a y vdd vdd vss vss lib__buf_1\n.ends\n")
    assert cells == []


def test_run_netgen_lvs_refuses_a_black_box_it_cannot_size(tmp_path, monkeypatch):
    # shakedown breakage 21: a std cell the reference never defines was
    # compared as an empty box. A PDK device class netgen does compare the
    # properties of (lappend devices ...) is fine as a placeholder.
    pdk = fake_pdk(tmp_path, monkeypatch)
    (pdk / "libs.tech" / "netgen" / "gf180mcuD_setup.tcl").write_text(
        "set devices {}\nlappend devices ppolyf_u\n", encoding="utf-8")
    log = "Final result: Circuits match uniquely.\n"

    def netgen_saying(stdout):
        def fake(*a, **k):
            (tmp_path / "out.log").write_text(log, encoding="utf-8")
            return FakeProc(stdout)
        return fake

    monkeypatch.setattr(layoutlib, "run_eda", netgen_saying(
        "Call to undefined subcircuit ppolyf_u\n"))
    matched, _text = layoutlib.run_netgen_lvs(
        tmp_path, "a.spice", "a", "b.spice", "b", "out.log")
    assert matched is True
    monkeypatch.setattr(layoutlib, "run_eda", netgen_saying(
        "Call to undefined subcircuit ppolyf_u\n"
        "Call to undefined subcircuit gf180mcu_fd_sc_mcu7t5v0__buf_20\n"))
    with pytest.raises(layoutlib.LayoutError, match="buf_20 as black boxes"):
        layoutlib.run_netgen_lvs(
            tmp_path, "a.spice", "a", "b.spice", "b", "out.log")


def test_setup_spelling_respells_setup_devices_only(tmp_path):
    # ring_osc_div's netlist called cap_mim_2f0ff; the PDK setup's
    # case-exact lsearch for cap_mim_2f0fF missed it, so the equate with
    # the extracted cap_mim_2f0_m4m5_noshield never ran and LVS failed.
    setup = tmp_path / "setup.tcl"
    setup.write_text("lappend devices cap_mim_2f0fF\n"
                     "lappend devices ppolyf_u\n", encoding="utf-8")
    text = ("* xc0 a b cap_mim_2f0ff is a comment\n"
            "xc1 a b CAP_MIM_2F0FF c_width=1e-5\n"
            "xr1 a b vss ppolyf_u r_width=1u\n"
            "xc2 a b cap_mim_2f0ffx c_width=1e-5\n")
    assert layoutlib.setup_spelling(text, setup) == (
        "* xc0 a b cap_mim_2f0ff is a comment\n"
        "xc1 a b cap_mim_2f0fF c_width=1e-5\n"
        "xr1 a b vss ppolyf_u r_width=1u\n"
        "xc2 a b cap_mim_2f0ffx c_width=1e-5\n")
    empty = tmp_path / "empty.tcl"
    empty.write_text("# no devices\n", encoding="utf-8")
    assert layoutlib.setup_spelling(text, empty) == text


def test_match_sequence_abba_with_dummies():
    seq = layoutlib.match_sequence({"a": 4, "b": 4}, "ABBA")
    assert seq == [None, "a", "b", "b", "a", "a", "b", "b", "a", None]
    assert layoutlib.match_sequence({"a": 2, "b": 2}, "ABBA", 0) == ["a", "b", "b", "a"]


@pytest.mark.parametrize("units", [{"a": 3, "b": 3}, {"a": 2, "b": 4},
                                   {"a": 2, "b": 2, "c": 2}])
def test_match_sequence_abba_refuses_what_it_cannot_make(units):
    with pytest.raises(layoutlib.LayoutError, match="common_centroid"):
        layoutlib.match_sequence(units, "ABBA")


def _centroid(seq, name):
    at = [i for i, n in enumerate(seq) if n == name]
    return sum(at) / len(at)


@pytest.mark.parametrize("units", [{"a": 2, "b": 2}, {"a": 4, "b": 2, "c": 1},
                                   {"a": 1, "b": 6}, {"a": 8, "b": 1}])
def test_match_sequence_common_centroid_shares_the_rows_centroid(units):
    seq = layoutlib.match_sequence(units, "common_centroid", dummies=2)
    assert seq[:2] == seq[-2:] == [None, None]
    assert seq == seq[::-1]
    for n, k in units.items():
        assert seq.count(n) == k
        assert _centroid(seq, n) == (len(seq) - 1) / 2


def test_match_sequence_common_centroid_refuses_two_odd_counts():
    with pytest.raises(layoutlib.LayoutError, match="a, b all have odd"):
        layoutlib.match_sequence({"a": 3, "b": 1}, "common_centroid")


def test_match_sequence_interdigitate_spreads_each_device():
    assert layoutlib.match_sequence({"a": 2, "b": 2}, "interdigitate", 0) == \
        ["a", "b", "a", "b"]
    seq = layoutlib.match_sequence({"a": 6, "b": 2}, "interdigitate", 0)
    assert seq.count("a") == 6 and seq.count("b") == 2
    assert "aaaa" not in "".join(seq)
    # one unit per device keeps the order given
    assert layoutlib.match_sequence({"c": 1, "a": 1, "b": 1}, "interdigitate") \
        == [None, "c", "a", "b", None]


@pytest.mark.parametrize("args, msg", [
    (({"a": 2}, "ABBA"), "two or more devices"),
    (({"a": 0, "b": 2}, "interdigitate"), "two or more devices"),
    (({"a": 2, "b": 2}, "spiral"), "one of ABBA"),
    (({"a": 2, "b": 2}, "ABBA", -1), "dummies"),
])
def test_match_sequence_refusals(args, msg):
    with pytest.raises(layoutlib.LayoutError, match=msg):
        layoutlib.match_sequence(*args)


def test_matched_row_places_one_cell_per_slot_and_maps_its_pads():
    pytest.importorskip("gdsfactory")
    import gdsfactory as gf

    layoutlib.gf180_cells()
    L = layoutlib.GF180_LAYER
    cell = gf.Component()
    cell.add_polygon([(0, 0), (1, 0), (1, 1), (0, 1)], layer=L["metal1"])
    cell.add_polygon([(3, 0), (4, 0), (4, 1), (3, 1)], layer=L["metal1"])
    top = gf.Component()
    row = layoutlib.matched_row(top, cell, {"a": 2, "b": 2}, pitch=-2.0,
                                pattern="ABBA", axis="y", origin=(5.0, 10.0))
    assert row["sequence"] == [None, "a", "b", "b", "a", None]
    assert len(row["refs"]) == 6
    assert row["units"] == {"a": [(5.0, 8.0), (5.0, 2.0)],
                            "b": [(5.0, 6.0), (5.0, 4.0)]}
    assert row["dummies"] == [(5.0, 10.0), (5.0, 0.0)]
    pad = row["pads"][1]
    assert (pad["device"], pad["slot"], pad["origin"]) == ("a", 1, (5.0, 8.0))
    assert pad["boxes"] == [(5.0, 8.0, 6.0, 9.0), (8.0, 8.0, 9.0, 9.0)]
    got = layoutlib.layer_boxes(top, L["metal1"])
    assert len(got) == 12
    with pytest.raises(layoutlib.LayoutError, match="non-zero pitch"):
        layoutlib.matched_row(top, cell, {"a": 1, "b": 1}, pitch=0)


# ------------------------------------------- poly resistor body residual

POLY_TECH = """\
extract
 style ngspice variants (),(hrhc)
# parasitic capacitance values are taken directly from the source document
# PDS_035_03, in units of aF/um^2
variants ()
 defaultareacap     *poly active  110.67
 defaultperimeter   *poly active  50.72
variants (hrhc)
 defaultareacap     *poly active  130.21
 defaultperimeter   *poly active  53.551
variants *
 device rsubcircuit ppolyf_u  rpp    *poly allnwell,allpsub error l=r_length w=r_width
 device rsubcircuit pplus_u  rpd  *pdiff allnwell error l=r_length w=r_width
end
"""

POLY_MODEL = """\
.subckt ppolyf_u 1 2 3 r_length=l r_width=w dtemp=0 par=1 s=1 r_dw='2.55e-8*(1+mc_dw_ppolyf_u*res_mc_skew*sw_stat_global)' r_dl=2e-11 r_l='s*(r_length-2*r_dl)' r_w='r_width-2*r_dw'
.model fox_sub c cj=8.85e-05
rt1 1 11 ppolyf_u_t dtemp=dtemp l='s*1u' w=r_w
c1 1 3 fox_sub dtemp=dtemp l='r_l/2' w=r_w
rb 11 21 ppolyf_u_body r=1 w=r_w l=r_l dtemp=dtemp
rt2 21 2 ppolyf_u_t dtemp=dtemp l='s*1u' w=r_w
c2 2 3 fox_sub dtemp=dtemp l='r_l/2' w=r_w
.ends ppolyf_u
"""


def poly_caps(variant=None, model=POLY_MODEL):
    tech = layoutlib.tech_poly_caps(POLY_TECH, "t.tech", variant)
    return tech, layoutlib.model_poly_body_c(model, "m.spice", tech["models"])


def test_ppolyf_u_body_residual_known_answer():
    # one r2r_dac unit, 2 x 148.36 um: tech 110.67 x 296.72 + 50.72 x
    # 296.72 = 47.89 fF, model 88.5 x 1.949 x 148.36 = 25.59 fF -> the
    # ruling's ~22 fF (7 area + 15.3 fringe), 11.15 fF at each end
    tech, models = poly_caps()
    assert models["ppolyf_u"]["cj_af_um2"] == pytest.approx(88.5)
    assert tech["cites"] == {"area": "t.tech:6", "perim": "t.tech:7",
                             "source": "t.tech:4 (PDS_035_03)"}
    assert models["ppolyf_u"]["cites"]["cj"] == "m.spice:2"
    res = layoutlib.poly_body_residual_ff(2.0, 148.36, tech,
                                          models["ppolyf_u"])
    assert res == pytest.approx(22.30, abs=0.01)
    pex = (".subckt top a b vss\n"
           "X29 a b vss.t0 ppolyf_u r_width=2u r_length=0.14836m\n"
           "X3 a vss vss pplus_u r_width=2u r_length=10u\n"
           "C0 a vss 1f\n.ends\n")
    text, bodies = layoutlib.add_poly_body_residuals(pex, tech, models)
    assert [b["device"] for b in bodies] == ["X29"]  # diffusion: none
    assert bodies[0]["model_c_ff"] == pytest.approx(25.59, abs=0.01)
    lines = text.splitlines()
    assert lines.index(".ends") == len(lines) - 1
    caps = [ln.split() for ln in lines if ln.startswith("Crb")]
    assert [c[1:3] for c in caps] == [["a", "vss.t0"], ["b", "vss.t0"]]
    assert all(float(c[3][:-1]) == pytest.approx(11.15, abs=0.01)
               for c in caps)


def test_poly_body_residual_reads_the_variant_and_the_multiplier():
    tech, models = poly_caps("hrhc")
    assert tech["area_af_um2"] == 130.21
    pex = (".subckt top a b vss\n"
           "X1 a b vss ppolyf_u r_width=2u r_length=100u m=2\n.ends\n")
    _, bodies = layoutlib.add_poly_body_residuals(pex, tech, models)
    want = 2 * layoutlib.poly_body_residual_ff(2, 100, tech,
                                               models["ppolyf_u"])
    assert bodies[0]["residual_ff"] == pytest.approx(want, abs=1e-4)


def test_poly_body_residual_refuses_a_model_of_another_form():
    other = POLY_MODEL.replace("l='r_l/2' w=r_w\nrb", "l='r_l' w=r_w\nrb")
    with pytest.raises(layoutlib.LayoutError, match="fox_sub c1/c2"):
        poly_caps(model=other)
    with pytest.raises(layoutlib.LayoutError, match="variant \\(lrlc\\)"):
        layoutlib.tech_poly_caps(POLY_TECH, "t.tech", "lrlc")


def test_poly_body_residual_refuses_a_negative_residual():
    tech, models = poly_caps()
    tech = dict(tech, area_af_um2=10.0, perim_af_um=0.0)
    with pytest.raises(layoutlib.LayoutError, match="more C in its model"):
        layoutlib.add_poly_body_residuals(
            ".subckt t a b c\nX1 a b c ppolyf_u r_width=2u r_length=10u\n"
            ".ends\n", tech, models)

# a magic tech cut down to what mim_plate_areacaps reads: the MIM's top
# plate type is in the metal4 alias, its area cap differs by variant
MIM_TECH = """\
aliases
  allm4 *m4,rm4,*mimcap
end
extract
 style ngspice variants (),(hrhc)
 variants ()
 defaultareacap allm4 metal4 7.602
 variants (hrhc)
 defaultareacap allm4 metal4 8.213
 variants *
 device csubcircuit cap_mim_2f0_m4m5_noshield *mimcap *m4 l=c_length w=c_width
 style pnm
 defaultareacap allm4 metal4 99
end
"""
MIM_EXT = 'style ngspice()\nsubstrate "vss" 0 0 0 0 pw\n'
MIM_PEX = """\
.subckt top d2 out vss
X0 d2.t0 out.t1 cap_mim_2f0_m4m5_noshield c_width=10u c_length=10u
C0 d2 vss 0.6f
C1 d2.t0 vss 0.4f
C2 d2 out 0.1f
C3 out vss 0.2f
.ends
"""


def test_mim_plate_areacaps_reads_the_styles_variant():
    assert layoutlib.mim_plate_areacaps(MIM_TECH, "ngspice", "") == {
        "cap_mim_2f0_m4m5_noshield": 7.602}
    assert layoutlib.mim_plate_areacaps(MIM_TECH, "ngspice", "hrhc") == {
        "cap_mim_2f0_m4m5_noshield": 8.213}


def test_move_mim_plate_caps_puts_the_bottom_plates_c_on_its_net():
    # 100 um^2 x 7.602 aF/um^2 = 0.7602 fF off d2's 1.0 fF to vss, scaled
    # together, onto the bottom plate's node; the coupling stays
    out, moved = layoutlib.move_mim_plate_caps(
        MIM_EXT, MIM_PEX, {"cap_mim_2f0_m4m5_noshield": 7.602})
    assert moved == {"X0": {"top": "d2", "bottom": "out", "ff": 0.7602}}
    assert c_to(out, "d2", "vss") == pytest.approx(0.2398, abs=1e-5)
    assert c_to(out, "out", "vss") == pytest.approx(0.9602, abs=1e-5)
    assert c_to(out, "d2", "out") == pytest.approx(0.1)
    assert out.splitlines()[-1] == ".ends"


def test_move_mim_plate_caps_refuses_when_something_under_the_plate_took_it():
    # d2 has less C to vss than its plate area gives it: a shape under the
    # MIM took it as an overlap this cannot split
    with pytest.raises(layoutlib.LayoutError, match="under the MIM"):
        layoutlib.move_mim_plate_caps(
            MIM_EXT, MIM_PEX, {"cap_mim_2f0_m4m5_noshield": 20.0})


def test_move_mim_plate_caps_leaves_a_netlist_without_a_mim_alone():
    pex = ".subckt top a vss\nC0 a vss 1f\n.ends\n"
    assert layoutlib.move_mim_plate_caps(
        MIM_EXT, pex, {"cap_mim_2f0_m4m5_noshield": 7.602}) == (pex, {})
    assert not layoutlib._has_mim(MIM_TECH, pex)
    assert layoutlib._has_mim(MIM_TECH, MIM_PEX)


# ---- parallel devices in a fixed order before netgen compares them

_PAR_REF = """.subckt top vss
.param a=150u b=148.36u
xr1 vss vss vss ppolyf_u r_width=2u r_length={a}
xm1 vss vss vss vss nfet_05v0 w=1u l=1u
xr2 vss vss vss ppolyf_u r_width=2u r_length={b}
.ends
"""


def test_parallel_sort_swaps_whole_lines_and_keeps_the_rest_in_place():
    out = layoutlib.parallel_devices_sorted(_PAR_REF).splitlines()
    assert out[2].startswith("xr2 ") and out[4].startswith("xr1 ")
    assert out[3].startswith("xm1 ")


def test_parallel_sort_gives_both_netlists_the_same_order():
    ext = (".subckt top vss\n"
           "X19 vss vss vss ppolyf_u r_width=2u r_length=0.15m\n"
           "X34 vss vss vss ppolyf_u r_width=2u r_length=0.14836m\n"
           ".ends\n")
    a = layoutlib.parallel_devices_sorted(ext).splitlines()
    assert a[1].startswith("X34 ") and a[2].startswith("X19 ")


def test_parallel_sort_leaves_devices_on_other_nets_and_unknown_values():
    other = (".subckt top a b\n"
             "x1 a a a ppolyf_u r_length=2u\n"
             "x2 b b b ppolyf_u r_length=1u\n"
             ".ends\n")
    assert layoutlib.parallel_devices_sorted(other) == other
    unread = (".subckt top a\n"
              "x1 a a a ppolyf_u r_length={nope}\n"
              "x2 a a a ppolyf_u r_length=1u\n"
              ".ends\n")
    assert layoutlib.parallel_devices_sorted(unread) == unread

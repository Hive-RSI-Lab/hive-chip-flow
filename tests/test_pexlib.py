"""engine/lib/pexlib.py: the second extractor's read of a hardened block's
critical nets (issue #77) - the SPEF, magic netlist and .ext parsers, the
DEF via resistance, the critical-net pick and the comparison. The `slow`
test runs the real magic and OpenRCX on one Metal2 wire.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_timing  # noqa: E402
import checklib  # noqa: E402
import pexlib  # noqa: E402
import pytest  # noqa: E402

SPEF_HEAD = """*SPEF "IEEE 1481-1998"
*DESIGN "t"
*DIVIDER /
*DELIMITER :
*BUS_DELIMITER [ ]
*T_UNIT 1 NS
*C_UNIT 1 PF
*R_UNIT 1 KOHM
*L_UNIT 1 HENRY

*NAME_MAP
*1 a
*2 b\\[0\\]
"""


def spef(nets: dict[str, tuple[float, list[float]]]) -> str:
    """A SPEF with each net's total C (pF) and its *RES segments (kohm)."""
    out = SPEF_HEAD
    for name, (c, rs) in nets.items():
        out += f"\n*D_NET {name} {c}\n*CONN\n*I x:Z O\n*CAP\n1 x:Z {c}\n"
        if rs:
            out += "*RES\n" + "".join(
                f"{i + 1} {name}:{i} {name}:{i + 1} {r}\n"
                for i, r in enumerate(rs))
        out += "*END\n"
    return out


def test_parse_spef_reads_units_and_the_name_map():
    got = pexlib.parse_spef(spef({"*1": (0.012, [0.01, 0.005]),
                                  "*2": (0.003, []),
                                  "plain": (0.0005, [0.1])}))
    assert got["a"] == pytest.approx({"c_ff": 12.0, "r_ohm": 15.0})
    # escapes dropped, so the name matches magic's and OpenSTA's
    assert got["b[0]"] == pytest.approx({"c_ff": 3.0, "r_ohm": 0.0})
    assert got["plain"] == pytest.approx({"c_ff": 0.5, "r_ohm": 100.0})


def test_parse_spef_refuses_an_unknown_unit_and_an_empty_file():
    with pytest.raises(pexlib.PexError, match="C_UNIT"):
        pexlib.parse_spef(spef({"a": (1, [])}).replace("1 PF", "1 XF"))
    with pytest.raises(pexlib.PexError, match="D_NET"):
        pexlib.parse_spef(SPEF_HEAD)


def test_parse_spice_caps_counts_a_coupling_cap_on_both_nets():
    caps = pexlib.parse_spice_caps(
        "* comment C9 a b 1f\nC0 a b 1.5f\nC1 a VSUBS 0.002p\n"
        "C2 b VSUBS 500a\nR0 a b 10\nC3 a b junk\n")
    assert caps["a"] == pytest.approx(3.5)
    assert caps["b"] == pytest.approx(2.0)
    assert caps["VSUBS"] == pytest.approx(2.5)


def test_parse_ext_r_squares_each_metal_class_and_skips_the_rest():
    # three classes: a 1000-ohm/sq diffusion (skipped, >= METAL_RS_MAX), a
    # 90 mohm/sq metal and a 40 mohm/sq one; scale cancels in (P/2)^2/A
    ext = ("tech t\nresistclasses 1000000 90 40\n"
           # 100 x 1 wire in class 1 (P 202, A 100: 102.01 sq), nothing else
           'node "w" 0 1.0 0 0 m2 5 20 100 202 0 0\n'
           # 10 x 2 in class 2 (P 24, A 20: 7.2 sq)
           'node "v" 0 1.0 0 0 m3 0 0 0 0 20 24\n')
    r = pexlib.parse_ext_r(ext)
    assert r["w"] == pytest.approx(0.09 * 102.01)
    assert r["v"] == pytest.approx(0.04 * 7.2)
    with pytest.raises(pexlib.PexError, match="resistclasses"):
        pexlib.parse_ext_r('node "w" 0 1 0 0 m2 0 0\n')


TECH_LEF = """LAYER Via1
  TYPE CUT ;
  RESISTANCE 4.5 ;
END Via1
LAYER Metal2
  TYPE ROUTING ;
  RESISTANCE RPERSQ 0.09 ;
END Metal2
VIA Via1_HV DEFAULT
  RESISTANCE 4.5 ;
  LAYER Via1 ;
END Via1_HV
"""

DEF_VIAS = """VIAS 1 ;
- via1_2x2 + VIARULE Via1_GEN_HH + CUTSIZE 520 520 + LAYERS Metal1 Via1 Metal2
  + CUTSPACING 520 520 + ROWCOL 2 2 ;
END VIAS
NETS 2 ;
- n\\[1\\] ( u0 Z ) ( u1 I ) + USE SIGNAL
  + ROUTED Metal2 ( 0 0 ) ( 100 0 ) Via1_HV
  NEW Metal2 ( 100 0 ) ( 200 0 ) via1_2x2
  NEW Metal2 ( 200 0 ) ( 300 0 ) Via1_HV ;
- m ( u2 Z ) ( u3 I ) + USE SIGNAL
  + ROUTED Metal2 ( 0 0 ) ( 100 0 ) ;
END NETS
"""


def test_def_vias_at_the_tech_lef_resistance():
    vias, cuts = pexlib.lef_via_r(TECH_LEF)
    assert vias == {"Via1_HV": 4.5} and cuts == {"Via1": 4.5}
    r = pexlib.def_via_r(DEF_VIAS, vias, cuts)
    # two named vias, one generated 2 x 2 array (four cuts in parallel)
    assert r["n[1]"] == pytest.approx(4.5 + 4.5 + 4.5 / 4)
    assert r["m"] == 0.0


CRIT_OUT = (f"noise\n{pexlib.CRIT_BEGIN}\n"
            f"{pexlib.CRIT_MARK} max 1.5 a {{b[0]}} \\c\n"
            f"{pexlib.CRIT_MARK} max 2.0 a d\n"
            f"{pexlib.CRIT_MARK} min 0.1 e\n"
            f"{pexlib.CRIT_MARK} min INF unclocked\n"
            f"{pexlib.CRIT_END}\n")


def test_critical_nets_take_each_net_at_its_worst_slack():
    paths = pexlib.parse_critical_paths(CRIT_OUT)
    assert paths[0] == ("max", 1.5, ["a", "b[0]", "\\c"])
    nets = pexlib.critical_nets(paths)
    # setup and hold in turn: a (1.5, also on the 2.0 path), then hold's
    # e; hold has no more, so setup's b[0]/c at 1.5, then d. An
    # unconstrained path names nothing
    assert nets == [("a", 1.5, "setup"), ("e", 0.1, "hold"),
                    ("b[0]", 1.5, "setup"), ("c", 1.5, "setup"),
                    ("d", 2.0, "setup")]
    assert pexlib.critical_nets(paths, 2) == [("a", 1.5, "setup"),
                                              ("e", 0.1, "hold")]


def test_hold_slacks_do_not_crowd_setup_nets_out():
    """Hold slacks sit far below setup ones on a real design; ranked
    together, every one of the 20 nets would come from hold paths."""
    paths = ([("min", 0.5 + 0.01 * i, [f"h{i}"]) for i in range(30)]
             + [("max", 9.8 + 0.1 * i, [f"s{i}"]) for i in range(30)])
    nets = pexlib.critical_nets(paths)
    assert len(nets) == pexlib.CRIT_NETS
    assert [n for n, _, side in nets if side == "setup"] == [
        f"s{i}" for i in range(10)]
    assert [n for n, _, side in nets if side == "hold"] == [
        f"h{i}" for i in range(10)]
    # a net on both sides is taken once, at its first side's slack
    both = pexlib.critical_nets([("max", 9.0, ["x", "y"]),
                                 ("min", 0.2, ["x", "z"])])
    assert [n for n, _, _ in both] == ["x", "z", "y"]


def test_a_cut_short_critical_path_listing_refuses():
    with pytest.raises(pexlib.PexError, match=pexlib.CRIT_END):
        pexlib.parse_critical_paths(CRIT_OUT.split(pexlib.CRIT_END)[0])
    with pytest.raises(pexlib.PexError):
        pexlib.parse_critical_paths("nothing here\n")


def _nets(*names):
    return [(n, 0.5 + i) for i, n in enumerate(names)]


def test_compare_flags_only_past_both_tolerance_and_floor():
    sp = {"ok": {"c_ff": 10.0, "r_ohm": 100.0},
          "hi_c": {"c_ff": 10.0, "r_ohm": 100.0},
          "tiny": {"c_ff": 1.0, "r_ohm": 5.0},
          "hi_r": {"c_ff": 10.0, "r_ohm": 100.0}}
    mc = {"ok": 11.0, "hi_c": 13.0, "tiny": 1.9, "hi_r": 10.0}
    mr = {"ok": 90.0, "hi_c": 100.0, "tiny": 12.0, "hi_r": 130.0}
    out = pexlib.compare(_nets("ok", "hi_c", "tiny", "hi_r", "gone"),
                         sp, mc, mr, {"hi_r": 15.0})
    rows = {r["net"]: r for r in out["nets"]}
    assert not rows["ok"]["c_mismatch"] and not rows["ok"]["r_mismatch"]
    assert rows["hi_c"]["c_mismatch"] and rows["hi_c"]["c_ratio"] == 1.3
    # 90 % high but under the 1 fF / 10 ohm floors
    assert not rows["tiny"]["c_mismatch"] and not rows["tiny"]["r_mismatch"]
    # 130 ohm of wire plus 15 of vias against 100: x1.45
    assert rows["hi_r"]["r_mismatch"] and rows["hi_r"]["r_ratio"] == 1.45
    assert out["unmatched"] == ["gone"]
    assert out["c"]["worst_net"] == "tiny" and out["c"]["worst"] == 1.9
    assert out["thresholds"]["c_tol"] == pexlib.C_TOL


def test_a_zero_openrcx_figure_is_judged_by_the_floor():
    sp = {"z": {"c_ff": 0.0, "r_ohm": 0.0}, "zt": {"c_ff": 0.0, "r_ohm": 0.0}}
    out = pexlib.compare(_nets("z", "zt"), sp, {"z": 5.0, "zt": 0.5},
                         {"z": 300.0, "zt": 4.0}, {})
    rows = {r["net"]: r for r in out["nets"]}
    assert rows["z"]["c_ratio"] is None
    assert rows["z"]["c_mismatch"] and rows["z"]["r_mismatch"]
    assert not rows["zt"]["c_mismatch"] and not rows["zt"]["r_mismatch"]


def test_compare_unescapes_magic_names():
    sp = {"cnt[3]": {"c_ff": 10.0, "r_ohm": 100.0}}
    out = pexlib.compare([("cnt[3]", 0.5)], sp, {"cnt\\[3\\]": 10.0},
                         {"cnt\\[3\\]": 100.0}, {})
    assert out["unmatched"] == [] and out["nets"][0]["c_ratio"] == 1.0


def test_parse_spef_keeps_ports_and_inductance_out():
    text = (SPEF_HEAD + "\n*PORTS\n*1 I\n*2 O\n"
            "\n*D_NET *1 0.01\n*CONN\n*P *1 I\n*I x:Z O\n*CAP\n"
            "1 *1 0.01\n*RES\n1 *1 x:Z 0.02\n*INDUC\n1 *1 x:Z 5\n*END\n")
    nets = pexlib.parse_spef(text)
    assert set(nets) == {"a"}
    assert nets["a"]["r_ohm"] == pytest.approx(20.0)


def test_halving_one_nets_caps_in_the_spef_flags_it_by_name():
    """#77's check: a SPEF copy with one net's caps halved is flagged by
    name - and the same SPEF unhalved is clean."""
    names = [f"n{i}" for i in range(20)]
    nets = {n: (0.004 + 0.001 * i, [0.02]) for i, n in enumerate(names)}
    magic_c = {n: 1000 * c for n, (c, _) in nets.items()}
    magic_r = {n: 20.0 for n in names}
    crit = _nets(*names)
    clean = pexlib.compare(crit, pexlib.parse_spef(spef(nets)),
                           magic_c, magic_r, {})
    assert check_timing.pex_violations(
        {"corner": "nom_tt_025C_3v30", **clean}, "t", "warning") == []
    halved = dict(nets, n7=(nets["n7"][0] / 2, nets["n7"][1]))
    out = pexlib.compare(crit, pexlib.parse_spef(spef(halved)),
                         magic_c, magic_r, {})
    [v] = check_timing.pex_violations(
        {"corner": "nom_tt_025C_3v30", **out}, "t", "warning")
    assert v["kind"] == "pex_mismatch" and v["severity"] == "warning"
    assert v["refs"] == ["n7"] and "n7 (C x2.0" in v["msg"], v


# ------------------------------------------------------------ real tools

def _magic_tech(pdk_root: Path) -> Path:
    return pdk_root / "gf180mcuD" / "libs.tech" / "magic" / "gf180mcuD.tech"


def _pdk_m2_ff(pdk_root: Path, width_um: float, length_um: float) -> float:
    """The PDK's own Metal2-to-substrate figure for one isolated wire:
    the magic tech file's first (nominal) style's area and perimeter
    capacitance, aF/um^2 and aF/um, over the wire's area and both
    sides (the ends are noise at 100 um)."""
    text = _magic_tech(pdk_root).read_text(encoding="utf-8")
    area = float(re.search(r"defaultareacap\s+allm2\s+metal2\s+(\S+)",
                           text).group(1))
    per = float(re.search(r"defaultperimeter\s+allm2\s+metal2\s+(\S+)",
                          text).group(1))
    return (area * width_um * length_um + per * 2 * length_um) / 1000


def _one_wire_def(width_um: float, length_um: float) -> str:
    """One Metal2 net, min width, with a pin at one end named as the net
    (as LibreLane names an I/O net; magic names a node by its label)."""
    dbu = 2000
    w = int(width_um * dbu)
    h, x, y0, y1 = w // 2, 20000, 20000, 20000 + int(length_um * dbu)
    pins = (f"- w + NET w + DIRECTION INPUT + USE SIGNAL + PORT + LAYER "
            f"Metal2 ( -{h} -{h} ) ( {h} {h} ) + PLACED ( {x} {y0} ) N ;")
    return (
        'VERSION 5.8 ;\nDIVIDERCHAR "/" ;\nBUSBITCHARS "[]" ;\n'
        f"DESIGN wire ;\nUNITS DISTANCE MICRONS {dbu} ;\n"
        f"DIEAREA ( 0 0 ) ( 60000 {y1 + 20000} ) ;\n"
        f"PINS 1 ;\n{pins}\nEND PINS\nNETS 1 ;\n"
        f"- w ( PIN w ) + USE SIGNAL + ROUTED Metal2 "
        f"( {x} {y0} ) ( * {y1} ) ;\nEND NETS\nEND DESIGN\n")


@pytest.fixture(scope="module")
def m2_wire(tmp_path_factory):
    """A 100 um min-width Metal2 wire, extracted by magic (pexlib, as the
    gate runs it) and by OpenRCX (as LibreLane runs it), with the PDK's
    own figure for it."""
    pdk_root = check_timing._pdk_root()
    pdk = pdk_root / "gf180mcuD"
    tlef = (pdk / "libs.ref" / "gf180mcu_fd_sc_mcu7t5v0" / "techlef"
            / "gf180mcu_fd_sc_mcu7t5v0__nom.tlef")
    rules = pdk / "libs.tech" / "librelane" / "rules.openrcx.gf180mcuD.nom"
    if not tlef.is_file() or not rules.is_file():
        pytest.skip(f"no gf180mcuD tech LEF / OpenRCX rules under {pdk}")
    width, length = 0.28, 100.0
    d = tmp_path_factory.mktemp("m2_wire")
    (d / "wire.def").write_text(_one_wire_def(width, length),
                                encoding="utf-8")
    caps, _ = pexlib.extract(check_timing.EDA_BIN, checklib.run_tool, tlef,
                             [], d / "wire.def", "wire", d / "magic", 300)
    (d / "rcx.tcl").write_text(
        f"read_lef {tlef}\nread_def {d / 'wire.def'}\n"
        "define_process_corner -ext_model_index 0 X\n"
        f"extract_parasitics -ext_model_file {rules} -lef_res\n"
        f"write_spef {d / 'wire.spef'}\nexit\n", encoding="utf-8")
    subprocess.run([str(check_timing.EDA_BIN), "openroad", "-exit",
                    str(d / "rcx.tcl")], cwd=d, capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=300)
    rcx = pexlib.parse_spef((d / "wire.spef").read_text(encoding="utf-8"))
    return {"pdk": _pdk_m2_ff(pdk_root, width, length),
            "magic": caps["w"], "rcx": rcx["w"]["c_ff"]}


@pytest.mark.slow
def test_magic_lands_one_metal2_wire_within_10pct_of_the_pdk(m2_wire):
    # measured 7.12 fF against the PDK's 7.08 (#77)
    assert m2_wire["magic"] == pytest.approx(m2_wire["pdk"], rel=0.10), m2_wire


@pytest.mark.slow
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "#77: OpenRCX's gf180mcuD nom rules put an isolated min-width Metal2 "
    "wire 16 % over the PDK's area+fringe figure (8.23 fF against 7.08); "
    "strict, so the day the rules agree this turns into a failure to "
    "remove the mark"))
def test_openrcx_lands_one_metal2_wire_within_10pct_of_the_pdk(m2_wire):
    assert m2_wire["rcx"] == pytest.approx(m2_wire["pdk"], rel=0.10), m2_wire


@pytest.mark.slow
def test_openrcx_reads_the_metal2_wire_at_most_25pct_high(m2_wire):
    """The gap the xfail above records, pinned: should OpenRCX's figure
    move further from the PDK's the cross-check's 25 % C tolerance stops
    meaning anything on an isolated wire."""
    assert 1.0 < m2_wire["rcx"] / m2_wire["pdk"] < 1.25, m2_wire

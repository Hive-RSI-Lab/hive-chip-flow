"""engine/lib/emirlib.py and the em_ir gate's own helpers
(engine/scripts/check_analog_em_ir.py, issue #120): the limits table, magic's
FastHenry parse, the tier-one bound, the cut charging and the findings, on
hand-made fixtures. Hermetic: no magic or ngspice run (klayout's Python
module builds the cut pads where a test needs them). The real-tool run is
the ade corpus's em_ir plants."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import emirlib  # noqa: E402
from checklib import CheckError  # noqa: E402

LIM = emirlib.load_limits()


# --- the limits table --------------------------------------------------------

def test_limits_table_units_and_shared_contact_layer():
    m1 = LIM["layers"]["metal1"]
    assert m1["metal"] and m1["limit"] == pytest.approx(670.0)  # A/m = mA/um
    assert LIM["layers"]["poly"]["limit"] is None
    contact = LIM["cuts"][(33, 0)]
    # one drawn contact layer serves diffusion, well and poly: lowest limit
    assert contact["limit"] == pytest.approx(0.14e-3)
    assert contact["under"] and contact["pads"] == {(34, 0)}
    via1 = LIM["cuts"][(35, 0)]
    assert not via1["under"] and via1["pads"] == {(34, 0), (36, 0)}


def test_layers_of_reads_thickness_and_refuses_an_unknown_one():
    assert emirlib.layers_of(0.4, 0.55, LIM) == (
        "metal1", "metal2", "metal3", "metal4")
    assert emirlib.layers_of(1.2, 1.0, LIM) == ("metal5",)
    assert emirlib.layers_of(0.26, 0.26, LIM) == ()        # a cut
    with pytest.raises(emirlib.EmirError, match="h=0.77"):
        emirlib.layers_of(0.4, 0.77, LIM)


# --- magic's FastHenry file --------------------------------------------------

FH = """\
* List of nodes in network
.external Nvss
Nvss x=0 y=0 z=0
N1 x=4 y=0 z=0
N2 x=4 y=0 z=0
.equiv N2 N2_0_0
E1 Nvss N1 w=0.5 h=0.55
E2 N1 N2_0_1 w=0.26 h=0.26
.freq fmin=1 fmax=1
* List of nodes in network
.external Nout
Nout x=0 y=5 z=0
N1 x=3 y=5 z=0
E1 Nout N1 w=0.4 h=0.55
.freq fmin=1 fmax=1
"""


def test_parse_fh_reads_each_network_on_its_own_and_aliases_subnodes():
    nets = emirlib.parse_fh(FH)
    assert set(nets) == {"vss", "out"}
    vss = nets["vss"]
    assert vss.port == "vss" and len(vss.segs) == 2
    # N2_0_1 is in neither the node list nor the .equiv: it is N2
    assert vss.rep("2_0_1") == "2" and vss.nodes["2_0_1"] == (4.0, 0.0, 0.0)
    # node ids restart per network
    assert nets["out"].nodes["1"] == (3.0, 5.0, 0.0)


def test_parse_fh_refuses_a_segment_to_an_unlisted_node():
    bad = FH.replace("E1 Nout N1", "E1 Nout N9")
    with pytest.raises(emirlib.EmirError, match="does not list"):
        emirlib.parse_fh(bad)


# --- the operating point and the bound ---------------------------------------

def test_injections_and_bound():
    volts = {"vdd": 3.3, "vdd.n1": 3.29, "vdd.t2": 3.28}
    inj = emirlib.injections([("vdd", "vdd.n1", 10.0),
                              ("vdd.n1", "vdd.t2", 10.0)], volts)
    assert inj["vdd"] == pytest.approx(1e-3)
    assert inj["vdd.t2"] == pytest.approx(-1e-3)
    net = emirlib.FhNet("vdd", "vdd", nodes={"vdd": (0, 0, 0), "1": (4, 0, 0)},
                        segs=[("vdd", "1", 1.0, 0.55)])
    b = emirlib.net_bound("VDD", inj, net, LIM)
    assert b["i_total"] == pytest.approx(1e-3)
    assert b["w_min_um"] == 1.0
    assert b["wire_ratio"] == pytest.approx(1e-3 / 1e-6 / 670.0)
    assert b["cut_ratio"] == pytest.approx(1e-3 / 0.14e-3)


def test_bound_refuses_a_current_with_no_geometry():
    with pytest.raises(emirlib.EmirError, match="no geometry"):
        emirlib.net_bound("vdd", {"vdd": 1e-3}, None, LIM)


def test_injections_refuse_a_node_with_no_voltage():
    with pytest.raises(emirlib.EmirError, match="vdd.n1"):
        emirlib.injections([("vdd", "vdd.n1", 10.0)], {"vdd": 3.3})


def test_ir_drops_takes_each_rails_worst_node():
    volts = {"vdd": 3.3, "vdd.n1": 3.29, "vdd.t2": 3.295, "vss": 0.0,
             "vss.n1": 0.004, "out": 1.0}
    d = emirlib.ir_drops(volts, ["VDD", "vss"])
    assert d["VDD"][0] == pytest.approx(0.01) and d["VDD"][1] == "vdd.n1"
    assert d["vss"][0] == pytest.approx(0.004)


# --- findings ----------------------------------------------------------------

def _seg(amps, width, h=0.55):
    return emirlib.SegCurrent("vdd", "a", "b", (0, 0, 0), (1, 0, 0), width,
                              emirlib.layers_of(width, h, LIM), amps)


def test_wire_over_holds_metal_to_its_limit_and_skips_poly():
    ok, over = _seg(0.6e-3, 1.0), _seg(0.7e-3, 1.0)
    poly = _seg(5e-3, 0.3, h=0.2)
    hits = emirlib.wire_over([ok, over, poly], LIM)
    assert [h[0] for h in hits] == [over]
    assert hits[0][1] == pytest.approx(700.0)
    assert emirlib.worst_wire([ok, over, poly], LIM)[0] is over


def test_cuts_over_shares_an_arrays_current_by_its_cuts():
    one = emirlib.Cluster((35, 0), (0, 0, 0.26, 0.26), 1, amps=0.2e-3)
    two = emirlib.Cluster((35, 0), (2, 0, 2.62, 0.26), 2, amps=0.3e-3)
    hits = emirlib.cuts_over({(35, 0): [one, two]}, LIM)
    assert [h[0] for h in hits] == [one]
    assert emirlib.worst_cut({(35, 0): [one, two]}, LIM)[0] is one


# --- charging cut arrays -----------------------------------------------------

def _region(*boxes, dbu=0.001):
    kdb = pytest.importorskip("klayout.db")
    r = kdb.Region()
    for x0, y0, x1, y1 in boxes:
        r.insert(kdb.Box(*(int(round(v / dbu)) for v in (x0, y0, x1, y1))))
    return r


def _charge(clusters, cap=float("inf")):
    """A metal1 strap (0,0)-(4,0), 0.5 um wide, its port at (0,0) and a
    0.1 mA device current at (4,0), with the via1 arrays given."""
    net = emirlib.FhNet("vss", "vss", nodes={"vss": (0, 0, 0), "1": (4, 0, 0)},
                        segs=[("vss", "1", 0.5, 0.55)])
    placed = [("1", 1e-4)]
    segs, left = emirlib.solve_net(net, placed, LIM)
    assert left == pytest.approx(0.0)
    emirlib.assign_cuts(net, segs, placed, {(35, 0): clusters}, LIM, cap=cap)


def test_an_array_on_another_nets_metal_is_not_charged():
    # both arrays sit within CUT_REACH_UM of the node at (4,0); only the
    # first lands on metal the strap overlaps (the second is, say, a gate
    # contact's pad 0.6 um off the strap)
    mine = emirlib.Cluster((35, 0), (3.9, -0.1, 4.1, 0.1), 1,
                           pad=_region((3.8, -0.2, 4.2, 0.2)))
    other = emirlib.Cluster((35, 0), (3.9, 0.6, 4.1, 0.8), 1,
                            pad=_region((3.8, 0.5, 4.2, 0.9)))
    _charge([mine, other])
    assert mine.amps == pytest.approx(1e-4)
    assert other.amps == 0.0


def test_with_no_array_on_the_net_every_one_in_reach_is_charged():
    other = emirlib.Cluster((35, 0), (3.9, 0.6, 4.1, 0.8), 1,
                            pad=_region((3.8, 0.5, 4.2, 0.9)))
    _charge([other])
    assert other.amps == pytest.approx(1e-4)


def test_a_nets_charge_on_an_array_is_capped_at_its_total():
    mine = emirlib.Cluster((35, 0), (3.9, -0.1, 4.1, 0.1), 1)  # no pad: kept
    _charge([mine], cap=4e-5)
    assert mine.amps == pytest.approx(4e-5)


def test_footprint_takes_a_cut_as_its_node_and_leaves_out_poly():
    kdb = pytest.importorskip("klayout.db")
    # a metal1 wire, a 2.6 um square cut (magic's merged array) at (5,0)
    # and a poly wire: the square would reach the pad at (6,1)
    net = emirlib.FhNet("n", "n", nodes={"a": (0, 0, 0), "b": (2, 0, 0),
                                         "c": (5, 0, 0), "d": (5, 0, 1),
                                         "e": (5, 4, 0)},
                        segs=[("a", "b", 0.5, 0.55), ("c", "d", 2.6, 2.6),
                              ("c", "e", 1.0, 0.2)])
    fp = emirlib.net_footprint(net, 0.001, LIM)
    near = emirlib.Cluster((35, 0), (5.9, 0.9, 6.1, 1.1), 1,
                           pad=_region((5.8, 0.8, 6.2, 1.2)))
    on_poly = emirlib.Cluster((35, 0), (4.9, 2.9, 5.1, 3.1), 1,
                              pad=_region((4.8, 2.8, 5.2, 3.2)))
    own = emirlib.Cluster((35, 0), (4.9, -0.1, 5.1, 0.1), 1,
                          pad=_region((4.8, -0.2, 5.2, 0.2)))
    strap = emirlib.Cluster((35, 0), (1.9, -0.1, 2.1, 0.1), 1,
                            pad=_region((1.8, -0.2, 2.2, 0.2)))
    assert not emirlib._on_net(near, fp)
    assert not emirlib._on_net(on_poly, fp)
    assert emirlib._on_net(own, fp)
    assert emirlib._on_net(strap, fp)
    assert isinstance(fp, kdb.Region)


def test_an_orphan_goes_to_the_smallest_array_on_the_net():
    net = emirlib.FhNet("n", "n", nodes={"a": (0, 0, 0)}, segs=[])
    mine = emirlib.Cluster((33, 0), (2, 0, 3, 1), 3)
    other = emirlib.Cluster((33, 0), (2.5, 0, 2.7, 0.2), 1)  # in the band
    for on_net, want in ((lambda c: c is mine, mine),
                         (lambda c: False, other),   # none on it: every one
                         (None, other)):
        charge = {}
        rec = emirlib._charge_orphan(net, {"a"}, {(0, 0)}, 1e-4, [(33, 0)],
                                     {(33, 0): [mine, other]}, "x", charge,
                                     on_net)
        assert [c for c, _ in charge.values()] == [want]
        assert rec["cuts"] == want.cuts


def test_an_orphans_array_is_kept_between_timepoints_and_still_refused():
    # a transient charges the same orphan node at every timepoint: the
    # array it picks is geometry alone, kept in the memo, and the amps of
    # each call still land on it; a node with no array in reach is refused
    # every time, memo or not
    net = emirlib.FhNet("n", "n", nodes={"a": (0, 0, 0)}, segs=[])
    mine = emirlib.Cluster((33, 0), (2, 0, 3, 1), 3)
    calls, memo, charge = [], {}, {}
    def on_net(c):
        calls.append(c)
        return True
    for amps in (1e-4, 3e-4):
        rec = emirlib._charge_orphan(net, {"a"}, {(0, 0)}, amps, [(33, 0)],
                                     {(33, 0): [mine]}, "x", charge, on_net,
                                     memo)
        assert rec["amps"] == amps and rec["cuts"] == 3
    assert len(calls) == 1
    assert charge[id(mine)][1] == pytest.approx(4e-4)
    far = {(33, 0): [emirlib.Cluster((33, 0), (50, 0, 51, 1), 3)]}
    memo = {}                      # another net: its own memo
    for _ in range(2):
        with pytest.raises(emirlib.EmirError):
            emirlib._charge_orphan(net, {"a"}, {(0, 0)}, 1e-4, [(33, 0)],
                                   far, "x", {}, on_net, memo)


def test_an_orphan_goes_to_the_smallest_array_near_the_nearest():
    # a 3-cut array 0.5 um off the node, a 2-cut one 1.2 um off and a
    # 1-cut one 4 um off, all on the net: the far single cut is out of
    # the band, the 2-cut one is within CUT_REACH_UM of the nearest
    net = emirlib.FhNet("n", "n", nodes={"a": (0, 0, 0)}, segs=[])
    near3 = emirlib.Cluster((35, 0), (0.5, 0, 1.5, 0.26), 3)
    near2 = emirlib.Cluster((35, 0), (1.2, 0.3, 1.8, 0.56), 2)
    far1 = emirlib.Cluster((35, 0), (4, 0, 4.26, 0.26), 1)
    charge = {}
    rec = emirlib._charge_orphan(net, {"a"}, {(0, 0)}, 1e-4, [(35, 0)],
                                 {(35, 0): [near3, near2, far1]}, "x",
                                 charge, lambda c: True)
    assert [c for c, _ in charge.values()] == [near2]
    assert rec["cuts"] == 2


def test_an_arrays_pad_does_not_chain_across_its_layers(tmp_path):
    kdb = pytest.importorskip("klayout.db")
    # a via1 on a metal1 pad at the left; a metal2 strap of another net runs
    # over that pad and on to a far metal1 shape. Merged together the two
    # layers would join into one pad reaching the far shape
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("t")
    def box(layer, *b):
        top.shapes(ly.layer(*layer)).insert(
            kdb.Box(*(int(round(v / ly.dbu)) for v in b)))
    box((35, 0), 0.0, 0.0, 0.26, 0.26)
    box((34, 0), -0.1, -0.1, 0.36, 0.36)
    box((36, 0), -0.1, 0.1, 20.0, 0.6)       # the other net's metal2
    box((34, 0), 19.0, -0.1, 20.0, 1.0)      # metal1 far off, under it
    gds = tmp_path / "t.gds"
    ly.write(str(gds))
    clusters = emirlib.cut_clusters(gds, "t", LIM)[(35, 0)]
    assert len(clusters) == 1
    b = clusters[0].pad.bbox()
    assert b.right * ly.dbu == pytest.approx(20.0)   # its own metal2 strap
    # the far metal1 is not part of the pad: only the strap's metal2 is
    far = _region((19.0, -0.1, 20.0, 1.0)) - _region((19.0, 0.1, 20.0, 0.6))
    assert (clusters[0].pad & far).is_empty()


def test_cut_clusters_marks_the_substrate_taps(tmp_path):
    kdb = pytest.importorskip("klayout.db")
    # a contact on P+ COMP in the substrate is a tap; one on P+ COMP in an
    # Nwell (a PMOS source) and one on bare COMP are not
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("t")
    def box(layer, *b):
        top.shapes(ly.layer(*layer)).insert(
            kdb.Box(*(int(round(v / ly.dbu)) for v in b)))
    for x0, implant, well in ((0, True, False), (10, True, True),
                              (20, False, False)):
        box((22, 0), x0, 0, x0 + 1, 1)
        box((33, 0), x0 + 0.39, 0.39, x0 + 0.61, 0.61)
        if implant:
            box((31, 0), x0 - 0.5, -0.5, x0 + 1.5, 1.5)
        if well:
            box((21, 0), x0 - 2, -2, x0 + 3, 3)
    gds = tmp_path / "t.gds"
    ly.write(str(gds))
    taps = [round(c.box[0]) for c in emirlib.cut_clusters(gds, "t", LIM)[(33, 0)]
            if c.tap]
    assert taps == [0]


def _substrate_join(clusters):
    """vss: a metal1 strap from its port (0,0) to (4,0), where magic joins
    a substrate plane to it with no drawn contact; 0.1 mA (a transient's
    body capacitance) enters the plane at (50,0)."""
    net = emirlib.FhNet("vss", "vss", nodes={"vss": (0, 0, 0), "j": (4, 0, 0),
                                             "w": (50, 0, 0)},
                        segs=[("vss", "j", 0.5, 0.55), ("j", "w", 10, 0.01)])
    placed = [("w", 1e-4)]
    segs, left = emirlib.solve_net(net, placed, LIM)
    assert left == pytest.approx(0.0)
    return emirlib.assign_cuts(net, segs, placed, {(33, 0): clusters}, LIM)


def test_a_tapless_substrate_join_charges_every_drawn_tap():
    tap3 = emirlib.Cluster((33, 0), (100, 100, 101, 101), 3, tap=True)
    tap1 = emirlib.Cluster((33, 0), (200, 100, 200.2, 100.2), 1, tap=True)
    poly = emirlib.Cluster((33, 0), (300, 100, 300.2, 100.2), 1)
    orphans = _substrate_join([tap3, tap1, poly])
    assert tap3.amps == pytest.approx(1e-4) and tap1.amps == pytest.approx(1e-4)
    assert poly.amps == 0.0
    assert orphans[0]["charged"] == "substrate taps"
    assert orphans[0]["taps"] == 2 and orphans[0]["cuts"] == 1
    # 0.1 mA on one cut is under contact's 0.14 mA; 0.2 mA would not be
    assert emirlib.cuts_over({(33, 0): [tap1]}, LIM) == []


def test_a_tapless_substrate_join_with_no_tap_anywhere_is_refused():
    poly = emirlib.Cluster((33, 0), (300, 100, 300.2, 100.2), 1)
    with pytest.raises(emirlib.EmirError, match="draws no substrate tap"):
        _substrate_join([poly])


# --- the gate's own helpers --------------------------------------------------

import check_analog_em_ir as gate  # noqa: E402

BENCH = """\
* emir
.include {extracted}
xdut vdd 0 out mirror
.end
"""


def test_with_op_adds_the_control_block_before_end():
    out = gate.with_op(BENCH)
    assert out.index("op\nprint all") < out.rindex(".end")
    assert gate.dut_line(BENCH) == (["vdd", "0", "out"], "mirror")


def test_with_op_prints_the_operating_point_at_twelve_digits():
    # 6 digits (ngspice's default) is 1 uV on a 3 V rail: 30 nA over 33 ohm,
    # which made a deviceless n-well node look like a current source.
    out = gate.with_op(BENCH)
    assert out.index("set numdgt=12") < out.index("op\nprint all")


def test_with_op_refuses_a_bench_with_its_own_control_block():
    with pytest.raises(CheckError, match=".control"):
        gate.with_op(BENCH.replace(".end", ".control\nop\n.endc\n.end"))


def test_rails_from_spec_supply_and_the_empty_list():
    pins = ["VDD", "VSS", "OUT"]
    assert gate.rails_of({"supply": {"vdd": 3.3}}, pins) == ["VDD", "VSS"]
    assert gate.rails_of({"ir_rails": []}, ["A", "B"]) == []
    with pytest.raises(CheckError, match="ir_rails"):
        gate.rails_of({"ir_rails": ["avdd"]}, pins)
    with pytest.raises(CheckError, match="no supply rail"):
        gate.rails_of({"supply": {"vdd": 3.3}}, ["A", "B"])


def test_ir_bound_from_spec_or_two_percent_of_vdd():
    assert gate.ir_limit_v({"ir_drop_mv": 5})[0] == pytest.approx(5e-3)
    assert gate.ir_limit_v({"supply": {"vdd": 3.3}})[0] == pytest.approx(0.066)
    with pytest.raises(CheckError, match="supply.vdd"):
        gate.ir_limit_v({})


# --- spec_lint of the two fields ---------------------------------------------

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


@pytest.mark.parametrize("line,kind", [
    ("ir_drop_mv: 0", "bad_ir_drop"),
    ("ir_drop_mv: true", "bad_ir_drop"),
    ("ir_drop_mv: '5'", "bad_ir_drop"),
    ("ir_rails: vdd", "bad_ir_rails"),
    ("ir_rails: [vdd, vdd]", "bad_ir_rails"),
    ("ir_rails: ['']", "bad_ir_rails"),
])
def test_bad_ir_fields_are_refused(tmp_path, capsys, line, kind):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(GOOD_YAML + line + "\n")
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [v["kind"] for v in out["violations"]] == [kind]


@pytest.mark.parametrize("line", ["ir_drop_mv: 2.5", "ir_rails: []",
                                  "ir_rails: [vdd]"])
def test_good_ir_fields_pass(tmp_path, capsys, line):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(GOOD_YAML + line + "\n")
    assert check_spec_lint_ade.main(["--workspace", str(ws)]) == 0, \
        capsys.readouterr().out


# --- PDK resistors (issue #268) ----------------------------------------------

PEX_RES = """\
.subckt ladder a b vss
X1 a mid vss ppolyf_u r_width=0.8u r_length=10u
X2 mid b vss ppolyf_u r_width=0.8u r_length=10u
X3 b vss vss nplus_u_m1 lr=5u wr=1u
X4 a b rm1 r_length=4u r_width=0.23u
R1 a a.n1 0.5
.ends
"""


def test_resistor_instances_read_size_rule_and_probe():
    rs = emirlib.resistor_instances(PEX_RES, "ladder", LIM)
    assert [r.name for r in rs] == ["x1", "x2", "x3", "x4"]
    x1 = rs[0]
    assert (x1.model, x1.a, x1.b) == ("ppolyf_u", "a", "mid")
    assert x1.width_um == pytest.approx(0.8) and x1.length_um == pytest.approx(10)
    assert x1.rule["body"] == pytest.approx(1000.0)        # 1 mA/um
    assert x1.rule["head_limit"] == pytest.approx(0.14e-3)
    assert x1.probe("xdut") == "@b.xdut.x1.brb[i]"
    assert rs[2].rule["body"] is None and rs[2].width_um == pytest.approx(1)
    # a metal resistor is a wire: its layer's limit, no head
    assert rs[3].rule["body"] == pytest.approx(670.0)
    assert rs[3].rule["head"] is None
    # the well/substrate terminal, where a two-terminal one has none
    assert (rs[0].bulk, rs[2].bulk, rs[3].bulk) == ("vss", "vss", "")


def test_handed_on_nodes_are_bulk_terminals_and_dummy_heads():
    pex = PEX_RES.replace("R1 a a.n1 0.5",
                          "X5 vss.t3 vss.t4 vss.t0 ppolyf_u r_width=2u r_length=5u")
    rs = emirlib.resistor_instances(pex, "ladder", LIM)
    # X1-X3's bulk, and both heads of X5 (a dummy shorted to vss), not
    # X1's heads, which sit on two nets and carry the ladder's current
    assert emirlib.handed_on_nodes(rs) == frozenset(
        {"vss", "vss.t0", "vss.t3", "vss.t4"})


def test_resistor_instances_refuse_parallel_copies():
    bad = PEX_RES.replace("r_length=10u\nX2", "r_length=10u par=2\nX2")
    with pytest.raises(emirlib.EmirError, match="par=2"):
        emirlib.resistor_instances(bad, "ladder", LIM)


def test_ext_resistors_read_key_and_place_in_um():
    ext = ('scale 1000 1 0.5\n'
           'device rsubckt ppolyf_u 0 8872 1 8873 l=29672 w=400 "vss" '
           '"a_0_8872" 0 0 "n1" 400 52800,1064 "mb1" 400 52800,1064\n')
    (d,) = emirlib.ext_resistors(ext)
    assert d["key"] == ("ppolyf_u", frozenset({"n1", "mb1"}), 148.36, 2.0)
    assert d["xy"] == pytest.approx((0.0, 44.36))


def _resistor_gds(path, head_cuts):
    """A 2 x 20 um poly body with a row of `head_cuts` contacts at each end."""
    import klayout.db as kdb
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("ladder")
    poly, cont = ly.layer(30, 0), ly.layer(33, 0)
    top.shapes(poly).insert(kdb.Box(0, 0, 20000, 2000))
    for x0 in (200, 19420):
        for j in range(head_cuts):
            y = 200 + j * 500
            top.shapes(cont).insert(kdb.Box(x0, y, x0 + 220, y + 220))
    ly.write(str(path))


def _inst(name="x1", width=2.0, length=20.0):
    return emirlib.ResInst(name, "ppolyf_u", "a", "b", width, length,
                           LIM["resistors"]["ppolyf_u"])


def test_resistor_heads_count_each_rows_contacts(tmp_path):
    gds = tmp_path / "r.gds"
    _resistor_gds(gds, head_cuts=3)
    r = _inst()
    ext = [{"key": r.key, "xy": (0.0, 0.0)}]
    (h,) = emirlib.resistor_heads(gds, "ladder", [r], ext, LIM)
    assert h["cuts"] == [3, 3] and h["keys"] == [r.key]
    assert h["limit"] == pytest.approx(0.14e-3)


def test_resistor_heads_refuse_a_count_mismatch(tmp_path):
    gds = tmp_path / "r.gds"
    _resistor_gds(gds, head_cuts=3)
    with pytest.raises(emirlib.EmirError, match="in the .ext"):
        emirlib.resistor_heads(gds, "ladder", [_inst(), _inst("x2")],
                               [{"key": _inst().key, "xy": (0, 0)}], LIM)


def test_min_width_ppolyf_u_over_its_body_limit_is_a_finding():
    # 0.8 um, the PDK minimum: 1 mA/um holds it to 0.8 mA
    r = _inst(width=0.8)
    v, f = gate.check_resistors("tt", {"x1": 0.9e-3}, [r], [], LIM,
                                "layout/x.gds", "ladder")
    assert [x["kind"] for x in v] == ["em_res_body_over"]
    assert f["bodies"][0]["ratio"] == pytest.approx(0.9 / 0.8, rel=1e-3)
    v, _f = gate.check_resistors("tt", {"x1": 0.7e-3}, [r], [], LIM,
                                 "layout/x.gds", "ladder")
    assert v == []


def test_head_row_is_held_to_its_contacts():
    r = _inst()
    head = {"model": "ppolyf_u", "layer": [30, 0], "box": [0, 0, 20, 2],
            "cuts": [3, 2], "keys": [r.key], "cut": "contact_poly",
            "limit": 0.14e-3}
    # 0.3 mA over the smaller row's 2 contacts: 0.15 mA a contact
    v, f = gate.check_resistors("tt", {"x1": 0.3e-3}, [r], [head], LIM,
                                "layout/x.gds", "ladder")
    assert [x["kind"] for x in v] == ["em_res_contact_over"]
    assert f["worst_head"]["cuts"] == 2
    with pytest.raises(CheckError, match="no contact"):
        gate.check_resistors("tt", {"x1": 0.3e-3}, [r], [dict(head, cuts=[])],
                             LIM, "layout/x.gds", "ladder")


# --- transient benches: pulsed nets (issue #268) -----------------------------

RAW = """\
Title: emir
Date: today
Plotname: Transient Analysis
Flags: real
No. Variables: 3
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tv(xdut.out)\tvoltage
\t2\ti(@b.xdut.x1.brb[i])\tcurrent
Values:
 0\t0.000000e+00
\t1.0e+00
\t2.0e-03
 1\t1.000000e-06
\t1.5e+00
\t-1.0e-03
"""


def test_parse_ascii_raw_and_series_names():
    names, arr = emirlib.parse_ascii_raw(RAW)
    assert names == ["time", "v(xdut.out)", "i(@b.xdut.x1.brb[i])"]
    assert arr.shape == (2, 3) and arr[1, 2] == pytest.approx(-1e-3)
    series, t = gate._series_of(names, arr)
    assert set(series) == {"xdut.out", "@b.xdut.x1.brb[i]"}
    assert t[1] == pytest.approx(1e-6)


def test_a_source_current_does_not_overwrite_its_node_voltage():
    # ngspice saves vdd's branch current as i(vdd) beside node v(vdd): the
    # supply pin must read 3.3 V, not the source's few uA
    import numpy as np
    names = ["time", "v(vdd)", "i(vdd)", "i(@b.xdut.x1.brb[i])"]
    arr = np.array([[0.0, 3.3, -4e-5, 1e-3], [1e-6, 3.3, -5e-5, 2e-3]])
    series, _t = gate._series_of(names, arr)
    assert series["vdd"][1] == pytest.approx(3.3)
    assert series["i(vdd)"][1] == pytest.approx(-5e-5)
    assert series["@b.xdut.x1.brb[i]"][1] == pytest.approx(2e-3)


def test_wave_stats_weigh_uneven_timepoints():
    import numpy as np
    t = np.array([0.0, 0.1, 1.0])
    w = emirlib.trapezoid_weights(t)
    assert w.sum() == pytest.approx(1.0)
    mean, rms, peak = emirlib.wave_stats(np.array([1.0, 1.0, 1.0]), w)
    assert (mean, rms, peak) == pytest.approx((1.0, 1.0, 1.0))


def _pulse(peak, duty):
    import numpy as np
    eps = 1e-6
    t = np.array([0.0, eps, duty, duty + eps, 1.0])
    return t, np.array([peak, peak, peak, 0.0, 0.0])


def test_pulsed_ratios_follow_the_yaml_derating():
    import numpy as np
    t, i = _pulse(2e-3, 0.05)
    st = emirlib.wave_stats(i, emirlib.trapezoid_weights(t))
    assert st[0] == pytest.approx(1e-4, rel=1e-3)
    assert st[1] == pytest.approx(2e-3 * np.sqrt(0.05), rel=1e-3)
    r = emirlib.pulsed_ratios(*st, 1e-3, LIM)
    assert r["duty"] == pytest.approx(0.05, rel=1e-3)
    # under the floor, the peak is held to L / duty_floor
    assert r["peak"] == pytest.approx(LIM["pulsed"]["duty_floor"] * 2.0,
                                      rel=1e-3)


STRAP_FH = """\
* List of nodes in network
.external Nout
Nout x=0 y=0 z=0
N1 x=10 y=0 z=0
E1 Nout N1 w=0.4 h=0.55
.freq fmin=1 fmax=1
"""


def _strap_case(peak, duty):
    """A 0.4 um metal1 strap (limit 0.268 mA DC) fed at its far end with a
    pulse. Cuts are left out of it (a lifted cut limit)."""
    import copy as _copy
    lim = _copy.deepcopy(LIM)
    for c in lim["cuts"].values():
        c["limit"] = 1.0
    fh = emirlib.parse_fh(STRAP_FH)
    t, i = _pulse(peak, duty)
    res = [("out", "out.n1", 1.0)]
    volts = {"out": 0.0 * i, "out.n1": -i * 1.0}  # current into the wire at n1
    rnodes = {"out.n1": (10.0, 0.0)}
    return lim, fh, t, i, res, volts, rnodes


def test_a_resistor_bulk_terminal_far_off_the_geometry_is_handed_on():
    """A poly resistor's well terminal sits where magic puts it, 40 um from
    any wire of the net; in a transient its body capacitance's current
    flows there. Placed as a wire node it is refused (a lost wire); named as
    a resistor's bulk it is handed on through its extracted resistor."""
    net = emirlib.parse_fh(STRAP_FH)["out"]
    inj = {"out.t5": 1e-4}
    rnodes = {"out.t5": (50.0, 0.0), "out.n1": (10.0, 0.0)}
    res = [("out.t5", "out.n1", 1.0)]
    volts = {"out.t5": 1e-4, "out.n1": 0.0}
    with pytest.raises(emirlib.EmirError, match="um away"):
        emirlib.place(net, inj, rnodes, res, volts)
    placed, folded = emirlib.place(net, inj, rnodes, res, volts,
                                   bulk=frozenset({"out.t5"}))
    assert folded == 1
    assert placed == [("1", pytest.approx(1e-4))]


def _kinds(v):
    return sorted(x["kind"] for x in v)


def test_a_narrow_pulsed_strap_passes_dc_but_fails_rms():
    lim, fh, t, i, res, volts, rnodes = _strap_case(2e-3, 0.05)
    # the DC check at the mean current (0.1 mA) passes the strap
    mean = float(emirlib.wave_stats(i, emirlib.trapezoid_weights(t))[0])
    dc = {"out": 0.0, "out.n1": -mean}
    v, _f = gate.analyse("tt", dc, fh, rnodes, res, {}, lim, [], 1.0,
                         "layout/x.gds", "strap")
    assert v == []
    v, f = gate.analyse_tran("tt", t, volts, fh, rnodes, res, {}, lim,
                             "layout/x.gds", "strap")
    assert _kinds(v) == ["em_wire_rms_over"]
    assert f["worst_wire"]["ratio"] > 1.0


def test_a_rare_narrow_pulse_fails_the_duty_scaled_peak_only():
    # duty 0.5 %: rms 0.21 mA and mean 15 uA pass, 0.1 x 3 mA does not
    lim, fh, t, i, res, volts, rnodes = _strap_case(3e-3, 0.005)
    v, _f = gate.analyse_tran("tt", t, volts, fh, rnodes, res, {}, lim,
                              "layout/x.gds", "strap")
    assert _kinds(v) == ["em_wire_peak_over"]


def test_a_quiet_pulsed_strap_passes():
    lim, fh, t, i, res, volts, rnodes = _strap_case(0.2e-3, 0.5)
    v, f = gate.analyse_tran("tt", t, volts, fh, rnodes, res, {}, lim,
                             "layout/x.gds", "strap")
    assert v == [] and f["nets"][0]["i_peak"] == pytest.approx(0.2e-3)


def test_with_tran_saves_the_probes_and_needs_a_tran_line():
    tb = BENCH.replace(".end", ".tran 1n 1u\n.end")
    out = gate.with_tran(tb, "x.raw", ["@b.xdut.x1.brb[i]"])
    assert "save all @b.xdut.x1.brb[i]\nrun\nwrite x.raw" in out
    assert out.index(".control") < out.rindex(".end")
    with pytest.raises(CheckError, match=".tran"):
        gate.with_tran(BENCH, "x.raw")


WELL_PEX = """\
.subckt pump vdd out
X1 out g vdd.t1 vdd.t2 pfet_03v3 w=50u l=4u
X2 out g vdd.t3 vdd.t4 pfet_03v3 w=50u l=4u
X3 out vss vss ppolyf_u r_width=1u r_length=10u
R1 vdd.t2 vdd.n9 80
R2 vdd.n9 vdd.t4 80
R3 vdd.n9 vdd.n1 30
R4 vdd.n1 vdd.n7 1
R5 vdd.n7 vdd.n8 1
.ends pump
"""


def test_device_terminals_names_each_mos_bulk():
    terms, bulks = emirlib.device_terminals(WELL_PEX, "pump")
    assert bulks == {"vdd.t2", "vdd.t4"}
    assert {"out", "g", "vdd.t1", "vdd.t3", "vss"} <= terms
    assert "vdd.n9" not in terms


def test_body_nodes_take_the_well_between_bulk_pins_and_stop_at_metal():
    """vdd.n9 is n-well between the two PFETs' bulk pins, 20 um from the
    vdd metal: it joins the body. vdd.n1 is the tap's metal (on the
    geometry), so vdd.n8 beyond it, far and deviceless as it is, stays a
    wire node the gate defends."""
    fh = emirlib.parse_fh(STRAP_FH.replace("Nout", "Nvdd"))
    rnodes = {"vdd.t2": (25.0, 0.0), "vdd.t4": (35.0, 0.0),
              "vdd.n9": (30.0, 0.0), "vdd.n1": (10.0, 0.0),
              "vdd.n7": (5.0, 0.0), "vdd.n8": (40.0, 0.0)}
    terms, bulks = emirlib.device_terminals(WELL_PEX, "pump")
    res = emirlib.subckt_resistors(WELL_PEX, "pump")
    body = emirlib.body_nodes(emirlib.handed_on_nodes([], bulks), terms,
                              res, fh, rnodes)
    assert body == {"vdd.t2", "vdd.t4", "vdd.n9"}


def test_a_well_node_far_off_the_geometry_is_handed_on_as_body():
    """The PLL case: a deviceless well node 17.6 um from the vdd metal
    carries its own capacitance current in a transient."""
    net = emirlib.parse_fh(STRAP_FH)["out"]
    inj = {"out.n9": 7e-8}
    rnodes = {"out.n9": (30.0, 0.0), "out.n1": (10.0, 0.0)}
    res = [("out.n9", "out.n1", 33.0)]
    volts = {"out.n9": 7e-8 * 33.0, "out.n1": 0.0}
    with pytest.raises(emirlib.EmirError, match="um away"):
        emirlib.place(net, inj, rnodes, res, volts)
    placed, folded = emirlib.place(net, inj, rnodes, res, volts,
                                   bulk=frozenset({"out.n9"}))
    assert folded == 1
    assert placed == [("1", pytest.approx(7e-8))]

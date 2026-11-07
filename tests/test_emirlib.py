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

"""engine/lib/filllib.py: the post-fill view pex_sim benches when spec.yaml
asks for `pex_fill` (#136) - which nets are fill, the two ways fill is
tied off (floating, folded out exactly; grounded), and the spec field."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))

import filllib  # noqa: E402
import layoutlib  # noqa: E402
import statelib  # noqa: E402

# two real nets a, b and vss; f1, f2 fill islands joined by C; f3 an island
# that touches only other fill (a group with no real net); m1 a device
PEX = """\
* extracted
.subckt blk a b vss
m1 a b vss vss nfet w=1u l=1u
r1 a n1 10
Cp a b 1f
C1 a f1 2f
C2 f1 f2 2f
C3 f2 b 1f
C4 f3 f4 5f
C5 f1 vss 0
.ends
.subckt other x
C9 x f1 1f
.ends
"""


def _caps(text: str, subckt: str = "blk") -> dict[tuple[str, str], float]:
    import netlistlib
    start, end = filllib._in_subckt(text, subckt)
    out: dict[tuple[str, str], float] = {}
    for line in text[start:end].splitlines():
        p = line.split()
        if p and p[0][0].lower() == "c":
            key = tuple(sorted((p[1], p[2])))
            out[key] = out.get(key, 0.0) + netlistlib.spice_number(p[3])
    return out


def test_fill_nets_are_the_nets_only_capacitors_touch():
    assert filllib.fill_nets(PEX, "blk") == {"f1", "f2", "f3", "f4"}
    # pins, device terminals and resistor ends are never fill
    assert filllib.fill_nets(PEX, "other") == {"f1"}


def test_float_fill_folds_series_islands_into_one_cap():
    nets = filllib.fill_nets(PEX, "blk")
    out = filllib.float_fill(PEX, "blk", nets)
    caps = _caps(out)
    # a -2f- f1 -2f- f2 -1f- b: series 2,2,1 -> 0.4 fF; f1's 0 F cap to vss
    # adds nothing; the f3-f4 group reaches no real net and drops out
    assert caps[("a", "b")] == pytest.approx(1e-15 + 0.4e-15, rel=1e-5)
    assert set(caps) == {("a", "b")}
    assert not filllib.fill_nets(out, "blk")
    # everything that is not a fill cap stays, and other subckts are untouched
    assert "m1 a b vss vss nfet" in out and "r1 a n1 10" in out
    assert out.endswith(".subckt other x\nC9 x f1 1f\n.ends\n")


def test_float_fill_is_the_exact_divider_of_a_star():
    # one island coupled to three real nets: C_xy = Cx*Cy / sum
    text = (".subckt s a b c\nm1 a b c c n\nC1 a f 1f\nC2 b f 2f\n"
            "C3 c f 3f\n.ends\n")
    caps = _caps(filllib.float_fill(text, "s", {"f"}), "s")
    for (x, y), cx_cy in {("a", "b"): 2, ("a", "c"): 3, ("b", "c"): 6}.items():
        assert caps[(x, y)] == pytest.approx(cx_cy / 6 * 1e-15, rel=1e-5)


def test_float_fill_refuses_a_cap_value_it_cannot_read():
    text = ".subckt s a\nm1 a a a a n\nC1 a f {cc}\n.ends\n"
    with pytest.raises(layoutlib.LayoutError, match="not a number"):
        filllib.float_fill(text, "s", {"f"})


def test_ground_fill_ties_every_island_to_ground():
    nets = filllib.fill_nets(PEX, "blk")
    caps = _caps(filllib.ground_fill(PEX, "blk", nets, "vss"))
    # a's and b's C to fill become C to vss; fill-to-fill and fill-to-vss
    # caps end up with both ends on vss and are dropped
    assert caps == {("a", "b"): 1e-15, ("a", "vss"): 2e-15,
                    ("b", "vss"): 1e-15}


def test_ground_net_is_the_first_ground_pin_else_global_ground():
    assert filllib.ground_net(["out", "VSS", "vdd"]) == "VSS"
    assert filllib.ground_net(["out", "vdd"]) == "0"


def test_no_such_subckt_is_an_error():
    with pytest.raises(layoutlib.LayoutError):
        filllib.float_fill(PEX, "nope", set())


@pytest.mark.parametrize("field, want", [
    (None, []), ("all", ["floating", "grounded"]),
    (["grounded"], ["grounded"]),
    (["grounded", "floating"], ["floating", "grounded"]),
])
def test_modes_of(field, want):
    assert filllib.modes_of(field) == want


@pytest.mark.parametrize("field", [
    "floating", [], ["floating", "floating"], ["nope"], [1], 3, {}])
def test_modes_of_refuses_anything_else(field):
    with pytest.raises(ValueError, match="pex_fill"):
        filllib.modes_of(field)


def test_fill_views_are_kept_and_judged_by_hash_alone(tmp_path):
    view = tmp_path / "log" / "pex_sim" / "blk_fill_floating.spice"
    view.parent.mkdir(parents=True)
    view.write_text(".subckt blk a\n.ends\n", encoding="utf-8")
    sha = statelib.hash_artifact(view, "text_eol")
    facts = {"fill": {"views": [{"mode": "floating", "sha256": sha,
                                 "netlist": "log/pex_sim/blk_fill_floating.spice"}]}}
    views = statelib.views_of(facts)
    assert views == {"fill_floating": {
        "path": "log/pex_sim/blk_fill_floating.spice", "sha256": sha,
        "kind": "fill"}}
    entry = {"last": {"views": views}}
    # no RC-variant pair to check: an unchanged fill view stands
    assert statelib.view_changes(entry, tmp_path) == []
    view.write_text(".subckt blk a\nC1 a 0 1f\n.ends\n", encoding="utf-8")
    assert statelib.view_changes(entry, tmp_path) == ["view:fill_floating"]

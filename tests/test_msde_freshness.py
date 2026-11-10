"""A stale gate pass never reads as fresh (#113, #138).

#113: every msde gate hashes every file it reads. A planted edit to one input
(a tt_pins entry in the digital spec.yaml, a W in analog/sizing/sizing.yaml,
a width in the root digital_spec.yaml, ...) stales exactly the gates that read
it and the gates after those (#147), and leaves the others fresh. The expected
sets are written out here from what each check script opens, not read back
from invalidation.yaml, so a row that drops an input fails this test.

#164: the LEF precheck copies in and release stages, and the SDF and SPEF
release archives, are inputs of the gates that read them, so a swapped view
stales those gates and no others.

#138: gate records pin the PDK. A changed open_pdks commit, model file or
liberty stales the gates that read it through PDK_ROOT and no other; a record
made before a kind joined its gate's row reads stale, not fresh.

Hermetic: the PDK is a fake gf180mcuD tree under a fake EDA_TOOLCHAIN.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402
import yaml  # noqa: E402

import state as state_mod  # noqa: E402
import statelib  # noqa: E402

MSDE_GATES = ("split", "cosim", "top_harden", "top_drc", "top_lvs",
              "precheck", "release")


@pytest.fixture
def pdk(tmp_path, monkeypatch):
    """A fake toolchain whose gf180mcuD tree has every file a PDK kind
    names; statelib asked afresh, the hash cache in tmp."""
    t = tmp_path / "toolchain"
    root = t / "foss" / "pdks" / "gf180mcuD"
    files = {
        "SOURCES": "open_pdks 1689ac3f2dc763876eaf967227c7dfe831b031ae\n",
        "libs.tech/ngspice/sm141064.ngspice": ".model nfet_03v3 nmos\n",
        "libs.tech/ngspice/design.ngspice": ".include sm141064.ngspice\n",
        "libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/"
        "gf180mcu_fd_sc_mcu7t5v0__tt_025C_3v30.lib": "library (tt) { }\n",
        "libs.tech/magic/gf180mcuD.tech": "tech gf180mcuD\n",
        "libs.tech/netgen/gf180mcuD_setup.tcl": "permute default\n",
        "libs.tech/klayout/tech/gf180mcu.lyt": "<technology/>\n",
        "libs.tech/librelane/config.tcl": "set ::env(X) 1\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (t / "foss" / "pdks" / "ciel" / "gf180mcu").mkdir(parents=True)
    (t / "foss" / "pdks" / "ciel" / "gf180mcu" / "current").write_text(
        "1689ac3f2dc763876eaf967227c7dfe831b031ae\n")
    monkeypatch.setenv("EDA_TOOLCHAIN", str(t))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(statelib, "_PDK_ROOT_CACHE", [], raising=False)
    return root


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _y(path: Path, doc) -> None:
    _w(path, yaml.safe_dump(doc, sort_keys=False))


def msde_block(ws: Path) -> state_mod.State:
    """An msde workspace with every file its top gates read, every gate
    recorded as a pass on them."""
    st = state_mod.State.init(ws, "msde", "blk")
    _y(ws / "interface.yaml", {"signals": [
        {"name": "ctrl", "direction": "d2a", "level": "3v3", "domain": "clk",
         "width": 4, "load": "1p"}], "ua_pins": {"vout": 0}})
    _y(ws / "digital_spec.yaml", {"interface": [
        {"name": "ctrl", "direction": "out", "level": "3v3", "domain": "clk",
         "width": 4, "load": "1p"}]})
    _y(ws / "analog_spec.yaml", {"interface": [
        {"name": "ctrl", "direction": "in", "level": "3v3", "domain": "clk",
         "width": 4, "load": "1p"}], "pads": ["vout"]})
    _y(ws / "digital" / "spec" / "spec.yaml", {
        "top": "blk", "ports": {"ctrl": {"width": 4}},
        "tt_pins": {"ctrl": "uo_out[3:0]", "en": "ui_in[0]"}})
    _w(ws / "digital" / "rtl" / "blk.v", "module blk(); endmodule\n")
    _w(ws / "digital" / "harden" / "constraints.sdc", "set_load 0.1\n")
    _y(ws / "analog" / "spec" / "spec.yaml", {"top": "blk_analog"})
    _w(ws / "analog" / "netlist" / "blk_analog.cir",
       ".subckt blk_analog ctrl vout\nM1 vout ctrl 0 0 nfet_03v3 W=w1\n.ends\n")
    _y(ws / "analog" / "sizing" / "sizing.yaml", {"w1": {"value": "2u"}})
    _w(ws / "analog" / "layout" / "gen_blk_analog.py", "def build(): pass\n")
    _w(ws / "tb" / "test_blk.py", "async def test(dut): pass\n")
    _w(ws / "top" / "spec" / "spec.yaml", "top: blk\n")
    _w(ws / "top" / "macros" / "blk_analog.spice", ".subckt blk_analog\n.ends\n")
    _w(ws / "top" / "harden" / "runs" / "run" / "final" / "gds" / "t.gds", "G")
    _w(ws / "top" / "harden" / "runs" / "run" / "final" / "pnl" / "t.v", "N")
    for view, text in (("lef", "L"), ("sdf", "S"), ("spef", "P")):
        _w(ws / "top" / "harden" / "runs" / "run" / "final" / view
           / f"t.{view}", text)
    for g in MSDE_GATES:
        st.record_gate(g, {"status": "pass"})
    st.save()
    return st


def stale_after(ws: Path, edit) -> set[str]:
    edit()
    data = state_mod.State.load(ws / "state.json").data
    rep = statelib.freshness_report(data, ws)
    assert not rep["summary"]["unknown"]
    return set(rep["summary"]["stale"])


def _edit_yaml(path: Path, fn):
    def go():
        doc = yaml.safe_load(path.read_text())
        fn(doc)
        _y(path, doc)
    return go


def _set(doc, keys, value):
    for k in keys[:-1]:
        doc = doc[k]
    doc[keys[-1]] = value


# the gates that read what top_harden writes (gate_writes, #147): an edit
# that stales top_harden stales them too, before it has rewritten top/.
# release reads its LEF, SDF and SPEF (#164).
AFTER_TOP_HARDEN = {"top_drc", "top_lvs", "top_erc", "precheck", "release"}

# (what is edited, the edit, the gates that read it - from the scripts, plus
# the gates after a stale one)
PLANTED = {
    # check_top_harden.assemble copies tt_pins (minus interface signals)
    # into top/spec; release attests the nested spec; cosim builds the
    # same top's pad frame from it (#208); split reads its clock domains
    # (check_split.digital_clock_domains, #169)
    "tt_pins entry in digital/spec/spec.yaml": (
        lambda ws: _edit_yaml(ws / "digital" / "spec" / "spec.yaml",
                              lambda d: _set(d, ["tt_pins", "en"], "ui_in[1]")),
        {"split", "cosim", "top_harden", "release"} | AFTER_TOP_HARDEN),
    # check_split.cdc_findings holds the digital side's cdc ports to the
    # interface (#169); the digital side's own cdc gate is in its nested
    # state, not here
    "ports in digital/spec/cdc.yaml": (
        lambda ws: (lambda: _y(ws / "digital" / "spec" / "cdc.yaml",
                               {"ports": {"ctrl": "async"}})),
        {"split"}),
    # the sized .subckt: cosim's bench and top_harden's macro, which
    # top_lvs reads back from top/macros
    "W in analog/sizing/sizing.yaml": (
        lambda ws: _edit_yaml(ws / "analog" / "sizing" / "sizing.yaml",
                              lambda d: _set(d, ["w1", "value"], "4u")),
        {"cosim", "top_harden", "release"} | AFTER_TOP_HARDEN),
    # check_split.SIDE_SPECS
    "width in digital_spec.yaml": (
        lambda ws: _edit_yaml(ws / "digital_spec.yaml",
                              lambda d: _set(d, ["interface", 0, "width"], 8)),
        {"split", "release"}),
    "pads in analog_spec.yaml": (
        lambda ws: _edit_yaml(ws / "analog_spec.yaml",
                              lambda d: _set(d, ["pads"], ["vout", "vref"])),
        {"split", "release"}),
    # reference_cell reads the analog spec's top; cosim names the frame's
    # macro model after it (#208)
    "top in analog/spec/spec.yaml": (
        lambda ws: _edit_yaml(ws / "analog" / "spec" / "spec.yaml",
                              lambda d: _set(d, ["top"], "blk_ana")),
        {"cosim", "top_harden"} | AFTER_TOP_HARDEN),
    "analog netlist": (
        lambda ws: (lambda: _w(ws / "analog" / "netlist" / "blk_analog.cir",
                               ".subckt blk_analog ctrl vout\n.ends\n")),
        {"cosim", "top_harden", "release"} | AFTER_TOP_HARDEN),
    # #147: the layout generator is read by top_harden alone; top_drc and
    # top_lvs follow it, with no analog/layout row of their own
    "analog/layout generator": (
        lambda ws: (lambda: _w(ws / "analog" / "layout" / "gen_blk_analog.py",
                               "def build(): return 1\n")),
        {"top_harden", "release"} | AFTER_TOP_HARDEN),
    "top/macros .subckt": (
        lambda ws: (lambda: _w(ws / "top" / "macros" / "blk_analog.spice",
                               ".subckt blk_analog x\n.ends\n")),
        {"top_lvs"}),
    # #164: precheck copies the LEF into its design dir and release stages
    # it; release archives the SDF and SPEF, which no other gate reads
    "top LEF": (
        lambda ws: (lambda: _w(ws / "top" / "harden" / "runs" / "run" /
                               "final" / "lef" / "t.lef", "L2")),
        {"precheck", "release"}),
    "top SDF": (
        lambda ws: (lambda: _w(ws / "top" / "harden" / "runs" / "run" /
                               "final" / "sdf" / "t.sdf", "S2")),
        {"release"}),
    "top SPEF": (
        lambda ws: (lambda: _w(ws / "top" / "harden" / "runs" / "run" /
                               "final" / "spef" / "t.spef", "P2")),
        {"release"}),
    "top powered netlist": (
        lambda ws: (lambda: _w(ws / "top" / "harden" / "runs" / "run" /
                               "final" / "pnl" / "t.v", "N2")),
        {"top_lvs"}),
}


@pytest.mark.parametrize("what", sorted(PLANTED))
def test_a_planted_msde_edit_stales_exactly_the_gates_that_read_it(
        tmp_path, pdk, what):
    ws = tmp_path / "blk"
    ws.mkdir()
    msde_block(ws)
    make_edit, expected = PLANTED[what]
    assert stale_after(ws, lambda: None) == set()
    assert stale_after(ws, make_edit(ws)) == expected


def test_a_new_open_pdks_commit_stales_every_pdk_gate_and_only_those(
        tmp_path, pdk):
    ws = tmp_path / "blk"
    ws.mkdir()
    msde_block(ws)
    stale = stale_after(ws, lambda: _w(pdk / "SOURCES", "open_pdks deadbeef\n"))
    assert stale == {"cosim", "top_harden", "top_drc", "top_lvs", "top_erc",
                     "precheck", "release"}


def test_a_new_toolchain_root_stales_the_pdk_gates(tmp_path, pdk, monkeypatch):
    """An image move: the same PDK content under another root still reads
    the same; a missing PDK reads stale."""
    ws = tmp_path / "blk"
    ws.mkdir()
    msde_block(ws)
    import shutil
    other = tmp_path / "toolchain2"
    shutil.copytree(tmp_path / "toolchain", other)
    monkeypatch.setenv("EDA_TOOLCHAIN", str(other))
    monkeypatch.setattr(statelib, "_PDK_ROOT_CACHE", [], raising=False)
    assert stale_after(ws, lambda: None) == set()
    (other / "foss" / "pdks" / "gf180mcuD" / "SOURCES").unlink()
    monkeypatch.setattr(statelib, "_PDK_ROOT_CACHE", [], raising=False)
    assert "top_harden" in stale_after(ws, lambda: None)


def _skill_block(ws: Path, skill: str, gates) -> None:
    st = state_mod.State.init(ws, skill, "blk")
    _y(ws / "spec" / "spec.yaml", {"top": "blk"})
    _w(ws / "rtl" / "blk.v", "module blk(); endmodule\n")
    _w(ws / "netlist" / "blk.cir", ".subckt blk a\n.ends\n")
    for g in gates:
        st.record_gate(g, {"status": "pass"})
    st.save()


def test_a_model_byte_stales_the_ade_sims_not_spec_lint(tmp_path, pdk):
    ws = tmp_path / "blk"
    ws.mkdir()
    _skill_block(ws, "ade", ("spec_lint", "netlist_lint", "sim_tt", "mc",
                             "drc"))
    stale = stale_after(ws, lambda: _w(
        pdk / "libs.tech/ngspice/sm141064.ngspice", ".model nfet_03v3 pmos\n"))
    assert stale == {"sim_tt", "mc"}


def test_a_liberty_byte_stales_synth_timing_harden_not_lint(tmp_path, pdk):
    ws = tmp_path / "blk"
    ws.mkdir()
    _skill_block(ws, "vde", ("lint", "sim", "synth", "harden", "timing",
                             "drc"))
    lib = next((pdk / "libs.ref").rglob("*.lib"))
    stale = stale_after(ws, lambda: lib.write_text("library (tt) { x }\n"))
    assert stale == {"synth", "harden", "timing", "drc"}    # drc after harden


def test_a_record_made_before_an_input_joined_its_row_reads_stale(
        tmp_path, pdk):
    """A pass recorded when the gate's row had no pdk_version (every block
    recorded before #138) vouches for no PDK: stale until it re-runs."""
    ws = tmp_path / "blk"
    ws.mkdir()
    _skill_block(ws, "vde", ("lint", "synth"))
    data = state_mod.State.load(ws / "state.json").data
    del data["gates"]["synth"]["last"]["inputs"]["pdk_version"]
    rep = statelib.freshness_report(data, ws)
    assert rep["gates"]["synth"]["changed_inputs"] == ["pdk_version"]
    assert rep["summary"]["stale"] == ["synth"]
    assert rep["summary"]["fresh"] == ["lint"]


def test_pdk_version_reads_open_pdks_commit_and_ciel(pdk):
    v = statelib.pdk_version()
    assert v["open_pdks"] == "1689ac3f2dc763876eaf967227c7dfe831b031ae"
    assert v["ciel"] == "1689ac3f2dc763876eaf967227c7dfe831b031ae"


def test_pdk_hash_cache_sees_an_in_place_edit(tmp_path, pdk):
    f = pdk / "libs.tech/ngspice/sm141064.ngspice"
    a = statelib.hash_pdk(f, "text_eol")
    assert statelib.hash_pdk(f, "text_eol") == a
    f.write_text(".model nfet_03v3 nmos level=54\n")
    assert statelib.hash_pdk(f, "text_eol") != a


def test_no_toolchain_hashes_pdk_kinds_none(tmp_path, monkeypatch):
    monkeypatch.setattr(statelib, "_PDK_ROOT_CACHE", [None])
    imap = statelib.load_map()
    assert statelib.hash_kind(tmp_path, "pdk_version", imap) == (
        "${PDK_ROOT}/SOURCES", None)

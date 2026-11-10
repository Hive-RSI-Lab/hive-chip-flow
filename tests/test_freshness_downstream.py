"""Freshness carries downstream (#147).

The hash layer compares a gate's own recorded inputs only, so before #147 an
undeclared rtl/ edit staled harden while timing, drc, lvs, glsim, lec and
precheck read fresh: harden/ had not changed yet. invalidation.yaml's
gate_writes now says which gate writes which kind, and a gate after a gate
that is not fresh, or after one that re-ran since it was recorded, reads
stale in `freshness`, the router's preconditions and attest (the score).

Hermetic: no PDK, no tools; the gate records are written by state.py.
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

import attest  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402
import task_router as tr  # noqa: E402

VDE_GATES = ("lint", "sim", "synth", "harden", "timing", "drc", "lvs",
             "glsim", "lec", "precheck", "release")
AFTER_HARDEN = {"timing", "drc", "lvs", "glsim", "lec", "precheck", "release"}


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(autouse=True)
def no_pdk(tmp_path, monkeypatch):
    """No toolchain: every PDK kind hashes None, the same at record and
    report time, so only workspace edits move a gate."""
    monkeypatch.setenv("EDA_TOOLCHAIN", str(tmp_path / "no-toolchain"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(statelib, "_PDK_ROOT_CACHE", [], raising=False)


def vde_block(ws: Path) -> state_mod.State:
    st = state_mod.State.init(ws, "vde", "blk")
    _w(ws / "spec" / "spec.yaml", yaml.safe_dump({"top": "blk"}))
    _w(ws / "rtl" / "blk.v", "module blk(); endmodule\n")
    _w(ws / "tb" / "test_blk.py", "async def test(dut): pass\n")
    _w(ws / "harden" / "info.yaml", "project: {}\n")
    _w(ws / "harden" / "runs" / "run" / "final" / "gds" / "blk.gds", "G")
    for g in VDE_GATES:
        st.record_gate(g, {"status": "pass"})
    st.save()
    return st


def report(ws: Path) -> dict:
    data = state_mod.State.load(ws / "state.json").data
    return statelib.freshness_report(data, ws)


def set_ts(ws: Path, ts: dict[str, str]) -> None:
    st = state_mod.State.load(ws / "state.json")
    for g, t in ts.items():
        st.data["gates"][g]["last"]["ts"] = t
    st.save()


def test_upstream_order_comes_from_what_each_gate_writes():
    imap = statelib.load_map()
    for g in AFTER_HARDEN:
        assert statelib.upstream_gates("vde", g, imap) == ["harden"]
    # harden rewrites harden/config.json, one of its own inputs: no self-edge
    assert statelib.upstream_gates("vde", "harden", imap) == []
    for g in ("top_drc", "top_lvs", "top_erc", "precheck"):
        assert statelib.upstream_gates("msde", g, imap) == ["top_harden"]
    assert statelib.upstream_gates("msde", "top_harden", imap) == ["macro_lib"]


def test_top_lvs_lists_no_by_hand_derived_inputs():
    """The analog sources top/macros derives from are top_harden's inputs;
    top_lvs follows them through top_harden, not through its own row."""
    row = statelib.load_map()["gate_inputs"]["msde"]["top_lvs"]
    assert not {"analog_layout", "analog_netlist", "analog_sizing"} & set(row)


def test_an_undeclared_rtl_edit_stales_every_gate_after_harden(tmp_path):
    ws = tmp_path / "blk"
    ws.mkdir()
    vde_block(ws)
    assert report(ws)["summary"]["stale"] == []
    _w(ws / "rtl" / "blk.v", "module blk(input a); endmodule\n")
    rep = report(ws)
    # lint, sim, synth, harden, lec, release read rtl/ themselves; the rest
    # read only harden/, which has not changed
    assert set(rep["summary"]["stale"]) == (
        {"lint", "sim", "synth", "harden"} | AFTER_HARDEN)
    assert rep["gates"]["drc"]["hash_valid"] is True
    assert rep["gates"]["drc"]["upstream_stale"] == [
        "upstream gate harden is not fresh"]


def test_a_gate_after_one_that_re_ran_since_reads_stale(tmp_path):
    ws = tmp_path / "blk"
    ws.mkdir()
    vde_block(ws)
    set_ts(ws, {g: "2026-01-01T00:00:00" for g in VDE_GATES})
    assert report(ws)["summary"]["stale"] == []
    # harden re-ran to the same harden/ (a reproducible re-harden): its
    # hashes still match, but drc and the rest vouch for an earlier run
    set_ts(ws, {"harden": "2026-01-02T00:00:00"})
    rep = report(ws)
    assert set(rep["summary"]["stale"]) == AFTER_HARDEN
    assert "re-ran at 2026-01-02T00:00:00" in rep["gates"]["drc"][
        "upstream_stale"][0]
    # each re-runs after it, and reads fresh again
    set_ts(ws, {g: "2026-01-03T00:00:00" for g in AFTER_HARDEN})
    assert report(ws)["summary"]["stale"] == []


def test_a_same_second_record_is_not_a_re_run(tmp_path):
    ws = tmp_path / "blk"
    ws.mkdir()
    vde_block(ws)
    set_ts(ws, {g: "2026-01-01T00:00:00" for g in VDE_GATES})
    assert report(ws)["summary"]["stale"] == []


def test_an_unrecorded_upstream_stales_nothing(tmp_path):
    """A gate that never ran upstream has not re-run since: the gates after
    it keep their own verdicts (an msde block without macro_lib)."""
    ws = tmp_path / "blk"
    ws.mkdir()
    vde_block(ws)
    st = state_mod.State.load(ws / "state.json")
    del st.data["gates"]["harden"]
    st.save()
    assert report(ws)["summary"]["stale"] == []


def test_router_and_attest_read_the_downstream_gate_stale(tmp_path):
    ws = tmp_path / "blk"
    ws.mkdir()
    vde_block(ws)
    _w(ws / "rtl" / "blk.v", "module blk(input a); endmodule\n")
    data = state_mod.State.load(ws / "state.json").data
    view = tr.resume_view({"state": data, "workspace": str(ws)})
    assert "drc" in view["gates_stale"]
    [pc] = tr.check_preconditions({"preconditions": ["gates_fresh:drc"]},
                                  {"exists": True}, view)
    assert not pc["ok"] and "stale" in pc["detail"]
    imap = statelib.load_map()
    fresh = statelib.freshness_report(data, ws, imap)
    check = attest.gate_check(ws, "vde", "drc", data, imap, fresh)
    assert check["ok"] is False
    assert check["reason"] == "stale: upstream gate harden is not fresh"


def test_load_map_rejects_a_gate_order_cycle(tmp_path):
    bad = tmp_path / "invalidation.yaml"
    bad.write_text("""
artifact_kinds:
  x: {path: "x", norm: text_eol}
  y: {path: "y", norm: text_eol}
gate_inputs:
  vde: {g1: [x], g2: [y]}
gate_writes:
  vde: {g1: [y], g2: [x]}
edit_classes:
  vde: {}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        statelib.load_map(bad)


def test_load_map_rejects_gate_writes_naming_an_unknown_gate(tmp_path):
    bad = tmp_path / "invalidation.yaml"
    bad.write_text("""
artifact_kinds:
  x: {path: "x", norm: text_eol}
gate_inputs:
  vde: {g1: [x]}
gate_writes:
  vde: {g9: [x]}
edit_classes:
  vde: {}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown gate"):
        statelib.load_map(bad)

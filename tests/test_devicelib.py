"""#137: devices.yaml (engine/scripts/pdk_devices.py), its readers in
engine/lib/devicelib.py, the layoutlib wrappers that refuse a device whose
round trip failed, and netlist_lint's device checks. The fast tests use a
planted table; the `slow` ones run the real round trip."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402

import check_netlist_lint  # noqa: E402
import devicelib  # noqa: E402
import layoutlib  # noqa: E402
import pdk_devices  # noqa: E402

OK = {f: True for f in devicelib.FLAGS}
TABLE = {
    "cap_nmos_03v3": {**OK, "kind": "mos_cap", "reason": None,
                      "tt_reason": None},
    "pnp_10p00x10p00": {**OK, "kind": "bjt", "extract_ok": False,
                        "lvs_ok": False, "reason": "magic extracts pnp_x",
                        "tt_reason": None},
    "cap_mim_2f0_m4m5_noshield": {**OK, "kind": "mim", "tt_legal": False,
                                  "reason": None,
                                  "tt_reason": "Metal5 (81/0) is forbidden"},
}


# ---- the readers -----------------------------------------------------

def test_lvs_problem():
    assert devicelib.lvs_problem("cap_nmos_03v3", TABLE) is None
    assert devicelib.lvs_problem("nfet_03v3", TABLE) is None  # no row
    why = devicelib.lvs_problem("PNP_10p00x10p00", TABLE)
    assert "not lvs_ok" in why and "magic extracts" in why


def test_tt_problem():
    assert devicelib.tt_problem("cap_nmos_03v3", TABLE) is None
    assert devicelib.tt_problem("nfet_03v3", TABLE) is None
    assert "Metal5" in devicelib.tt_problem("cap_mim_2f0_m4m5_noshield",
                                            TABLE)


def test_not_ready():
    assert devicelib.not_ready("cap_nmos_03v3", TABLE) is None
    assert "no devices.yaml row" in devicelib.not_ready("nfet_03v3", TABLE)
    why = devicelib.not_ready("pnp_10p00x10p00", TABLE)
    assert "extract_ok, lvs_ok" in why
    assert "tt_legal" in devicelib.not_ready("cap_mim_2f0_m4m5_noshield",
                                             TABLE)


def test_shipped_table_covers_the_catalog():
    rows = devicelib.load()
    assert set(rows) == {m.lower() for m in devicelib.CATALOG}
    for m, r in rows.items():
        assert set(devicelib.FLAGS) <= set(r), m
        if not all(r[f] for f in devicelib.FLAGS):
            assert r["reason"] or r["tt_reason"], m


# ---- layoutlib refuses a device that failed --------------------------

@pytest.fixture
def planted_yaml(tmp_path, monkeypatch):
    import yaml
    p = tmp_path / "devices.yaml"
    p.write_text(yaml.safe_dump({"devices": TABLE}), encoding="utf-8")
    monkeypatch.setattr(devicelib, "DEVICES_YAML", p)
    return p


def test_wrapper_refuses_a_failed_device(planted_yaml):
    with pytest.raises(layoutlib.LayoutError, match="lvs_ok"):
        layoutlib.bjt("pnp_10p00x10p00")


def test_wrapper_refuses_a_device_with_no_row(planted_yaml):
    with pytest.raises(layoutlib.LayoutError, match="no devices.yaml row"):
        layoutlib.diode("diode_nd2ps_03v3", 1.0, 1.0)


def test_wrapper_refuses_the_wrong_kind(planted_yaml):
    with pytest.raises(layoutlib.LayoutError):
        layoutlib.diode("cap_nmos_03v3", 1.0, 1.0)


# ---- the extracted-netlist reader ------------------------------------

def test_extracted_models():
    spice = (".subckt t a b\n"
             "X0 a b cap_nmos_03v3 c_width=1e-6 c_length=1e-6\n"
             "D0 a b diode_nd2ps_03v3 area=1p pj=4u\n"
             "Q0 a b c d npn_10p00x10p00\n"
             "* a comment\n.ends\n")
    assert pdk_devices._extracted_models(spice) == [
        "cap_nmos_03v3", "diode_nd2ps_03v3", "npn_10p00x10p00"]


# ---- netlist_lint's device checks -------------------------------------

NETLIST = """\
.subckt blk a b vss
xc1 a vss cap_mim_2f0_m4m5_noshield c_width=10e-6 c_length=10e-6
xq1 vss vss a pnp_10p00x10p00
d1 b vss pnp_10p00x10p00 area=1p
xc2 b vss cap_nmos_03v3 c_width=2e-6 c_length=2e-6
.ends
"""


def _kinds(vs):
    return sorted((v["kind"], v["severity"], v["refs"][0]) for v in vs)


def test_device_violations_plain_block(tmp_path):
    vs = check_netlist_lint.device_violations(tmp_path, {}, "n.cir",
                                              NETLIST, TABLE)
    # no layout/: an unextractable device is a warning; not a TT tile
    assert _kinds(vs) == [("device_not_lvs_ok", "warning", "d1"),
                          ("device_not_lvs_ok", "warning", "xq1")]


def test_an_empty_layout_scaffold_is_not_a_layout(tmp_path):
    # state.py and faults.py make layout/ before any generator exists
    # (the corpus bandgap's pnp tripped this on CI)
    (tmp_path / "layout").mkdir()
    vs = check_netlist_lint.device_violations(tmp_path, {}, "n.cir",
                                              NETLIST, TABLE)
    assert {v["severity"] for v in vs} == {"warning"}


def test_device_violations_with_layout_and_tt(tmp_path):
    (tmp_path / "layout").mkdir()
    (tmp_path / "layout" / "gen_blk.py").write_text("", encoding="utf-8")
    vs = check_netlist_lint.device_violations(
        tmp_path, {"tt_tile": True}, "n.cir", NETLIST, TABLE)
    assert _kinds(vs) == [("device_not_lvs_ok", "error", "d1"),
                          ("device_not_lvs_ok", "error", "xq1"),
                          ("device_not_tt_legal", "error", "xc1")]


def test_msde_side_is_a_tt_tile(tmp_path):
    ws = tmp_path / "analog"
    ws.mkdir()
    (tmp_path / "interface.yaml").write_text("{}\n", encoding="utf-8")
    assert check_netlist_lint.in_tt_tile(ws, {})
    assert not check_netlist_lint.in_tt_tile(ws, {"tt_tile": False})
    vs = check_netlist_lint.device_violations(ws, {}, "n.cir", NETLIST,
                                              TABLE)
    assert ("device_not_tt_legal", "error", "xc1") in _kinds(vs)


def test_bad_tt_tile_value(tmp_path):
    vs = check_netlist_lint.device_violations(
        tmp_path, {"tt_tile": "yes"}, "n.cir", NETLIST, TABLE)
    assert [v["kind"] for v in vs] == ["bad_tt_tile"]


# ---- the real round trip ---------------------------------------------

def _pdk_devices(*args, cwd=REPO):
    p = subprocess.run([sys.executable, str(ENGINE / "scripts" /
                                            "pdk_devices.py"), *args],
                       capture_output=True, text=True, cwd=cwd, timeout=900)
    return p.returncode, json.loads(p.stdout)


needs_pdk = pytest.mark.skipif(not layoutlib.pdk_root().is_dir(),
                               reason="no gf180mcuD PDK")


@pytest.mark.slow
@needs_pdk
def test_check_reproduces_the_table(tmp_path):
    # one that passes and one that fails at extraction, per kind
    models = ["cap_nmos_03v3", "diode_nd2ps_03v3", "npn_00p54x02p00",
              "pnp_05p00x05p00"]
    rc, out = _pdk_devices("--out", str(tmp_path), "--check",
                           "--models", *models, "--jobs", "4")
    assert rc == 0, out
    assert out["violations"] == []
    assert sorted(out["passed"]) == ["cap_nmos_03v3", "diode_nd2ps_03v3",
                                     "npn_00p54x02p00"]


@pytest.mark.slow
@needs_pdk
def test_planted_misspelt_netgen_class_fails_lvs(tmp_path):
    setup = (layoutlib.pdk_root() / "libs.tech" / "netgen" /
             "gf180mcuD_setup.tcl")
    text = setup.read_text(encoding="utf-8")
    assert "cap_nmos_03v3" in text
    bad = tmp_path / "setup.tcl"
    bad.write_text(text.replace("cap_nmos_03v3", "cap_nmos_03v3_typo"),
                   encoding="utf-8")
    rc, out = _pdk_devices("--out", str(tmp_path / "w"), "--models",
                           "cap_nmos_03v3", "--yaml",
                           str(tmp_path / "d.yaml"), "--netgen-setup",
                           str(bad))
    assert rc == 0, out
    row = out["rows"]["cap_nmos_03v3"]
    assert row["drc_ok"] and row["extract_ok"]
    assert row["lvs_ok"] is False, row
    assert "LVS" in row["reason"]


@pytest.mark.slow
@needs_pdk
def test_wrapper_draws_a_passing_device(tmp_path):
    cell = layoutlib.mos_cap("cap_nmos_03v3", 2.0, 2.0, name="mc")
    gds = tmp_path / "mc.gds"
    cell.write_gds(str(gds))
    assert gds.stat().st_size > 0

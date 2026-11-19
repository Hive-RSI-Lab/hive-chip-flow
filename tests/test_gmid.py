"""The gf180 gm/Id tables and sizing helper (#102): engine/lib/gmidlib.py,
engine/scripts/gmid_lut.py and gmid.py.

The known answer and the gm cross-check run ngspice for real on one op
deck each, against the nfet tt table made once for the module (about 4 s
of ngspice: the table is the thing under test, so it is made the way the
cache makes it and must hash to the committed pin). The planted
wrong-corner table runs on a tiny grid. The full regeneration of all six
tables (gmid_lut.py --check) is `slow`.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "lib"))
sys.path.insert(0, str(ROOT / "engine" / "scripts"))

import gmid  # noqa: E402
import gmid_lut  # noqa: E402
import gmidlib  # noqa: E402
import layoutlib  # noqa: E402


@pytest.fixture(scope="module")
def nfet_tt(tmp_path_factory):
    ref = tmp_path_factory.mktemp("gmid")
    return gmidlib.load("nfet_03v3", "tt", ref=ref)


def _op(tmp_path: Path, device: str, corner: str, points) -> list[dict]:
    """ngspice op on one instance per (w_um, l_um, vgs, vds, vsb):
    each one's |id| and gm."""
    pdk = layoutlib.pdk_root() / "libs.tech" / "ngspice"
    section, temp_c = gmidlib.CORNERS[corner]
    s = -1 if device.startswith("p") else 1
    lines = [f"* gmid op {device}", f".include '{pdk}/design.ngspice'",
             f".lib '{pdk}/sm141064.ngspice' {section}", f".temp {temp_c:g}"]
    for i, (w, l, vgs, vds, vsb) in enumerate(points):
        lines += [f"vd{i} d{i} 0 {s * vds:.9g}", f"vg{i} g{i} 0 {s * vgs:.9g}",
                  f"vb{i} b{i} 0 {-s * vsb:.9g}",
                  f"x{i} d{i} g{i} 0 b{i} {device} w={w:.9g}u l={l:.9g}u"]
    lines += [".control", "op"]
    for i in range(len(points)):
        lines += [f"let id{i} = abs(@m.x{i}.m0[id])",
                  f"let gm{i} = @m.x{i}.m0[gm]", f"print id{i}", f"print gm{i}"]
    lines += [".endc", ".end"]
    cir = tmp_path / "op.cir"
    cir.write_text("\n".join(lines) + "\n")
    v = layoutlib.parse_ngspice_prints(
        layoutlib.run_ngspice(cir, cwd=tmp_path, timeout=60))
    return [{"id": v[f"id{i}"], "gm": v[f"gm{i}"]} for i in range(len(points))]


def test_the_committed_manifest_pins_all_six_tables_on_the_scripts_grid():
    doc = gmidlib.read_manifest()
    assert doc["grid"] == gmid_lut.grid_doc()
    want = {gmidlib.table_name(d, c) for d in gmidlib.DEVICES
            for c in gmidlib.CORNERS}
    assert set(doc["tables"]) == want
    for name, pin in doc["tables"].items():
        assert name == gmidlib.table_name(pin["device"], pin["corner"])
        assert len(pin["sha256"]) == 64


def test_known_answer_a_sized_nfet_lands_on_the_tables_gm_id_and_id(
        nfet_tt, tmp_path):
    """Issue #102's known answer: L=1u, gm/Id=10, Id=10 uA, VDS=VDD/2.
    ngspice's op at the W and VGS the helper gives: Id within 3% and
    gm/Id within 5% (so Id/W within 3% too: W is the helper's)."""
    r = gmidlib.size(nfet_tt, 10.0, 10e-6, 1.0, vds=1.65)
    assert 0.3 < r["W_um"] < 100 and 0.5 < r["VGS"] < 1.5
    (op,) = _op(tmp_path, "nfet_03v3", "tt",
                [(r["W_um"], 1.0, r["VGS"], 1.65, 0.0)])
    assert op["id"] == pytest.approx(10e-6, rel=0.03), (r, op)
    assert op["gm"] / op["id"] == pytest.approx(10.0, rel=0.05), (r, op)
    assert op["id"] / r["W_um"] == pytest.approx(r["ID_W"], rel=0.05)
    assert r["fT"] > 1e8 and r["gm_gds"] > 10


def test_the_tables_gm_matches_ngspice_at_five_random_points(nfet_tt,
                                                             tmp_path):
    t = nfet_tt
    rnd = random.Random(102)
    picks = []
    while len(picks) < 5:
        il = rnd.randrange(len(t["L"]))
        ig = rnd.randrange(len(t["VGS"]))
        idd = rnd.randrange(1, len(t["VDS"]))
        isb = rnd.randrange(len(t["VSB"]))
        if t["GM"][il, ig, idd, isb] > 1e-6:    # not cut off
            picks.append((il, ig, idd, isb))
    pts = [(float(t["W"]), float(t["L"][il]), float(t["VGS"][ig]),
            float(t["VDS"][idd]), float(t["VSB"][isb]))
           for il, ig, idd, isb in picks]
    ops = _op(tmp_path, "nfet_03v3", "tt", pts)
    for (il, ig, idd, isb), p, op in zip(picks, pts, ops):
        assert t["GM"][il, ig, idd, isb] == pytest.approx(op["gm"],
                                                          rel=1e-3), p
        assert t["ID"][il, ig, idd, isb] == pytest.approx(op["id"],
                                                          rel=1e-3), p


def test_pygmid_reads_the_table_and_agrees_with_the_helper(nfet_tt):
    from pygmid import Lookup
    lk = Lookup(nfet_tt["path"])
    r = gmidlib.size(nfet_tt, 12.0, 10e-6, 1.0, vds=1.65)
    got = float(lk.look_up("ID_W", GM_ID=12.0, L=1.0, VDS=1.65))
    assert got == pytest.approx(r["ID_W"], rel=0.02)


def test_a_gm_id_the_device_cannot_reach_is_refused(nfet_tt):
    with pytest.raises(gmidlib.GmidError, match="peak"):
        gmidlib.size(nfet_tt, 60.0, 1e-6, 1.0)
    with pytest.raises(gmidlib.GmidError, match="outside"):
        gmidlib.size(nfet_tt, 10.0, 1e-6, 20.0)


def test_the_cli_sizes_from_the_cache(nfet_tt, monkeypatch, capsys):
    ref = Path(nfet_tt["path"]).parent
    monkeypatch.setattr(gmidlib, "cache_dir", lambda: ref)
    assert gmid.main(["size", "--gmid", "12", "--id", "10u", "--l",
                      "1u"]) == 0
    out = json.loads(capsys.readouterr().out)
    want = gmidlib.size(nfet_tt, 12.0, 10e-6, 1.0)
    assert out["W_um"] == pytest.approx(want["W_um"])
    assert {"gm", "gds", "fT", "Cgg", "VGS"} <= set(out)
    assert out["spice"].startswith("nfet_03v3 w=")


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(gmid_lut, "L_UM", (1.0,))
    monkeypatch.setattr(gmid_lut, "VSB", (0.0,))
    monkeypatch.setattr(gmid_lut, "VSTEP_GS", 0.3)
    monkeypatch.setattr(gmid_lut, "VSTEP_DS", 1.1)


def test_planted_a_table_made_at_the_wrong_corner_fails_the_pin(
        tiny, tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    manifest = tmp_path / "manifest.json"
    assert gmid_lut.main(["--device", "nfet_03v3", "--corner", "tt",
                          "--corner", "ss", "--out", str(ref),
                          "--manifest", str(manifest)]) == 0
    tt = gmidlib.table_name("nfet_03v3", "tt")
    ss = gmidlib.table_name("nfet_03v3", "ss")
    assert gmidlib.load("nfet_03v3", "tt", ref=ref, manifest=manifest)
    shutil.copy(ref / ss, ref / tt)           # the ss sweep, named tt
    with pytest.raises(gmidlib.GmidError, match="sha256"):
        gmidlib.load("nfet_03v3", "tt", ref=ref, manifest=manifest)
    # and re-pinned to the wrong file: its own label still refuses it
    doc = json.loads(manifest.read_text())
    doc["tables"][tt]["sha256"] = gmidlib.sha256(ref / tt)
    manifest.write_text(json.dumps(doc))
    with pytest.raises(gmidlib.GmidError, match="labelled SS"):
        gmidlib.load("nfet_03v3", "tt", ref=ref, manifest=manifest)
    # --check against the real pins sees the tiny grid as a finding
    assert gmid_lut.main(["--device", "nfet_03v3", "--corner", "tt",
                          "--manifest", str(gmidlib.MANIFEST)]
                         + ["--check"]) == 1


def test_an_unpinned_table_is_refused(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tables": {}}))
    with pytest.raises(gmidlib.GmidError, match="not pinned"):
        gmidlib.load("nfet_03v3", "tt", ref=tmp_path, manifest=manifest)


@pytest.mark.slow
def test_every_table_regenerates_to_its_pin():
    assert gmid_lut.main(["--check"]) == 0


@pytest.mark.slow
def test_known_answer_pfet(tmp_path):
    t = gmidlib.load("pfet_03v3", "tt", ref=tmp_path / "ref")
    r = gmidlib.size(t, 10.0, 10e-6, 1.0, vds=1.65)
    (op,) = _op(tmp_path, "pfet_03v3", "tt",
                [(r["W_um"], 1.0, r["VGS"], 1.65, 0.0)])
    assert op["id"] == pytest.approx(10e-6, rel=0.03), (r, op)
    assert op["gm"] / op["id"] == pytest.approx(10.0, rel=0.05), (r, op)

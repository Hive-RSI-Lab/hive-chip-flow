"""release/manifest.json and `chip-flow release --verify` (issue #132).

Hermetic: a recorded msde top and vde tile (resolved.json, final views, an
assemble.py-shaped tt/ overlay), gate passes fabricated with record_gate, no
harden. test_ttship.py covers the workflow side of staging."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import attest as attest_mod  # noqa: E402
import check_release  # noqa: E402
import manifestlib  # noqa: E402
import release as release_mod  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402
import ttship  # noqa: E402
from test_check_release import make_msde_ws  # noqa: E402
from test_ttship import make_ws as make_vde_tile  # noqa: E402

TOP = "tt_um_pll"
WRAPPER = f"/* generated */\nmodule {TOP}(input wire clk);\n  core u(.clk(clk));\nendmodule\n"
BLACKBOX = "// generated\nmodule macro(input wire a);\nendmodule\n"


def make_msde_tile(tmp_path: Path, overlay: bool = False) -> Path:
    """An msde block with a recorded top_harden, all gates passed."""
    ws = make_msde_ws(tmp_path)
    (ws / "digital" / "rtl" / "core.v").write_text("module core(input clk);\nendmodule\n")
    regate(ws / "digital", "vde")
    att, problems = attest_mod.build(ws / "digital")
    assert att is not None, problems
    attest_mod.write_attestation(ws / "digital", att)
    h = ws / "top" / "harden"
    (h / "runs" / "run").mkdir(parents=True)
    (ws / "top" / "macros").mkdir()
    (ws / "top" / "macros" / "macro.v").write_text(BLACKBOX)
    (h / f"{TOP}.v").write_text(WRAPPER)
    (h / "config.json").write_text(json.dumps({"DESIGN_NAME": TOP}))
    (h / "info.yaml").write_text(f"project:\n  top_module: {TOP}\n  tiles: 1x2\n  analog_pins: 2\n")
    run = h / "runs" / "run"
    (run / "resolved.json").write_text(json.dumps({
        "DESIGN_NAME": TOP, "PDK": "gf180mcuD",
        "meta": {"librelane_version": "3.1.0.dev3"}}))
    for view, name, data in (("gds", f"{TOP}.gds", b"gds bytes"),
                             ("lef", f"{TOP}.lef", b"lef bytes"),
                             ("pnl", f"{TOP}.pnl.v", b"powered"),
                             ("spef/max", f"{TOP}.spef", b"spef")):
        (run / "final" / view).mkdir(parents=True)
        (run / "final" / view / name).write_bytes(data)
    if overlay:
        tt = ws / "tt"
        (tt / "src").mkdir(parents=True)
        (tt / "test").mkdir()
        (tt / "src" / "project.v").write_text("// the tile's top, by hand\n" + WRAPPER)
        (tt / "src" / "macro.v").write_text(BLACKBOX)
        (tt / "test" / "tb.v").write_text("module tb; endmodule\n")
        (tt / "info.yaml").write_text(
            f"project:\n  title: PLL\n  top_module: {TOP}\n  tiles: 1x2\n  analog_pins: 2\n")
    regate(ws)
    return ws


def regate(ws: Path, skill: str = "msde") -> None:
    st = state_mod.State.load(ws / "state.json")
    for g in statelib.load_map()["gate_inputs"][skill]:
        st.record_gate(g, {"status": "pass"})
    st.save()


def released(ws: Path, capsys, *extra) -> dict:
    code = check_release.main(["--workspace", str(ws), *extra])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out


def verify(capsys, *args) -> tuple[int, dict]:
    code = release_mod.main(["--verify", *map(str, args)])
    return code, json.loads(capsys.readouterr().out)


def manifest(ws: Path) -> dict:
    return json.loads((ws / manifestlib.PATH).read_text())


def by_path(man: dict) -> dict:
    return {e["path"]: e for e in man["deliverables"] if e["path"]}


# ------------------------------------------------------------ the manifest

def test_every_deliverable_names_the_gates_that_hold_it(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    man = manifest(ws)
    assert man["released"] is True and man["refused"] == []
    files = by_path(man)
    gds = files[f"gds/{TOP}.gds"]
    assert gds["sha256"] == ttship._sha256(ws / gds["source"])
    gates = {t["gate"]: t for t in gds["gates"]}
    assert set(gates) == {"top_harden", "top_drc", "top_lvs", "precheck"}
    assert gates["top_drc"]["relation"] == "read"
    assert gates["top_drc"]["kind"] == "top_gds"
    assert gates["top_drc"]["verdict"] == "pass" and gates["top_drc"]["fresh"]
    assert gates["top_drc"]["inputs_sha256"]
    # no gate after top_harden hashes the LEF: the record says only that
    assert [t["relation"] for t in files[f"lef/{TOP}.lef"]["gates"]] == ["produced"]
    assert {t["gate"] for t in files["src/core.v"]["gates"]} >= {"top_harden", "cosim"}
    assert files[".github/workflows/gds.yaml"]["gates"][0]["gate"] == "release"


def test_manifest_records_versions_and_archive_views(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    man = manifest(ws)
    v = man["versions"]
    assert v["librelane"] == "3.1.0.dev3"
    assert v["precheck_tt_support_tools"] == ttship.vendored_tools_commit()
    assert v["checker_version"] and "open_pdks" in v
    assert set(v["toolchain"]) == {"image", "klayout", "magic", "netgen"}
    archived = [e for e in man["deliverables"] if e["path"] is None]
    assert {Path(e["source"]).name for e in archived} == {f"{TOP}.pnl.v", f"{TOP}.spef"}
    assert all(e["gates"] for e in archived)


def test_attestation_holds_the_manifest_digest(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    doc = json.loads((ws / "reports" / "checks.json").read_text())
    assert doc["tt_submission"]["manifest_sha256"] == manifest(ws)["manifest_sha256"]
    code, out = verify(capsys, ws, "--workspace", ws)
    assert code == 0, out


def test_vde_tile_gets_the_same_manifest(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ttship, "librelane_pdk_hash", lambda *_: None)
    ws = make_vde_tile(tmp_path)
    regate(ws, "vde")
    released(ws, capsys)
    files = by_path(manifest(ws))
    assert {t["gate"] for t in files[f"gds/{ttship_top(ws)}.gds"]["gates"]} >= {
        "harden", "timing", "drc", "lvs", "glsim", "precheck"}
    assert "rtl" in {t["kind"] for t in files["src/counter8.v"]["gates"]}
    code, out = verify(capsys, ws)
    assert code == 0, out
    assert out["files_checked"] == len(files)


def ttship_top(ws: Path) -> str:
    return json.loads((ws / "harden" / "config.json").read_text())["DESIGN_NAME"]


# ------------------------------------------------------------ --verify faults

def test_flipped_byte_in_staged_gds_fails_and_names_the_file(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    code, out = verify(capsys, ws)
    assert code == 0, out
    gds = ws / "release" / "tt" / "gds" / f"{TOP}.gds"
    data = bytearray(gds.read_bytes())
    data[0] ^= 1
    gds.write_bytes(bytes(data))
    code, out = verify(capsys, ws)
    assert code == 1
    assert [(v["kind"], v["file"]) for v in out["violations"]] == [
        ("deliverable_changed", f"gds/{TOP}.gds")]
    assert f"gds/{TOP}.gds" in out["violations"][0]["msg"]
    assert out["files_match"] is False


def test_verify_finds_a_missing_file_a_planted_gds_and_a_new_workflow(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    (tree / f"lef/{TOP}.lef").unlink()
    (tree / "gds" / "other.gds").write_bytes(b"x")
    (tree / ".github/workflows/reharden.yaml").write_text("name: x\n")
    code, out = verify(capsys, tree)
    assert code == 1
    assert {(v["kind"], v["file"]) for v in out["violations"]} == {
        ("deliverable_missing", f"lef/{TOP}.lef"),
        ("deliverable_extra", "gds/other.gds"),
        ("deliverable_extra", ".github/workflows/reharden.yaml")}


def test_edited_manifest_refuses(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    man = manifest(ws)
    man["deliverables"][0]["sha256"] = "0" * 64
    (ws / manifestlib.PATH).write_text(json.dumps(man))
    code, out = verify(capsys, ws)
    assert code == 1
    assert "manifest_edited" in {v["kind"] for v in out["violations"]}


def test_no_manifest_is_an_error_not_a_pass(tmp_path, capsys):
    code, out = verify(capsys, tmp_path)
    assert code == 2 and out["remediation"]


# ------------------------------------------------------------ stale, untied

def test_rtl_edit_after_top_harden_refuses_as_stale(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    code, _ = verify(capsys, ws)
    assert code == 0
    (ws / "digital" / "rtl" / "core.v").write_text("module core(input clk, input b);\nendmodule\n")
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any("top_harden: stale" in v["msg"] and "digital_rtl" in v["msg"]
               for v in out["violations"])
    man = manifest(ws)
    assert man["released"] is False
    assert any("top_harden: stale" in r for r in man["refused"])
    entry = by_path(man)["src/core.v"]
    assert {t["gate"]: t["fresh"] for t in entry["gates"]}["top_harden"] is False
    # the bytes agree with the refused manifest, and that is still no pass
    code, out = verify(capsys, ws)
    assert code == 1 and out["files_match"] is True and out["released"] is False
    assert [v["kind"] for v in out["violations"]] == ["manifest_not_released"]


def test_overlay_that_says_the_same_as_the_signed_file_is_tied(tmp_path, capsys):
    ws = make_msde_tile(tmp_path, overlay=True)
    released(ws, capsys)
    files = by_path(manifest(ws))
    assert files["src/project.v"]["agrees_with"] == f"top/harden/{TOP}.v"
    assert files["src/project.v"]["gates"]
    assert files["info.yaml"]["agrees_with"] == "top/harden/info.yaml"
    assert files["test/tb.v"]["gates"] == [] and files["test/tb.v"]["unsigned"]


def test_overlay_that_differs_refuses_until_waived_on_its_bytes(tmp_path, capsys):
    ws = make_msde_tile(tmp_path, overlay=True)
    bad = ws / "tt" / "src" / "project.v"
    bad.write_text(WRAPPER.replace("clk(clk)", "clk(1'b0)"))
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    v = [v for v in out["violations"] if v["kind"] == "deliverable_untied"]
    assert len(v) == 1 and "src/project.v" in v[0]["msg"]
    assert not (ws / "reports" / "checks.json").exists()

    sha = ttship._sha256(bad)
    (ws / "reports" / "waivers.json").write_text(json.dumps([
        {"deliverable": "src/project.v", "sha256": sha, "reason": "power pins by hand",
         "approved_by": "reviewer"}]))
    released(ws, capsys)
    man = manifest(ws)
    assert man["waivers"]["deliverables"][0]["reason"] == "power pins by hand"
    assert by_path(man)["src/project.v"]["waiver"]["sha256"] == sha

    bad.write_text(WRAPPER.replace("clk(clk)", "clk(1'b1)"))
    code = check_release.main(["--workspace", str(ws)])
    assert code == 1
    capsys.readouterr()


def test_info_yaml_that_disagrees_on_tiles_refuses(tmp_path, capsys):
    ws = make_msde_tile(tmp_path, overlay=True)
    info = ws / "tt" / "info.yaml"
    info.write_text(info.read_text().replace("1x2", "2x2"))
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any(v["kind"] == "deliverable_untied" and "tiles" in v["msg"]
               for v in out["violations"])


# ------------------------------------------------------------ the analog tree

def assemble_plan(ws: Path) -> set[str]:
    """What the PLL's hand-written tt/assemble.py lays out for this block."""
    names = {p.name for p in (ws / "tt" / "src").glob("*.v")}
    names |= {p.name for p in (ws / "digital" / "rtl").glob("*.v")}
    return ({f"src/{n}" for n in names}
            | {f"test/{p.name}" for p in (ws / "tt" / "test").iterdir()}
            | {"info.yaml", f"gds/{TOP}.gds", f"lef/{TOP}.lef"})


def test_stage_analog_lays_out_what_assemble_does(tmp_path, capsys):
    ws = make_msde_tile(tmp_path, overlay=True)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "src" / "config.json").write_text("{}")
    (repo / ".github/workflows/fpga.yaml").write_text("name: fpga\n")
    wf = ttship.default_workflow()
    (repo / ".github/workflows/gds.yaml").write_text(wf)
    released(ws, capsys, "--tt-repo", str(repo))
    assert not (repo / "src/config.json").exists()
    assert not (repo / ".github/workflows/fpga.yaml").exists()
    assert (repo / ".github/workflows/gds.yaml").read_text() == wf
    man = manifest(ws)
    assert set(by_path(man)) == assemble_plan(ws) | {".github/workflows/gds.yaml"}
    assert man["tree"] is None
    # a repo outside the workspace carries its manifest; --manifest also works
    code, out = verify(capsys, repo)
    assert code == 0, out
    code, out = verify(capsys, repo, "--manifest", ws / manifestlib.PATH)
    assert code == 0, out


def test_stage_analog_without_overlay_uses_top(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    assert (tree / "src" / "project.v").read_text() == WRAPPER
    assert (tree / "src" / "macro.v").read_text() == BLACKBOX
    assert "custom_gds" in (tree / ".github/workflows/gds.yaml").read_text()
    assert f"project:\n  top_module: {TOP}" in (tree / "info.yaml").read_text()


def test_refused_ship_check_on_msde(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    # a repo whose workflow ships another GDS
    wf = ws / "release" / "tt" / ".github" / "workflows" / "gds.yaml"
    wf.write_text(wf.read_text().replace("gds_path: gds/", "gds_path: gds/old_"))
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "tt_ship_mismatch" in {v["kind"] for v in out["violations"]}
    assert manifest(ws)["released"] is False

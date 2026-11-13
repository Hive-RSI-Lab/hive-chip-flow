"""release/manifest.json and `chip-flow release --verify` (issue #132).

Hermetic: a recorded msde top and vde tile (resolved.json, final views, an
assemble.py-shaped tt/ overlay), gate passes fabricated with record_gate, no
harden. test_ttship.py covers the workflow side of staging."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

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
WRAPPER = (f"/* generated */\nmodule {TOP}(input wire clk, inout wire VGND, inout wire VDPWR);\n"
           "  core u(.clk(clk));\nendmodule\n")
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
                             ("pnl", f"{TOP}.pnl.v", b"module x(inout VPWR, inout VGND); endmodule\n"),
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
    """Every gate passes; the harden records the outputs it hands on, as
    gate.py does on a pass."""
    imap = statelib.load_map()
    st = state_mod.State.load(ws / "state.json")
    for g in imap["gate_inputs"][skill]:
        st.record_gate(g, {"status": "pass"})
        statelib.write_harden_records(ws.resolve(), skill, g, imap)
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
    # #164 known answer: precheck hashes the LEF it copies in, and on an
    # unchanged run that read is fresh
    lef = {t["gate"]: t for t in files[f"lef/{TOP}.lef"]["gates"]}
    assert set(lef) == {"precheck", "top_harden"}
    assert lef["precheck"]["relation"] == "read"
    assert lef["precheck"]["kind"] == "top_lef" and lef["precheck"]["fresh"]
    assert lef["top_harden"]["relation"] == "produced"
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


def test_attest_build_after_release_keeps_the_tile_verifying(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    checks = ws / attest_mod.CHECKS_PATH
    before = checks.read_text()
    assert attest_mod.main(["build", "--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["problems"][0].startswith("sealed_record")
    assert checks.read_text() == before
    code, report = verify(capsys, ws, "--workspace", ws)
    assert code == 0, report


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

def _refusals(ws: Path, capsys) -> list[dict]:
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert manifest(ws)["released"] is False
    return out["violations"]


def test_lef_swapped_after_top_harden_refuses_as_stale(tmp_path, capsys):
    """#164 plant: the gates pass, then a different abstract lands in
    final/lef. precheck and release read it stale, and release names it."""
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    lef = ws / "top" / "harden" / "runs" / "run" / "final" / "lef" / f"{TOP}.lef"
    lef.write_bytes(b"some other abstract")
    rep = statelib.freshness_report(
        state_mod.State.load(ws / "state.json").data, ws)
    assert rep["gates"]["precheck"]["changed_inputs"] == ["top_lef"]
    assert rep["gates"]["release"]["changed_inputs"] == ["top_lef"]
    assert set(rep["summary"]["stale"]) == {"precheck", "release"}
    msgs = {v["kind"]: v["msg"] for v in _refusals(ws, capsys)}
    assert "top_lef" in msgs["harden_output_changed"]
    assert "top_harden" in msgs["harden_output_changed"]
    assert "precheck: stale" in msgs["gate_not_ready"]
    assert "top_lef" in msgs["gate_not_ready"]


@pytest.mark.parametrize("view,kind", [("spef/max", "top_spef"),
                                       ("sdf", "top_sdf")])
def test_timing_view_swapped_after_top_harden_refuses(tmp_path, capsys,
                                                      view, kind):
    """Release holds the SDF and SPEF to the hashes top_harden recorded, so
    re-running it does not launder a swap. Only release reads the SDF;
    top_stress also reads the SPEF (its cell loads), so a SPEF swap leaves
    that gate stale as well."""
    ws = make_msde_tile(tmp_path)
    d = ws / "top" / "harden" / "runs" / "run" / "final" / view
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{TOP}.{kind[4:]}").write_bytes(b"other parasitics")
    st = state_mod.State.load(ws / "state.json")
    st.record_gate("release", {"status": "pass"})   # release re-recorded
    st.save()
    violations = _refusals(ws, capsys)
    msgs = {v["kind"]: v["msg"] for v in violations}
    want = {"harden_output_changed"} | (
        {"gate_not_ready"} if kind == "top_spef" else set())
    assert set(msgs) == want and len(violations) == len(want)
    assert kind in msgs["harden_output_changed"]
    if kind == "top_spef":
        assert "top_stress: stale" in msgs["gate_not_ready"]


def test_a_clone_reads_the_new_views_from_the_harden_record(tmp_path, capsys):
    """git carries no runs/: the LEF, SDF and SPEF hashes come from
    outputs.json, so the gates that read them stay fresh (#239)."""
    ws = make_msde_tile(tmp_path)
    rep0 = statelib.freshness_report(
        state_mod.State.load(ws / "state.json").data, ws)
    assert rep0["summary"]["stale"] == []
    record = json.loads((ws / "top" / "harden" / "outputs.json").read_text())
    assert {"runs/run/final/lef", "runs/run/final/spef"} <= set(record["outputs"])
    import shutil
    shutil.rmtree(ws / "top" / "harden" / "runs")
    rep = statelib.freshness_report(
        state_mod.State.load(ws / "state.json").data, ws)
    assert rep["summary"]["stale"] == []
    assert statelib.harden_outputs_changed(
        ws, "msde", "top_harden", statelib.load_map()) == []


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
    assert files["info.yaml"]["derived"] == "ttship.stamped_info"
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
    # the repo's own workflow (no staged header): kept as it is
    wf = "".join(l for l in ttship.default_workflow().splitlines(keepends=True)
                 if not l.startswith(ttship.WF_HEADER))
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


def test_stage_analog_refreshes_its_own_stale_workflow(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    gds = ws / "release/tt/.github/workflows/gds.yaml"
    current = gds.read_text()
    body = ttship.WF_MARKER.sub("", current, count=1)
    gds.write_text(ttship._marked(body.replace(ttship.PNG_REPORT_STEP, "\n")))
    released(ws, capsys)
    assert gds.read_text() == current


def test_stamped_info_yaml_stays_tied_only_while_it_is_what_stage_writes(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    assert yaml.safe_load((tree / "info.yaml").read_text())["project"]["author"] == "Hive"
    assert (ws / "top/harden/info.yaml").read_text() != (tree / "info.yaml").read_text()
    entry = by_path(manifest(ws))["info.yaml"]
    assert entry["derived"] == "ttship.stamped_info" and entry["gates"]
    edited = tree / "info.yaml"
    edited.write_text(edited.read_text().replace(f"top_module: {TOP}", "top_module: other"))
    record = {"sources": {"info.yaml": "top/harden/info.yaml"},
              "signoff": {"top_module": TOP}}
    entries, problems = manifestlib.deliverables(ws, "msde", tree, record, [])
    assert any(p.startswith("deliverable_untied: info.yaml") for p in problems)
    assert "derived" not in {e["path"]: e for e in entries}["info.yaml"]


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


# ------------------------------------------------------------ the netlist (#149)

def test_msde_wrapper_with_no_power_pins_ships_the_powered_netlist(tmp_path, capsys):
    # top_harden's generated wrapper has no VGND: TT's analog template's
    # src/project.v would fail precheck, so the staged workflow names final/pnl
    ws = make_msde_tile(tmp_path)
    (ws / "top" / "harden" / f"{TOP}.v").write_text(f"module {TOP}(input wire clk);\nendmodule\n")
    regate(ws)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    assert "verilog_path: gl/" in (tree / ".github/workflows/gds.yaml").read_text()
    assert (tree / "gl" / f"{TOP}.v").read_bytes() == (
        ws / f"top/harden/runs/run/final/pnl/{TOP}.pnl.v").read_bytes()
    assert verify(capsys, ws)[0] == 0
    (tree / "gl" / f"{TOP}.v").write_text("changed")
    code, out = verify(capsys, ws)
    assert code == 1
    assert ("deliverable_changed", f"gl/{TOP}.v") in {
        (v["kind"], v["file"]) for v in out["violations"]}


# ------------------------------------------------- the template's power (#165)

POWERED_BLACKBOX = ("// generated\nmodule macro(\n`ifdef USE_POWER_PINS\n"
                    "    inout wire vdd, inout wire vss,\n`endif\n"
                    "    input wire a, inout wire vout);\nendmodule\n")


def make_spec_tile(tmp_path: Path, overlay: bool = False) -> Path:
    """An msde top whose wrapper is what top/spec/spec.yaml generates, as
    check_top_harden writes them; with overlay, a hand-kept tt/src that is
    the powered project.v with its own header and the macro blackbox."""
    import ttlib
    ws = make_msde_tile(tmp_path)
    spec = {"top": "pll",
            "ports": {"n": {"dir": "input", "width": 2},
                      "a": {"dir": "output", "width": 1},
                      "lock": {"dir": "output", "width": 1}},
            "tt_pins": {"n": "ui_in[1:0]", "lock": "uo_out[0]"},
            "macros": [{"cell": "macro", "instance": "u_analog",
                        "pins": {"a": "a"}, "ua": {"vout": 0},
                        "power": {"vdd": "vdd", "vss": "vss"}}]}
    (ws / "top" / "spec").mkdir(exist_ok=True)
    (ws / "top" / "spec" / "spec.yaml").write_text(json.dumps(spec))
    (ws / "top" / "harden" / f"{TOP}.v").write_text(ttlib.generate_tt_wrapper(spec))
    (ws / "top" / "macros" / "macro.v").write_text(POWERED_BLACKBOX)
    if overlay:
        (ws / "tt" / "src").mkdir(parents=True)
        (ws / "tt" / "src" / "project.v").write_text(
            "/* the PLL's top, kept by hand */\n"
            + ttlib.generate_tt_project(spec).split("\n", 1)[1])
        (ws / "tt" / "src" / "macro.v").write_text(POWERED_BLACKBOX)
    regate(ws)
    return ws


def test_generated_project_has_the_template_power_ports(tmp_path, capsys):
    ws = make_spec_tile(tmp_path)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    proj = (tree / "src" / "project.v").read_text()
    assert "input  wire       VGND," in proj and "input  wire       VDPWR," in proj
    assert ".vdd(VDPWR)," in proj and ".vss(VGND)," in proj
    assert ttship._has_power_pins(tree / "src" / "project.v")
    assert "verilog_path: src/project.v" in (tree / ".github/workflows/gds.yaml").read_text()
    entry = by_path(manifest(ws))["src/project.v"]
    assert entry["source"] == f"top/harden/{TOP}.v" and entry["gates"]
    assert entry["derived"] == "ttship.powered_project"
    assert verify(capsys, ws)[0] == 0


def test_hand_kept_powered_project_ties_with_no_waiver(tmp_path, capsys):
    ws = make_spec_tile(tmp_path, overlay=True)
    released(ws, capsys)
    files = by_path(manifest(ws))
    assert files["src/project.v"]["agrees_with"] == f"top/harden/{TOP}.v"
    assert files["src/project.v"]["gates"] and "waiver" not in files["src/project.v"]
    assert files["src/macro.v"]["agrees_with"] == "top/macros/macro.v"


def _refused(ws: Path, capsys) -> list[dict]:
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    return [v for v in out["violations"] if v["kind"] == "deliverable_untied"]


def test_hand_kept_powered_project_with_a_fault_refuses(tmp_path, capsys):
    ws = make_spec_tile(tmp_path, overlay=True)
    proj = ws / "tt" / "src" / "project.v"
    good = proj.read_text()
    for fault in (("ui_in[1:0]", "{ui_in[0], ui_in[1]}"),   # swapped bits
                  (".vdd(VDPWR)", ".vdd(VGND)")):           # supply on ground
        assert fault[0] in good
        proj.write_text(good.replace(*fault))
        v = _refused(ws, capsys)
        assert len(v) == 1 and "src/project.v" in v[0]["msg"], v


def test_powered_project_ties_only_while_the_spec_makes_the_hardened_wrapper(tmp_path, capsys):
    ws = make_spec_tile(tmp_path, overlay=True)
    spec_path = ws / "top" / "spec" / "spec.yaml"
    spec = json.loads(spec_path.read_text())
    spec["tt_pins"]["n"] = "ui_in[3:2]"
    spec_path.write_text(json.dumps(spec))
    regate(ws)
    v = _refused(ws, capsys)
    assert len(v) == 1 and "src/project.v" in v[0]["msg"], v


def test_msde_with_a_powered_top_ships_it_and_has_no_gl_job(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    tree = ws / "release" / "tt"
    wf = (tree / ".github/workflows/gds.yaml").read_text()
    assert "verilog_path: src/project.v" in wf and "gl_test" not in wf
    assert not (tree / "gl").exists()


def test_msde_workflow_with_no_netlist_refused(tmp_path, capsys):
    ws = make_msde_tile(tmp_path)
    released(ws, capsys)
    wf = ws / "release/tt/.github/workflows/gds.yaml"
    wf.write_text(wf.read_text().replace("          verilog_path: src/project.v\n", ""))
    assert check_release.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any(v["msg"].startswith("tt_verilog_missing") for v in out["violations"])

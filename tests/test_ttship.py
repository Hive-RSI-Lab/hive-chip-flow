"""engine/lib/ttship.py + check_release: the GDS a TT tile ships is the GDS
chip-flow signed off (issue #131). Hermetic: the signed-off run is a
recorded tree (resolved.json as LibreLane 3.1.0.dev3 writes it, final GDS
and LEF bytes, a PDK SOURCES file), the workflows are planted files, gate
passes are fabricated with record_gate; no harden runs."""
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

import attest as attest_mod  # noqa: E402
import check_release  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402
import ttship  # noqa: E402

TOP = "tt_um_counter8"
SIGNOFF_LL = "3.1.0.dev3"
SIGNOFF_PDK = "1689ac3f2dc763876eaf967227c7dfe831b031ae"
# what LibreLane 3.1.0.dev3's pdk_hashes.yaml pins for gf180mcu
LL_PDK = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
TOOLS = ttship.vendored_tools_commit()
GDS = b"signed-off gds bytes"
LEF = b"signed-off lef bytes"
PNL = f"module {TOP}(inout VPWR, inout VGND);\nendmodule\n".encode()

# ttgf-verilog-template's own gds job: the re-harden action, no pins - so
# LibreLane 3.0.14 and tt-support-tools main (action.yml @ ttgf26c).
TEMPLATE_WF = """name: gds
on: [push, workflow_dispatch]
jobs:
  gds:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v6
      - name: Build GDS
        uses: TinyTapeout/tt-gds-action@ttgf26c
        with:
          pdk: gf180mcuD
"""


def pinned_wf(ll=SIGNOFF_LL, tools=TOOLS):
    return TEMPLATE_WF + (f"          librelane-version: {ll}\n"
                          f"          tools-ref: {tools}\n")


def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    state_mod.State.init(ws, "vde", "counter8")
    (ws / "rtl" / "counter8.v").write_text("module counter8; endmodule\n")
    (ws / "spec" / "spec.yaml").write_text(
        "top: counter8\nrequirements: []\n"
        "ports: {clk: {dir: input, width: 1}, q: {dir: output, width: 1}}\n"
        "tt_pins: {clk: clk, q: 'uo_out[0]'}\n")
    (ws / "tb" / "test_counter8.py").write_text(
        "import cocotb\n@cocotb.test()\nasync def test_x(dut):\n    pass\n")
    h = ws / "harden"
    (h / f"{TOP}.v").write_text(f"module {TOP}; endmodule\n")
    (h / "constraints.sdc").write_text("set_false_path -from [get_ports rst_n]\n")
    (h / "config.json").write_text(json.dumps({
        "DESIGN_NAME": TOP, "CLOCK_PERIOD": 20.0,
        "VERILOG_FILES": ["dir::../rtl/counter8.v", f"dir::{TOP}.v"],
        "PNR_SDC_FILE": "dir::constraints.sdc"}))
    (h / "info.yaml").write_text(f"project:\n  top_module: {TOP}\n  tiles: 1x1\n")
    pdk_root = tmp_path / "pdks"
    (pdk_root / "gf180mcuD").mkdir(parents=True)
    (pdk_root / "gf180mcuD" / "SOURCES").write_text(
        f"open_pdks {SIGNOFF_PDK}\nciel\n")
    run = h / "runs" / "run"
    for kind, data in (("gds", GDS), ("lef", LEF)):
        (run / "final" / kind).mkdir(parents=True)
        (run / "final" / kind / f"{TOP}.{kind}").write_bytes(data)
    (run / "final" / "pnl").mkdir()
    (run / "final" / "pnl" / f"{TOP}.pnl.v").write_bytes(PNL)
    (run / "final" / "nl").mkdir()
    (run / "final" / "nl" / f"{TOP}.nl.v").write_bytes(
        f"module {TOP}();\nendmodule\n".encode())
    record_glsim(ws, ttship._sha256(run / "final" / "pnl" / f"{TOP}.pnl.v"))
    (run / "resolved.json").write_text(json.dumps({
        "DESIGN_NAME": TOP, "PDK": "gf180mcuD", "PDK_ROOT": str(pdk_root),
        "meta": {"version": 1, "librelane_version": SIGNOFF_LL}}))
    return ws


def record_glsim(ws: Path, sha: str | None) -> None:
    (ws / "reports").mkdir(exist_ok=True)
    (ws / "reports" / "gate-glsim.json").write_text(
        json.dumps({"netlists": {"pnl_sha256": sha}}))


def pdk_hash(pdk=LL_PDK):
    return lambda version, _pdk: pdk if version == SIGNOFF_LL else None


def plant(ws: Path, text: str) -> None:
    wf = ws / "release" / "tt" / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "gds.yaml").write_text(text)


def pass_every_gate(ws: Path) -> None:
    st = state_mod.State.load(ws / "state.json")
    for g in statelib.load_map()["gate_inputs"]["vde"]:
        st.record_gate(g, {"status": "pass"})
    st.save()


def rules(problems):
    return sorted(p.split(":", 1)[0] for p in problems)


# ---------------------------------------------------------------- signoff

def test_signoff_reads_the_run_not_the_install(tmp_path):
    ws = make_ws(tmp_path)
    sign = ttship.signoff(ws, pdk_hash=pdk_hash())
    assert sign["librelane"] == SIGNOFF_LL
    assert sign["open_pdks"] == SIGNOFF_PDK
    assert sign["librelane_pdk"] == LL_PDK
    assert sign["tt_support_tools"] and len(sign["tt_support_tools"]) == 40
    assert sign["gds_sha256"] == ttship._sha256(
        ws / "harden/runs/run/final/gds" / f"{TOP}.gds")


def test_no_run_is_a_refusal(tmp_path):
    ws = make_ws(tmp_path)
    (ws / "harden/runs/run/resolved.json").unlink()
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_no_signoff"]


# ---------------------------------------------------------------- staging

def test_stage_writes_the_tt_layout_and_a_custom_gds_workflow(tmp_path):
    ws = make_ws(tmp_path)
    problems, rec = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert problems == [], problems
    repo = ws / "release" / "tt"
    cfg = json.loads((repo / "src" / "config.json").read_text())
    assert cfg["VERILOG_FILES"] == ["dir::counter8.v", f"dir::{TOP}.v"]
    assert cfg["PNR_SDC_FILE"] == "dir::constraints.sdc"
    assert cfg["CLOCK_PERIOD"] == 20.0
    for f in ("src/counter8.v", f"src/{TOP}.v", "src/constraints.sdc",
              "info.yaml", f"gds/{TOP}.gds", f"lef/{TOP}.lef"):
        assert (repo / f).is_file(), f
    assert (repo / "gds" / f"{TOP}.gds").read_bytes() == GDS
    wf = (repo / ".github/workflows/gds.yaml").read_text()
    assert "tt-gds-action/custom_gds@ttgf26c" in wf
    assert f"tools-ref: {TOOLS}" in wf
    assert "verilog_path: gl/${{ steps.top_module.outputs.TOP_MODULE }}.v" in wf
    assert (repo / "gl" / f"{TOP}.v").read_bytes() == PNL
    assert [s["mode"] for s in rec["shipped"]] == ["custom_gds"]
    # staging never writes under harden/ (it would stale every signoff gate)
    assert not (ws / "harden" / "src").exists()


def test_stage_keeps_an_existing_workflow(tmp_path):
    ws = make_ws(tmp_path)
    plant(ws, TEMPLATE_WF)
    ttship.release_check(ws, pdk_hash=pdk_hash())
    assert (ws / "release/tt/.github/workflows/gds.yaml").read_text() == TEMPLATE_WF


# ---------------------------------------------------------------- re-harden

def test_template_workflow_refused_for_librelane_3_0_14(tmp_path):
    # Known answer (#131): the TT template re-hardens with 3.0.14 and
    # tt-support-tools main; we signed off with 3.1.0.dev3 and the vendored
    # commit.
    ws = make_ws(tmp_path)
    plant(ws, TEMPLATE_WF)
    problems, rec = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_librelane_mismatch", "tt_tools_mismatch"]
    assert "3.0.14" in problems[0] and SIGNOFF_LL in problems[0]
    assert rec["shipped"][0]["librelane"] == "3.0.14"
    assert rec["signoff"]["librelane"] == SIGNOFF_LL


def test_explicit_3_0_14_pin_refused(tmp_path):
    ws = make_ws(tmp_path)
    plant(ws, pinned_wf(ll="3.0.14"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_librelane_mismatch"]


def test_matching_pin_on_the_same_pdk_passes(tmp_path):
    ws = make_ws(tmp_path)
    plant(ws, pinned_wf())
    problems, rec = ttship.release_check(ws, pdk_hash=pdk_hash(SIGNOFF_PDK))
    assert problems == [], problems
    step = rec["shipped"][0]
    assert (step["librelane"], step["open_pdks"], step["tt_support_tools"]) \
        == (SIGNOFF_LL, SIGNOFF_PDK, TOOLS)


def test_matching_pin_but_librelane_fetches_another_pdk_refused(tmp_path):
    # Today's image: LibreLane 3.1.0.dev3 pins gf180mcu f6eeac7, the image's
    # PDK is open_pdks 1689ac3f - a re-harden would not be on our PDK.
    ws = make_ws(tmp_path)
    plant(ws, pinned_wf())
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash(LL_PDK))
    assert rules(problems) == ["tt_pdk_mismatch"]


def test_unknown_action_ref_refused(tmp_path):
    ws = make_ws(tmp_path)
    plant(ws, TEMPLATE_WF.replace("@ttgf26c", "@ttgf99"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_action_unknown"]


# ---------------------------------------------------------------- custom_gds

def staged_wf(ws: Path) -> str:
    return (ws / "release/tt/.github/workflows/gds.yaml").read_text()


def test_custom_gds_of_another_gds_refused(tmp_path):
    ws = make_ws(tmp_path)
    ttship.release_check(ws, pdk_hash=pdk_hash())        # stage
    (ws / "release/tt/gds" / f"{TOP}.gds").write_bytes(b"re-hardened elsewhere")
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    # staging copies the signed-off GDS back over it: the staged tree is
    # always the signed-off one ...
    assert problems == []
    # ... but a workflow that ships some other file is refused
    plant(ws, staged_wf(ws).replace("gds_path: gds/", "gds_path: old/"))
    (ws / "release/tt/old").mkdir()
    (ws / "release/tt/old" / f"{TOP}.gds").write_bytes(b"stale")
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_gds_mismatch"]


def test_no_shipping_step_refused(tmp_path):
    ws = make_ws(tmp_path)
    plant(ws, "name: test\non: [push]\njobs:\n  t:\n    runs-on: x\n"
              "    steps:\n      - run: make test\n")
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_not_shipped"]


# ---------------------------------------------------------------- release

@pytest.fixture
def fake_pdk_hash(monkeypatch):
    monkeypatch.setattr(ttship, "librelane_pdk_hash", pdk_hash())


def test_release_refuses_the_template_workflow(tmp_path, capsys, fake_pdk_hash):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    plant(ws, TEMPLATE_WF)
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert kinds == {"tt_ship_mismatch"}
    assert not (ws / "reports" / "checks.json").exists()
    assert out["tt_submission"]["shipped"][0]["librelane"] == "3.0.14"


def test_release_passes_and_records_both_versions(tmp_path, capsys, fake_pdk_hash):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    doc = json.loads((ws / "reports" / "checks.json").read_text())
    rec = doc["tt_submission"]
    assert rec["signoff"]["librelane"] == SIGNOFF_LL
    assert rec["signoff"]["open_pdks"] == SIGNOFF_PDK
    assert rec["signoff"]["tt_support_tools"] == TOOLS
    assert rec["shipped"][0]["mode"] == "custom_gds"
    assert rec["shipped"][0]["gds_sha256"] == rec["signoff"]["gds_sha256"]
    # the record is inside the attested body: verify still holds
    assert attest_mod.verify(ws)["valid"], attest_mod.verify(ws)


def test_release_passes_with_a_matching_reharden_pin(tmp_path, capsys,
                                                     monkeypatch):
    monkeypatch.setattr(ttship, "librelane_pdk_hash", pdk_hash(SIGNOFF_PDK))
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    plant(ws, pinned_wf())
    code = check_release.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    shipped = out["tt_submission"]["shipped"][0]
    assert shipped["librelane"] == SIGNOFF_LL


# ---------------------------------------------------------------- the netlist (#149)

def staged(ws):
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert problems == [], problems
    return ws / "release" / "tt"


def test_workflow_staged_before_149_is_refused(tmp_path):
    # Known answer: what release staged until now names no netlist, so
    # custom_gds ships an empty .v and precheck fails on VGND
    ws = make_ws(tmp_path)
    staged(ws)
    plant(ws, "\n".join(l for l in staged_wf(ws).splitlines()
                        if "verilog_path" not in l) + "\n")
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_verilog_missing"]
    assert "VGND" in problems[0]


def test_empty_netlist_is_refused(tmp_path):
    ws = make_ws(tmp_path)
    repo = staged(ws)
    (repo / "empty.v").write_text("")
    plant(ws, staged_wf(ws).replace("verilog_path: gl/${{ steps.top_module.outputs.TOP_MODULE }}.v",
                                    "verilog_path: empty.v"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_verilog_unpowered"]


def test_a_path_that_is_not_there_is_refused(tmp_path):
    ws = make_ws(tmp_path)
    staged(ws)
    plant(ws, staged_wf(ws).replace("verilog_path: gl/", "verilog_path: nope/"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_verilog_missing"]


@pytest.mark.parametrize("name,data", [
    # the pre-PnR netlist with its comment carrying the names precheck wants:
    # a text search that kept comments would pass this one
    ("nl.v", b"// VGND VPWR\nmodule x(); endmodule\n"),
    ("other_run.v", PNL + b"// another run\n"),
])
def test_another_netlist_is_refused_on_its_bytes(tmp_path, name, data):
    ws = make_ws(tmp_path)
    repo = staged(ws)
    (repo / name).write_bytes(data)
    plant(ws, staged_wf(ws).replace("verilog_path: gl/${{ steps.top_module.outputs.TOP_MODULE }}.v",
                                    f"verilog_path: {name}"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == (["tt_verilog_unpowered"] if name == "nl.v"
                               else ["tt_verilog_mismatch"])


def test_staged_netlist_swapped_for_final_nl_is_put_back(tmp_path):
    # The staged copy can't drift: staging writes the run's bytes every time.
    ws = make_ws(tmp_path)
    repo = staged(ws)
    (repo / "gl" / f"{TOP}.v").write_bytes(
        (ws / "harden/runs/run/final/nl" / f"{TOP}.nl.v").read_bytes())
    staged(ws)
    assert (repo / "gl" / f"{TOP}.v").read_bytes() == PNL


def test_run_pnl_replaced_after_glsim_is_refused(tmp_path):
    # The run's own netlist changed after glsim read it
    ws = make_ws(tmp_path)
    pnl = ws / "harden/runs/run/final/pnl" / f"{TOP}.pnl.v"
    pnl.write_bytes(PNL + b"// another run\n")
    problems, rec = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_verilog_not_simulated"]
    assert rec["shipped"][0]["verilog_sha256"] == ttship._sha256(pnl)


def test_no_glsim_record_of_the_netlist_is_refused(tmp_path):
    ws = make_ws(tmp_path)
    (ws / "reports" / "gate-glsim.json").unlink()
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_verilog_not_simulated"]
    assert "run the glsim gate" in problems[0]


def test_release_records_the_netlist_beside_glsim(tmp_path, capsys, fake_pdk_hash):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    assert check_release.main(["--workspace", str(ws)]) == 0, capsys.readouterr().out
    capsys.readouterr()
    step = json.loads((ws / "reports/checks.json").read_text())["tt_submission"]["shipped"][0]
    assert step["verilog_path"] == f"gl/{TOP}.v"
    assert step["verilog_sha256"] == step["glsim_sha256"] == ttship._sha256(
        ws / "harden/runs/run/final/pnl" / f"{TOP}.pnl.v")
    man = json.loads((ws / "release/manifest.json").read_text())
    gl = next(e for e in man["deliverables"] if e["path"] == f"gl/{TOP}.v")
    assert "glsim" in {g["gate"] for g in gl["gates"]}


def test_release_refuses_with_a_ship_mismatch(tmp_path, capsys, fake_pdk_hash):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    record_glsim(ws, "0" * 64)
    assert check_release.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert {v["kind"] for v in out["violations"]} == {"tt_ship_mismatch"}
    assert not (ws / "reports" / "checks.json").exists()


# ---------------------------------------------------------------- the GL test (#149)

def test_staged_gl_test_job_is_pinned_to_the_signoff_pdk(tmp_path):
    ws = make_ws(tmp_path)
    repo = staged(ws)
    jobs = yaml.safe_load(staged_wf(ws))["jobs"]
    assert jobs["gl_test"]["needs"] == "gds"
    ciel = next(s for s in jobs["gl_test"]["steps"]
                if "ciel-action" in str(s.get("uses")))
    assert ciel["with"]["pdk_version"] == SIGNOFF_PDK
    assert ciel["with"]["pdk_name"] == "gf180mcuD"
    run = next(s["run"] for s in jobs["gl_test"]["steps"] if "GATES=yes" in str(s.get("run")))
    assert "cp tt_submission/*.v test/gate_level_netlist.v" in run
    mk = (repo / "test" / "Makefile").read_text()
    assert "TOPLEVEL = counter8" in mk and "COCOTB_TEST_MODULES = test_counter8" in mk
    assert (repo / "test" / "test_counter8.py").is_file()
    assert "`ifdef USE_POWER_PINS" in (repo / "test" / "glsim_harness.v").read_text()


def test_tt_gl_test_action_on_custom_gds_is_refused(tmp_path):
    # custom_gds's pdk.json has no PDK_VERSION: gl_test would ask ciel for null
    ws = make_ws(tmp_path)
    staged(ws)
    plant(ws, staged_wf(ws) + """
  gl_action:
    needs: gds
    runs-on: ubuntu-24.04
    steps:
      - uses: TinyTapeout/tt-gds-action/gl_test@ttgf26c
""")
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_gl_test_no_pdk"]


@pytest.mark.parametrize("version", ["f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7", "null"])
def test_gl_test_on_another_pdk_is_refused(tmp_path, version):
    ws = make_ws(tmp_path)
    staged(ws)
    plant(ws, staged_wf(ws).replace(f"pdk_version: {SIGNOFF_PDK}", f"pdk_version: {version}"))
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert "tt_gl_pdk_mismatch" in rules(problems)


def test_vde_workflow_with_no_gl_test_job_is_refused(tmp_path):
    ws = make_ws(tmp_path)
    staged(ws)
    plant(ws, staged_wf(ws).split("  # Not TT's gl_test action")[0])
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_gl_test_missing"]


def test_block_with_no_tests_cant_ship(tmp_path):
    ws = make_ws(tmp_path)
    (ws / "tb" / "test_counter8.py").unlink()
    problems, _ = ttship.release_check(ws, pdk_hash=pdk_hash())
    assert rules(problems) == ["tt_gl_test_missing"]

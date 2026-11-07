"""engine/scripts/precheck_drift.py: the vendored precheck against the shuttle's
(issue #133). Offline: upstream's answers come from a fixture through the code
the live compare runs. The file list is the real compare of 01d5d281...9d13ff3e
(2026-11-06), the known answer for what counts as gf180."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import checklib  # noqa: E402
import precheck_drift as drift  # noqa: E402
import ttship  # noqa: E402

SCRIPT = ENGINE / "scripts" / "precheck_drift.py"
PLANT = REPO / "tests" / "fixtures" / "precheck_drift" / "plant.json"
WORKFLOW = REPO / ".github" / "workflows" / "precheck-drift.yml"
SHA = "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"

REAL_DRIFT = {
    drift.GF180: [
        "precheck/precheck.py", "precheck/requirements.txt",
        "precheck/tech_data.py", "tech.py",
        "tech/gf180mcuD/def/tt_block_2x1_pgvdd.def",
        "tech/gf180mcuD/def/tt_block_4x6_pgvdd.def",
        "tech/gf180mcuD/def/tt_block_4x8_pgvdd.def",
        "tech/gf180mcuD/tile_sizes.yaml",
    ],
    drift.OTHER_PDK: [
        "tech/ihp-sg13cmos5l/categories.json",
        "tech/ihp-sg13cmos5l/def/analog/tt_analog_1x2.def",
        "tech/ihp-sg13cmos5l/tile_sizes.yaml",
        "tech/ihp-sg13cmos5l/tt_logo_corner.gds",
    ],
    drift.OUTSIDE: [
        ".github/workflows/test-harden.yaml", "config.py", "doc_utils.py",
        "docs/typst/src/datasheet.typ", "logo.py", "precheck/reports/.gitignore",
        "rom.py", "rom/config.json", "tt_tool.py",
    ],
}


def plant() -> dict:
    return json.loads(PLANT.read_text(encoding="utf-8"))


def with_files(doc: dict, *paths: str) -> dict:
    doc["compare"]["files"] = [
        {"filename": p, "status": "modified", "additions": 1, "deletions": 1}
        for p in paths]
    doc["commit_files"] = {SHA: list(paths)}
    return doc


def analyse(doc: dict, tmp_path: Path | None = None,
            klayout: str = drift.IMAGE_KLAYOUT) -> dict:
    vendored = drift.VENDORED_TOOLS
    if tmp_path is not None:
        vendored = tmp_path
    return drift.analyse(drift.Fixture(doc), "0" * 40, vendored, klayout)


def kinds(payload: dict) -> set[str]:
    return {v["kind"] for v in payload["violations"]}


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    """A vendored tree whose klayout requirement the image meets."""
    req = tmp_path / drift.REQUIREMENTS
    req.parent.mkdir(parents=True)
    req.write_text("klayout>=0.30.8,<0.31.0\n", encoding="utf-8")
    return tmp_path


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("side, path", [
    (side, p) for side, paths in REAL_DRIFT.items() for p in paths])
def test_real_drift_classified(side: str, path: str):
    assert drift.classify(path) == side


@pytest.mark.parametrize("path, side", [
    # What the order names.
    ("precheck/magic_drc.tcl", drift.GF180),
    ("precheck/pyproject.toml", drift.GF180),
    ("precheck/tech-files/gf180mcuD_mr.drc", drift.GF180),
    # Fails closed: nobody has seen these yet.
    ("precheck/new_check.py", drift.GF180),
    ("precheck/tech-files/new_deck.drc", drift.GF180),
    ("tech/shared.yaml", drift.GF180),
    ("tech/gf180mcuE/tile_sizes.yaml", drift.GF180),
    # Another PDK's by name, and nothing else.
    ("precheck/tech-files/sky130A_mr.drc", drift.OTHER_PDK),
    ("tech/sky130A/tile_sizes.yaml", drift.OTHER_PDK),
    # Can't change a result.
    ("precheck/README.md", drift.OUTSIDE),
    ("docs/tech.py", drift.OUTSIDE),
])
def test_classify(path: str, side: str):
    assert drift.classify(path) == side


def test_rename_out_of_gf180_still_counts():
    change = {"filename": "tech/ihp/x.yaml",
              "previous_filename": "tech/gf180mcuD/x.yaml"}
    assert drift.classify_change(change) == drift.GF180


def test_plant_fails_naming_file_and_commit(tools: Path):
    doc = plant()
    doc["tree"] = None
    payload = analyse(doc, tools)
    assert kinds(payload) == {"precheck_drift_gf180"}
    assert payload["status"] == "violations"
    found = payload["violations"][0]
    assert found["file"] == "precheck/tech_data.py"
    assert found["refs"] == [SHA]
    assert "precheck/tech_data.py" in found["msg"] and SHA[:8] in found["msg"]
    assert "raise the gf180mcuD power pin" in found["msg"]


def test_plant_cli_exits_one(tmp_path: Path):
    out = tmp_path / "out.json"
    res = cli("--fixture", str(PLANT), "--out", str(out))
    assert res.returncode == 1, res.stderr
    assert "precheck_drift_gf180" in {
        v["kind"] for v in json.loads(out.read_text())["violations"]}


def test_workflow_runs_the_plant_and_needs_nothing_private():
    text = WORKFLOW.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert "tests/fixtures/precheck_drift/plant.json" in text
    assert "secrets." not in text
    for job in doc["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
    steps = doc["jobs"]["drift"]["steps"]
    plants = [i for i, s in enumerate(steps) if "--fixture" in s.get("run", "")]
    reals = [i for i, s in enumerate(steps)
             if "precheck_drift.py" in s.get("run", "")
             and "--fixture" not in s["run"]]
    assert plants and reals and plants[0] < reals[0], "the plant runs first"
    assert doc["permissions"] == {"contents": "read"}


def test_other_pdk_only_warns_and_passes(tools: Path):
    doc = with_files(plant(), "tech/sky130A/tile_sizes.yaml")
    payload = analyse(doc, tools)
    assert payload["status"] == "pass"
    assert payload["violations"] == []
    assert [w["kind"] for w in payload["warnings"]] == ["precheck_drift_other_pdk"]
    assert "tech/sky130A/tile_sizes.yaml" in payload["warnings"][0]["msg"]


def test_outside_paths_pass_without_a_warning(tools: Path):
    payload = analyse(with_files(plant(), "rom.py"), tools)
    assert payload["status"] == "pass" and payload["warnings"] == []
    assert [f["path"] for f in payload["outside"]] == ["rom.py"]


def test_identical_passes(tools: Path):
    doc = with_files(plant())
    doc["compare"].update(status="identical", ahead_by=0, commits=[],
                          total_commits=0)
    payload = analyse(doc, tools)
    assert payload["status"] == "pass" and payload["head"] == "0" * 40


def test_diverged_fails(tools: Path):
    doc = with_files(plant(), "rom.py")
    doc["compare"].update(status="diverged", behind_by=2)
    assert kinds(analyse(doc, tools)) == {"precheck_drift_diverged"}


@pytest.mark.parametrize("change", [
    {"files": [{"filename": f"f{i}", "status": "added"} for i in range(300)]},
    {"total_commits": 300},
])
def test_cut_short_compare_refuses(tools: Path, change: dict):
    doc = plant()
    doc["compare"].update(copy.deepcopy(change))
    with pytest.raises(checklib.CheckError, match="cut short"):
        analyse(doc, tools)


def test_api_failure_refuses_exit_2(tmp_path: Path):
    """gh failing is a refusal naming what to do, never a pass."""
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho 'HTTP 404: no such commit' >&2\nexit 1\n")
    fake.chmod(0o755)
    res = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True,
        timeout=60, env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert res.returncode == 2, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert "HTTP 404" in payload["remediation"] or "HTTP 404" in res.stdout


def test_action_without_a_tools_ref_refuses(tools: Path):
    doc = plant()
    doc["action_yml"] = "inputs:\n  other:\n    default: main\n"
    with pytest.raises(checklib.CheckError, match="tools-ref"):
        analyse(doc, tools)


def test_tools_ref_is_read_from_the_action():
    yml = ("inputs:\n  tools-repo:\n    default: 'a/b'\n"
           "  tools-ref:\n    description: 'x'\n    default: 'develop'\n"
           "    required: false\n")
    assert drift.action_tools_ref(yml) == "develop"


@pytest.mark.parametrize("line, klayout, ok", [
    ("klayout>=0.30.8,<0.31.0", "0.30.12", True),
    ("klayout>=0.30.8,<0.31.0", "0.31.0", False),
    ("klayout==0.30.8", "0.30.12", False),
    ("klayout==0.30.*", "0.30.12", True),
    ("klayout==0.29", "0.30.12", False),
    ("klayout", "0.30.12", True),
    ("klayoutish==1", "0.30.12", False),
    ("klayout~=0.30", "0.30.12", False),
])
def test_klayout_requirement(line: str, klayout: str, ok: bool):
    assert (drift.klayout_problem(line + "\nnumpy==2\n", klayout) is None) is ok


def test_upstream_klayout_requirement_out_of_range(tools: Path):
    doc = with_files(plant(), "rom.py")
    doc["requirements"] = "klayout==0.29\n"
    payload = analyse(doc, tools)
    assert kinds(payload) == {"precheck_klayout_out_of_range"}
    assert "upstream main" in payload["violations"][0]["msg"]


def test_vendored_klayout_requirement_out_of_range(tools: Path):
    (tools / drift.REQUIREMENTS).write_text("klayout==0.29\n", encoding="utf-8")
    payload = analyse(with_files(plant(), "rom.py"), tools)
    assert kinds(payload) == {"precheck_klayout_out_of_range"}
    assert "vendored" in payload["violations"][0]["msg"]


def test_vendored_file_must_be_the_pins_blob(tools: Path):
    (tools / "tech.py").write_text("x = 1\n", encoding="utf-8")
    doc = with_files(plant(), "rom.py")
    req_sha = drift.git_blob_sha(tools / drift.REQUIREMENTS)
    doc["tree"] = {drift.REQUIREMENTS: req_sha,
                   "tech.py": drift.git_blob_sha(tools / "tech.py")}
    assert analyse(doc, tools)["violations"] == []
    doc["tree"]["tech.py"] = "0" * 40
    payload = analyse(doc, tools)
    assert kinds(payload) == {"precheck_vendored_modified"}
    assert payload["violations"][0]["file"] == "tech.py"
    del doc["tree"]["tech.py"]
    assert kinds(analyse(doc, tools)) == {"precheck_vendored_modified"}


def test_blob_sha_is_gits(tmp_path: Path):
    f = tmp_path / "x"
    f.write_bytes(b"hello\n")
    assert drift.git_blob_sha(f) == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_summary_names_files_and_commits(tools: Path):
    doc = plant()
    text = drift.summary_md(analyse(doc, tools))
    assert "precheck/tech_data.py" in text and SHA[:8] in text


# The script runs without the image, so it keeps its own copies of these.
def test_own_copies_match_ttship():
    assert drift.vendored_pin() == ttship.vendored_tools_commit()
    assert drift.ACTION_REF == ttship.DEFAULT_REF


def test_image_klayout_constant_is_the_image():
    res = subprocess.run([str(REPO / "bin" / "eda"), "klayout", "-b", "-v"],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["KLayout", drift.IMAGE_KLAYOUT]


def test_script_needs_only_the_standard_library():
    """The CI job runs it with the runner's python3: no yaml, no toolchain."""
    probe = ("import sys, runpy; "
             "sys.modules.update(yaml=None, cocotb=None, ttlib=None); "
             f"sys.argv = ['x', '--help']; runpy.run_path({str(SCRIPT)!r}, "
             "run_name='__main__')")
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 0, res.stderr

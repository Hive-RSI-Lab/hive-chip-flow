"""#381: an ade layout gate rebuilds <block>.gds and its abstract into
layout/ before it reads them (layout_gen.build), so the `layout` hash taken
at the gate's start can predate the GDS the check read. invalidation.yaml
gate_rebuilds lists those gates; gate.py then records the end-of-run hash
when only those outputs moved, and keeps the start hash (stale) when
anything else in the dir moved during the run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import gate  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402

# A drc check that does what the real ade layout gates do first: rebuild the
# GDS and abstract into layout/ (new bytes every run). A GEN_EDIT marker
# also edits the generator mid-run, as a person editing during a long gate.
REBUILDING_CHECK = '''
import time
from pathlib import Path

def run(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    lay = ws / "layout"
    stamp = str(time.time_ns())
    (lay / "blk.gds").write_text("gds " + stamp, encoding="utf-8")
    (lay / "blk.abstract.json").write_text('{"s": "%s"}' % stamp,
                                           encoding="utf-8")
    if (ws / "GEN_EDIT").exists():
        with open(lay / "gen_blk.py", "a", encoding="utf-8") as f:
            f.write("# edited mid-run " + stamp + "\\n")
    if (ws / "OTHER_EDIT").exists():
        (lay / "sub").mkdir(exist_ok=True)
        (lay / "sub" / "blk.gds").write_text(stamp, encoding="utf-8")
    payload = {"script": "check_fakedrc", "status": "pass",
               "counts": {"total": 0}, "violations": [],
               "report_schema": 1, "checker_version": 1,
               "generated_at": "2026-01-01T00:00:00+00:00",
               "input": str(ws), "input_digest": None}
    return payload, args.out
'''


def _setup(tmp_path: Path, gate_name: str = "drc"):
    ws = tmp_path / "ws"
    state_mod.State.init(ws, "ade", "blk")
    lay = ws / "layout"
    lay.mkdir(exist_ok=True)
    (lay / "gen_blk.py").write_text("def build():\n    pass\n",
                                    encoding="utf-8")
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "check_fakedrc.py").write_text(REBUILDING_CHECK,
                                             encoding="utf-8")
    gates_yaml = tmp_path / "gates.yaml"
    gates_yaml.write_text(
        f"version: 1\ngates:\n  ade:\n    {gate_name}:\n      phase: P3\n"
        "      tool: fakedrc\n      fail_severities: [error]\n"
        "      max_count: 0\n", encoding="utf-8")
    return ws, checks, gates_yaml


def _run(ws, checks, gates_yaml, gate_name="drc"):
    return gate.main(["--gate", gate_name, "--workspace", str(ws),
                      "--gates", str(gates_yaml),
                      "--checks-dir", str(checks)])


def _fresh(ws, gate_name="drc") -> bool:
    rep = state_mod.State.load(ws / "state.json").freshness()
    return rep["gates"][gate_name]["fresh"]


def test_a_gate_that_rebuilds_its_gds_reads_fresh_after_its_pass(
        tmp_path, capsys):
    ws, checks, gy = _setup(tmp_path)
    assert _run(ws, checks, gy) == 0           # no GDS at start
    out = json.loads(capsys.readouterr().out)
    assert out["rebuilt_during_run"] == ["layout"]
    assert not out["record_result"].get("inputs_changed_during_run")
    assert _fresh(ws)
    assert _run(ws, checks, gy) == 0           # an old GDS at start
    capsys.readouterr()
    assert _fresh(ws)
    # a real layout edit after the gate still makes it stale
    with open(ws / "layout" / "gen_blk.py", "a", encoding="utf-8") as f:
        f.write("# moved a finger\n")
    assert not _fresh(ws)


def test_a_generator_edited_mid_run_still_reads_stale(tmp_path, capsys):
    ws, checks, gy = _setup(tmp_path)
    (ws / "GEN_EDIT").write_text("x", encoding="utf-8")
    assert _run(ws, checks, gy) == 0
    out = json.loads(capsys.readouterr().out)
    assert "rebuilt_during_run" not in out
    assert "layout" in out["record_result"]["inputs_changed_during_run"]
    assert not _fresh(ws)


def test_a_gds_outside_the_dir_top_is_not_a_rebuilt_output(tmp_path, capsys):
    ws, checks, gy = _setup(tmp_path)
    (ws / "OTHER_EDIT").write_text("x", encoding="utf-8")
    assert _run(ws, checks, gy) == 0
    capsys.readouterr()
    assert not _fresh(ws)


def test_a_gate_not_listed_keeps_its_start_hash(tmp_path, capsys,
                                                monkeypatch):
    ws, checks, gy = _setup(tmp_path)
    real = statelib.load_map

    def no_rebuilds(*a, **k):
        m = dict(real(*a, **k))
        m["gate_rebuilds"] = {}
        return m
    monkeypatch.setattr(statelib, "load_map", no_rebuilds)
    assert _run(ws, checks, gy) == 0
    capsys.readouterr()
    assert not _fresh(ws)                      # the #381 symptom


def test_only_rebuilt_outputs_moved():
    f = statelib.only_rebuilt_outputs_moved
    assert f({"gen.py": "a"}, {"gen.py": "a", "b.gds": "1",
                               "b.abstract.json": "2"})
    assert f({"gen.py": "a", "b.gds": "1"}, {"gen.py": "a", "b.gds": "9"})
    assert f({"b.gds": "1"}, {})
    assert not f({"gen.py": "a"}, {"gen.py": "b", "b.gds": "1"})
    assert not f({}, {"x/b.gds": "1"})
    assert not f({}, {"notes.txt": "1"})


def test_load_map_refuses_bad_gate_rebuilds(tmp_path):
    src = (ENGINE / "reference" / "invalidation.yaml").read_text(
        encoding="utf-8")
    for bad, msg in ((" drc: [layout]", "unknown gate"),
                     (" drc: [layout, rtl]", "not in its row")):
        p = tmp_path / "inv.yaml"
        repl = bad.replace("drc", "nosuchgate") if "unknown" in msg else bad
        p.write_text(src.replace("    drc:          [layout]",
                                 "   " + repl, 1), encoding="utf-8")
        with pytest.raises(ValueError, match=msg):
            statelib.load_map(p)
    p = tmp_path / "inv.yaml"
    p.write_text(src.replace("gate_rebuilds:\n  ade:", "gate_rebuilds:\n"
                             "  nosuch: {}\n  ade:", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown skills"):
        statelib.load_map(p)


@pytest.mark.slow
def test_mirror_releases_after_em_ir_and_a_layout_edit_still_stales(
        tmp_path, capsys):
    """The #381 rung on a small analog rung: every ade gate in order on a
    fresh scratch mirror (no GDS at the start: drc builds it, em_ir and
    pex_sim rebuild it), then release passes; editing the generator after
    makes drc read stale and release refuses."""
    import faults
    ws = faults.make_scratch_workspace(tmp_path, REPO / "corpus" / "ade" /
                                       "mirror", "ade", "mirror")
    order = [g for _, g in state_mod.applicable_gate_order("ade")]
    assert order[-1] == "release"
    for g in order:
        code = gate.main(["--gate", g, "--workspace", str(ws)])
        out = capsys.readouterr().out
        assert code == 0, (g, out[-2000:])
    fresh = state_mod.State.load(ws / "state.json").freshness()["gates"]
    assert fresh["drc"]["fresh"] and fresh["em_ir"]["fresh"]
    gen = ws / "layout" / "gen_mirror.py"
    gen.write_text(gen.read_text(encoding="utf-8") + "\n# moved a finger\n",
                   encoding="utf-8")
    fresh = state_mod.State.load(ws / "state.json").freshness()["gates"]
    assert not fresh["drc"]["fresh"]
    assert gate.main(["--gate", "release", "--workspace", str(ws)]) != 0
    assert "drc" in capsys.readouterr().out

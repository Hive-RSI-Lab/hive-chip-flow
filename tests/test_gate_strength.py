"""Gate strength (#163, docs/design.md 1.6): gates.yaml declares a `strength`
per gate, bumped by hand when a change can turn a pass into a fail. A record
below it reads stale and release refuses it. The engine source is not hashed,
so a change with no bump stales nothing. Fabricated results, no tool runs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402
import yaml  # noqa: E402

import attest as attest_mod  # noqa: E402
import check_release  # noqa: E402
import checklib  # noqa: E402
import gate as gate_mod  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402

REAL_GATES = ENGINE / "reference" / "gates.yaml"


def make_ws(tmp_path: Path, skill="vde", block="counter8") -> Path:
    ws = tmp_path / "ws"
    state_mod.State.init(ws, skill, block)
    return ws


def pass_every_gate(ws: Path, skill="vde", fail: str | None = None) -> None:
    st = state_mod.State.load(ws / "state.json")
    for g in statelib.load_map()["gate_inputs"][skill]:
        if g != "release":
            st.record_gate(g, {"status": "fail" if g == fail else "pass"})
    st.save()


def gates_with(tmp_path: Path, name: str, skill: str, gate: str, value) -> Path:
    """A copy of the real gates.yaml with one row's strength set."""
    data = yaml.safe_load(REAL_GATES.read_text(encoding="utf-8"))
    data["gates"][skill][gate]["strength"] = value
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def bump(monkeypatch, tmp_path, skill="vde", gate="lint", value=1) -> None:
    monkeypatch.setattr(statelib, "DEFAULT_GATES", gates_with(
        tmp_path, f"gates-{skill}-{gate}-{value}.yaml", skill, gate, value))


def freshness(ws: Path) -> dict:
    return state_mod.State.load(ws / "state.json").freshness()


# ---------------------------------------------------------------- the table

def test_every_gate_row_declares_an_integer_strength():
    data = yaml.safe_load(REAL_GATES.read_text(encoding="utf-8"))
    for skill, rows in data["gates"].items():
        for gate, row in rows.items():
            assert type(row.get("strength")) is int, (skill, gate)
            assert row["strength"] >= 0, (skill, gate)


@pytest.mark.parametrize("bad", ["1", 1.5, True, -1, None])
def test_a_malformed_strength_raises(tmp_path, bad):
    p = gates_with(tmp_path, "bad.yaml", "vde", "lint", bad)
    with pytest.raises(ValueError, match="vde.lint.strength"):
        statelib.load_strengths(p)


def test_a_row_with_no_strength_and_an_unknown_gate_read_as_zero(tmp_path):
    p = tmp_path / "g.yaml"
    p.write_text("gates: {vde: {lint: {tool: x}}}\n", encoding="utf-8")
    assert statelib.gate_strength("vde", "lint", p) == 0
    assert statelib.gate_strength("vde", "nope", p) == 0
    assert statelib.gate_strength("nope", "lint", p) == 0


# ---------------------------------------------------------------- the record

def test_record_stores_the_strength_in_force(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    bump(monkeypatch, tmp_path, value=3)
    st = state_mod.State.load(ws / "state.json")
    st.record_gate("lint", {"status": "pass"})
    assert st.data["gates"]["lint"]["last"]["strength"] == 3


def test_record_refuses_a_malformed_strength_at_start(tmp_path):
    st = state_mod.State.load(make_ws(tmp_path) / "state.json")
    with pytest.raises(state_mod.CheckError, match="strength_at_start"):
        st.record_gate("lint", {"status": "pass", "strength_at_start": "1"})


BUMPING_CHECK = '''
def run(argv=None):
    import argparse, os
    from pathlib import Path
    import statelib
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    statelib.DEFAULT_GATES = Path(os.environ["STRENGTH_TEST_GATES"])
    payload = {"script": "check_fakegate", "status": "pass",
              "counts": {"total": 0}, "violations": [],
              "report_schema": 1, "checker_version": 1,
              "generated_at": "2026-01-01T00:00:00+00:00",
              "input": str(args.workspace), "input_digest": None}
    return payload, args.out
'''

FAKE_GATES_YAML = '''
version: 1
gates:
  vde:
    lint:
      phase: P4
      tool: fakegate
      fail_severities: [error]
      max_count: 0
'''


def test_a_bump_during_a_run_leaves_that_run_stale(tmp_path, monkeypatch, capsys):
    """A long job started under the weaker gate gave the weaker gate's pass,
    so it records the strength at its start, not the one at its end."""
    ws = make_ws(tmp_path)
    (ws / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "check_fakegate.py").write_text(BUMPING_CHECK, encoding="utf-8")
    gates = tmp_path / "fake-gates.yaml"
    gates.write_text(FAKE_GATES_YAML, encoding="utf-8")
    monkeypatch.setattr(statelib, "DEFAULT_GATES", statelib.DEFAULT_GATES)
    monkeypatch.setenv("STRENGTH_TEST_GATES", str(
        gates_with(tmp_path, "bumped.yaml", "vde", "lint", 1)))
    code = gate_mod.main(["--gate", "lint", "--workspace", str(ws),
                          "--gates", str(gates), "--checks-dir", str(checks)])
    assert code == 0, capsys.readouterr()
    assert json.loads(capsys.readouterr().out)["strength_at_start"] == 0
    st = state_mod.State.load(ws / "state.json")
    assert st.data["gates"]["lint"]["last"]["strength"] == 0
    assert st.freshness()["gates"]["lint"]["fresh"] is False


# ---------------------------------------------------------------- freshness

def test_a_bump_stales_the_record_and_names_the_gate_and_both_values(
        tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    assert freshness(ws)["gates"]["lint"]["fresh"] is True
    bump(monkeypatch, tmp_path, value=2)
    rep = freshness(ws)
    verdict = rep["gates"]["lint"]
    assert verdict["fresh"] is False
    assert verdict["hash_valid"] is True        # the inputs did not move
    assert verdict["strength_stale"] == (
        "gate lint is now strength 2, but this result was recorded at "
        "strength 0; re-run it")
    assert "lint" in rep["summary"]["stale"]


def test_a_bump_stales_only_that_gate_in_that_skill(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    bump(monkeypatch, tmp_path, skill="ade", gate="drc")
    assert freshness(ws)["gates"]["lint"]["fresh"] is True
    assert freshness(ws)["summary"]["stale"] == []
    bump(monkeypatch, tmp_path, value=1)
    rep = freshness(ws)
    assert rep["summary"]["stale"] == ["lint"]


def test_rerunning_at_the_new_strength_clears_it(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    bump(monkeypatch, tmp_path)
    st = state_mod.State.load(ws / "state.json")
    st.record_gate("lint", {"status": "pass"})
    st.save()
    assert freshness(ws)["gates"]["lint"]["fresh"] is True


def test_a_record_with_no_strength_reads_zero(tmp_path, monkeypatch):
    """Nothing changes for old records until the first bump."""
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    data = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    for entry in data["gates"].values():
        del entry["last"]["strength"]
    (ws / "state.json").write_text(json.dumps(data), encoding="utf-8")
    rep = statelib.freshness_report(data, ws)
    assert rep["gates"]["lint"]["fresh"] is True
    assert "strength_stale" not in rep["gates"]["lint"]
    bump(monkeypatch, tmp_path)
    rep = statelib.freshness_report(data, ws)
    assert "recorded at strength 0" in rep["gates"]["lint"]["strength_stale"]


@pytest.mark.parametrize("junk", ["2", True, 1.5, None, -1])
def test_an_unreadable_recorded_strength_reads_zero(junk):
    entry = {"last": {"inputs": {"a": "x"}, "strength": junk}}
    verdict = statelib.gate_freshness(entry, {"a": "x"}, "g", 1)
    assert "recorded at strength 0" in verdict["strength_stale"]


def test_a_higher_recorded_strength_is_not_stale():
    entry = {"last": {"inputs": {"a": "x"}, "strength": 5}}
    assert statelib.gate_freshness(entry, {"a": "x"}, "g", 1)["fresh"] is True


def test_an_engine_change_with_no_bump_leaves_it_fresh(tmp_path, monkeypatch):
    """A gate's code is not an input: a record holds no engine hash, and a
    new checker version or edited gate row with the strength unchanged moves
    nothing."""
    ws = make_ws(tmp_path)
    pass_every_gate(ws)
    before = freshness(ws)["gates"]
    data = yaml.safe_load(REAL_GATES.read_text(encoding="utf-8"))
    data["gates"]["vde"]["lint"]["fail_severities"] = ["error", "warning"]
    edited = tmp_path / "edited.yaml"
    edited.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(statelib, "DEFAULT_GATES", edited)
    monkeypatch.setattr(checklib, "CHECKER_VERSION",
                        checklib.CHECKER_VERSION + 1)
    assert freshness(ws)["gates"] == before
    assert before["lint"]["fresh"] is True
    assert "engine/" not in json.dumps(statelib.load_map()["artifact_kinds"])


# ---------------------------------------------------------------- release

def test_release_refuses_a_record_below_the_gates_strength(
        tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    (ws / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(
        "top: top\nrequirements: []\n", encoding="utf-8")
    pass_every_gate(ws)
    assert check_release.main(["--workspace", str(ws)]) == 0, capsys.readouterr()
    capsys.readouterr()
    bump(monkeypatch, tmp_path, value=2)
    assert check_release.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    msgs = " ".join(v["msg"] for v in out["violations"])
    assert "gate lint is now strength 2" in msgs and "strength 0" in msgs


def test_attest_build_names_the_bump(tmp_path, monkeypatch):
    ws = make_ws(tmp_path, "msde", "sensor_counted")
    pass_every_gate(ws, "msde")
    gate = next(iter(statelib.load_map()["gate_inputs"]["msde"]))
    bump(monkeypatch, tmp_path, skill="msde", gate=gate, value=4)
    att, problems = attest_mod.build(ws)
    assert att is None
    assert any(f"gate {gate} is now strength 4" in p and "strength 0" in p
               for p in problems), problems


def test_verify_fails_an_attestation_once_a_gate_is_bumped(tmp_path, monkeypatch):
    """A nested msde release goes through attest verify, so a bump reaches it."""
    ws = make_ws(tmp_path, "msde", "sensor_counted")
    pass_every_gate(ws, "msde")
    att, problems = attest_mod.build(ws)
    assert problems == []
    attest_mod.write_attestation(ws, att)
    assert attest_mod.verify(ws)["valid"] is True
    gate = next(iter(statelib.load_map()["gate_inputs"]["msde"]))
    bump(monkeypatch, tmp_path, skill="msde", gate=gate)
    assert attest_mod.verify(ws)["valid"] is False


def test_the_reason_keeps_the_bump_when_an_input_moved_too(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    (ws / "rtl" / "top.v").write_text("module a; endmodule\n", encoding="utf-8")
    pass_every_gate(ws)
    (ws / "rtl" / "top.v").write_text("module b; endmodule\n", encoding="utf-8")
    bump(monkeypatch, tmp_path)
    _, problems = attest_mod.build(ws)
    assert any("input changed" in p and "gate lint is now strength 1" in p
               for p in problems), problems


def test_a_waiver_does_not_cover_a_bump(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    (ws / "rtl" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(
        "top: top\nrequirements: []\n", encoding="utf-8")
    pass_every_gate(ws, fail="mutate")
    bump(monkeypatch, tmp_path, gate="mutate")
    data = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    current = statelib.freshness_report(data, ws)["gates"]["mutate"][
        "current_inputs"]
    (ws / "reports").mkdir(exist_ok=True)
    (ws / "reports" / "waivers.json").write_text(json.dumps([{
        "gate": "mutate", "reason": "approved before the gate got stricter",
        "approved_by": "a reviewer",
        "inputs_sha256": attest_mod.waiver_inputs_sha256(current),
        "checker_version": checklib.CHECKER_VERSION,
    }]), encoding="utf-8")
    assert check_release.main(["--workspace", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any("gate mutate is now strength 1" in v["msg"]
               for v in out["violations"]), out

"""engine/lib/measurelib.py + its two callers (issue #89): a vde
`check: measure` requirement names the recorded fact it is held to,
spec_lint refuses one that names none, and release fails a bound the
recorded value misses or a value nobody recorded. Hermetic: gate records are
written by hand, never a real tool run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import attest as attest_mod  # noqa: E402
import check_release  # noqa: E402
import measurelib  # noqa: E402
import speclib  # noqa: E402
import state as state_mod  # noqa: E402
import statelib  # noqa: E402
from test_check_release import make_msde_ws  # noqa: E402

RTL = ("module top(input wire clk, output reg q);\n"
       "  always @(posedge clk) q <= ~q;\nendmodule\n")


def spec_with(bounds: str) -> str:
    return ("top: top\nrequirements:\n"
            "  - id: REQ-TIM\n    text: clk meets timing\n"
            f"    check: measure\n    bounds: {bounds}\n")


def record(ws: Path, gate: str, facts: dict) -> None:
    d = ws / "reports" / "recorded"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"gate-{gate}.json").write_text(
        json.dumps({"gate": gate, "status": "pass", "facts": facts}),
        encoding="utf-8")


def make_ws(tmp_path: Path, spec: str, name: str = "ws") -> Path:
    ws = tmp_path / name
    state_mod.State.init(ws, "vde", "counter8")
    (ws / "rtl" / "top.v").write_text(RTL, encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(spec, encoding="utf-8")
    st = state_mod.State.load(ws / "state.json")
    for g in statelib.load_map()["gate_inputs"]["vde"]:
        st.record_gate(g, {"status": "pass"})
    st.save()
    return ws


def release(ws: Path, capsys) -> tuple[int, dict]:
    code = check_release.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def lint(spec: str) -> list[dict]:
    import yaml
    return speclib.lint_spec(yaml.safe_load(spec))


TIMING = "{clk_wns_ns: {min: 100, source: timing.corners.nom_tt.setup_ws}}"


def test_release_fails_a_measure_outside_its_limit(tmp_path, capsys):
    # planted fault: bound min 100 on a recorded WNS of ~+70 ns
    ws = make_ws(tmp_path, spec_with(TIMING))
    record(ws, "timing", {"corners": {"nom_tt": {"setup_ws": 70.2}}})
    code, out = release(ws, capsys)
    assert code == 1, out
    kinds = [v["kind"] for v in out["violations"]]
    assert kinds == ["measure_out_of_bounds"], out["violations"]
    assert "REQ-TIM" in out["violations"][0]["refs"]
    assert not (ws / "reports" / "checks.json").exists()


def test_release_passes_a_measure_within_its_limit(tmp_path, capsys):
    ws = make_ws(tmp_path, spec_with(TIMING.replace("100", "0")))
    record(ws, "timing", {"corners": {"nom_tt": {"setup_ws": 70.2}}})
    code, out = release(ws, capsys)
    assert code == 0, out
    assert out["measures"] == [{
        "req": "REQ-TIM", "name": "clk_wns_ns",
        "source": "timing.corners.nom_tt.setup_ws", "min": 0,
        "value": 70.2, "status": "pass", "why": ""}]
    assert (ws / "reports" / "checks.json").is_file()


def test_release_fails_a_measure_nobody_recorded(tmp_path, capsys):
    ws = make_ws(tmp_path, spec_with(TIMING.replace("100", "0")))
    code, out = release(ws, capsys)                    # no timing record
    assert code == 1
    assert [v["kind"] for v in out["violations"]] == ["measure_missing"]
    record(ws, "timing", {"corners": {}})              # path not in facts
    code, out = release(ws, capsys)
    assert code == 1
    assert "nom_tt" in out["violations"][0]["msg"]


def test_release_fails_a_non_numeric_value_for_min_max(tmp_path, capsys):
    ws = make_ws(tmp_path, spec_with(TIMING.replace("100", "0")))
    record(ws, "timing", {"corners": {"nom_tt": {"setup_ws": None}}})
    code, out = release(ws, capsys)
    assert code == 1
    assert out["violations"][0]["kind"] == "measure_missing"


def test_known_answer_counter_cell_count(tmp_path):
    # known answer: cells <= N passes at the recorded N, fails at N-1
    def rows(n):
        ws = tmp_path / f"n{n}"
        (ws / "spec").mkdir(parents=True)
        record(ws, "synth", {"cell_count": 23})
        spec = {"requirements": [{"id": "REQ-AREA", "check": "measure",
                "bounds": {"cells": {"max": n, "source": "synth.cell_count"}}}]}
        return measurelib.evaluate(ws, spec)
    assert rows(23)[0]["status"] == "pass"
    assert rows(22)[0]["status"] == "fail"


def test_equals_bound(tmp_path):
    record(tmp_path, "lvs", {"matched": True})
    spec = {"requirements": [{"id": "R", "check": "measure", "bounds": {
        "ok": {"equals": True, "source": "lvs.matched"},
        "not1": {"equals": 1, "source": "lvs.matched"}}}]}
    st = {r["name"]: r["status"] for r in measurelib.evaluate(tmp_path, spec)}
    assert st == {"ok": "pass", "not1": "fail"}


MSDE_SPEC = {"requirements": [{"id": "R", "check": "measure", "bounds": {
    "lock": {"max": 50, "source": "msde:cosim.measures.lock_time_us"}}}]}


def test_msde_bound_is_deferred_by_digital_and_read_by_msde(tmp_path):
    top = tmp_path / "pll"
    top.mkdir()
    (top / "state.json").write_text(json.dumps({"skill": "msde"}))
    ws = top / "digital"
    ws.mkdir()
    # digital releases before cosim runs: no record yet must not block it
    assert measurelib.evaluate(ws, MSDE_SPEC)[0]["status"] == "deferred"
    assert measurelib.nested_msde_rows(ws, MSDE_SPEC)[0]["status"] == "missing"
    record(top, "cosim", {"measures": {"lock_time_us": 4.5}})
    assert measurelib.nested_msde_rows(ws, MSDE_SPEC)[0]["status"] == "pass"
    record(top, "cosim", {"measures": {"lock_time_us": 60}})
    assert measurelib.nested_msde_rows(ws, MSDE_SPEC)[0]["status"] == "fail"


def test_msde_bound_with_no_msde_block_is_missing_not_deferred(tmp_path):
    # nobody above would ever check it, so deferring would be a silent pass
    alone = tmp_path / "alone"
    alone.mkdir()
    assert measurelib.evaluate(alone, MSDE_SPEC)[0]["status"] == "missing"


def test_digital_release_passes_with_a_deferred_bound(tmp_path, capsys):
    top = tmp_path / "pll"
    top.mkdir()
    (top / "state.json").write_text(json.dumps({"skill": "msde"}))
    ws = make_ws(top, spec_with(
        "{lock: {max: 50, source: 'msde:cosim.measures.lock_time_us'}}"),
        name="digital")
    code, out = release(ws, capsys)
    assert code == 0, out
    assert out["measures"][0]["status"] == "deferred"


def _msde_ws(tmp_path, lock_us):
    """A whole msde block, digital side released with an msde-sourced bound
    and every top gate passed; cosim recorded lock_us."""
    top = make_msde_ws(tmp_path)
    sub = top / "digital"
    (sub / "spec" / "spec.yaml").write_text(spec_with(
        "{lock: {max: 50, source: 'msde:cosim.measures.lock_time_us'}}"),
        encoding="utf-8")
    st = state_mod.State.load(sub / "state.json")
    for g in statelib.load_map()["gate_inputs"]["vde"]:
        st.record_gate(g, {"status": "pass"})
    st.save()
    att, problems = attest_mod.build(sub)
    assert att is not None, problems
    attest_mod.write_attestation(sub, att)
    # the top gates hash the digital spec, so they run after this edit
    st = state_mod.State.load(top / "state.json")
    for g in statelib.load_map()["gate_inputs"]["msde"]:
        st.record_gate(g, {"status": "pass"})
    st.save()
    record(top, "cosim", {"measures": {"lock_time_us": lock_us}})
    return top


def test_msde_release_refuses_a_nested_bound_cosim_misses(tmp_path, capsys):
    top = _msde_ws(tmp_path, 60.0)
    code, out = release(top, capsys)
    assert code == 1
    assert [v["kind"] for v in out["violations"]] == ["measure_out_of_bounds"]
    assert not (top / "reports" / "checks.json").exists()


def test_msde_release_passes_a_nested_bound_cosim_meets(tmp_path, capsys):
    top = _msde_ws(tmp_path, 4.5)
    code, out = release(top, capsys)
    assert code == 0, out
    assert out["measures"][0]["status"] == "pass"
    assert (top / "reports" / "checks.json").is_file()


def test_wiring_a_source_leaves_hardened_gates_fresh_but_a_limit_does_not(
        tmp_path):
    # a source says where release reads a number; no gate hashing this reads
    # it. A changed limit still moves the hash, as any other spec edit does.
    def digest(bound):
        p = tmp_path / "spec.yaml"
        p.write_text(spec_with(bound), encoding="utf-8")
        return statelib._norm_json_canonical_sans_formal(p)
    bare = digest("{x: {min: 0}}")
    assert digest("{x: {min: 0, source: timing.a, note: n}}") == bare
    assert digest("{x: {min: 1, source: timing.a}}") != bare


def test_spec_lint_refuses_a_measure_with_no_source():
    found = lint(spec_with("{clk_wns_ns: {min: 0}}"))
    assert [v["kind"] for v in found] == ["requirement_measure_no_source"]


def test_spec_lint_refuses_a_bad_source_or_bound():
    kinds = lambda b: [v["kind"] for v in lint(spec_with(b))]  # noqa: E731
    assert kinds("{x: {min: 0, source: nosuchgate.a}}") == \
        ["requirement_measure_bad_source"]
    assert kinds("{x: {min: 0, source: timing}}") == \
        ["requirement_measure_bad_source"]
    assert kinds("{x: {source: timing.a}}") == ["requirement_measure_bad_bound"]
    assert kinds("{x: {min: high, source: timing.a}}") == \
        ["requirement_measure_bad_bound"]
    assert kinds("{x: {min: 0, source: msde:cosim.measures.a}}") == []
    assert kinds("{x: {min: 0, source: timing.corners.tt.setup_ws}}") == []


def test_pll_digital_measures_are_wired():
    # the PLL's 11 measure requirements each name a source. RELOCK and
    # MUT-KILL read missing on purpose: nothing records those numbers yet
    ws = REPO / "blocks" / "pll" / "digital"
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    assert speclib.lint_spec(spec) == []
    assert len(measurelib.measure_requirements(spec)) == 11
    by_req = {}
    for r in measurelib.evaluate(ws, spec):
        by_req.setdefault(r["req"], set()).add(r["status"])
    assert by_req["REQ-MUT-KILL"] == {"missing"}
    assert by_req["REQ-LOOP-LOCK"] == {"deferred"}
    assert by_req["REQ-HARDEN-STANDALONE"] == {"pass"}
    nested = {r["req"]: r["status"]
              for r in measurelib.nested_msde_rows(ws, spec)}
    assert nested["REQ-LOOP-RELOCK"] == "missing"
    assert nested["REQ-LOOP-LOCK"] == "pass"

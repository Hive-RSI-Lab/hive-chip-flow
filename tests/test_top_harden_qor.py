"""top_harden's QoR against the recorded top_harden (#340): the assembled top
is compared with its own last record, reports/recorded/gate-top_harden.json
of the msde workspace, never with the digital side's harden. LibreLane is
faked as in test_check_harden; the top's assembly is stubbed."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))
sys.path.insert(0, str(REPO / "bin"))

import check_harden  # noqa: E402
import check_top_harden  # noqa: E402
import gate  # noqa: E402
from test_check_harden import (  # noqa: E402
    _clean_behavior, _fake_run_factory, make_ws)

TS = "2026-11-01T00:00:00Z"
# the top's per-corner slew and cap counts, all clean (#177)
CLEAN_DRV = {f"design__max_{k}_violation__count__corner:nom_tt_025C_3v30": 0
             for k in ("slew", "cap")}


def _msde_ws(tmp_path: Path, monkeypatch) -> Path:
    """An msde workspace whose assembled top/ is a plain harden workspace,
    with a macro that needs no probe, no don't-touch net and no clock."""
    ws = tmp_path / "msde"
    ws.mkdir()
    top = make_ws(tmp_path)
    shutil.move(str(top), str(ws / "top"))
    spec = {"macros": [{"cell": "tile_analog", "instance": "u_analog",
                        "location": [10, 10], "size_um": [5, 5],
                        "pins": {}}],
            "harden_signoff": {}}
    monkeypatch.setattr(check_top_harden, "assemble",
                        lambda w: (ws / "top", spec))
    monkeypatch.setattr(check_top_harden, "unpowered_supplies", lambda m: [])
    return ws


def _top_harden(ws: Path, monkeypatch, capsys, setup_ws: float):
    shutil.rmtree(ws / "top" / "runs", ignore_errors=True)
    monkeypatch.setattr(check_harden.subprocess, "run", _fake_run_factory(
        _clean_behavior({"timing__setup__ws": setup_ws, **CLEAN_DRV})))
    check_top_harden.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert "qor" in out, out
    return out


def _record(ws: Path, report: dict, ts: str) -> dict:
    """What gate.py records for the run: the gate result, written where the
    next top_harden reads its baseline."""
    g = gate.load_gates(gate.DEFAULT_GATES)["msde"]["top_harden"]
    result = gate.evaluate("top_harden", g, report)
    gate.write_recorded_copy(ws, "top_harden", result,
                             {"last": {"ts": ts}, "attempts": 1})
    return result


def test_rerecorded_top_harden_with_worse_slack_shows_the_regression(
        tmp_path, monkeypatch, capsys):
    import check_all
    ws = _msde_ws(tmp_path, monkeypatch)
    # a digital-side harden record must not be the top's baseline
    (ws / "top" / "reports" / "recorded").mkdir(parents=True)
    (ws / "top" / "reports" / "recorded" / "gate-harden.json").write_text(
        json.dumps({"status": "pass", "recorded_ts": "x",
                    "facts": {"qor": {"metrics": {}}}}), encoding="utf-8")

    first = _top_harden(ws, monkeypatch, capsys, 1.0)
    assert first["qor_diff"]["baseline"] == "none", first["qor_diff"]
    assert first["violations"] == [], [v["msg"] for v in first["violations"]]
    _record(ws, first, TS)

    second = _top_harden(ws, monkeypatch, capsys, 0.5)
    d = second["qor_diff"]
    assert d["baseline"] == "recorded" and d["baseline_ts"] == TS
    assert [r["metric"] for r in d["regressions"]] == ["setup_ws"]
    regs = [v for v in second["violations"] if v["kind"] == "qor_regression"]
    assert len(regs) == 1 and regs[0]["severity"] == "warning"
    assert "recorded top_harden of " + TS in regs[0]["msg"]

    # check-all prints it under the top_harden step, from the gate result
    logs = tmp_path / "logs"
    logs.mkdir()
    result = gate.evaluate(
        "top_harden",
        gate.load_gates(gate.DEFAULT_GATES)["msde"]["top_harden"], second)
    (logs / "b-top_harden.json").write_text(json.dumps(result))
    line = check_all.qor_line(logs, "b", "top_harden")
    assert line == (f"qor vs recorded of {TS}: 1 regression(s): "
                    "setup_ws 1.0 -> 0.5"), line


def test_the_digital_specs_opt_in_makes_a_top_regression_fail(
        tmp_path, monkeypatch, capsys):
    """harden_signoff.qor_regressions: error, carried from the digital spec
    into top/spec/spec.yaml, fails the top_harden gate on a regression."""
    ws = _msde_ws(tmp_path, monkeypatch)
    with (ws / "top" / "spec" / "spec.yaml").open("a", encoding="utf-8") as f:
        f.write("harden_signoff: {qor_regressions: error}\n")
    _record(ws, _top_harden(ws, monkeypatch, capsys, 1.0), TS)
    second = _top_harden(ws, monkeypatch, capsys, 0.5)
    regs = [v for v in second["violations"] if v["kind"] == "qor_regression"]
    assert [v["severity"] for v in regs] == ["error"]
    assert second["status"] == "violations"
    assert _record(ws, second, "2026-11-02T00:00:00Z")["status"] == "fail"

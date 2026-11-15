"""#83: every cocotb gate runs at a pinned, recorded random seed.

cocotblib.sim_seed picks it (a gate's --seed, else spec sim.seed, else a
digest of the bench); the fast tests pin that choice. The slow ones run real
check_sim twice at one seed and see the same draw, and plant a seed the bench
fails at and see it named, with its replay line, in the report."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import cocotblib  # noqa: E402
from checklib import CheckError  # noqa: E402


def bench(tmp_path: Path) -> Path:
    tb = tmp_path / "tb"
    tb.mkdir()
    (tb / "test_top.py").write_text("x = 1\n", encoding="utf-8")
    return tb


def test_override_beats_spec_beats_digest(tmp_path):
    tb = bench(tmp_path)
    assert cocotblib.sim_seed({"sim": {"seed": 7}}, tb, 9) == 9
    assert cocotblib.sim_seed({"sim": {"seed": 7}}, tb) == 7
    assert cocotblib.sim_seed({"sim": {"seed": 0}}, tb) == 0
    d = cocotblib.sim_seed({}, tb)
    assert 0 <= d <= cocotblib.SEED_MAX


@pytest.mark.parametrize("bad", [-1, 2**31, "5", 1.5, True])
def test_bad_seed_is_an_error_not_a_silent_default(tmp_path, bad):
    tb = bench(tmp_path)
    with pytest.raises(CheckError, match="sim.seed"):
        cocotblib.sim_seed({"sim": {"seed": bad}}, tb)
    with pytest.raises(CheckError, match="--seed"):
        cocotblib.sim_seed({}, tb, bad)


def test_digest_seed_follows_the_bench_and_nothing_else(tmp_path):
    tb = bench(tmp_path)
    first = cocotblib.sim_seed({}, tb)
    assert cocotblib.sim_seed({}, tb) == first
    # what a run leaves behind is not the bench
    (tb / "__pycache__").mkdir()
    (tb / "__pycache__" / "test_top.cpython-312.pyc").write_bytes(b"\0")
    (tb / "coverage.dat").write_text("junk", encoding="utf-8")
    assert cocotblib.sim_seed({}, tb) == first
    (tb / "test_top.py").write_text("x = 2\n", encoding="utf-8")
    assert cocotblib.sim_seed({}, tb) != first


def test_replay_line_names_the_gate_and_seed():
    assert cocotblib.replay_command("check_sim", 42) == (
        "bin/eda python engine/scripts/check_sim.py --workspace <ws> "
        "--seed 42")
    assert cocotblib.replay_command("check_mutate", 3, "--sim-seed") \
        .endswith("check_mutate.py --workspace <ws> --sim-seed 3")


# A bench whose verdict hangs on Python's `random`: odd draw fails. Under
# cocotb 2.1 seed 1 draws odd, seed 2 even.
TB_DRAW = """\
import random
import cocotb
from cocotb.triggers import Timer

# req: REQ-WRAP
@cocotb.test()
async def test_draw(dut):
    await Timer(1, unit="ns")
    v = random.getrandbits(32)
    assert v % 2 == 0, f"drew {v}"
"""


def run_sim(ws: Path, *extra: str) -> tuple[int, dict]:
    import check_sim
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = check_sim.main(["--workspace", str(ws), *extra])
    return code, json.loads(buf.getvalue())


@pytest.mark.slow
def test_same_seed_same_draw_and_a_failing_seed_is_named(tmp_path):
    from test_check_sim import RTL_GOOD, SPEC_ONE_REQ, make_ws
    ws = make_ws(tmp_path, RTL_GOOD, SPEC_ONE_REQ, TB_DRAW)
    runs = [run_sim(ws, "--seed", "1") for _ in range(2)]
    for code, out in runs:
        assert code == 1, out
        assert out["seed"] == 1
        assert out["replay"].endswith("--seed 1")
    msgs = [next(v["msg"] for v in out["violations"]
                 if v["kind"] == "test_failed") for _, out in runs]
    assert msgs[0] == msgs[1], "one seed, two different draws"
    assert "at seed 1" in msgs[0] and "--seed 1)" in msgs[0], msgs[0]

    code, out = run_sim(ws, "--seed", "2")
    assert code == 0 and out["seed"] == 2, out


@pytest.mark.slow
def test_spec_seed_and_digest_seed_are_recorded(tmp_path):
    from test_check_sim import RTL_GOOD, SPEC_ONE_REQ, make_ws
    ws = make_ws(tmp_path, RTL_GOOD, SPEC_ONE_REQ + "sim:\n  seed: 1\n",
                 TB_DRAW)
    code, out = run_sim(ws)
    assert code == 1 and out["seed"] == 1, out

    ws2 = make_ws(tmp_path / "b", RTL_GOOD, SPEC_ONE_REQ, TB_DRAW)
    want = cocotblib.sim_seed({}, ws2 / "tb")
    a, b = run_sim(ws2), run_sim(ws2)
    assert a[1]["seed"] == b[1]["seed"] == want
    assert a[0] == b[0]

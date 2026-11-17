"""engine/lib/wavelib.py and engine/scripts/wave.py (#93): a failing sim's
waves as a text window, a failure signature, and the clustering that uses
it. Hermetic: the VCDs here are written with pyvcd (in the eda image), and
every window is cross-checked against pyvcd's own reader, a second VCD
parser this module does not share code with. Needs `eda python -m pytest`
(pyvcd); the real-sim tests are in test_check_sim.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import cluster_violations  # noqa: E402
import wave  # noqa: E402
import wavelib  # noqa: E402

vcd = pytest.importorskip("vcd")


def write_counter_vcd(path: Path, wrap_at: int = 254, cycles: int = 300,
                      period_ps: int = 20000) -> None:
    """A tt_frame-shaped dump: top scope with clk/rst/count, the block
    below it with its own count register, an X on count before reset. The
    counter wraps to 0 after wrap_at (the planted fault: 254, one early)."""
    from vcd import VCDWriter
    with open(path, "w", encoding="utf-8") as f, \
            VCDWriter(f, timescale="1 ps", date="today") as w:
        clk = w.register_var("tt_frame_x", "clk", "wire", size=1, init=0)
        rst = w.register_var("tt_frame_x", "rst", "wire", size=1, init=1)
        cnt = w.register_var("tt_frame_x", "count", "wire", size=8,
                             init="x")
        reg = w.register_var("tt_frame_x.dut.u_x", "count", "reg", size=8,
                             init="x")
        n = None
        for k in range(cycles):
            t = k * period_ps
            w.change(clk, t + period_ps // 2, 1)
            # the flop: updates on the edge from the value before it
            n = 0 if k < 3 or n is None else (0 if n >= wrap_at else n + 1)
            if k >= 1:
                w.change(cnt, t + period_ps // 2, n)
                w.change(reg, t + period_ps // 2, n)
            if k == 2:
                w.change(rst, t + period_ps // 2 + 1, 0)
            w.change(clk, t + period_ps, 0)


def pyvcd_values(path: Path) -> tuple[dict, dict]:
    """({full path: id}, {id: [(t, int|str)]}) read with pyvcd's tokenizer."""
    from vcd.reader import TokenKind, tokenize
    ids, ch, stack, t = {}, {}, [], 0
    with open(path, "rb") as f:
        for tok in tokenize(f):
            k = tok.kind
            if k is TokenKind.SCOPE:
                stack.append(tok.data.ident)
            elif k is TokenKind.UPSCOPE:
                stack.pop()
            elif k is TokenKind.VAR:
                ids.setdefault(".".join(stack + [tok.data.reference]),
                               tok.data.id_code)
            elif k is TokenKind.CHANGE_TIME:
                t = tok.data
            elif k in (TokenKind.CHANGE_SCALAR, TokenKind.CHANGE_VECTOR):
                ch.setdefault(tok.data.id_code, []).append((t, tok.data.value))
    return ids, ch


def pyvcd_before(ids, ch, path: str, t: int):
    """The value of path just before t, from pyvcd's reading."""
    prev = None
    for tt, v in ch[ids[path]]:
        if tt >= t:
            break
        prev = v
    return prev


def as_text(v, width: int) -> str:
    """pyvcd's value as wavelib prints it: decimal, or the bits written
    out to the var's width (an all-x vector reads back as 'x')."""
    if isinstance(v, int):
        return str(v)
    s = str(v).lower()
    if s and set(s) <= {"0", "1"}:
        return str(int(s, 2))
    return s * width if len(s) == 1 and width > 1 else s


def cross_check(dump: Path, win: dict) -> None:
    """Every edge row of win equals pyvcd's value just before that edge."""
    ids, ch = pyvcd_values(dump)
    widths = {p: w for p, (_, w) in wavelib.parse_vcd(dump).vars.items()}
    scope = win["scope"]
    for row in win["rows"][:-1]:
        t = int(round(row["t_ns"] * 1000))
        for s in win["signals"]:
            p = f"{scope}.{s}"
            want = as_text(pyvcd_before(ids, ch, p, t), widths[p])
            assert row[s] == want, (row, s)


def test_wraps_one_early_window_shows_254_then_0_and_agrees_with_pyvcd(
        tmp_path):
    dump = tmp_path / "w.vcd"
    write_counter_vcd(dump)
    v = wavelib.parse_vcd(dump)
    scope = wavelib.block_scope(v, ["clk", "rst", "count"])
    assert scope == "tt_frame_x"
    # count holds 254 into the edge at 5150 ns, which wraps it to 0; the
    # test reads 0 at the falling edge after it, 5160 ns
    fail_ps = 5160 * 1000
    win = wavelib.window(v, scope, "clk", ["rst", "count", "dut.u_x.count"],
                         fail_ps, cycles=4)
    counts = [r["count"] for r in win["rows"]]
    assert counts[-3:] == ["253", "254", "0"], wavelib.format_window(win)
    assert [r["dut.u_x.count"] for r in win["rows"]] == counts
    assert win["rows"][-1]["at_failure"] is True
    assert len(win["rows"]) == 5
    cross_check(dump, win)
    text = wavelib.format_window(win)
    assert "<- failure" in text.splitlines()[-1]


def test_window_reports_unknown_bits_as_bits(tmp_path):
    dump = tmp_path / "w.vcd"
    write_counter_vcd(dump, cycles=8)
    v = wavelib.parse_vcd(dump)
    win = wavelib.window(v, "tt_frame_x", "clk", ["count"], 3 * 20000,
                         cycles=8)
    assert win["rows"][0]["count"] == "xxxxxxxx"
    cross_check(dump, win)


def test_keep_stores_only_the_kept_signals(tmp_path):
    dump = tmp_path / "w.vcd"
    write_counter_vcd(dump, cycles=8)
    v = wavelib.parse_vcd(dump, keep=lambda p: p.endswith(".clk"))
    assert len(v.changes) == 1
    assert "tt_frame_x.dut.u_x.count" in v.vars


def test_signature_masks_numbers_so_one_assert_is_one_signature():
    a = wavelib.signature("/w/tb/test_c.py", 35,
                          "expected 255 got 0\nassert 0 == ((254 + 1) % 256)")
    b = wavelib.signature("/other/tb/test_c.py", 35,
                          "expected 7 got 3\nassert 3 == ((6 + 1) % 256)")
    assert a == b == ("test_c.py:35: expected N got N assert N == "
                      "((N + N) % N)")
    assert wavelib.signature("/w/tb/test_c.py", 36, "expected 7 got 3") != a
    # names with digits stay names; hex counts as a number
    assert wavelib.mask_numbers("u_reg2 0xff 1.5e3") == "u_reg2 N N"


def test_assert_location_is_the_innermost_tb_frame(tmp_path):
    tb = tmp_path / "tb"
    tb.mkdir()
    (tb / "test_x.py").write_text("x\n")
    (tb / "helpers.py").write_text("y\n")
    trace = (f'  File "{tb}/test_x.py", line 20, in test_a\n'
             f'  File "{tb}/helpers.py", line 7, in check\n'
             '  File "/usr/lib/cocotb/x.py", line 99, in inner\n')
    assert wavelib.assert_location(trace, tb) == (f"{tb}/helpers.py", 7)
    assert wavelib.assert_location("no frames", tb) == (None, None)


def test_test_reads_is_the_dut_signals_of_that_test_only():
    src = ("async def test_a(dut):\n    x = dut.count.value\n"
           "    dut.rst.value = 1\n\n"
           "@cocotb.test()\nasync def test_b(dut):\n    dut.other.value = 0\n")
    assert wavelib.test_reads(src, "test_a") == ["count", "rst"]
    assert wavelib.test_reads(src, "test_b") == ["other"]


def _failed(test, sig=None):
    v = {"check": "sim", "severity": "error", "file": None, "module": test,
         "kind": "test_failed", "refs": [], "msg": f"{test} failed",
         "source": "cocotb"}
    if sig:
        v["signature"] = sig
    return v


def test_cluster_groups_one_assert_across_tests_by_signature():
    sig = "test_c.py:12: expected N got N"
    vs = [_failed("test_a", sig), _failed("test_b", sig),
          _failed("test_c", sig), _failed("test_d", "test_c.py:40: bad N")]
    cl = cluster_violations.cluster(vs)
    assert sorted(c["count"] for c in cl) == [1, 3]
    big = next(c for c in cl if c["count"] == 3)
    assert big["signature"] == sig and big["module"] is None
    one = next(c for c in cl if c["count"] == 1)
    assert one["module"] == "test_d"
    # no signature: the (file, module, kind) key as before
    assert len(cluster_violations.cluster(
        [_failed("test_a"), _failed("test_b")])) == 2


def test_wave_cli_prints_the_window_and_refuses_unknown_names(
        tmp_path, capsys):
    dump = tmp_path / "w.vcd"
    write_counter_vcd(dump)
    code = wave.main([str(dump), "--signals", "count,dut.u_x.count",
                      "--at", "5160",
                      "--window", "3"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["scope"] == "tt_frame_x"
    assert [r["count"] for r in out["rows"]] == ["252", "253", "254", "0"]
    assert "<- failure" in out["window"]
    code = wave.main([str(dump), "--signals", "nope", "--at", "100"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["status"] == "error" and out["remediation"]

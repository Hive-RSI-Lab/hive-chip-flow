"""Held-out tests for ihsan_sa_pll (spec only, never run by the visible
suite): mid-run reset pulse, live n_sel changes through illegal codes, a
wide pulse on the 15th clean cycle, DN-side wide pulses, PFD extremes,
live obs_sel at 3.0 ns vco_out, back-to-back control register changes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tb"))

import cocotb  # noqa: E402
from cocotb.triggers import Timer  # noqa: E402

from pll_ref import (REF_T, LockModel, Monitor, decode_n, now,  # noqa: E402
                     ref_clock, vco_clock, wait_until)
import test_pll as v  # noqa: E402  (helpers only; its tests are not re-collected here)


# req: REQ-DIV-RESET-CLEAN REQ-LOCK-ASSERT
@cocotb.test()
async def test_midrun_reset_pulse(dut):
    fb, rises, _ = await v.pfd_setup(dut, [-10.0] * 60)
    mfb, mobs = Monitor(dut.clk_fb), Monitor(dut.obs_out)
    await wait_until(rises[25] + 20)
    assert int(dut.lock.value) == 1, "lock not high before the reset pulse"
    t_rst = rises[25] + 23.7
    await wait_until(t_rst)
    dut.rst_n.value = 0
    await Timer(1, unit="ns")
    assert int(dut.lock.value) == 0 and int(dut.clk_fb.value) == 0, "1 ns reset pulse not async"
    dut.rst_n.value = 1
    for i in range(26, 40):
        await wait_until(rises[i] + REF_T / 2 - 0.1)
        assert int(dut.lock.value) == 0, f"lock back {i - 25} cycles after mid-run reset"
    await wait_until(rises[59])
    r = mfb.rises(t_rst)
    assert len(r) >= 3 and abs(r[1] - r[0] - 80.0) <= 0.01, f"first clk_fb period after reset: {r[:3]}"
    for m in (mfb, mobs):
        for s, w in m.pulses(t_rst - 1):
            assert w >= v.TV - 0.01, f"runt pulse {w} ns at {s} after mid-run reset"
    assert int(dut.lock.value) == 1, "lock did not reacquire after the reset pulse"


# req: REQ-DIV-ILLEGAL REQ-DIV-N
@cocotb.test()
async def test_live_nsel_changes(dut):
    await v.start_vco(dut, n_sel=5)
    m = Monitor(dut.clk_fb)
    prev = 5
    for code in (5, 0, 4, 7, 2, 6, 3, 1):
        dut.n_sel.value = code
        n = decode_n(code)
        t0 = now()
        # skip two periods of the old or new N (whichever is longer), then check
        settle = 2 * 8 * max(n, prev) * v.TV
        await Timer(int(settle + 6 * 8 * n * v.TV + 400), unit="ns")
        r = m.rises(t0 + settle)
        prev = n
        assert len(r) >= 2, f"n_sel={code}: clk_fb stalled after live change"
        v.check_period(f"live n_sel={code}", r, 8 * n * v.TV)


# req: REQ-LOCK-DEASSERT REQ-LOCK-ASSERT
@cocotb.test()
async def test_wide_on_fifteenth_clean_cycle(dut):
    offs = [-10.0] * 20 + [-60.0] + [-10.0] * 15 + [-60.0] + [-10.0] * 20
    seen = await v.lock_run(dut, offs)
    assert seen[20][1] == 1 and seen[36][1] == 1, "wide cycles not wide"
    assert all(l == 0 for l, _ in seen[21:37 + 15]), "lock asserted inside a broken 16-cycle window"
    assert seen[37 + 16][0] == 1, "lock not reasserted 16 clean cycles after the second wide"


# req: REQ-LOCK-DEASSERT REQ-PFD-FREQ
@cocotb.test()
async def test_dn_side_wide_drops_lock(dut):
    # lock on 10 ns UP pulses, then clk_fb runs fast (vco 7 ns -> 56 ns) so
    # DN is still high at clk falling edges
    await v.init(dut, n_sel=1)
    state = {}
    cocotb.start_soon(vco_clock(dut, v.TV, stop=state))
    await Timer(30, unit="ns")
    await v.release(dut)
    m = Monitor(dut.clk_fb)
    while not m.rises():
        await Timer(1, unit="ns")
    fb0 = m.rises()[0] + 2 * REF_T
    rises = [fb0 - 10 + k * REF_T for k in range(60)]
    # priming clk edge clears the DN pending since clk_fb ran alone (as v.pfd_setup)
    cocotb.start_soon(ref_clock(dut, [fb0 - REF_T + 1.0] + rises))
    await wait_until(rises[25] + 30)
    assert int(dut.lock.value) == 1, "no lock with 10 ns UP pulses"
    state["stop"] = True
    await Timer(10, unit="ns")
    cocotb.start_soon(vco_clock(dut, 7.0))
    md = Monitor(dut.pfd_dn)
    wide_seen = False
    for i in range(27, 59):
        await wait_until(rises[i] + REF_T / 2 - 0.1)
        if int(dut.pfd_dn.value):
            wide_seen = True
            await wait_until(rises[i + 1] + REF_T / 2 - 0.1)
            assert int(dut.lock.value) == 0, f"DN wide at cycle {i} did not drop lock"
            break
    assert wide_seen, "fast clk_fb never produced a DN pulse high at the clk falling edge"
    assert md.high_time(rises[28], rises[48]) > 0


# req: REQ-PFD-UP-LEAD REQ-PFD-DN-LEAD
@cocotb.test()
async def test_pfd_extreme_offsets(dut):
    for off, sig, w in ((-1.0, "pfd_up", 1.0), (-70.0, "pfd_up", 70.0), (1.0, "pfd_dn", 1.0)):
        mu, md = Monitor(dut.pfd_up), Monitor(dut.pfd_dn)
        fb, rises, _ = await v.pfd_setup(dut, [off] * 10)
        await wait_until(fb[-1] + 50)
        lead = mu if sig == "pfd_up" else md
        other = md if sig == "pfd_up" else mu
        ps = lead.pulses(fb[3] - 75, fb[-1])
        assert len(ps) >= 5, f"off={off}: {sig} pulsed {len(ps)} times"
        for s, width in ps:
            assert abs(width - w) <= 0.01, f"off={off}: {sig} width {width}, expected {w}"
        assert all(x <= 0.5 for _, x in other.pulses(fb[3] - 75, fb[-1])), f"off={off}: other side pulsed"
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


# req: REQ-OBS-SEL REQ-PRE-DIV8
@cocotb.test()
async def test_obs_sel_live_fast_vco(dut):
    tv = 3.0
    await v.start_vco(dut, tv=tv, n_sel=1, obs_sel=0)
    m = Monitor(dut.obs_out)
    for sel, div in ((0, 8), (1, 16), (0, 8), (1, 16)):
        dut.obs_sel.value = sel
        t0 = now()
        await Timer(int(12 * 16 * tv), unit="ns")
        v.check_period(f"obs_sel={sel} at 3.0 ns", m.rises(t0 + 2 * 16 * tv), div * tv)


# req: REQ-CTRL-REG
@cocotb.test()
async def test_ctrl_regs_every_cycle(dut):
    await v.init(dut)
    await v.release(dut)
    t0 = now() + 5
    rises = [t0 + k * REF_T for k in range(20)]
    cocotb.start_soon(ref_clock(dut, rises))
    seq = [(b >> 2, b & 3) for b in (7, 0, 5, 2, 6, 1, 3, 4, 7, 7, 0)]
    for i, (en, trim) in enumerate(seq):
        await wait_until(rises[i] + REF_T - 2)  # 2 ns before the next edge
        dut.pll_en_in.value = en
        dut.cp_trim_in.value = trim
        await wait_until(rises[i + 1] + 1)
        got = (int(dut.pll_en.value), int(dut.cp_trim1.value) * 2 + int(dut.cp_trim0.value))
        assert got == (en, trim), f"step {i}: {got}, expected {(en, trim)}"

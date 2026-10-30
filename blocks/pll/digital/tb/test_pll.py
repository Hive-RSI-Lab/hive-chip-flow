"""Visible tests for ihsan_sa_pll, written from spec/spec.md + spec.yaml
before any RTL. All drive/sample is Timer-anchored to bench-scheduled edge
times (clk and vco_out are generated here, never Clock-triggered), and
edge timing is recorded with ValueChange monitors (pll_ref.Monitor)."""
import cocotb
from cocotb.triggers import Timer

from pll_ref import (REF_T, LockModel, Monitor, decode_n, now, ref_clock,
                     vco_clock, wait_until)

TV = 10.0  # vco_out period for open-loop tests: clk_pre = 80 ns
# glsim's SDF pass runs these tests on the hardened netlist, where every
# port-observed edge carries cell and clock-tree delay and a PFD pulse
# carries the reset delay. Both slacks stay under the smallest step an RTL
# fault moves an edge by (a 5 ns vco half period, the 5 ns minimum dt).
GL_TOL = 1.0   # two port-observed edges or widths that are equal at RTL
GL_SKEW = 4.0  # PFD: port-to-PFD clock skew plus the reset delay


async def init(dut, n_sel=1, obs_sel=0):
    dut.rst_n.value = 0
    dut.clk.value = 0
    dut.vco_out.value = 0
    dut.n_sel.value = n_sel
    dut.obs_sel.value = obs_sel
    dut.pll_en_in.value = 0
    dut.cp_trim_in.value = 0
    await Timer(50, unit="ns")


async def release(dut):
    dut.rst_n.value = 1
    await Timer(1, unit="ns")


_vco_task = None


def run_vco(dut, tv):
    """Start vco_out at period tv, stopping any vco_out driver a previous
    loop iteration started (two drivers on vco_out corrupt its period)."""
    global _vco_task
    if _vco_task is not None and not _vco_task.done():
        _vco_task.cancel()
    _vco_task = cocotb.start_soon(vco_clock(dut, tv))


async def start_vco(dut, tv=TV, n_sel=1, obs_sel=0):
    await init(dut, n_sel, obs_sel)
    run_vco(dut, tv)
    await Timer(3 * tv, unit="ns")  # vco already running at release
    await release(dut)


def periods(ts):
    return [b - a for a, b in zip(ts, ts[1:])]


def check_period(name, ts, want, tol=0.01):
    ps = periods(ts)
    assert ps, f"{name}: no full periods observed"
    for i, p in enumerate(ps):
        assert abs(p - want) <= tol, (
            f"{name}: period {i} = {p:.3f} ns, expected {want:.3f} ns")


# req: REQ-PRE-DIV8
@cocotb.test()
async def test_prescaler_div8(dut):
    for tv in (TV, 3.0):
        await start_vco(dut, tv=tv, n_sel=1, obs_sel=0)
        m_obs = Monitor(dut.obs_out)
        m_vco = Monitor(dut.vco_out)
        t0 = now()
        await Timer(int(40 * 8 * tv), unit="ns")
        r = m_obs.rises(t0 + 8 * tv)
        assert len(r) >= 30, f"tv={tv}: obs_out (clk_pre) rose {len(r)} times"
        check_period(f"clk_pre tv={tv}", r, 8 * tv)
        # exactly 8 vco rises between consecutive clk_pre rises
        vr = m_vco.rises()
        for a, b in zip(r, r[1:]):
            k = sum(1 for t in vr if a < t <= b)
            assert k == 8, f"tv={tv}: {k} vco_out rises per clk_pre period, expected 8"
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


# req: REQ-OBS-SEL
@cocotb.test()
async def test_obs_sel(dut):
    for sel, div in ((0, 8), (1, 16)):
        await start_vco(dut, n_sel=1, obs_sel=sel)
        m = Monitor(dut.obs_out)
        t0 = now()
        await Timer(int(30 * div * TV), unit="ns")
        r = m.rises(t0 + div * TV)
        check_period(f"obs_out obs_sel={sel}", r, div * TV)
        for s, w in m.pulses(t0 + div * TV):
            assert abs(w - div * TV / 2) <= GL_TOL, (
                f"obs_sel={sel}: high time {w} ns, expected {div * TV / 2} (50% duty)")
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


async def check_ratio(dut, code):
    n = decode_n(code)
    await start_vco(dut, n_sel=code)
    m_fb = Monitor(dut.clk_fb)
    m_pre = Monitor(dut.obs_out)
    t0 = now()
    await Timer(int(12 * 8 * n * TV), unit="ns")
    r = m_fb.rises(t0 + 8 * n * TV)
    assert len(r) >= 8, f"n_sel={code}: clk_fb rose only {len(r)} times (stalled?)"
    check_period(f"clk_fb n_sel={code}", r, 8 * n * TV)
    pre = m_pre.rises()
    for a, b in zip(r, r[1:]):
        k = sum(1 for t in pre if a < t <= b)
        assert k == n, f"n_sel={code}: {k} clk_pre rises per clk_fb period, expected {n}"
    for t in r:
        assert any(abs(t - p) <= GL_TOL for p in pre), (
            f"n_sel={code}: clk_fb rise at {t} not aligned to a clk_pre rise")
    if n == 1:
        # pure pass-through: identical waveform to clk_pre
        a, b = m_fb.pulses(t0 + 8 * TV), m_pre.pulses(t0 + 8 * TV)
        n_cmp = min(len(a), len(b))
        assert n_cmp >= 8 and all(
            abs(x[0] - y[0]) <= GL_TOL and abs(x[1] - y[1]) <= GL_TOL
            for x, y in zip(a[:n_cmp], b[:n_cmp])), (
            f"n_sel={code}: clk_fb {a[:3]} is not clk_pre {b[:3]} passed through")
    for s, w in m_fb.pulses(t0):
        assert abs(w - 4 * TV) <= GL_TOL, (
            f"n_sel={code}: clk_fb high time {w}, expected one clk_pre high time {4 * TV}")
    dut.rst_n.value = 0
    await Timer(20, unit="ns")


# req: REQ-DIV-N
@cocotb.test()
async def test_divider_ratio(dut):
    for code in (1, 2, 3, 4, 5):
        await check_ratio(dut, code)


# req: REQ-DIV-ILLEGAL
@cocotb.test()
async def test_divider_illegal_codes(dut):
    for code in (0, 6, 7):
        await check_ratio(dut, code)


# req: REQ-DIV-RESET-CLEAN
@cocotb.test()
async def test_reset_release_clean(dut):
    for code in (3, 5):
        n = decode_n(code)
        await init(dut, n_sel=code)
        run_vco(dut, TV)
        await Timer(int(7.3 * TV * 1000), unit="ps")  # release mid vco phase
        mons = {s: Monitor(getattr(dut, s)) for s in ("clk_fb", "obs_out")}
        await release(dut)
        await Timer(int(4 * 8 * n * TV), unit="ns")
        r = mons["clk_fb"].rises()
        assert len(r) >= 3, f"N={n}: clk_fb rose {len(r)} times after reset"
        assert abs(r[1] - r[0] - 8 * n * TV) <= 0.01, (
            f"N={n}: first full clk_fb period {r[1] - r[0]} ns, expected {8 * n * TV}")
        for name, m in mons.items():
            for s, w in m.pulses():
                assert w >= TV - 0.01, f"N={n}: {name} runt pulse {w} ns at {s}"
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


async def pfd_setup(dut, offsets):
    """N=1, vco 10 ns -> clk_fb period 80 ns. clk rising edges are placed
    at clk_fb_rise + off (off < 0: reference leads). clk_fb runs before
    clk starts, so pfd_dn is pending from the first clk_fb edge; one priming
    clk edge 1 ns after the clk_fb edge before fb[0] clears it, so the PFD
    starts the measured cycles from its idle state. Returns (fb_rises,
    ref_rises, prime) - ref_rises excludes the priming edge at `prime`."""
    await start_vco(dut, n_sel=1)
    m = Monitor(dut.clk_fb)
    t_give_up = now() + 4 * 8 * TV  # a few clk_fb periods: a stalled divider fails, not hangs
    while not m.rises():
        assert now() < t_give_up, "clk_fb never rose (stalled prescaler or divider)"
        await Timer(1, unit="ns")
    fb0 = m.rises()[0] + 2 * REF_T
    fb = [fb0 + k * REF_T for k in range(len(offsets))]
    rises = [f + o for f, o in zip(fb, offsets)]
    prime = fb0 - REF_T + 1.0
    cocotb.start_soon(ref_clock(dut, [prime] + rises))
    return fb, rises, prime


async def lead_test(dut, lead_sig, other_sig, ref_leads):
    # width tracks dt: dt plus one offset c shared by every dt (0 at RTL;
    # skew plus reset delay at gate level)
    c = None
    for dt in (5.0, 15.0, 30.0):
        offs = [(-dt if ref_leads else dt)] * 12
        mu, md = Monitor(dut.pfd_up), Monitor(dut.pfd_dn)
        fb, rises, _ = await pfd_setup(dut, offs)
        await wait_until(fb[-1] + 50)
        mon = {"pfd_up": mu, "pfd_dn": md}
        lead, other = mon[lead_sig], mon[other_sig]
        t0 = min(fb[2], rises[2]) - 1
        lp = lead.pulses(t0, fb[-1])
        assert len(lp) >= 8, f"dt={dt}: {lead_sig} pulsed {len(lp)} times"
        if c is None:
            c = lp[0][1] - dt
            assert abs(c) <= GL_SKEW, f"dt={dt}: {lead_sig} width {lp[0][1]}, expected {dt}"
        for s, w in lp:
            assert abs(w - dt - c) <= GL_TOL, (
                f"dt={dt}: {lead_sig} width {w}, expected {dt} + {c:.3f} (tracks dt)")
            start = rises if ref_leads else fb
            assert any(abs(s - x) <= GL_SKEW for x in start), (
                f"dt={dt}: {lead_sig} pulse at {s} not started by the leading edge")
        for s, w in other.pulses(t0, fb[-1]):
            assert w <= GL_SKEW, f"dt={dt}: {other_sig} pulse {w} ns, expected reset-width only"
        # REQ-PFD-RESET: both cleared right after the lagging edge
        ends = fb if ref_leads else rises
        for e in ends[3:-1]:
            await wait_until(e + GL_SKEW + 0.5)
            assert int(dut.pfd_up.value) == 0 and int(dut.pfd_dn.value) == 0, (
                f"dt={dt}: PFD not reset after lagging edge at {e}: "
                f"up={dut.pfd_up.value} dn={dut.pfd_dn.value}")
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


# req: REQ-PFD-UP-LEAD REQ-PFD-RESET
@cocotb.test()
async def test_pfd_up_lead(dut):
    await lead_test(dut, "pfd_up", "pfd_dn", ref_leads=True)


# req: REQ-PFD-DN-LEAD REQ-PFD-RESET
@cocotb.test()
async def test_pfd_dn_lead(dut):
    await lead_test(dut, "pfd_dn", "pfd_up", ref_leads=False)


# req: REQ-PFD-FREQ
@cocotb.test()
async def test_pfd_frequency(dut):
    for tv, want in ((12.0, "up"), (8.0, "dn")):  # clk_fb 96 ns / 64 ns
        await start_vco(dut, tv=tv, n_sel=1)
        mu, md = Monitor(dut.pfd_up), Monitor(dut.pfd_dn)
        t0 = now() + 5
        rises = [t0 + k * REF_T for k in range(22)]
        cocotb.start_soon(ref_clock(dut, rises))
        await wait_until(rises[21])
        up, dn = mu.high_time(rises[1], rises[21]), md.high_time(rises[1], rises[21])
        if want == "up":
            assert up > dn, f"clk_fb slower: UP {up} ns should exceed DN {dn} ns"
        else:
            assert dn > up, f"clk_fb faster: DN {dn} ns should exceed UP {up} ns"
        dut.rst_n.value = 0
        await Timer(20, unit="ns")


async def lock_run(dut, offsets, first_window_slack=True):
    """Cycle-by-cycle check of lock against LockModel. Sample 0.1 ns before
    each clk falling edge (pfd outputs and lock are stable there). The
    priming clk edge is a real reference cycle, so it is checked and fed to
    the model too; seen[] is indexed by ref_rises (priming entry dropped).
    Returns (lock, wide) per cycle; lock_run_updn() gives up/dn separately."""
    trace = await lock_run_updn(dut, offsets, first_window_slack)
    return [(l, u | d) for l, u, d in trace]


async def lock_run_updn(dut, offsets, first_window_slack=True):
    """Same checks as lock_run(), but returns (lock, pfd_up, pfd_dn) per
    reference cycle, so a test can tell a wide UP from a wide DN sample."""
    fb, ref_rises, prime = await pfd_setup(dut, offsets)
    rises = [prime] + ref_rises
    model = LockModel()
    first = first_window_slack
    seen = []
    for i, r in enumerate(rises):
        gap = rises[i + 1] - r if i + 1 < len(rises) else REF_T
        await wait_until(r + min(REF_T / 2, gap / 2) - 0.1)
        lock = int(dut.lock.value)
        wide = int(dut.pfd_up.value) | int(dut.pfd_dn.value)
        exp = model.expected()
        if lock and not exp:
            # reset value of the wide sampler may count as one clean cycle
            assert first and model.clean == 15, (
                f"cycle {i}: lock high after only {model.clean} clean cycles")
        if exp:
            assert lock == 1, f"cycle {i}: lock low after {model.clean} clean cycles"
        if wide:
            first = False
            assert lock == 0 or model.clean >= 16, f"cycle {i}: lock high in wide cycle"
        seen.append((lock, int(dut.pfd_up.value), int(dut.pfd_dn.value)))
        model.sample(wide)
    return seen[1:]


# req: REQ-LOCK-ASSERT
@cocotb.test()
async def test_lock_assert(dut):
    seen = await lock_run(dut, [-10.0] * 30)  # 10 ns UP pulses: never wide
    assert not any(w for _, w in seen), "10 ns UP pulse was judged wide"
    assert seen[-1][0] == 1, "lock never asserted after 30 clean cycles"
    assert sum(l for l, _ in seen[:15]) == 0, "lock asserted within 15 cycles"


# req: REQ-LOCK-DEASSERT
@cocotb.test()
async def test_lock_deassert(dut):
    offs = [-10.0] * 24 + [-60.0] + [-10.0] * 24  # one 60 ns (wide) UP pulse
    seen = await lock_run(dut, offs)
    assert seen[24][1] == 1, "60 ns UP pulse not visible at clk falling edge"
    assert seen[23][0] == 1, "lock not asserted before the wide cycle"
    assert seen[25][0] == 0, "lock did not drop on the reference edge after the wide pulse"
    assert seen[25 + 15][0] == 0, "lock reasserted before 16 clean cycles"
    assert seen[25 + 16][0] == 1, "lock did not reassert after 16 clean cycles"


# req: REQ-LOCK-DEASSERT REQ-LOCK-ASSERT
@cocotb.test()
async def test_lock_deassert_dn(dut):
    """Spec: pfd_up OR pfd_dn still high at the clk falling edge is a wide
    cycle. Two cycles with the reference lagging clk_fb by 50 ns put the
    clk_fb edge between a clk rise and its fall (reference period 80 ns,
    high time 40 ns), so pfd_dn alone is high at that one falling edge."""
    offs = [-10.0] * 24 + [50.0, 50.0] + [-10.0] * 24
    seen = await lock_run_updn(dut, offs)
    lk, up, dn = seen[24]
    assert dn == 1 and up == 0, (
        f"cycle 24: expected a DN-only wide sample, got pfd_up={up} pfd_dn={dn}")
    wides = [i for i, (_, u, d) in enumerate(seen) if u or d]
    assert wides == [24], f"wide samples at cycles {wides}, expected only [24]"
    assert seen[23][0] == 1, "lock not asserted before the wide DN cycle"
    assert seen[25][0] == 0, (
        "lock did not drop on the reference edge after a wide pfd_dn pulse")
    for k in range(25, 25 + 16):
        assert seen[k][0] == 0, (
            f"cycle {k}: lock high only {k - 25} clean cycles after the wide DN pulse")
    assert seen[25 + 16][0] == 1, "lock did not reassert after 16 clean cycles"
    assert seen[-1][0] == 1, "lock not held at the end of the clean run"


# req: REQ-CTRL-REG
@cocotb.test()
async def test_ctrl_regs(dut):
    await init(dut)
    await release(dut)
    t0 = now() + 5
    rises = [t0 + k * REF_T for k in range(12)]
    cocotb.start_soon(ref_clock(dut, rises))
    pattern = [(1, 0), (1, 1), (0, 2), (0, 3), (1, 3), (1, 3), (0, 0)]
    prev = (0, 0)
    for i, (en, trim) in enumerate(pattern):
        await wait_until(rises[i] + REF_T / 2 + 5)  # change mid-cycle
        dut.pll_en_in.value = en
        dut.cp_trim_in.value = trim
        await Timer(1, unit="ns")
        got = (int(dut.pll_en.value), int(dut.cp_trim1.value) * 2 + int(dut.cp_trim0.value))
        assert got == prev, f"step {i}: outputs changed before clk edge: {got} vs held {prev}"
        await wait_until(rises[i + 1] + 10)  # past clk-to-output delay
        got = (int(dut.pll_en.value), int(dut.cp_trim1.value) * 2 + int(dut.cp_trim0.value))
        assert got == (en, trim), f"step {i}: (pll_en, trim) = {got}, expected {(en, trim)}"
        prev = got


# req: REQ-RST-ASYNC
@cocotb.test()
async def test_async_reset_outputs(dut):
    fb, rises, _ = await pfd_setup(dut, [-60.0] * 3 + [-10.0] * 20)
    dut.pll_en_in.value = 1
    dut.cp_trim_in.value = 3
    await wait_until(fb[-1] - 3)
    dut.obs_sel.value = 0
    dut.rst_n.value = 0
    # past the reset-to-output delay: at gate level rst_n reaches the PFD
    # flops through the resizer's buffers and pfd_up/pfd_dn through the
    # recovery override, several ns at the nominal corner
    await Timer(20, unit="ns")
    for s in ("pfd_up", "pfd_dn", "lock", "clk_fb", "obs_out", "pll_en", "cp_trim0", "cp_trim1"):
        assert int(getattr(dut, s).value) == 0, f"{s}={getattr(dut, s).value} during rst_n low"


def walk(h, depth=0):
    yield h
    if depth > 6:
        return
    try:
        kids = list(h)
    except TypeError:
        return
    for k in kids:
        yield from walk(k, depth + 1)


# req: REQ-TT-TIEOFF
@cocotb.test()
async def test_tt_tieoff(dut):
    await start_vco(dut, n_sel=2)
    for name in ("uio_oe", "uio_out"):
        if hasattr(dut, name):
            assert int(getattr(dut, name).value) == 0, f"{name} = {getattr(dut, name).value}"
    if hasattr(dut, "ui_in") and hasattr(dut, "uio_in"):
        m = Monitor(dut.clk_fb)
        await Timer(2000, unit="ns")
        base = periods(m.rises(now() - 1500))
        dut.ui_in.value = int(dut.ui_in.value) ^ 0x30
        dut.uio_in.value = int(dut.uio_in.value) ^ 0xFC
        t1 = now()
        await Timer(2000, unit="ns")
        after = periods(m.rises(t1 + 200))
        assert after and all(abs(p - base[0]) < 0.01 for p in base + after), (
            f"spare ui_in[5:4]/uio_in[7:2] changed clk_fb: {base} -> {after}")
    else:
        # core level: the spare pins must not exist as core ports at all
        for name in ("ui_in", "uio_in", "uio_out", "uio_oe"):
            assert not hasattr(dut, name), f"core exposes TT port {name}"


# req: REQ-NO-UA
@cocotb.test()
async def test_no_vctrl_bias_ua(dut):
    await init(dut)
    bad = []
    for h in walk(dut):
        nm = h._name.lower().split(".")[-1]
        if nm in ("vctrl", "bias_ref", "ua") or nm.startswith("ua["):
            bad.append(h._path)
    assert not bad, f"digital side carries forbidden analog names: {bad}"


# --- runaway recovery ---------------------------------------------------
W_RECOVER = 1024  # reference cycles unlocked (pll_en high) before recovery
M_RECOVER = 128   # reference cycles of forced DN


async def recover_run(dut, offsets, en_events=()):
    """pfd_setup's reference/feedback pattern with pll_en_in raised before
    the priming edge, so pll_en is 1 from edge E[0] (E = [prime] + the
    measured rises). en_events: (k, value) sets pll_en_in mid-cycle after
    E[k]. Returns (E, recoveries, mon_up, mon_lock); a recovery is a
    pfd_dn high interval longer than four reference periods, as (start, width)."""
    fb, rises, prime = await pfd_setup(dut, offsets)
    dut.pll_en_in.value = 1
    E = [prime] + rises
    md, mu, ml = Monitor(dut.pfd_dn), Monitor(dut.pfd_up), Monitor(dut.lock)

    async def drive_en():
        for k, v in en_events:
            await wait_until(E[k] + REF_T / 2)
            dut.pll_en_in.value = v

    cocotb.start_soon(drive_en())
    await wait_until(E[-1] + REF_T / 2)
    # longer than any PFD pulse: the DN that pfd_setup leaves pending until
    # its priming edge lasts about one reference period
    recs = [(s, w) for s, w in md.pulses() if w > 4 * REF_T]
    dut.rst_n.value = 0
    await Timer(20, unit="ns")
    return E, recs, mu, ml


def edge_index(E, t):
    """Index of the reference edge a port change at t follows (clk-to-q and
    gate delay are under GL_SKEW)."""
    k = min(range(len(E)), key=lambda i: abs(E[i] - t))
    assert abs(E[k] - t) <= GL_SKEW, f"change at {t} ns is on no reference edge"
    return k


# req: REQ-RECOVER REQ-RECOVER-OUT
@cocotb.test()
async def test_recover_unlocked(dut):
    """Feedback never close enough to lock (60 ns UP pulses every cycle):
    recovery starts on edge E[W], holds pfd_dn high and pfd_up low for
    exactly M reference periods, then the count restarts from 0."""
    n = 2 * W_RECOVER + 2 * M_RECOVER + 30
    E, recs, mu, _ = await recover_run(dut, [-60.0] * n)
    assert len(recs) == 2, f"expected two recoveries in {n} cycles, got {recs}"
    first = W_RECOVER
    second = W_RECOVER + M_RECOVER + W_RECOVER
    for (s, w), k in zip(recs, (first, second)):
        assert edge_index(E, s) == k, (
            f"recovery started on edge {edge_index(E, s)}, expected {k}")
        assert abs(w - M_RECOVER * REF_T) <= GL_TOL, (
            f"recovery lasted {w} ns, expected {M_RECOVER} reference periods")
        assert mu.high_time(s + GL_SKEW, s + w) == 0, (
            "pfd_up pulsed during recovery")
    # outside recovery the PFD is unchanged: wide UP pulses every cycle
    before = mu.pulses(E[first - 10], E[first - 1])
    assert len(before) >= 8 and all(abs(w - 60.0) <= GL_SKEW for _, w in before), (
        f"pfd_up before recovery: {before[:3]}")
    after = mu.pulses(E[first + M_RECOVER] + GL_SKEW, E[first + M_RECOVER + 10])
    assert len(after) >= 8, "pfd_up did not pulse again after recovery ended"


# req: REQ-RECOVER
@cocotb.test()
async def test_recover_lock_clears(dut):
    """Lock at any reference edge clears the count: a short locked stretch
    before W unlocked cycles postpones recovery to W edges after lock falls."""
    offs = [-60.0] * 1000 + [-10.0] * 24 + [-60.0] * (W_RECOVER + M_RECOVER + 40)
    E, recs, _, ml = await recover_run(dut, offs)
    falls = [t for t, v in ml.ev[1:] if v == 0]
    assert ml.rises() and falls, "lock never rose and fell"
    j = edge_index(E, falls[-1])
    assert len(recs) == 1, f"expected one recovery, got {recs}"
    k = edge_index(E, recs[0][0])
    assert k == j + W_RECOVER, (
        f"recovery on edge {k}, expected {j + W_RECOVER} (W after lock fell on {j})")


# req: REQ-RECOVER
@cocotb.test()
async def test_recover_pll_en(dut):
    """pll_en low clears the count and ends a recovery in progress; the
    count restarts from the edge pll_en is seen high again."""
    back = 611                       # pll_en is 1 again from E[611]
    start = back + W_RECOVER
    stop = start + 20                # pll_en low from E[start + 21]
    offs = [-60.0] * (start + 60)
    E, recs, _, _ = await recover_run(
        dut, offs, en_events=[(600, 0), (back - 1, 1), (stop, 0)])
    assert len(recs) == 1, f"expected one recovery, got {recs}"
    s, w = recs[0]
    assert edge_index(E, s) == start, (
        f"recovery on edge {edge_index(E, s)}, expected {start}")
    assert edge_index(E, s + w) == stop + 2, (
        f"recovery ended on edge {edge_index(E, s + w)}, expected {stop + 2}")

"""Shared bench helpers and reference models for ihsan_sa_pll (spec only).
Not a test module. Times are ns floats from get_sim_time."""
import cocotb
from cocotb.triggers import Timer, ValueChange
from cocotb.utils import get_sim_time

REF_T = 80.0  # clk period (12.5 MHz)


def now():
    return float(get_sim_time(unit="ps")) / 1000.0


async def wait_until(t_ns):
    dt = int(round((t_ns - now()) * 1000))
    if dt > 0:
        await Timer(dt, unit="ps")


class Monitor:
    """Timestamps every value change of a 1-bit signal."""
    def __init__(self, sig):
        self.sig = sig
        self.ev = [(now(), int(sig.value))]
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await ValueChange(self.sig)
            self.ev.append((now(), int(self.sig.value)))

    def rises(self, t0=0.0, t1=1e18):
        out, prev = [], None
        for t, v in self.ev:
            if v == 1 and prev == 0 and t0 <= t < t1:
                out.append(t)
            prev = v
        return out

    def pulses(self, t0=0.0, t1=1e18):
        """(start, width) of every high interval starting in [t0, t1)."""
        out, start = [], None
        for t, v in self.ev:
            if v == 1 and start is None:
                start = t
            elif v == 0 and start is not None:
                if t0 <= start < t1:
                    out.append((start, t - start))
                start = None
        return out

    def high_time(self, t0, t1):
        tot, start = 0.0, None
        for t, v in self.ev:
            if v == 1 and start is None:
                start = t
            elif v == 0 and start is not None:
                tot += max(0.0, min(t, t1) - max(start, t0))
                start = None
        if start is not None:
            tot += max(0.0, t1 - max(start, t0))
        return tot


async def vco_clock(dut, period_ns, stop=None):
    """Free-running vco_out, exact period (ps rounding with no drift)."""
    t = now()
    v = 0
    while stop is None or not stop.get("stop"):
        t += period_ns / 2
        await wait_until(t)
        v ^= 1
        dut.vco_out.value = v


async def ref_clock(dut, rises):
    """clk with rising edges at the given absolute times; high time is
    min(REF_T/2, gap/2) so arbitrary per-cycle phase offsets are legal."""
    for i, r in enumerate(rises):
        await wait_until(r)
        dut.clk.value = 1
        gap = rises[i + 1] - r if i + 1 < len(rises) else REF_T
        await wait_until(r + min(REF_T / 2, gap / 2))
        dut.clk.value = 0


def decode_n(code):
    """Spec: n_sel 1..5 -> N, 0/6/7 -> 1."""
    return code if 1 <= code <= 5 else 1


class LockModel:
    """Lock detector reference: fed the (pfd_up|pfd_dn) value seen at each
    clk falling edge; expected lock after the next clk rising edge is
    (consecutive clean samples >= 16). Any wide sample restarts the count."""
    K = 16

    def __init__(self):
        self.clean = 0

    def sample(self, wide):
        self.clean = 0 if wide else self.clean + 1

    def expected(self):
        return self.clean >= self.K

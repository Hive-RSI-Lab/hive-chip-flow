"""convlib.py - data-converter metrics (issue #126), numpy only, so a bench,
a corpus test and a lab script all compute the same numbers the same way.

    coherent_tone(fs, n, fin_target)      -> {fin, bin, fs, n}
    dynamic(samples, fs, fin, nbits)      -> sndr/snr/sfdr/thd/enob (dB, bits)
    static_ramp(codes_or_volts, nbits)    -> dnl/inl, missing codes, monotonic
    static_histogram(codes, nbits, sine)  -> the same, from a code histogram

Refuses rather than guesses: Refused carries a `kind`, and a number that
cannot be trusted is never returned.

Dynamic. A rectangular window over a whole number of periods (IEEE 1241
4.1.4), so all of the tone sits in one bin and everything else is noise or
distortion. `not_coherent` is judged twice: from the arguments (fin must land
on a bin) and from the data (the peak must be that bin, and the bins past +-1
must not ALL stand above the floor - a leakage skirt lifts every one of them
on both sides, a spur lifts one). `bin_not_coprime` refuses a bin sharing a factor with n: the
record repeats the same few phases, and the quantization noise it reports is
not the converter's.

Static. dnl[k] and inl[k] are indexed by code; a value a method cannot define
is nan, never 0 (the *_max_abs values skip it).
  Levels (float input, one level per code, e.g. a DAC sweep): dnl[k] is the
  step from code k-1 up to code k; inl[k] is level k's distance from the line.
  Codes (integer input, or a histogram): the levels are the code transitions,
  so level k is the edge between code k-1 and code k; dnl[k] is code k's width
  and inl[k] the error of its lower edge. Codes 0 and 2**nbits-1 have no outer
  edge, so their width is undefined (an overdriven input piles up there).
Both lines are in the result: `endpoint` (through the first and last level,
which is the LSB most DAC specs use) and `best_fit` (least squares).
"""
from __future__ import annotations

import math

import numpy as np

# harmonics counted as distortion: h2..h6, the usual "first five"
DEFAULT_HARMONICS = 5
# fin's distance from a bin, in bins. Leakage into the next bin is about
# -20log10(pi*d) dBc per side, so 1e-6 is -110 dBc: under any floor here.
BIN_TOL = 1e-6
# Skirt test: EVERY bin at +-2..+-4 against the median of the rest. Leakage
# falls smoothly as 1/m^2 and lifts all of them; the spurs of a coarse
# quantizer are sparse and tall, so a mean over those bins would mistake one
# spur for a skirt. 30x the median is about 20x the mean of a noise-like
# floor, e^-20 per bin by chance. Below SKIRT_MIN_DBC it does not matter
# (numerical noise on a synthetic tone has no meaningful floor).
SKIRT_BINS = (2, 3, 4)
SKIRT_RATIO = 30.0
SKIRT_MIN_DBC = -200.0
# a step this close to no step at all is a code that did not move
MISSING_EPS = 1e-9


class Refused(ValueError):
    """The input cannot give a number that means what its name says. `kind`
    is stable (a caller or test can switch on it); str() says what to do."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def _is_prime(k: int) -> bool:
    return k >= 2 and all(k % d for d in range(2, math.isqrt(k) + 1))


def coherent_tone(fs: float, n: int, fin_target: float) -> dict:
    """The prime bin nearest fin_target that is coprime with n, so a record
    of n samples holds a whole, prime number of periods and hits n distinct
    phases. Ties go to the lower bin."""
    if not (fs > 0 and n >= 8 and 0 < fin_target < fs / 2):
        raise Refused("bad_input", f"need fs > 0, n >= 8, 0 < fin_target < "
                      f"fs/2; got fs={fs}, n={n}, fin_target={fin_target}")
    n = int(n)
    want = fin_target * n / fs
    usable = [k for k in range(2, (n + 1) // 2)
              if _is_prime(k) and math.gcd(k, n) == 1]
    if not usable:
        raise Refused("bad_input", f"no prime bin coprime with n={n} below "
                      "Nyquist: use a longer record")
    k = min(usable, key=lambda b: (abs(b - want), b))
    return {"fin": k * fs / n, "bin": k, "fs": fs, "n": n}


def _samples(samples) -> np.ndarray:
    x = np.asarray(samples, dtype=float)
    if x.ndim != 1 or x.size < 8:
        raise Refused("bad_input", f"need a 1-D record of at least 8 samples, "
                      f"got shape {x.shape}")
    if not np.all(np.isfinite(x)):
        raise Refused("bad_input", "the record has a nan or inf sample")
    return x


def _harmonic_bins(k0: int, n: int, count: int) -> list[int]:
    """Where h2.. fold to, once each. A harmonic folding onto DC or the tone
    cannot be told from them, so it is not one of these bins."""
    out = []
    for h in range(2, count + 2):
        b = h * k0 % n
        b = min(b, n - b)
        if b not in (0, k0) and b not in out:
            out.append(b)
    return out


def _db(ratio: float) -> float:
    return 10.0 * math.log10(ratio) if ratio > 0 else math.inf


def dynamic(samples, fs: float, fin: float, nbits: int,
            harmonics: int = DEFAULT_HARMONICS) -> dict:
    """sndr, snr, sfdr (dB), thd (dBc, negative), enob (bits, from sndr) and
    enob_loss (nbits - enob), from a coherently sampled record. Raises
    Refused: not_coherent, bin_not_coprime, bad_input."""
    x = _samples(samples)
    n = x.size
    if not (fs > 0 and 0 < fin < fs / 2):
        raise Refused("bad_input", f"need 0 < fin < fs/2; got fs={fs}, fin={fin}")
    if nbits < 1 or harmonics < 0:
        raise Refused("bad_input", f"need nbits >= 1, harmonics >= 0; got "
                      f"{nbits}, {harmonics}")
    pick = "use convlib.coherent_tone(fs, n, fin) to pick fin"
    exact = fin * n / fs
    k0 = round(exact)
    if abs(exact - k0) > BIN_TOL:
        raise Refused("not_coherent", f"fin={fin:g} falls {exact:.6f} bins "
                      f"into a record of {n}, not on a bin: {pick}")
    if math.gcd(k0, n) != 1:
        raise Refused("bin_not_coprime", f"bin {k0} shares a factor with "
                      f"n={n}, so the record repeats the same phases: {pick}")

    # one-sided power; the Nyquist bin of an even record has no mirror
    power = np.abs(np.fft.rfft(x - x.mean())) ** 2
    power[1:] *= 2.0
    if n % 2 == 0:
        power[-1] /= 2.0
    power[0] = 0.0
    signal = float(power[k0])
    if signal <= 0.0:
        raise Refused("bad_input", f"no signal at bin {k0}")
    peak = int(np.argmax(power))
    if peak != k0:
        raise Refused("not_coherent", f"the largest tone is at bin {peak}, "
                      f"not fin's bin {k0}: the record is not at fin ({pick})")

    far = np.ones(power.size, bool)
    far[0] = False
    far[max(k0 - SKIRT_BINS[-1], 0):k0 + SKIRT_BINS[-1] + 1] = False
    floor = float(np.median(power[far])) if far.any() else 0.0
    sides = [[power[k0 + s * m] for m in SKIRT_BINS
              if 0 < k0 + s * m < power.size] for s in (-1, 1)]
    skirt = min((float(min(s)) for s in sides if s), default=0.0)
    if (all(sides) and skirt > SKIRT_RATIO * floor
            and skirt > signal * 10 ** (SKIRT_MIN_DBC / 10)):
        raise Refused("not_coherent", f"energy leaks past +-1 bin of bin {k0} "
                      f"on both sides ({_db(skirt / signal):.1f} dBc, floor "
                      f"{_db(floor / signal):.1f} dBc): the record does not "
                      f"hold a whole number of periods ({pick})")

    rest = power.copy()
    rest[k0] = 0.0
    harm = _harmonic_bins(k0, n, harmonics)
    distortion = float(rest[harm].sum())
    total = float(rest.sum())
    noise = total - distortion
    sndr = _db(signal / total) if total > 0 else math.inf
    enob = (sndr - 1.76) / 6.02
    return {
        "sndr": sndr,
        "snr": _db(signal / noise) if noise > 0 else math.inf,
        "sfdr": _db(signal / float(rest.max())) if total > 0 else math.inf,
        "thd": -_db(signal / distortion) if distortion > 0 else -math.inf,
        "enob": enob,
        "enob_loss": nbits - enob,
        "bin": k0,
        "n": n,
        "harmonic_bins": harm,
    }


def _nanmax_abs(a: np.ndarray) -> float:
    return float(np.nanmax(np.abs(a)))


def _lines(codes: np.ndarray, levels: np.ndarray, size: int, what: str,
           dnl_at) -> dict:
    """endpoint and best_fit dnl/inl for `levels` at `codes`, in full
    2**nbits-sized (or level-count-sized) arrays. dnl_at maps a step between
    levels i and i+1 to the code index that step belongs to."""
    def line(lsb: float, offset: float) -> dict:
        dnl = np.full(size, np.nan)
        dnl[dnl_at] = np.diff(levels) / lsb - 1.0
        inl = np.full(size, np.nan)
        inl[codes] = (levels - (offset + lsb * codes)) / lsb
        return {"lsb": lsb, "offset": offset, "dnl": dnl, "inl": inl,
                "dnl_max_abs": _nanmax_abs(dnl), "inl_max_abs": _nanmax_abs(inl)}

    ep_lsb = float((levels[-1] - levels[0]) / (codes[-1] - codes[0]))
    if ep_lsb == 0.0:
        raise Refused("bad_input", f"{what}: first and last level are equal, "
                      "so there is no LSB to measure in")
    slope, icept = np.polyfit(codes.astype(float), levels, 1)
    return {"endpoint": line(ep_lsb, float(levels[0] - ep_lsb * codes[0])),
            "best_fit": line(float(slope), float(icept))}


def _from_levels(levels: np.ndarray) -> dict:
    if levels.ndim != 1 or levels.size < 3:
        raise Refused("bad_input", f"need one level per code, at least 3; got "
                      f"shape {levels.shape}")
    if not np.all(np.isfinite(levels)):
        raise Refused("bad_input", "a level is nan or inf")
    size = levels.size
    res = _lines(np.arange(size), levels, size, "levels", np.arange(1, size))
    steps = res["endpoint"]["dnl"][1:] + 1.0
    res.update(
        kind="levels",
        n_codes=size,
        missing_codes=[int(k) + 1 for k in np.flatnonzero(np.abs(steps) <= MISSING_EPS)],
        monotonic=bool(np.all(steps > 0)),
    )
    return res


def _code_array(codes, nbits) -> np.ndarray:
    if nbits is None or isinstance(nbits, bool) or not 2 <= int(nbits) <= 24:
        raise Refused("bad_input", f"integer codes need nbits in 2..24, got {nbits}")
    c = np.asarray(codes)
    if c.ndim != 1 or c.size == 0:
        raise Refused("bad_input", f"need a 1-D record of codes, got shape {c.shape}")
    if not np.issubdtype(c.dtype, np.integer):
        raise Refused("bad_input", f"codes must be integers, got {c.dtype}")
    if c.min() < 0 or c.max() >= 2 ** int(nbits):
        raise Refused("bad_input", f"code outside 0..{2 ** int(nbits) - 1}: "
                      f"record spans {c.min()}..{c.max()}")
    return c


def static_histogram(codes, nbits: int, sine: bool = True) -> dict:
    """dnl/inl from how often each code was hit: an input whose amplitude
    distribution is known, sine (IEEE 1241 4.1.6.3, T[k] = -cos(pi * Hc[k]/S))
    or uniform (sine=False, a ramp). Overdrive the ends for a sine: amplitude
    and offset then fall out of the method. monotonic is None: a histogram
    keeps no order, so it says nothing either way."""
    c = _code_array(codes, nbits)
    size = 2 ** int(nbits)
    counts = np.bincount(c, minlength=size)
    below = np.cumsum(counts)[:-1]        # below[k-1] = hits under code k
    transitions = np.arange(1, size)
    cdf = below / c.size
    levels = -np.cos(np.pi * cdf) if sine else cdf.astype(float)
    res = _lines(transitions, levels, size, "histogram", np.arange(1, size - 1))
    res.update(
        kind="histogram_sine" if sine else "histogram_uniform",
        n_codes=size,
        missing_codes=[int(k) for k in np.flatnonzero(counts == 0)],
        monotonic=None,
        hits_per_code=float(c.size) / size,
    )
    return res


def static_ramp(codes_or_volts, nbits: int | None = None) -> dict:
    """Static transfer curve from a ramp. Float input is one level per code,
    in code order (a DAC's sweep; a list of Python floats counts). Integer
    input is an ADC's output codes over a slow ramp in time order, and needs
    nbits: its dnl/inl come from a uniform histogram, and monotonic is whether
    the record ever turns back against its direction. When float levels are
    given with nbits, there must be exactly 2**nbits of them."""
    a = np.asarray(codes_or_volts)
    if np.issubdtype(a.dtype, np.integer):
        res = static_histogram(a, nbits, sine=False)
        d = np.diff(a.astype(np.int64))
        res["monotonic"] = bool(np.all(d >= 0) or np.all(d <= 0))
        res["kind"] = "ramp_codes"
        return res
    if not np.issubdtype(a.dtype, np.floating):
        raise Refused("bad_input", f"need int codes or float levels, got {a.dtype}")
    if nbits is not None and a.size != 2 ** int(nbits):
        raise Refused("bad_input", f"nbits={nbits} needs {2 ** int(nbits)} "
                      f"levels, got {a.size}: an incomplete sweep")
    return _from_levels(a.astype(float))


def ramp_measures(res: dict) -> dict[str, float]:
    """The scalar measures a bench scores, named as the spec measures are:
    dnl_max_abs/inl_max_abs on the endpoint line, inl_best_fit_max_abs on the
    other, monotonic 1/0, missing_code_count. A histogram's unknown monotonic
    is left out, so a bound on it is sim_measure_missing rather than a pass."""
    out = {"dnl_max_abs": res["endpoint"]["dnl_max_abs"],
           "inl_max_abs": res["endpoint"]["inl_max_abs"],
           "inl_best_fit_max_abs": res["best_fit"]["inl_max_abs"],
           "missing_code_count": float(len(res["missing_codes"]))}
    if res["monotonic"] is not None:
        out["monotonic"] = 1.0 if res["monotonic"] else 0.0
    return out

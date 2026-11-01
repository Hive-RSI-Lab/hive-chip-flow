"""engine/lib/convlib.py (issue #126): known answers with no simulator, the
r2r_dac cross-check against its recorded gate, and sim_run's {{RAMP_FILE}}
wiring with ngspice faked."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))
sys.path.insert(0, str(REPO / "engine" / "scripts"))

import convlib  # noqa: E402
import sim_run  # noqa: E402

FS = 1e6
N = 4096


def tone(bin_, n=N):
    return 2 * np.pi * bin_ * np.arange(n) / n


def quantized(nbits, n=N, overdrive=1.0):
    t = convlib.coherent_tone(FS, n, FS / 11)
    half = 2 ** (nbits - 1)
    x = half * (1 + overdrive * np.sin(tone(t["bin"], n)))
    return t, np.clip(np.floor(x), 0, 2 ** nbits - 1).astype(int)


# ---------------------------------------------------------------- coherent_tone

def test_coherent_tone_picks_a_prime_bin_on_the_grid():
    t = convlib.coherent_tone(FS, N, 91e3)
    assert convlib._is_prime(t["bin"]) and math.gcd(t["bin"], N) == 1
    assert t["fin"] == t["bin"] * FS / N
    assert abs(t["fin"] - 91e3) < 3 * FS / N * 4


def test_coherent_tone_skips_a_prime_that_divides_the_record():
    # 1000 = 2^3 5^3: the nearest prime to bin 5 is 5, which shares a factor
    assert convlib.coherent_tone(1000.0, 1000, 5.0)["bin"] != 5


@pytest.mark.parametrize("args", [(FS, N, 0.0), (FS, N, FS / 2), (0.0, N, 1.0),
                                  (FS, 4, 1e3)])
def test_coherent_tone_bad_input(args):
    with pytest.raises(convlib.Refused) as e:
        convlib.coherent_tone(*args)
    assert e.value.kind == "bad_input"


# ---------------------------------------------------------------------- dynamic

@pytest.mark.parametrize("nbits", [6, 8, 10])
def test_ideal_quantizer_enob_and_snr(nbits):
    t, codes = quantized(nbits)
    d = convlib.dynamic(codes, FS, t["fin"], nbits)
    assert abs(d["enob"] - nbits) <= 0.05
    assert abs(d["snr"] - (6.02 * nbits + 1.76)) <= 0.1
    assert d["enob_loss"] == pytest.approx(nbits - d["enob"])


def test_hd3_at_minus_60_dbc_is_the_sfdr():
    t = convlib.coherent_tone(FS, N, FS / 11)
    w = tone(t["bin"])
    d = convlib.dynamic(np.sin(w) + 1e-3 * np.sin(3 * w), FS, t["fin"], 10)
    assert abs(d["sfdr"] - 60) <= 0.2
    assert d["thd"] == pytest.approx(-60, abs=0.2)
    assert 3 * t["bin"] in d["harmonic_bins"]
    # the spur is distortion, not noise: SNR must not see it
    assert d["sndr"] == pytest.approx(60, abs=0.2) and d["snr"] > 100


def test_harmonic_past_nyquist_folds():
    t = convlib.coherent_tone(FS, N, FS / 3)
    w = tone(t["bin"])
    d = convlib.dynamic(np.sin(w) + 1e-3 * np.sin(3 * w), FS, t["fin"], 10)
    assert abs(N - 3 * t["bin"]) == d["harmonic_bins"][1]
    assert d["thd"] == pytest.approx(-60, abs=0.2)


def test_fin_off_the_grid_is_refused():
    t = convlib.coherent_tone(FS, N, FS / 11)
    with pytest.raises(convlib.Refused) as e:
        convlib.dynamic(np.sin(tone(t["bin"])), FS, t["fin"] * 1.0001, 10)
    assert e.value.kind == "not_coherent"


@pytest.mark.parametrize("off", [0.01, 0.2, 0.5])
def test_record_not_at_the_claimed_fin_is_refused(off):
    # the arguments are coherent; the DATA leaks
    t = convlib.coherent_tone(FS, N, FS / 11)
    noise = 1e-4 * np.random.default_rng(1).normal(size=N)
    with pytest.raises(convlib.Refused) as e:
        convlib.dynamic(np.sin(tone(t["bin"] + off)) + noise, FS, t["fin"], 10)
    assert e.value.kind == "not_coherent"


def test_largest_tone_elsewhere_is_refused():
    t = convlib.coherent_tone(FS, N, FS / 11)
    x = 1e-3 * np.sin(tone(t["bin"])) + np.sin(tone(1009))
    with pytest.raises(convlib.Refused) as e:
        convlib.dynamic(x, FS, t["fin"], 10)
    assert e.value.kind == "not_coherent"


def test_spur_next_to_the_tone_is_not_called_leakage():
    # a real one-sided spur 3 bins out on a noisy coherent tone
    t = convlib.coherent_tone(FS, N, FS / 11)
    noise = 1e-4 * np.random.default_rng(2).normal(size=N)
    x = np.sin(tone(t["bin"])) + 1e-2 * np.sin(tone(t["bin"] + 3)) + noise
    assert convlib.dynamic(x, FS, t["fin"], 10)["sfdr"] == pytest.approx(40, abs=0.2)


def test_bin_sharing_a_factor_with_n_is_refused():
    with pytest.raises(convlib.Refused) as e:
        convlib.dynamic(np.sin(tone(64)), FS, 64 * FS / N, 10)
    assert e.value.kind == "bin_not_coprime"


@pytest.mark.parametrize("x", [np.zeros(N), [1.0, 2.0], [float("nan")] * 16])
def test_dynamic_bad_record(x):
    with pytest.raises(convlib.Refused) as e:
        convlib.dynamic(x, FS, 373 * FS / N, 10)
    assert e.value.kind == "bad_input"


# ------------------------------------------------------------------ static_ramp

def test_code_ramp_with_a_planted_missing_code():
    res = convlib.static_ramp(np.delete(np.arange(256), 100), nbits=8)
    assert res["missing_codes"] == [100]
    assert res["endpoint"]["dnl"][100] == pytest.approx(-1)
    assert res["endpoint"]["dnl_max_abs"] == pytest.approx(1)
    assert res["monotonic"] is True
    # the neighbours barely move, and the error stays as a step in INL
    assert abs(res["endpoint"]["dnl"][99]) < 0.01
    assert res["endpoint"]["inl"][101] - res["endpoint"]["inl"][100] == pytest.approx(-1)


def test_code_ramp_that_turns_back_is_not_monotonic():
    codes = np.arange(256)
    codes[[50, 51]] = codes[[51, 50]]
    assert convlib.static_ramp(codes, nbits=8)["monotonic"] is False


def test_ideal_dac_levels_are_zero_everywhere():
    res = convlib.static_ramp(0.5 + 0.01 * np.arange(256.0))
    for line in ("endpoint", "best_fit"):
        assert res[line]["dnl_max_abs"] < 1e-9 and res[line]["inl_max_abs"] < 1e-9
        assert res[line]["lsb"] == pytest.approx(0.01)
    assert res["missing_codes"] == [] and res["monotonic"] is True
    assert math.isnan(res["endpoint"]["dnl"][0])


def test_bowed_dac_tells_the_two_lines_apart():
    k = np.arange(256.0)
    bow = 1.0 * (k / 255) * (1 - k / 255) * 4     # 1 LSB peak at mid-scale
    res = convlib.static_ramp(k + bow)
    assert res["endpoint"]["inl_max_abs"] == pytest.approx(1.0, abs=0.01)
    # least squares splits a parabola's 1 LSB bow to about +-0.5..0.67 LSB
    assert 0.4 < res["best_fit"]["inl_max_abs"] < 0.7


def test_dac_stuck_code_and_inversion():
    k = np.arange(256.0)
    stuck = k.copy()
    stuck[77] = stuck[76]
    res = convlib.static_ramp(stuck)
    assert res["missing_codes"] == [77] and res["monotonic"] is False
    assert res["endpoint"]["dnl"][77] == pytest.approx(-1, abs=0.01)
    # a falling DAC is fine: its steps follow its own LSB
    down = convlib.static_ramp(255.0 - k)
    assert down["monotonic"] is True and down["endpoint"]["dnl_max_abs"] < 1e-9


def test_non_monotonic_dac():
    k = np.arange(256.0)
    k[10] = k[9] - 0.5
    res = convlib.static_ramp(k)
    assert res["monotonic"] is False and res["missing_codes"] == []


@pytest.mark.parametrize("call", [
    lambda: convlib.static_ramp(np.arange(256)),                 # codes, no nbits
    lambda: convlib.static_ramp(np.arange(300), nbits=8),        # out of range
    lambda: convlib.static_ramp(np.arange(100.0), nbits=8),      # short sweep
    lambda: convlib.static_ramp(np.ones(16)),                    # no LSB
    lambda: convlib.static_ramp([0.0, float("nan"), 1.0]),
    lambda: convlib.static_ramp(["a", "b", "c"]),
    lambda: convlib.static_histogram(np.array([0.5, 1.5]), 8),
])
def test_static_refusals(call):
    with pytest.raises(convlib.Refused) as e:
        call()
    assert e.value.kind == "bad_input"


# ------------------------------------------------------------- static_histogram

def test_sine_histogram_of_an_ideal_quantizer_is_clean():
    # a long, overdriven record: the cosine correction is what makes it flat
    t, codes = quantized(8, n=2 ** 18, overdrive=1.02)
    res = convlib.static_histogram(codes, 8, sine=True)
    assert res["missing_codes"] == [] and res["monotonic"] is None
    assert res["endpoint"]["dnl_max_abs"] < 0.05
    assert res["endpoint"]["inl_max_abs"] < 0.05
    # and without it the same record is plainly not flat
    assert convlib.static_histogram(codes, 8, sine=False)["endpoint"]["dnl_max_abs"] > 0.5


def test_sine_histogram_finds_a_planted_missing_code():
    _, codes = quantized(8, n=2 ** 18, overdrive=1.02)
    hit = codes.copy()
    hit[hit == 100] = 101
    res = convlib.static_histogram(hit, 8, sine=True)
    assert res["missing_codes"] == [100]
    assert res["endpoint"]["dnl"][100] == pytest.approx(-1)
    assert res["endpoint"]["dnl"][101] == pytest.approx(1, abs=0.05)


def test_histogram_measures_leave_monotonic_unknown():
    _, codes = quantized(8, n=2 ** 14, overdrive=1.02)
    m = convlib.ramp_measures(convlib.static_histogram(codes, 8))
    assert "monotonic" not in m and m["missing_code_count"] == 0


# ------------------------------------------------- cross-check with a real bench

SWEEP = json.loads((REPO / "tests" / "fixtures" / "convlib" /
                    "r2r_dac_sweep.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("corner", sorted(SWEEP["corners"]))
def test_r2r_dac_recorded_inl_dnl_recomputed(corner):
    data = SWEEP["corners"][corner]
    m = convlib.ramp_measures(convlib.static_ramp(data["levels"], nbits=8))
    assert abs(m["dnl_max_abs"] - data["recorded"]["dnl_max_abs"]) <= 1e-3
    assert abs(m["inl_max_abs"] - data["recorded"]["inl_max_abs"]) <= 1e-3
    assert m["monotonic"] == data["recorded"]["monotonic"]


# --------------------------------------------------------------- sim_run wiring

SPEC = "top: dac\nsupply: {vdd: 3.3}\ndevices: [x1]\n"
BENCH = """\
* static_ramp_codes: 4
.include '{{NETLIST}}'
.control
wrdata {{RAMP_FILE}} vo
.endc
.end
"""
BOUNDS = [{"measure": "inl_max_abs", "max": 0.5, "severity": "error"},
          {"measure": "monotonic", "min": 1, "severity": "error"}]
GOOD = "0 0.0\n1 1.0\n2 2.1\n3 3.0\n"


def run_bench(tmp_path, monkeypatch, sweep, bench=BENCH, name="b"):
    """One run_bench_at_corner with ngspice standing in: it writes `sweep`
    (None: nothing) to the file the deck asks for."""
    decks = []

    def fake(eda_bin, deck_path, cwd, timeout):
        decks.append(Path(deck_path).read_text(encoding="utf-8"))
        target = Path(deck_path).with_suffix(".ramp")
        assert f"wrdata {target.name} vo" in decks[-1]
        if sweep is not None:
            target.write_text(sweep, encoding="utf-8")
        return "", "", 0
    monkeypatch.setattr(sim_run.simlib, "run_ngspice", fake)
    corner = {"name": "tt", "process": "typical", "temp_c": 27, "supply_pct": 0}
    subs = {"NETLIST": "n.cir", "VDD": "3.3"}
    return sim_run.run_bench_at_corner(Path("eda"), f"{name}.cir", bench, BOUNDS,
                                       subs, corner, tmp_path, 1.0)


def kinds(res):
    return {v["kind"] for v in res["violations"]}


def test_sweep_is_scored_through_static_ramp(tmp_path, monkeypatch):
    res = run_bench(tmp_path, monkeypatch, GOOD)
    assert res["violations"] == []
    assert res["measures"]["monotonic"] == 1.0
    assert res["measures"]["inl_max_abs"] == pytest.approx(0.1)


def test_bad_sweep_fails_its_bound(tmp_path, monkeypatch):
    res = run_bench(tmp_path, monkeypatch, "0 0.0\n1 2.0\n2 1.0\n3 3.0\n")
    assert kinds(res) == {"sim_bound_fail"}


@pytest.mark.parametrize("sweep", [None, "", "0 0.0\n1 1.0\n",
                                   "0 0.0\n1 1.0\n1 2.0\n3 3.0\n",
                                   "0 0.0\n1 x\n2 2.0\n3 3.0\n"])
def test_no_usable_sweep_is_a_refusal_never_a_pass(tmp_path, monkeypatch, sweep):
    assert "sim_static_ramp_refused" in kinds(run_bench(tmp_path, monkeypatch, sweep))


def test_a_previous_runs_sweep_is_not_scored(tmp_path, monkeypatch):
    (tmp_path / "b__tt.ramp").write_text(GOOD, encoding="utf-8")
    assert "sim_static_ramp_refused" in kinds(run_bench(tmp_path, monkeypatch, None))


def test_placeholder_without_the_length_comment_is_refused(tmp_path, monkeypatch):
    res = run_bench(tmp_path, monkeypatch, GOOD,
                    bench=BENCH.replace("* static_ramp_codes: 4\n", ""))
    assert "sim_static_ramp_refused" in kinds(res)


def test_sweep_files_do_not_collide_between_benches(tmp_path, monkeypatch):
    # bench_strength runs mutants in parallel in one directory
    run_bench(tmp_path, monkeypatch, GOOD, name="m1__b")
    run_bench(tmp_path, monkeypatch, GOOD, name="m2__b")
    assert {p.name for p in tmp_path.glob("*.ramp")} == {"m1__b__tt.ramp", "m2__b__tt.ramp"}


def test_a_bench_without_the_placeholder_is_untouched(tmp_path, monkeypatch):
    def fake(eda_bin, deck_path, cwd, timeout):
        return "  Measurements for DC\nmonotonic = 1\ninl_max_abs = 0.1\n", "", 0
    monkeypatch.setattr(sim_run.simlib, "run_ngspice", fake)
    corner = {"name": "tt", "process": "typical", "temp_c": 27, "supply_pct": 0}
    res = sim_run.run_bench_at_corner(Path("eda"), "p.cir", ".end\n", BOUNDS,
                                      {"VDD": "3.3"}, corner, tmp_path, 1.0)
    assert res["violations"] == [] and list(tmp_path.glob("*.ramp")) == []

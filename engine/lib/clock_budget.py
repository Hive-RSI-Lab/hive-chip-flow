"""The setup clock uncertainty a declared jitter budget asks for (#73).

spec.yaml's `clock.jitter` names a clock's source jitter, keyed by the SDC
clock name it lands on (a domain such as clk or vco_out, or a generated
clock such as clk_pre):

    clock:
      period_ns: 80
      domains: [clk, vco_out]
      jitter:
        clk_pre:
          ps_rms: 12          # source jitter, picoseconds rms
          cycles: 2           # periods of the source it accumulates over
          accumulation: correlated   # or random (default correlated)
          sigma: 7            # k-sigma coverage (default 7)
          source: spec/jitter/vco_jitter.json   # optional evidence

The required setup uncertainty is ps_rms x sigma x growth(cycles), with
growth N for correlated jitter (a divided clock whose edges share the same
source error: the errors add) and sqrt(N) for random cycle-to-cycle jitter
(independent errors add in quadrature). 12 ps rms over 2 correlated cycles
at 7 sigma needs 0.168 ns, the PLL's own hand derivation (#73's known
answer). A clock with no declared jitter keeps whatever its SDC sets
(LibreLane's CLOCK_UNCERTAINTY_CONSTRAINT, 0.25 ns by default).

`source` is workspace-relative and must sit under spec/jitter/: that
directory is one of the timing gate's freshness inputs (invalidation.yaml
`clock_jitter`), so an analog re-measure written there stales a recorded
timing pass. check_timing fails a source that is not there.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath

DEFAULT_SIGMA = 7.0
ACCUMULATIONS = ("correlated", "random")
SOURCE_DIR = "spec/jitter"
FIELDS = {"ps_rms", "cycles", "accumulation", "sigma", "source"}


class ClockBudgetError(ValueError):
    """A `clock.jitter` entry that does not say a budget; str is
    '<kind>: <message>' as speclib's lint reports it."""


@dataclass(frozen=True)
class Budget:
    clock: str
    ps_rms: float
    cycles: int
    accumulation: str
    sigma: float
    source: str | None

    @property
    def required_ns(self) -> float:
        growth = (self.cycles if self.accumulation == "correlated"
                  else math.sqrt(self.cycles))
        return self.ps_rms * self.sigma * growth / 1000.0


def required_uncertainty_ns(ps_rms: float, cycles: int = 1,
                            accumulation: str = "correlated",
                            sigma: float = DEFAULT_SIGMA) -> float:
    return Budget("", ps_rms, cycles, accumulation, sigma, None).required_ns


def _positive(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def budgets(spec: dict) -> dict[str, Budget]:
    """{SDC clock name: Budget} from spec.yaml's clock.jitter; {} when it
    declares none. Raises ClockBudgetError on an entry that is not one."""
    clock = spec.get("clock")
    if not isinstance(clock, dict) or clock.get("jitter") is None:
        return {}
    jitter = clock["jitter"]
    if not isinstance(jitter, dict) or not jitter:
        raise ClockBudgetError(
            "clock_jitter_not_a_mapping: 'clock.jitter' must map an SDC "
            "clock name to {ps_rms, cycles, accumulation, sigma, source}")
    out = {}
    for name, entry in jitter.items():
        where = f"'clock.jitter.{name}'"
        if not isinstance(name, str) or not name:
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where}: the key must be a clock name")
        if not isinstance(entry, dict):
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where} must be a mapping")
        extra = sorted(set(entry) - FIELDS)
        if extra:
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where} has unknown field(s) "
                f"{extra}; it takes {sorted(FIELDS)}")
        ps_rms = entry.get("ps_rms")
        if not _positive(ps_rms):
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where}.ps_rms must be a positive "
                "number (picoseconds rms)")
        cycles = entry.get("cycles", 1)
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 1:
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where}.cycles must be a whole "
                "number of cycles, 1 or more")
        accumulation = entry.get("accumulation", "correlated")
        if accumulation not in ACCUMULATIONS:
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where}.accumulation must be one "
                f"of {list(ACCUMULATIONS)}")
        sigma = entry.get("sigma", DEFAULT_SIGMA)
        if not _positive(sigma):
            raise ClockBudgetError(
                f"clock_jitter_bad_entry: {where}.sigma must be a positive "
                "number")
        source = entry.get("source")
        if source is not None:
            p = PurePosixPath(source) if isinstance(source, str) else None
            if (p is None or p.is_absolute() or ".." in p.parts
                    or p.parts[:2] != tuple(SOURCE_DIR.split("/"))
                    or len(p.parts) < 3):
                raise ClockBudgetError(
                    f"clock_jitter_bad_source: {where}.source must be a "
                    f"workspace-relative file under {SOURCE_DIR}/, the "
                    "directory timing's freshness hashes")
        out[name] = Budget(name, float(ps_rms), cycles, accumulation,
                           float(sigma), source)
    return out

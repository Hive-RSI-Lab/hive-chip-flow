"""Fault: a clock whose declared jitter needs more setup uncertainty than
the SDC applies (#73). The PLL hand-derived its divided clock's
uncertainty from the VCO's measured jitter in a comment; nothing checked
that the SDC still carried it, so a re-measured, noisier VCO would have
gone through timing on the old margin.

counter8's RTL and harden config are untouched: its SDC keeps LibreLane's
generic CLOCK_UNCERTAINTY_CONSTRAINT, 0.25 ns. The plant declares 50 ps
rms of source jitter on clk in spec.yaml's clock.jitter, citing a
measurement under spec/jitter/; at the default 7 sigma over one cycle that
needs 0.35 ns. Every slack stays positive - only the uncertainty read-back
against the budget can catch it.

check_timing.py reads harden's own output on disk, so this fault runs
check_harden.run() itself first, as plant_timing.py does."""
from pathlib import Path

MARK = "  period_ns: 20\n  domains: [clk]\n"
JITTER = ("  # planted (#73): a measured 50 ps rms on the clock source; "
          "7 sigma needs 0.35 ns\n"
          "  jitter:\n"
          "    clk: {ps_rms: 50, source: spec/jitter/clk_source.json}\n")


def plant(ws: Path) -> None:
    spec_path = ws / "spec" / "spec.yaml"
    spec = spec_path.read_text(encoding="utf-8")
    if MARK not in spec:
        raise RuntimeError(
            f"{spec_path}: expected counter8's clock block {MARK!r} - the "
            "jitter budget below is keyed to its one clock, clk")
    spec_path.write_text(spec.replace(MARK, MARK + JITTER, 1), encoding="utf-8")
    jitter = ws / "spec" / "jitter"
    jitter.mkdir(parents=True, exist_ok=True)
    (jitter / "clk_source.json").write_text(
        '{"clock": "clk", "ps_rms": 50, "note": "planted measurement"}\n',
        encoding="utf-8")

    import check_harden
    payload, _out = check_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(
            "plant_timing_jitter.py: harden itself did not pass (status "
            f"{payload.get('status')!r}) - the fault needs a clean harden "
            f"output to check timing against: {payload.get('violations')}")

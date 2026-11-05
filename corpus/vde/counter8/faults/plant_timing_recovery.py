"""Fault: an asynchronous reset with its synchroniser removed (gates.yaml's
timing row; issue #195). counter8 becomes the #194 counter's shape minus
its two-flop synchroniser: rst asserts AND releases the count flops
asynchronously, so TT's rst_n pad drives their RN pins with no clk-timed
stage in between. A pad asynchronous to clk can release at any point in
the cycle, including just before a clock edge, so the plant's
harden/constraints.sdc says so on rst_n: a maximum input delay of 24 ns
(1.2 periods; LibreLane's base.sdc gives every input 20% of the period,
4 ns, which hides it). The release then lands inside the flops' liberty
recovery window - the flop may or may not leave reset on that edge, so the
8 bits can leave reset on different cycles - and OpenSTA's recovery check
fails. With the synchroniser in place the count flops' release comes from
a clk flop instead, and only the synchroniser's own first stage sees the
pad.

Recovery, not removal (the early-release side, input delay -min 0): OpenSTA
folds removal into its -min (hold) checks and LibreLane's
HOLD_VIOLATION_CORNERS is '*', so a removal miss stops harden itself at
Checker.HoldViolations before the timing gate runs. Recovery folds into
-max, whose SETUP_VIOLATION_CORNERS is '' here (informational), so harden
completes and only the timing gate can catch it. The constraint is also
set at the non-tt corners only, as plant_timing_corner_sdc.py does:
harden's PnR steps load the nom_tt corner, so the resizer never sees the
miss. Before #195 the gate named this a setup violation (recovery folded
into worst setup slack); it is now a `recovery_violation` with the flop
named.

check_timing.py reads harden's own output on disk, so this fault runs
check_harden.run() itself first, as plant_timing.py does."""
from pathlib import Path

SDC = """\
# planted: rst_n is asynchronous to clk and can release just before a clock edge
# (non-tt corners only: ::env(_CURRENT_CORNER_NAME) is the corner timed)
if {[info exists ::env(_CURRENT_CORNER_NAME)]
    && ![string match "*_tt_*" $::env(_CURRENT_CORNER_NAME)]} {
    set_input_delay -max 24 -clock [get_clocks clk] [get_ports rst_n]
}
"""


def plant(ws: Path) -> None:
    rtl = ws / "rtl" / "counter8.v"
    text = rtl.read_text(encoding="utf-8")
    old = "  always @(posedge clk) begin\n"
    if old not in text:
        raise RuntimeError(f"{rtl}: expected counter8's synchronous-reset "
                           "always block - has the RTL changed?")
    rtl.write_text(text.replace(old, "  always @(posedge clk or posedge rst) "
                                "begin\n", 1), encoding="utf-8")
    spec = (ws / "spec" / "spec.yaml").read_text(encoding="utf-8")
    if "rst: ~rst_n" not in spec:
        raise RuntimeError(f"{ws / 'spec' / 'spec.yaml'}: expected tt_pins "
                           "'rst: ~rst_n' - the SDC below names rst_n")
    harden = ws / "harden"
    harden.mkdir(exist_ok=True)
    (harden / "constraints.sdc").write_text(SDC, encoding="utf-8")

    import check_harden
    payload, _out = check_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(
            "plant_timing_recovery.py: harden itself did not pass on the "
            f"unsynchronised reset (status {payload.get('status')!r}) - the "
            "fault needs a clean harden output to check timing against: "
            f"{payload.get('violations')}")

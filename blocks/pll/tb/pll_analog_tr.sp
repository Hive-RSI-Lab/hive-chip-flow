* pll_analog_tr.sp - transistor-level wrapper for the cosim bridge.
* Re-orders the real cell's pins (analog/netlist/pll_analog.cir, .subckt
* pll_analog vco_out pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vctrl bias_ref
* vdd vss) into the cocotbext-ams wrapper order: digital inputs, digital
* outputs, vdd, vss. The cell itself, the PDK models and sizing/sizing.yaml
* come in through the bench's extra_lines (absolute paths resolved at run
* time), so this file carries no device of its own beyond the vco_out load.
* vctrl stays internal (x1.vctrl, the loop filter node; no pad capacitance,
* the analog spec's nominal loop condition, ol_pm_pn16_nopad). bias_ref is
* the tile pad ua[1], left open (the internal default bias) but with the
* pad's capacitance on it, 5 pF as in the analog startup bench. Why the pad
* is modelled: with bias_ref a bare node (zero capacitance) the Vgs/R
* reference does not settle after pll_en rises - vref relaxes 0.59..1.37 V
* with an ~80 ns period for as long as the run lasts (batch ngspice, tt_27c,
* cell alone, vctrl forced 1.7 V, op start with pll_en low then stepped
* high; 1 pF or 5 pF on bias_ref and it settles to 0.918 V). The analog
* benches never see this because each solves its DC point with pll_en
* already high. A pad node is never zero capacitance; the bare-node case is
* reported to the analog side as a finding, not hidden here.
.subckt pll_analog_cosim pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vco_out vdd vss
xpll vco_out pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vctrl bias_ref vdd vss pll_analog
* prescaler first flop CLK + one buffer (the analog benches' 15 fF)
cload vco_out vss 15f
* ua[1] pad capacitance on bias_ref
cpad_bias bias_ref vss 5p
.ends pll_analog_cosim

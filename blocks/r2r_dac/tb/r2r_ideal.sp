* r2r_ideal.sp - behavioral stand-in for r2r_dac_analog (analog/netlist/
* r2r_dac_analog.cir) in the cosim bench. The SKILL's known limit: a
* transistor-level block in the cocotbext-ams bridge has not converged, so
* this keeps the real ladder (ideal resistors, nominal unit R) and replaces
* only the eight buf_20 drivers with ideal threshold switches to vdd/vss.
*
* Port order is fixed by the cocotbext-ams wrapper (digital input bits LSB
* first, then output pins, then vdd vss): the wrapper's dac_code_<k> is the
* macro's dac_code[k], and vout is the macro's vout pin (ua[0]).
*
* Values: ru = 27k, the analog side's nominal unit R (sizing.yaml: 26.99k,
* each 2R branch trimmed to 2R including driver Ron, so an ideal 0-ohm
* switch + 2R is the same Thevenin network). No pad load in here: vout is
* on ua[0] (interface.yaml ua_pins), so cosim's pad run wraps this subckt in
* TT's real ua path (engine/lib/padlib.py: 500 ohm, 5 pF, the ESD diodes).
* The hand-made 100 ohm + 7 pF this used to carry stood in for that pad;
* kept, it would sit on the die side of the real pad and count it twice.
.subckt r2r_dac_ideal dac_code_0 dac_code_1 dac_code_2 dac_code_3 dac_code_4 dac_code_5 dac_code_6 dac_code_7 vout vdd vss
.param ru=27k
* ideal switch drivers, bit 7 = MSB
bd0 d0 vss v = v(dac_code_0,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd1 d1 vss v = v(dac_code_1,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd2 d2 vss v = v(dac_code_2,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd3 d3 vss v = v(dac_code_3,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd4 d4 vss v = v(dac_code_4,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd5 d5 vss v = v(dac_code_5,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd6 d6 vss v = v(dac_code_6,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
bd7 d7 vss v = v(dac_code_7,vss) > 0.5*v(vdd,vss) ? v(vdd,vss) : 0
* 2R branches, rung k; rung 7 is vout
rb7 d7 vout {2*ru}
rb6 d6 n6 {2*ru}
rb5 d5 n5 {2*ru}
rb4 d4 n4 {2*ru}
rb3 d3 n3 {2*ru}
rb2 d2 n2 {2*ru}
rb1 d1 n1 {2*ru}
rb0 d0 n0 {2*ru}
* series R between rungs
rs6 vout n6 {ru}
rs5 n6 n5 {ru}
rs4 n5 n4 {ru}
rs3 n4 n3 {ru}
rs2 n3 n2 {ru}
rs1 n2 n1 {ru}
rs0 n1 n0 {ru}
* LSB termination 2R to vss
rt n0 vss {2*ru}
.ends r2r_dac_ideal

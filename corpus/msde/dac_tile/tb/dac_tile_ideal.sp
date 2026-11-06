* dac_tile_ideal.sp - behavioral stand-in for dac_tile_analog (analog/
* netlist/dac_tile_analog.cir) in the cosim bench: the same ladder at its
* nominal values (ppolyf_u_1k at ~1k ohm/sq: R = 10um/2um = 5k, 2R = 10k),
* with the two CMOS reference inverters replaced by ideal inverting
* switches to vdd/vss (a leg sits at vdd when its bit is LOW, as in the
* real cell). Pad load = the analog spec's pad_caps_pf, 1 pF on vout.
*
* Port order is fixed by the cocotbext-ams wrapper (digital input bits LSB
* first, then output pins, then vdd vss): the wrapper's code_<k> is the
* cosim top's code[k] = {bmsb, blsb}[k], and vout is the macro's vout pin
* (ua[0]).
.subckt dac_tile_ideal code_0 code_1 vout vdd vss
.param ru=5k cpad=1p
bdl dl vss v = v(code_0,vss) > 0.5*v(vdd,vss) ? 0 : v(vdd,vss)
bdm dm vss v = v(code_1,vss) > 0.5*v(vdd,vss) ? 0 : v(vdd,vss)
rmsb  dm vout {ru}
r2lsb vout n0 {2*ru}
rlsb  dl n0 {ru}
r2msb n0 vss {2*ru}
cp vout vss {cpad}
.ends dac_tile_ideal

* sar_adc_ideal.sp - behavioral stand-in for sar_adc_analog in the cosim
* bench: an ideal S/H, an ideal 8-bit DAC and a strobed comparator, around
* the input tone the bench measures.
*
* vin   the test tone, a sine at fin around mid-rail. The bench reads fin
*       from the .param below and checks it is coherent with its own
*       sample rate and record length (convlib refuses it otherwise).
* S/H   a switch that closes while `sample` is high onto a 1 pF hold cap
*       (100 ohm on: 0.1 ns, a few hundred times shorter than the track).
* DAC   vdd/256 times the weighted dac_code bits (w0..w7 = 1..128).
* comp  cmp_out = vdd while comp_clk is high and the held input is above
*       the DAC's trial voltage, else 0.
*
* Port order is fixed by the cocotbext-ams wrapper: the digital inputs in
* the bench's pin order (dac_code LSB first, sample, comp_clk), then the
* output cmp_out, then vdd vss.
.param fin=46875 vmid=1.65 vamp=1.6
.subckt sar_adc_ideal dac_code_0 dac_code_1 dac_code_2 dac_code_3
+ dac_code_4 dac_code_5 dac_code_6 dac_code_7 sample comp_clk cmp_out vdd vss
.param w0=1 w1=2 w2=4 w3=8 w4=16 w5=32 w6=64 w7=128 chold=1p
vin vin vss sin({vmid} {vamp} {fin})
ssh vin vhold sample vss swsh
.model swsh sw vt=1.65 vh=0.1 ron=100 roff=1e12
ch vhold vss {chold}
bdac vdac vss v = v(vdd,vss)/256*(
+ {w0}*(v(dac_code_0,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w1}*(v(dac_code_1,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w2}*(v(dac_code_2,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w3}*(v(dac_code_3,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w4}*(v(dac_code_4,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w5}*(v(dac_code_5,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w6}*(v(dac_code_6,vss) > 0.5*v(vdd,vss) ? 1 : 0) +
+ {w7}*(v(dac_code_7,vss) > 0.5*v(vdd,vss) ? 1 : 0))
bcmp cmp_out vss v = (v(comp_clk,vss) > 0.5*v(vdd,vss)) &&
+ (v(vhold,vss) > v(vdac,vss)) ? v(vdd,vss) : 0
.ends sar_adc_ideal

# pll_analog - topology (P2)

Template anchor: `current_mirror.sp` (the only library template this block's
measures map onto: every DC current here - Icp, the trim codes, the bias, the
VCO starving current - is a mirror ratio off one reference). The ring VCO,
V-to-I, loop filter, switches and enable logic have no library template; they
are declared here device by device (see OPEN in the P2 report). No standard
cells (`split_devices` is empty). Models: `nfet_03v3`, `pfet_03v3`,
`ppolyf_u`, `cap_mim_2f0ff` from gf180mcuD `sm141064.spice`.

## Subckt and pin order

```
.subckt pll_analog vco_out pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vctrl bias_ref vdd vss
```

Ten pins in this order (the six interface.yaml signals, ua_pins `vctrl`
(ua[0]) and `bias_ref` (ua[1]), vdd, vss). LVS and the pex bench bind
positionally.

Internal nodes: `en_b`, `vbp` (PMOS bias gate), `va` (bias loop gain node), `vref` (bias loop node, Vgs of MN_B1),
`vst` (startup), `vcp_n`, `vcp_p` (pump reference gates), `up_b`, `dn_b`,
`up_s`, `dn_s` (switch source nodes), `vdump`, `vbt` / `vnt` (buffer PMOS /
NMOS pair tails), `vbl` / `vbh` (buffer NMOS / PMOS mirror-load gates),
`vfilt` (R1-C1 junction),
`vbp_vco`, `vbn_vco`, `vi` (V2I drain), `vs` (V2I source), `vcl` (ceiling drain), `r1..r5` (ring),
`nb` (NAND output).

## Blocks and refdes

### 1. Enable inverter
- `MP_ENI`, `MN_ENI`: `pll_en` -> `en_b`. Both polarities are used below.

### 2. Bias: Vgs/R (Vt-referenced) self-biased reference, Ib = 5 uA nominal
Gray-Meyer Vt-referenced core: the reference current flows through
RBIAS_TOP + RBIAS_INT and the resulting drop is the Vgs of MN_B1. The
loop node is the internal `vref`; the pad `bias_ref` is a tap between the
two resistors.
- `MP_B2`: PMOS diode, gate = drain = `vbp`; source vdd. Mirror master.
- `MP_B1`: PMOS, gate `vbp`, drain `va`. 1:1 copy into MN_B1.
- `MN_B1`: NMOS, gate `vref`, source vss, drain `va`. Senses V(vref).
- `MN_B2`: NMOS, gate `va`, source `vref`, drain `vbp`. Delivers Ib into the resistor.
- `RBIAS_TOP`: ppolyf_u, `vref` to `bias_ref` (~63 kOhm). Isolates the
  loop node from the pad capacitance (see Bias equations).
- `RBIAS_INT`: ppolyf_u, `bias_ref` to vss. Internal default; an external
  resistor on pad ua[1] sits in parallel with it and raises Ib, at most to
  Vgs / RBIAS_TOP (~2x) with the pad shorted.
- Startup: `MP_STL` (weak long PMOS, gate `en_b`, vdd -> `vst`), `MN_STD`
  (gate `vref`, `vst` -> vss), `MN_ST` (gate `vst`, `vbp` -> vss),
  `MN_STE` (gate `en_b`, `vst` -> vss, holds startup off in standby).
- Standby: `MP_ENB` (gate `pll_en`, `vbp` -> vdd), `MN_ENA` (gate `en_b`,
  `va` -> vss), `MN_ENB` (gate `en_b`, `vref` -> vss; the pad follows
  through RBIAS_INT).

`vbp` is the only bias distributed to the other blocks (PMOS gate);
`vref` (= Vgs of MN_B1) is the NMOS mirror gate (MN_BUFT, MN_CL, MN_FLR).

### 3. Charge pump, Icp = (4 + t0 + 2 t1 + t0 t1) x 5 uA = 20/25/30/40 uA
Trim acts on the pump reference, so UP and DN share one trimmed current.
- Trim legs (PMOS, gate `vbp`, source vdd, drains -> `vcp_n` through NMOS
  switches gated by the trim bits): `MP_T4` (4 units, always on, no switch),
  `MP_T1A` (1 unit) via `MN_SW0` (gate `cp_trim0`), `MP_T2` (2 units) via
  `MN_SW1` (gate `cp_trim1`), `MP_T1B` (1 unit) via series `MN_SW0B` (gate
  `cp_trim0`) and `MN_SW1B` (gate `cp_trim1`) - the series pair is the AND.
- `MN_CPREF`: NMOS diode on `vcp_n` (sums the legs). DN gate reference.
- `MN_CPREF2`: gate `vcp_n`, pulls the same current from `MP_CPREF`.
- `MP_CPREF`: PMOS diode on `vcp_p`. UP gate reference.
- UP: `MP_UPS` (source, gate `vcp_p`, drain `up_s`), `MP_UP` (switch, gate
  `up_b`, `up_s` -> `vctrl`), `MP_UPD` (steering switch, gate `pfd_up`,
  `up_s` -> `vdump`). `MP_UPI`/`MN_UPI`: inverter `pfd_up` -> `up_b`.
- DN: `MN_DNS` (sink, gate `vcp_n`, drain `dn_s`), `MN_DN` (switch, gate
  `pfd_dn`, `vctrl` -> `dn_s`), `MN_DND` (steering switch, gate `dn_b`,
  `vdump` -> `dn_s`). `MP_DNI`/`MN_DNI`: inverter `pfd_dn` -> `dn_b`.
- Standby: `MP_ENP` (gate `pll_en`, `vcp_p` -> vdd), `MN_ENN` (gate `en_b`,
  `vcp_n` -> vss).

Current steering keeps MP_UPS/MN_DNS conducting at all times; the idle
current goes to `vdump`, which the unity-gain buffer of section 3b holds at
`vctrl` (added at the P2 re-entry from P4: self-settling `vdump` floated at
1.4-2.9 V and the charge share on up_s/dn_s gave 25.1 fC against the
10 fC bound with UP/DN current mismatch under 1 fC).

### 3b. Dump-node buffer: vdump = vctrl (rail-to-rail unity-gain OTA)
Two `diff_pair.sp` input stages of opposite polarity, each with its
resistive loads replaced by a `current_mirror.sp` mirror, both mirror
outputs summed on `vdump`, which is tied back to both inverting inputs
(complementary-input single-stage OTA). The PMOS pair covers the bottom of
the vctrl range, the NMOS pair the top (the overlap bench scores the worst
|Q| at vctrl = 0.3 V, vdd/2 and vdd - 0.3 V, so one pair is not enough).
PMOS half (tail off `vbp`, NMOS mirror load):
- `MP_BUFT`: tail, source vdd, gate `vbp`, drain `vbt`. Itail = ratio x Ib,
  ratio 4 nominal (20 uA, = MP_T4's unit count) so the buffer can source or
  sink the whole code-00 Icp when only one of UP/DN is active and the
  other's idle current lands on `vdump` alone. Off in standby: MP_ENB
  already pulls `vbp` to vdd.
- `MP_BUF1`: input pair, gate `vctrl` (non-inverting), source `vbt`,
  drain `vbl`.
- `MP_BUF2`: input pair, gate `vdump` (inverting, the feedback), source
  `vbt`, drain `vdump` (the output).
- `MN_BUFL1`: mirror-load master, NMOS diode on `vbl`, source vss.
- `MN_BUFL2`: mirror-load slave, gate `vbl`, drain `vdump`, source vss.
NMOS half (tail off `vref`, PMOS mirror load):
- `MN_BUFT`: tail, source vss, gate `vref`, drain `vnt`. Itail =
  ratio x Ib off MN_B1 (like MN_FLR), ratio 4 nominal. Off in standby:
  MN_ENB already pulls `vref` to vss.
- `MN_BUF1`: input pair, gate `vctrl` (non-inverting), source `vnt`,
  drain `vbh`.
- `MN_BUF2`: input pair, gate `vdump` (inverting, the feedback), source
  `vnt`, drain `vdump` (the output).
- `MP_BUFL1`: mirror-load master, PMOS diode on `vbh`, source vdd.
- `MP_BUFL2`: mirror-load slave, gate `vbh`, drain `vdump`, source vdd.
Net current into vdump = (Ip2 - Ip1) + (In1 - In2) (Ip = PMOS pair sides
1/2, In = NMOS pair sides 1/2): vctrl up raises Ip2 and In1 and lowers
Ip1 and In2, so vdump follows vctrl; vdump up does the reverse
(negative feedback). Balance is at vdump = vctrl.
Nodes: `vbt`, `vnt`, `vbl`, `vbh` new; `vdump` is now driven. The only rewire is that
`vdump` gets a driver; MP_UPD / MN_DND and everything else keep their
connections. Gate load added to `vctrl` is one PMOS gate (fF), no DC path,
so vctrl_leak_off_na and the loop-filter poles are untouched.

### 4. Loop filter
- `R1`: ppolyf_u, `vctrl` -> `vfilt`, ~26 kOhm (150 um; was 22 kOhm, raised 09-29 for PM >= 45 deg).
- `C1`: cap_mim_2f0ff, `vfilt` -> vss, ~20 pF (100 x 100 um: the area risk).
- `C2`: cap_mim_2f0ff, `vctrl` -> vss, ~2 pF, sized with the ua[0] pad
  (up to 5 pF) in parallel.

### 5. VCO control: V-to-I with floor current
- `MN_V2I`: gate `vctrl`, source `vs`, drain `vi`. `R_V2I`: ppolyf_u,
  `vs` -> `vcl` (source degeneration; linearises Kvco).
- `MN_CL`: gate `vref`, source vss, drain `vcl` (bottom of R_V2I).
  Current ceiling: a (W/L)cl/(W/L)MN_B1 copy of Ib. In triode at low I_ctl
  (adds ~1-2 kOhm), saturates at Imax and caps fmax at ff/-40 C/3.63 V
  (R_V2I alone did not: the ring ran at ~390-400 MHz there).
- `MN_FLR`: gate `vref`, source vss, drain `vi`. Adds Ifloor (a copy of
  Ib) so the ring never stalls at vctrl = 0.3 V.
- `MN_ENS`: enable cascode, gate `pll_en`, `vi` -> `vbp_vco` (cuts the V2I
  path in standby while vctrl is still held by the filter).
- `MP_V2I`: PMOS diode on `vbp_vco` (PMOS starve rail).
- `MP_V2IB`: gate `vbp_vco`, drain `vbn_vco`. `MN_V2IM`: NMOS diode on
  `vbn_vco` (NMOS starve rail).
- Standby: `MP_ENV` (gate `pll_en`, `vbp_vco` -> vdd), `MN_ENV` (gate
  `en_b`, `vbn_vco` -> vss).

### 6. Ring, 5 stages (r1 -> r2 -> r3 -> r4 -> r5 -> r1)
Per stage k = 1..5: `MP_Sk` (PMOS starve, gate `vbp_vco`, source vdd),
`MP_Ik` / `MN_Ik` (inverter core, input r(k-1), output rk), `MN_Sk` (NMOS
starve, gate `vbn_vco`, source vss). 20 devices: `MP_S1..MP_S5`,
`MP_I1..MP_I5`, `MN_I1..MN_I5`, `MN_S1..MN_S5`.

### 7. Output buffer with hold
- NAND2(`r5`, `pll_en`) -> `nb`: `MP_G1` (gate r5), `MP_G2` (gate pll_en)
  in parallel to `nb`; `MN_G1` (gate r5), `MN_G2` (gate pll_en) in series
  to vss.
- `MP_O` / `MN_O`: inverter `nb` -> `vco_out`, sized for 15 fF, tr/tf <=
  0.15 ns. With pll_en = 0 vco_out is held at 0 (vco_out_off_toggles).

Device count: 2 + 12 + 23 + 10 + 3 + 10 + 20 + 6 = 86.

## Design equations (sized against at P4 / optimise)

Mirror (template header, square law, starting point only):
- Vgs = Vth + sqrt(2 I / (u Cox (W/L)));  Iout/Iref = (W/L)out / (W/L)ref;
  Vov = Vgs - Vth sets Vds,min; ro ~ 1/(lambda I).
- Template bounds: L 0.28..2 um, W 1..50 um, ratio 1..10, Iref 1..100 uA.
  Trim legs use units of one W (4 = m=4 or 4 W), ratio <= 8 : 1 to the
  master. Long L (>= 1 um) on MP_UPS/MN_DNS and their references for
  UP/DN matching over vctrl 0.3..vdd-0.3 V (cp_updn_mismatch_pct <= 10).

Bias:
- Ib = Vgs(MN_B1) / R_BIAS, R_BIAS = RBIAS_TOP + (RBIAS_INT || R_ext).
  Target Ib = 5 uA, so RBIAS_TOP + RBIAS_INT ~ 0.9 V / 5 uA = 180 kOhm
  (ppolyf_u 350 ohm/sq: ~510 sq); split 110 um / 430 um (38 / 151 kOhm).
- Bias MN_B1 at its zero-temperature-coefficient current density (Vth's
  -2 mV/K against Vov's mobility rise) so Ib's tempco is R's alone
  (ppolyf_u tc1 = -0.9e-4/K).
- Loop: V(vref) up -> I(MN_B1) up -> va down -> I(MN_B2) down ->
  V(vref) down: negative feedback. But MN_B2's current also returns to va
  through the MP_B2/MP_B1 mirror with gain 1 (positive loop). The
  admittance into va is Y = gm2 (gm1 Zs - 1) / (1 + gm2 Zs), Zs the
  impedance at MN_B2's source. With the pad cap Cp directly on the loop
  node, |Zs| < 1/gm1 above gm1 / (2 pi Cp) ~ 1.6 MHz, va sees a negative
  conductance and the loop relaxes. P4 saw exactly this with 5 pF on the
  pad: vco period 17 -> 200 ns sawtooth every ~1.3 us, bias 0.68..1.33 V,
  startup re-firing. The fix is RBIAS_TOP: Zs = RTOP + RINT || 1/(s Cp),
  so Re(Zs) >= RTOP at every frequency and Re(Y) > 0 when gm1 gm2 RTOP^2 > 1
  (gm1 RTOP ~ 2 here), for any pad capacitance. Compensation caps were
  tried instead (va-vss, va-vref Miller, vbp-vdd, 2..40 pF); they either
  did not stop the relaxation or left a >1 us settling tail. Pad settling:
  (RTOP || RINT) x Cp ~ 0.15 us, with Ib briefly high (up to Vgs/RTOP) while
  the pad charges.
- Startup: MP_STL current ~100-300 nA; MN_STD must sink it at
  Vgs = V(vref) ~ 0.9 V.

Charge pump:
- Icp(code) = (4 + t0 + 2 t1 + t0 t1) Ib, code 00 = 4 Ib = 20 uA;
  icp_code11_over_code00 = 8/4 = 2.
- Overlap charge: Q = (Iup - Idn) x 1 ns + (C_up_s x dV_up_s - C_dn_s x
  dV_dn_s); bound 10 fC. Switch nodes up_s/dn_s must be minimum area.
- Off leak (<= 1 nA at 125 C): MP_UP and MN_DN at minimum W, L above
  minimum; MIM leak is ~1e-14 S for 20 pF (negligible).

Dump-node buffer (diff_pair.sp header, square law, starting point only):
- gm = sqrt(2 u_p Cox (W/L) Itail/2) per input device; loop gain in
  unity-gain feedback Av = gm (ro_p || ro_n) (mirror load in place of R),
  so the static error of vdump against vctrl is (vctrl - vdump)/Av plus the
  systematic offset from the mirror's Vds imbalance (diode side sits at
  Vgs_n, output side at vdump): long L (>= 1 um) on MN_BUFL1/2 and on the
  pair, matched W, for offset in the few-mV class. Residual charge share is
  C(up_s, dn_s) x offset: fF x mV = aC, far under the 10 fC bound.
- Input common-mode range, at the worst case vdd - 10 % = 2.97 V and the
  range 0.3..vdd - 0.3 = 0.3..2.67 V (template header: lower limit
  Vov_tail + Vth + Vov_pair, upper limit vdd - the same):
  PMOS pair: below vss up to vdd - Vov_tail - |Vth_p| - |Vov_p| ~
  2.97 - 0.1 - 0.75 - 0.1 ~ 2.0 V (large W/L on the pair for a small
  |Vov_p|). NMOS pair: Vov_tail + Vth_n + Vov_n ~ 0.9 V up to above vdd.
  Union: rail to rail; overlap ~0.9..2.0 V where both pairs conduct and
  the loop gm doubles. All three bench points (0.3 V, vdd/2, vdd - 0.3 V)
  sit inside at every supply corner; at 3.3 V the limits are ~2.3 V and
  ~0.9 V. The gm step across the overlap edges changes the loop gain by
  2x, not the balance point, so the offset stays in the few-mV class.
- Output range: Vov_n (MN_BUFL2 saturated) up to vdd - |Vov_p|
  (MP_BUFL2 saturated), wider than 0.3..vdd - 0.3.
- Drive: source and sink limited to the active pair's Itail (class A),
  2 x Itail in the overlap region; Itail = ratio x Ib per pair, ratio 2..8
  (bound from current_mirror.sp's ratio <= 10, and the idle UP/DN
  imbalance of <= 10 % Icp it must absorb at every code plus a lone pulse
  at code 00). Nominal ratio 4 = 20 uA per tail, 40 uA total added in run.
- Settling: single stage into the switch-node parasitics (tens of fF),
  dominant pole at the output, unity-gain stable with no compensation;
  tau ~ C_vdump/gm ~ 50 fF / 100 uS = 0.5 ns. Speed is not the point -
  it only has to hold a DC level between PFD events.
- Template bounds (pfet_03v3/nfet_03v3): L 0.28..1 um pairs (use the top
  of the range), W 2..40 um pairs, Itail 5..200 uA per pair; loads
  L 0.28..2 um, W 1..50 um. The two mirror loads carry each other's
  pair current only through vdump, so match each mirror internally
  (same L, same W) rather than PMOS-to-NMOS.

Loop filter / open loop (ideal 1/(P N) divider):
- H(s) = (Icp/2pi) Kvco Z(s) / (s P N), Z(s) = (1 + s R1 C1) / (s (C1 + C2)
  (1 + s R1 C1 C2/(C1 + C2))).
- wn = sqrt(Icp Kvco / (P N C1)),  zeta = (R1/2) sqrt(Icp Kvco C1 / (P N)).
  Icp 20 uA, Kvco 2 pi 100 MHz/V, P N = 16, C1 20 pF -> fn ~ 400 kHz,
  R1 ~ 26 kOhm -> zeta ~ 0.65. C2 (+ pad) <= C1/10 for PM >= 45 deg
  without pad; with 5 pF pad PM ~ 32 deg (warning-level measure).

VCO:
- I_ctl = Ifloor + (vctrl - Vth - Vs)/R_V2I (degenerated V2I; Vs = I R_V2I),
  Ifloor = Ib x (W/L)MN_FLR/(W/L)MN_B1.
- td ~ C_node vdd / (2 I_ctl),  f = 1/(2 N td) = I_ctl / (N C_node vdd),
  N = 5. Kvco = df/dvctrl ~ f_mid / (vctrl_mid - Vth) ~ 100-150 MHz/V at
  mid range; fmin (vctrl = 0.3 V) set by Ifloor <= 50 MHz; fmax at tt
  (vctrl = 3.0 V) >= 200 MHz; fmax at ff/-40 C/3.63 V <= 333 MHz is what
  MN_CL's ceiling holds it: I_ctl <= Ifloor + Ib (W/L)MN_CL/(W/L)MN_B1,
  which tracks Ib (~1.15x at ff/-40 C) rather than vctrl/R_V2I (~1.6x).
- Buffer: tr/tf = 0.15 ns into 15 fF + NAND self-load; Wp/Wn ~ 2-3 for
  duty 40..60 %.

Standby (pll_en = 0): vbp, vcp_p, vbp_vco at vdd; va, vref, bias_ref, vcp_n,
vbn_vco, vst at vss; MN_ENS open; MP_BUFT and MN_BUFT off (vbt, vnt, vbl,
vbh, vdump float, no DC path); NAND forces vco_out = 0. Only device
leakage remains (standby_current_ua <= 1).

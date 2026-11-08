* two_stage_miller_ota.sp - two-stage Miller-compensated OTA (docs/design.md
* 5's topology library; issue #100). The first block in the library with a
* feedback loop: it is used closed loop (here, unity gain), so its spec sets
* `feedback: true` and bounds the loop's phase margin at every corner
* (skills/ade/templates/stb_tb.cir, psrr_tb.cir, noise_tb.cir). corpus/ade/
* ota/ is this template sized and benched.
*
* TOPOLOGY: stage 1 is diff_pair.sp's NMOS pair (xm1 gate inn, xm2 gate inp)
* on a mirrored tail xmt, with a PMOS mirror load xm3 (diode) / xm4 instead
* of resistors, single-ended at d2. Stage 2 is a PMOS common source xm6
* (gate d2) with an NMOS current sink xm7. xmb is the bias diode the ibias
* pin's current flows into; xmt and xm7 mirror it. The Miller cap xcc spans
* stage 2, d2 to out.
*
*        vdd ----+--------+-----------+
*               xm3(d)   xm4         xm6
*                |        |           |
*               d1       d2 ---xcc--- out
*                |        |           |
*      inn ---- xm1      xm2 ---- inp xm7 --- ibias
*                 \      /            |
*                  tail              vss
*                   |
*        ibias --- xmt              (xmb: ibias diode to vss)
*
* DESIGN EQUATIONS (square-law starting point; the benches decide):
*   gm1 = sqrt(2 * un*Cox * (W/L)_1 * Itail/2)      Itail = Ib * w_t/w_b
*   GBW = ugf ~= gm1 / (2*pi*Cc)
*   p2  ~= gm6 / (2*pi*CL)    (the output pole, split away by Cc)
*   z   ~= gm6 / (2*pi*Cc)    (right-half-plane zero: costs phase)
*   PM  ~= 90 - atan(ugf/p2) - atan(ugf/z): for ~60 deg, p2 >= ~2.2*ugf,
*          i.e. gm6 >= ~2.2*gm1*CL/Cc, and gm6 >> gm1 keeps the zero far out.
*   A0  = gm1*(ro2||ro4) * gm6*(ro6||ro7)
*   Systematic offset is ~0 when xm6's current density matches xm3/xm4's:
*     (W/L)_6 / (W/L)_4 = 2 * (W/L)_7 / (W/L)_t
*   PSRR at DC tracks A0; it falls about 20 dB/decade from the first
*   pole, so the band where it matters is a spec statement (psrr_at_f_db).
*   Output noise in unity gain is the input-referred noise; it is set by
*   gm1 and the load mirror (gm3/gm1 small helps) and by the flicker of
*   xm1-xm4 at low frequency (larger W*L).
*
* SIZING BOUNDS (3.3 V devices, this PDK; the sizing.yaml ranges):
*   w_b 1-20 um, w_t 2-40 um, l_b 0.5-4 um     bias diode, tail, sink length
*   w_in 2-50 um, l_in 0.28-4 um                 input pair
*   w_ld 1-20 um, l_ld 0.5-4 um                  mirror load
*   w_2 10-200 um, w_s 10-200 um, l_2 0.28-4 um  stage 2 (xm6, xm7 width)
*   cc_w 5-50 um                                 MIM side; C = 2 fF/um^2 * cc_w^2
*   Cc is the knob for PM: too small and PM falls (the fast corners first,
*   where gm1 is up); none at all and the loop rings. Cc too large costs
*   UGF and slew (Itail/Cc).
*
* PARAMETERS: w_b w_t l_b w_in l_in w_ld l_ld w_2 w_s l_2 cc_w (corpus/ade/
* ota/sizing/sizing.yaml holds a sizing that meets corpus/ade/ota/spec.yaml
* at every default corner).

.param w_b=4e-6 w_t=8e-6 l_b=1e-6 w_in=10e-6 l_in=1e-6 w_ld=4e-6 l_ld=1e-6
.param w_2=80e-6 w_s=80e-6 l_2=1e-6 cc_w=32e-6

.subckt two_stage_miller_ota inp inn out vdd vss ibias
xmb  ibias ibias vss  vss nfet_03v3 w=w_b  l=l_b
xmt  tail  ibias vss  vss nfet_03v3 w=w_t  l=l_b
xm1  d1    inn   tail vss nfet_03v3 w=w_in l=l_in
xm2  d2    inp   tail vss nfet_03v3 w=w_in l=l_in
xm3  d1    d1    vdd  vdd pfet_03v3 w=w_ld l=l_ld
xm4  d2    d1    vdd  vdd pfet_03v3 w=w_ld l=l_ld
xm6  out   d2    vdd  vdd pfet_03v3 w=w_2  l=l_2
xm7  out   ibias vss  vss nfet_03v3 w=w_s  l=l_b
xcc  d2    out   cap_mim_2f0_m4m5_noshield c_width=cc_w c_length=cc_w
.ends two_stage_miller_ota

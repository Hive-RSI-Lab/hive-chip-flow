# topology - r2r_dac_analog (P2, TOPOLOGY MODE)

Template: `skills/ade/reference/topologies/r2r_ladder.sp` (the only
ladder template). Used for its design equations and ratio-by-geometry
rule, not its skeleton, which differs from this spec in three ways:

1. It is 4-bit; this block is 8-bit.
2. Its orientation is the dual form (bit -> R -> spine, 2R spine
   segments). This block uses the standard R-2R form the spec names:
   bit -> 2R branch -> rung node, R series between rungs, 2R termination
   to vss at the LSB end, vout at the MSB rung.
3. It uses `rm1` with ideal bit sources and has no switch devices. This
   block uses `ppolyf_u` (spec) plus eight standard-cell driver cells
   (spec). The template has no driver devices; see OPEN in the P2 report.

## Subckt and pin order

```
.subckt r2r_dac_analog dac_code[7] dac_code[6] dac_code[5] dac_code[4]
+ dac_code[3] dac_code[2] dac_code[1] dac_code[0] vout vdd vss
```

Internal nodes: `d7..d0` (driver outputs), `n7..n0` (rung nodes, `n7` =
`vout`), `mb7..mb0` and `mt` (midpoints of the two-unit 2R strings).

```
 d7 --2R-- n7(vout)
            |R
 d6 --2R-- n6
            |R
   ...     ...
            |R
 d0 --2R-- n0 --2R-- vss   (termination)
```

## Devices (refdes and role)

Drivers (the only active devices; one cell type for all eight):

- `xd7`..`xd0`: `gf180mcu_fd_sc_mcu7t5v0__buf_20` (largest buf in the
  library: buf_1/2/3/4/8/12/16/20), I = `dac_code[k]`, Z = `dk`, VDD/VNW
  = vdd, VSS/VPW = vss. buf_20 over buf_16 for the lower Ron; P4 measures
  Ron_pu and Ron_pd at tt and records them here.

Resistors, all `ppolyf_u`, same W and nominal L (one "unit" = R):

- `xrb7a`,`xrb7b` .. `xrb0a`,`xrb0b` (16): the 2R branch of bit k, two
  units in series `dk - mbk - nk`. The `b` unit is the one trimmed by
  Ron_nom (shortened), `a` stays a full unit.
- `xrs6`..`xrs0` (7): series R, `xrsk` between `n(k+1)` and `nk`.
- `xrta`,`xrtb` (2): LSB termination 2R, `n0 - mt - vss`, two full units
  (no driver in series, so no trim).

Total: 8 drivers + 25 unit resistors = 33 devices.

## Design equations (from the template header, generalised to N = 8)

- Ideal output: vout = VDD * sum_k b_k 2^(k-8) = code/256 * VDD.
- 1 LSB = VDD / 2^N = 12.89 mV at 3.3 V.
- INL/DNL are set entirely by the realised R:2R ratio; worst DNL is at the
  MSB transition (127 -> 128), where an MSB-branch ratio error of e gives
  about 128*e LSB, so e must stay well under 0.4 % for 0.5 LSB.
- Ratio by geometry: same W for every unit, 2R = two units in series, so
  rsh (and its corner) cancels in the ratio. ppolyf_u model:
  R = 2 * 60 * 1u / W_eff (terminals, tc1 = -1.47e-3)
      + rsh * (L - 2 dl) / W_eff (body, rsh 350, tc1 = -0.9e-4),
  W_eff = W - 2 * 25.5 nm. Series units keep the terminal share in ratio
  too; only the trim unit breaks it.
- Unit sizing (starting point, P4 sizes): R = 250 ohm at W = 2 um:
  terminals 2 * 60/1.949 = 62 ohm, body 188 ohm -> L ~ 1.05 um, above the
  PDK drawn minimum 0.8 um (klayout pcell), so no parallel units needed.
  Template bounds (W 1-4 um) hold; its L range (2-20 um) is rm1-specific
  and does not apply to a 250 ohm ppolyf_u unit.
- Driver absorption: trimmed branch = 2R - Ron_nom, Ron_nom = (Ron_pu +
  Ron_pd)/2 at tt; trim is the `b` unit's length reduced by
  Ron_nom * W_eff / rsh.
- Output resistance R_out = R + ~Ron/2; tau = (R_out + R_pad) * C_load
  = (250 + ~30 + 100) * 7 pF ~ 2.7 ns; settle to 0.5 LSB = ln(512) tau
  ~ 17 ns < 20 ns.
- Supply: each driver sees ~3R = 750 ohm; full-scale termination current
  VDD/(2R-ish chain) ~ 6.6 mA; budget <= 15 mA average.

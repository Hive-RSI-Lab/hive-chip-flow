// pll_prescaler - /8 ripple prescaler on vco_out plus the /16 observation flop.
// Domain: vco_out (pre_q0), rippled on rising edges of each stage's Q.
// Each stage's toggle loop is one async-reset flop and one inverter.
// The first stage is hand-instantiated (u_pre_ff0, u_pre_inv0; must_keep,
// resizer dont_touch): dffrnq_2 + inv_1 is the fastest reset toggle loop in
// the library at ss (~3.4 ns at ss_125C_3v00, Liberty, against 3.7 ns for
// dffrnq_4), and the loop has to keep up with the VCO's acquisition peak
// at ss_pff (REQ-TIM-VCO). Stage 1 is clocked through its own buffer
// u_pre_ckbuf0, where the SDC puts the generated clock pre_q0: pre_q0's Q
// net then carries only the inverter and that buffer, and CTS builds its
// tree after the buffer instead of on the toggle loop. In simulation and
// formal it is the plain flop and stage 1 is clocked by pre_q0 itself.
`default_nettype none
module pll_prescaler (
    input  wire vco_out,
    input  wire rst_n,
    output wire clk_pre,   // vco_out / 8
    output wire obs_q3     // vco_out / 16 (clk_pre / 2)
);
  reg pre_q1, pre_q2, q3;

`ifdef FORMAL
  reg pre_q0;
  wire pre_c0 = pre_q0;
  always @(posedge vco_out or negedge rst_n)
    if (!rst_n) pre_q0 <= 1'b0;
    else        pre_q0 <= ~pre_q0;
`elsif SYNTHESIS
  wire pre_q0;
  wire pre_d0;
  wire pre_c0;
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__dffrnq_2 u_pre_ff0 (
      .CLK(vco_out), .D(pre_d0), .RN(rst_n), .Q(pre_q0));
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__inv_1 u_pre_inv0 (.I(pre_q0), .ZN(pre_d0));
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__clkbuf_1 u_pre_ckbuf0 (.I(pre_q0), .Z(pre_c0));
`else
  reg pre_q0;
  wire pre_c0 = pre_q0;
  always @(posedge vco_out or negedge rst_n)
    if (!rst_n) pre_q0 <= 1'b0;
    else        pre_q0 <= ~pre_q0;
`endif

  always @(posedge pre_c0 or negedge rst_n)
    if (!rst_n) pre_q1 <= 1'b0;
    else        pre_q1 <= ~pre_q1;

  always @(posedge pre_q1 or negedge rst_n)
    if (!rst_n) pre_q2 <= 1'b0;
    else        pre_q2 <= ~pre_q2;

  always @(posedge pre_q2 or negedge rst_n)
    if (!rst_n) q3 <= 1'b0;
    else        q3 <= ~q3;

  assign clk_pre = pre_q2;
  assign obs_q3  = q3;
endmodule
`default_nettype wire

`ifdef SYNTHESIS
`ifndef FORMAL
// Port stubs of the gf180mcu cells, as for the PFD's delay cells.
(* blackbox *)
module gf180mcu_fd_sc_mcu7t5v0__dffrnq_2 (
    input  wire CLK,
    input  wire D,
    input  wire RN,
    output wire Q
);
endmodule

(* blackbox *)
module gf180mcu_fd_sc_mcu7t5v0__inv_1 (
    input  wire I,
    output wire ZN
);
endmodule

(* blackbox *)
module gf180mcu_fd_sc_mcu7t5v0__clkbuf_1 (
    input  wire I,
    output wire Z
);
endmodule
`endif
`endif

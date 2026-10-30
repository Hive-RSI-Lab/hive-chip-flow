// pll_pfd - tri-state phase-frequency detector.
// pfd_ref_q (clk) and pfd_fb_q (clk_fb) are D = 1 flops cleared
// asynchronously by ~rst_n OR pfd_rst; pfd_rst is AND(up, dn) through two
// kept delay cells (anti dead-zone). In synthesis the cells are the
// hand-instantiated gf180mcu delay cells pfd_dly0/pfd_dly1 (must_keep); in
// simulation they are zero-delay (the reset width is a cosim measure).
`default_nettype none
module pll_pfd (
    input  wire clk,      // reference
    input  wire clk_fb,   // feedback divider output
    input  wire rst_n,
    output wire pfd_up,
    output wire pfd_dn
);
  reg  pfd_ref_q, pfd_fb_q;
  wire pfd_both = pfd_ref_q & pfd_fb_q;
  (* keep *) wire pfd_dly_mid;
  (* keep *) wire pfd_rst;
  wire pfd_clr = ~rst_n | pfd_rst;

`ifdef SYNTHESIS
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__dlya_1 pfd_dly0 (.I(pfd_both),    .Z(pfd_dly_mid));
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__dlya_1 pfd_dly1 (.I(pfd_dly_mid), .Z(pfd_rst));
`else
  assign pfd_dly_mid = pfd_both;
  assign pfd_rst     = pfd_dly_mid;
`endif

  always @(posedge clk or posedge pfd_clr)
    if (pfd_clr) pfd_ref_q <= 1'b0;
    else         pfd_ref_q <= 1'b1;

  always @(posedge clk_fb or posedge pfd_clr)
    if (pfd_clr) pfd_fb_q <= 1'b0;
    else         pfd_fb_q <= 1'b1;

  assign pfd_up = pfd_ref_q;
  assign pfd_dn = pfd_fb_q;
endmodule
`default_nettype wire

`ifdef SYNTHESIS
// Port stub of the gf180mcu delay cell so a plain read_verilog flow (no
// read_liberty -lib) can elaborate pll_pfd; yosys ignores a blackbox
// re-definition when the liberty cell is already loaded.
(* blackbox *)
module gf180mcu_fd_sc_mcu7t5v0__dlya_1 (
    input  wire I,
    output wire Z
);
endmodule
`endif

// ihsan_sa_pll - digital core of the TT GF180 charge-pump PLL.
// Flat top: prescaler (vco_out), divider (clk_pre), PFD (clk / clk_fb),
// lock detector and recovery (clk), control registers (clk), plus the
// obs_out mux and the recovery override on pfd_up/pfd_dn.
// The charge pump, loop filter, VCO and bias are the analog macro.
// clk_fb and obs_out leave through hand-instantiated, dont_touch clkbuf_8
// cells (must_keep u_clk_fb_obuf/u_obs_obuf): both are clock nets the
// resizer never repairs, and a buf_1 port buffer left them at 3.48 ns slew
// at ss. The PFD's clk_fb pin has its own net (clk_fb_int), off the port
// load. In simulation the buffers are plain assigns.
`default_nettype none
module ihsan_sa_pll (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [2:0] n_sel,
    input  wire       vco_out,
    input  wire       pll_en_in,
    input  wire       obs_sel,
    input  wire [1:0] cp_trim_in,
    output wire       obs_out,
    output wire       clk_fb,
    output wire       lock,
    output wire       pfd_up,
    output wire       pfd_dn,
    output wire       pll_en,
    output wire       cp_trim0,
    output wire       cp_trim1
`ifdef FORMAL
    // formal only: the harness's view of recovery (REQ-RECOVER)
    , output wire     recover_fv
`endif
);
  wire clk_pre;
  wire obs_q3;
  wire clk_fb_int;
  wire obs_int;
  wire pfd_up_q;
  wire pfd_dn_q;
  wire recover;

  pll_prescaler u_pre (
      .vco_out(vco_out), .rst_n(rst_n), .clk_pre(clk_pre), .obs_q3(obs_q3));

  pll_divider u_div (
      .clk_pre(clk_pre), .rst_n(rst_n), .n_sel(n_sel), .clk_fb(clk_fb_int));

  pll_pfd u_pfd (
      .clk(clk), .clk_fb(clk_fb_int), .rst_n(rst_n),
      .pfd_up(pfd_up_q), .pfd_dn(pfd_dn_q));

  // recovery holds the pump in DN (REQ-RECOVER)
  assign pfd_up = pfd_up_q & ~recover;
  assign pfd_dn = pfd_dn_q | recover;

  pll_lock_det u_lock (
      .clk(clk), .rst_n(rst_n), .pll_en(pll_en), .pfd_up(pfd_up),
      .pfd_dn(pfd_dn), .lock(lock), .recover(recover));

  pll_ctrl_regs u_ctrl (
      .clk(clk), .rst_n(rst_n), .pll_en_in(pll_en_in),
      .cp_trim_in(cp_trim_in), .pll_en(pll_en),
      .cp_trim0(cp_trim0), .cp_trim1(cp_trim1));

  // quasi-static select, not glitch-protected (spec Architecture)
  assign obs_int = obs_sel ? obs_q3 : clk_pre;

`ifdef FORMAL
  // yosys defines SYNTHESIS for formal too; a blackbox buffer there would
  // leave clk_fb and obs_out as free outputs
  assign clk_fb  = clk_fb_int;
  assign obs_out = obs_int;
  assign recover_fv = recover;
`elsif SYNTHESIS
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 u_clk_fb_obuf (.I(clk_fb_int), .Z(clk_fb));
  (* keep *) gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 u_obs_obuf    (.I(obs_int),    .Z(obs_out));
`else
  assign clk_fb  = clk_fb_int;
  assign obs_out = obs_int;
`endif
endmodule
`default_nettype wire

`ifdef SYNTHESIS
// Port stub of the gf180mcu clock buffer, as for the PFD's delay cells.
(* blackbox *)
module gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 (
    input  wire I,
    output wire Z
);
endmodule
`endif

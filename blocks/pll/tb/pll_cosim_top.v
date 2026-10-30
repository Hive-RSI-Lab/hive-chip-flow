`timescale 1ns/1ps
// pll_cosim_top - top of the msde cosim bench for the charge-pump PLL.
// The digital side is the released RTL itself (tb/*.v symlinks into
// ../digital/rtl, compiled by Icarus as-is, SIM path: zero-delay PFD reset).
// The analog side is a SPICE subcircuit run by ngspice through
// cocotbext-ams's MixedSignalBridge (test_pll_cosim.py). The crossing nets
// carry their interface.yaml names at this top level, so the bridge binds
// them by name: vco_out (a2d) is a reg nothing in Verilog assigns - the
// bench writes it with the digitized ngspice waveform at each sync point
// where it changes (test_pll_cosim.py, Bridge); pfd_up, pfd_dn, pll_en,
// cp_trim0, cp_trim1 (d2a) are driven by the RTL and read by the bridge
// into ngspice sources.
module pll_cosim_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [2:0] n_sel,
    input  wire       pll_en_in,
    input  wire       obs_sel,
    input  wire [1:0] cp_trim_in,
    output reg        vco_out,
    output wire       pfd_up,
    output wire       pfd_dn,
    output wire       pll_en,
    output wire       cp_trim0,
    output wire       cp_trim1,
    output wire       clk_fb,
    output wire       lock,
    output wire       obs_out
);
  ihsan_sa_pll u_dig (
      .clk(clk), .rst_n(rst_n), .n_sel(n_sel), .vco_out(vco_out),
      .pll_en_in(pll_en_in), .obs_sel(obs_sel), .cp_trim_in(cp_trim_in),
      .obs_out(obs_out), .clk_fb(clk_fb), .lock(lock),
      .pfd_up(pfd_up), .pfd_dn(pfd_dn), .pll_en(pll_en),
      .cp_trim0(cp_trim0), .cp_trim1(cp_trim1));
endmodule

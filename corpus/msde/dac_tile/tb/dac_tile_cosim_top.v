`timescale 1ns/1ps
// Cosim top for dac_tile: the digital side (tb/dac_tile_digital.v is a
// symlink to ../digital/rtl, never a copy) drives bmsb/blsb into the analog
// cell that cosimlib's CosimBridge simulates in ngspice
// (dac_tile_ideal.sp). The digital side runs as the tile ships it:
// tt_frame_dac_tile_digital (built by check_cosim, ttlib.write_sim_frame)
// is the generated TT wrapper around dac_tile_digital, its pad reset
// synchroniser in front of rst_n, with dac_tile_digital's own ports.
// `vout` is written by the bridge as its digitized (mid-rail) value, so it
// is a reg nothing here drives (cosimlib's write-on-change); a two-flop
// synchronizer on clk is the digital side acting on it, and its
// edges are the bench's toggle evidence.
module dac_tile_cosim_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [1:0] code_in,
    output wire       bmsb,
    output wire       blsb,
    output wire [1:0] code,
    output reg        vout,
    output reg        vout_sync
);
  reg vout_meta;
  initial begin
    vout_meta = 1'b0;
    vout_sync = 1'b0;
  end

  tt_frame_dac_tile_digital digital (
      .clk(clk), .rst_n(rst_n), .code_in(code_in), .bmsb(bmsb),
      .blsb(blsb));

  assign code = {bmsb, blsb};

  always @(posedge clk) begin
    vout_meta <= vout;
    vout_sync <= vout_meta;
  end
endmodule

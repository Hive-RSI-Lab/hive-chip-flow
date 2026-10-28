`timescale 1ns/1ps
// Cosim top for r2r_dac: the released digital front end (tb/r2r_dac_digital.v
// is a symlink to ../digital/rtl, never a copy) drives dac_code into the
// analog block that cocotbext-ams's MixedSignalBridge simulates in ngspice (r2r_ideal.sp).
// `vout` is the macro's analog pin, forced here by the bridge as its
// digitized (mid-rail threshold) value; a two-flop synchronizer on clk is
// the digital side acting on that ngspice-computed waveform, and its edges
// are the bench's toggle evidence.
module r2r_dac_cosim_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [7:0] ui_in,
    input  wire [1:0] mode,
    input  wire       slow,
    output wire [7:0] dac_code,
    output wire [7:0] uo_out,
    output wire       vout,
    output reg        vout_sync
);
  reg vout_meta;
  initial begin
    vout_meta = 1'b0;
    vout_sync = 1'b0;
  end

  r2r_dac_digital digital (
      .clk(clk), .rst_n(rst_n), .ena(ena), .ui_in(ui_in), .mode(mode),
      .slow(slow), .dac_code(dac_code), .uo_out(uo_out));

  always @(posedge clk) begin
    vout_meta <= vout;
    vout_sync <= vout_meta;
  end
endmodule

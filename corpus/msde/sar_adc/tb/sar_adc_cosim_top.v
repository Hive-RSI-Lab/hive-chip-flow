`timescale 1ns/1ps
// Cosim top for sar_adc: the SAR logic (sar_adc_digital.v) drives dac_code,
// sample and comp_clk into the analog side (S/H, DAC, comparator) that
// cosimlib's CosimBridge simulates in ngspice (sar_adc_ideal.sp), and
// decides each bit on the cmp_out the comparator gives back. cmp_out is
// written by the bridge on change, so it is a reg nothing here drives.
module sar_adc_cosim_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    output wire [7:0] dac_code,
    output wire       sample,
    output wire       comp_clk,
    output wire       done,
    output wire [7:0] result,
    output reg        cmp_out
);
  initial cmp_out = 1'b0;

  sar_adc_digital digital (
      .clk(clk), .rst_n(rst_n), .start(start), .cmp_out(cmp_out),
      .dac_code(dac_code), .sample(sample), .comp_clk(comp_clk),
      .done(done), .result(result));
endmodule

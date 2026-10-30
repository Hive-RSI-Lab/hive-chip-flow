/*
 * Copyright (c) 2026 the author of ihsan-sa/ece298a-counter
 * SPDX-License-Identifier: Apache-2.0
 *
 * ECE 298A - 8-bit programmable binary counter with
 *   - asynchronous, active-low reset
 *   - synchronous parallel load
 *   - count enable
 *   - tri-state output bus
 *
 * Pin map (see info.yaml / docs/info.md):
 *   uio[7:0]  bidirectional data bus.
 *             oe = 1 -> uio_oe = 8'hFF, the bus drives Q[7:0]
 *             oe = 0 -> uio_oe = 8'h00, the bus is released (Hi-Z) and is
 *                       sampled as the parallel load data D[7:0]
 *   ui[0]     load   - synchronous parallel load, active high (beats count);
 *                        ignored while oe = 1, when the bus is an output
 *   ui[1]     enable - count enable, active high
 *   ui[2]     oe     - tri-state output enable, active high
 *   ui[7:3]   unused, tie low
 *   uo[7:0]   Q[7:0] mirror, always driven so the count stays observable
 *             while the bidirectional bus is in Hi-Z
 */

`default_nettype none

module ece298a_counter (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // ---------------------------------------------------------------------
  // Control decode
  // ---------------------------------------------------------------------
  wire load   = ui_in[0];  // synchronous parallel load, active high
  wire enable = ui_in[1];  // count enable, active high
  wire oe     = ui_in[2];  // tri-state output enable, active high

  // Parallel load data arrives on the shared bidirectional bus. It is only
  // meaningful while oe is low, because that is the only time the pads are
  // configured as inputs. While oe is high the GF180 pad has its input side
  // disabled (the IO cell lists input + output enabled together as
  // "disallowed"), so uio_in is undefined; a load is therefore ignored
  // while the design is driving the bus.
  wire [7:0] load_data = uio_in;
  wire       do_load   = load & ~oe;

  // ---------------------------------------------------------------------
  // The counter itself
  //
  // Asynchronous reset: rst_n appears in the sensitivity list, so the count
  // clears the instant rst_n falls, without waiting for a clock edge.
  // Everything else happens on the rising clock edge only.
  //
  // Priority: reset > load (only when oe = 0) > count > hold.
  // ---------------------------------------------------------------------
  reg [7:0] count;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= 8'h00;          // asynchronous clear
    end else if (do_load) begin
      count <= load_data;      // synchronous load, beats counting; only with oe = 0
    end else if (enable) begin
      count <= count + 8'd1;   // count up, wraps 255 -> 0
    end
    // else: hold. count keeps its value.
  end

  // ---------------------------------------------------------------------
  // Outputs. Every output is driven in every state, so nothing infers a latch.
  // ---------------------------------------------------------------------

  // Tri-state bus: the TT harness turns uio_oe = 0 into a high-impedance pad.
  assign uio_oe  = {8{oe}};        // 8'hFF when oe, 8'h00 otherwise
  assign uio_out = count;          // value presented when the pads are enabled

  // Always-driven mirror of the count, so Q is observable even in Hi-Z.
  assign uo_out  = count;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in[7:3], 1'b0};

endmodule

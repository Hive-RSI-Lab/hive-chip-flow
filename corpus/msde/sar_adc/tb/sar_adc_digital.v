`timescale 1ns/1ps
// sar_adc_digital - the SAR control logic (spec.md "What the block does").
// While `start` is high it converts back to back, a fixed CONV_CLOCKS = 20
// clocks per conversion, so the conversions sample the input at a fixed
// rate (the bench's fs):
//   TRACK  4 clocks, sample = 1 (the analog S/H tracks the input)
//   then per bit, MSB first, 2 clocks:
//     SET  sample = 0 (hold), dac_code = the result so far | the trial bit,
//          comp_clk = 0
//     CMP  comp_clk = 1; at the clock that ends CMP, cmp_out = 1 (input above
//          the trial voltage) keeps the trial bit, 0 clears it
//   after bit 0: result <= the code, done pulses for one clock, and the next
//   TRACK starts at once.
module sar_adc_digital (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire       cmp_out,
    output reg  [7:0] dac_code,
    output reg        sample,
    output reg        comp_clk,
    output reg        done,
    output reg  [7:0] result
);
  localparam IDLE = 2'd0, TRACK = 2'd1, SET = 2'd2, CMP = 2'd3;
  localparam TRACK_CLOCKS = 4;

  reg [1:0] state;
  reg [2:0] bit_i;
  reg [2:0] cnt;

  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      dac_code <= 8'd0;
      sample <= 1'b0;
      comp_clk <= 1'b0;
      done <= 1'b0;
      result <= 8'd0;
      bit_i <= 3'd7;
      cnt <= 3'd0;
    end else begin
      done <= 1'b0;
      case (state)
        IDLE: if (start) begin
          state <= TRACK;
          sample <= 1'b1;
          cnt <= 3'd1;
        end
        TRACK: if (cnt == TRACK_CLOCKS - 1) begin
          state <= SET;
          sample <= 1'b0;
          bit_i <= 3'd7;
          dac_code <= 8'h80;
        end else
          cnt <= cnt + 3'd1;
        SET: begin
          state <= CMP;
          comp_clk <= 1'b1;
        end
        CMP: begin
          comp_clk <= 1'b0;
          if (bit_i == 3'd0) begin
            result <= cmp_out ? dac_code : (dac_code & ~8'd1);
            done <= 1'b1;
            if (start) begin
              state <= TRACK;
              sample <= 1'b1;
              cnt <= 3'd0;
            end else
              state <= IDLE;
          end else begin
            state <= SET;
            dac_code <= (cmp_out ? dac_code : (dac_code & ~(8'd1 << bit_i)))
                        | (8'd1 << (bit_i - 3'd1));
            bit_i <= bit_i - 3'd1;
          end
        end
      endcase
    end
  end
endmodule

// Formal wrapper for r2r_dac_digital (plain wrapper, no bind; immediate asserts).
module r2r_dac_digital_formal (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [7:0] ui_in,
    input  wire [1:0] mode,
    input  wire       slow,
    output wire [7:0] dac_code,
    output wire [7:0] uo_out
);
  r2r_dac_digital dut (.clk(clk), .rst_n(rst_n), .ena(ena), .ui_in(ui_in),
                       .mode(mode), .slow(slow), .dac_code(dac_code), .uo_out(uo_out));

`ifdef FORMAL
  reg past_valid = 0;
  reg past_valid2 = 0;
  reg past_valid3 = 0;
  always @(posedge clk) begin
    past_valid <= 1;
    past_valid2 <= past_valid;
    past_valid3 <= past_valid2;
  end

  // REQ-RESET: rst_n is registered once, so a low rst_n sampled on one edge
  // forces dac_code to 0 on the next.
  always @(posedge clk)
    if (past_valid2)
      REQ_RESET: assert ($past(rst_n, 2) || dac_code == 8'd0);

  // REQ-HOLD: two consecutive hold-mode edges out of reset keep dac_code.
  always @(posedge clk)
    if (past_valid3)
      REQ_HOLD: assert (!($past(rst_n, 2) && $past(rst_n, 3) && $past(mode) == 2'b11
                          && $past(mode, 2) == 2'b11) || dac_code == $past(dac_code));

  // REQ-ECHO: uo_out always equals dac_code.
  always @(*)
    REQ_ECHO: assert (uo_out == dac_code);

  always @(posedge clk) begin
    COVER_FULL: cover (dac_code == 8'hFF);
    COVER_HOLD: cover (past_valid2 && $past(mode) == 2'b11 && $past(mode, 2) == 2'b11 && $past(rst_n, 2) && dac_code != 8'd0);
  end
`endif
endmodule

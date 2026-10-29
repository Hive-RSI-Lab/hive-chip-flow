// pll_ctrl_regs - quasi-static control bits registered on clk.
`default_nettype none
module pll_ctrl_regs (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       pll_en_in,
    input  wire [1:0] cp_trim_in,
    output reg        pll_en,
    output reg        cp_trim0,
    output reg        cp_trim1
);
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      pll_en   <= 1'b0;
      cp_trim0 <= 1'b0;
      cp_trim1 <= 1'b0;
    end else begin
      pll_en   <= pll_en_in;
      cp_trim0 <= cp_trim_in[0];
      cp_trim1 <= cp_trim_in[1];
    end
endmodule
`default_nettype wire

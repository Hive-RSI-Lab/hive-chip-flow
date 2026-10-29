// pll_divider - programmable /N (N = 1..5, codes 0/6/7 -> 1) on clk_pre.
// div_cnt counts on the rising edge of clk_pre; fb_en is registered on the
// FALLING edge so it only changes while clk_pre is low, making
// clk_fb = clk_pre & fb_en glitch-free. For N = 1, fb_en is held at 1 and
// clk_fb is clk_pre passed through. n_sel is quasi-static (not synchronised).
`default_nettype none
module pll_divider (
    input  wire       clk_pre,
    input  wire       rst_n,
    input  wire [2:0] n_sel,
    output wire       clk_fb
);
  reg [2:0] div_cnt;
  reg       fb_en;

  // N - 1 : codes 1..5 -> 0..4, unsupported codes 0/6/7 -> 0 (N = 1)
  wire       n_legal = (n_sel >= 3'd1) && (n_sel <= 3'd5);
  wire [2:0] n_m1    = n_legal ? (n_sel - 3'd1) : 3'd0;
  // >= so a run-time drop of N from a larger count wraps instead of stalling
  wire       last    = (div_cnt >= n_m1);

  always @(posedge clk_pre or negedge rst_n)
    if (!rst_n)    div_cnt <= 3'd0;
    else if (last) div_cnt <= 3'd0;
    else           div_cnt <= div_cnt + 3'd1;

  always @(negedge clk_pre or negedge rst_n)
    if (!rst_n) fb_en <= 1'b0;
    else        fb_en <= last;

  assign clk_fb = clk_pre & fb_en;
endmodule
`default_nettype wire

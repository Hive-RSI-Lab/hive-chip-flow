// pll_lock_det - lock indicator and runaway recovery in the clk domain.
// wide_q samples (pfd_up | pfd_dn) on the FALLING edge of clk (half a
// reference period after the rising edge). lock_cnt counts reference rising
// edges with a clean sample, saturating at K = 16, and restarts at 0 on a
// wide sample; lock = (next lock_cnt == 16), registered on the same edge.
// wide_q resets to 1 (no sample taken yet = not clean), so the first
// reference edge after reset never counts toward K.
// pfd_dn comes from the clk_fb domain: single-flop sample, no synchroniser.
//
// Recovery (REQ-RECOVER): wd_cnt counts reference rising edges with pll_en
// high and lock low. At the W-th (1024) such edge recover rises and the top
// holds pfd_up low and pfd_dn high for M (128) reference periods, which
// pulls Vctrl to ground; then recover falls and the count starts again.
// pll_en low or lock high clears both. A VCO driven past what the
// prescaler can divide (Vctrl at the rail at a fast-resistor corner) makes
// clk_fb look slow, so the PFD alone would hold Vctrl there for good.
`default_nettype none
module pll_lock_det (
    input  wire clk,
    input  wire rst_n,
    input  wire pll_en,
    input  wire pfd_up,
    input  wire pfd_dn,
    output reg  lock,
    output reg  recover
);
  localparam [4:0] K = 5'd16;
  localparam [9:0] W_LAST = 10'd1023;  // W = 1024 reference cycles
  localparam [9:0] M_LAST = 10'd127;   // M = 128 reference cycles

  reg       wide_q;
  reg [4:0] lock_cnt;
  reg [9:0] wd_cnt;
  wire [4:0] cnt_nxt = wide_q          ? 5'd0 :
                       (lock_cnt == K) ? K    : lock_cnt + 5'd1;

  always @(negedge clk or negedge rst_n)
    if (!rst_n) wide_q <= 1'b1;
    else        wide_q <= pfd_up | pfd_dn;

  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      lock_cnt <= 5'd0;
      lock     <= 1'b0;
    end else begin
      lock_cnt <= cnt_nxt;
      lock     <= (cnt_nxt == K);
    end

  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      wd_cnt  <= 10'd0;
      recover <= 1'b0;
    end else if (!pll_en || lock) begin
      wd_cnt  <= 10'd0;
      recover <= 1'b0;
    end else if (wd_cnt == (recover ? M_LAST : W_LAST)) begin
      wd_cnt  <= 10'd0;
      recover <= ~recover;
    end else begin
      wd_cnt  <= wd_cnt + 10'd1;
    end
endmodule
`default_nettype wire

// ihsan_sa_pll_formal.sv - formal harness for block pll (core ihsan_sa_pll).
// Written from spec/spec.md + spec/spec.yaml only, before RTL exists.
// Plain structural wrapper, procedural assert/cover only (no bind, no SVA
// `assert property`, no hierarchical refs). The design has four clock
// domains (clk, vco_out, clk_pre, clk_fb) and an asynchronous reset, so
// every monitor runs on the formal global clock ($global_clock, one solver
// step) and sees each clock as a sampled signal: edges are $rose/$fell.
module ihsan_sa_pll_formal (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [2:0] n_sel,
    input  wire       vco_out,
    input  wire       pll_en_in,
    input  wire       obs_sel,
    input  wire [1:0] cp_trim_in,
    output wire       obs_out,
    output wire       clk_fb,
    output wire       lock,
    output wire       pfd_up,
    output wire       pfd_dn,
    output wire       pll_en,
    output wire       cp_trim0,
    output wire       cp_trim1
);
  // The core's formal-only view of a recovery (REQ-RECOVER): the one
  // internal state the port-level properties have to know about.
  wire recover;

  ihsan_sa_pll dut (
      .clk(clk), .rst_n(rst_n), .n_sel(n_sel), .vco_out(vco_out),
      .pll_en_in(pll_en_in), .obs_sel(obs_sel), .cp_trim_in(cp_trim_in),
      .obs_out(obs_out), .clk_fb(clk_fb), .lock(lock), .pfd_up(pfd_up),
      .pfd_dn(pfd_dn), .pll_en(pll_en), .cp_trim0(cp_trim0),
      .cp_trim1(cp_trim1), .recover_fv(recover));

`ifdef FORMAL
  reg past_valid = 1'b0;
  always @($global_clock) past_valid <= 1'b1;

  // Every run starts in reset, as on silicon (the flops have no init value).
  always @($global_clock) if (!past_valid) assume (!rst_n);

  // clk_pre (vco_out/8) is internal; with obs_sel = 0 obs_out is clk_pre
  // (REQ-OBS-SEL), so the /8 check observes it through obs_out.
  wire clk_pre_obs = obs_out;

  // N decode per spec: 1..5 as is, 0/6/7 -> 1.
  wire [2:0] n_eff = (n_sel >= 3'd1 && n_sel <= 3'd5) ? n_sel : 3'd1;

  // ---- prescaler shadow: vco_out rises since the last clk_pre rise ----
  reg [3:0] vco_cnt  = 4'd0;
  reg       pre_seen = 1'b0;   // one clk_pre rise seen since reset
  reg       pre_ok   = 1'b0;   // obs_sel held 0 since that rise
  // ---- divider shadow: clk_pre rises since the last clk_fb rise ----
  reg [3:0] pre_cnt  = 4'd0;
  reg       fb_seen  = 1'b0;
  reg       fb_ok    = 1'b0;   // obs_sel 0 and n_sel unchanged since then
  reg [2:0] n_last   = 3'd1;
  reg [2:0] n_q      = 3'd1;   // n_sel one step ago
  reg       n_stab   = 1'b1;   // n_sel unchanged since the last clk_fb rise
  // The divider samples n_sel during the period BEFORE a clk_fb rise (the
  // edge's own count and the falling-edge enable), so a period is checked
  // only when n_sel was already stable through the whole previous period:
  // the first period after an N change is a settling transient.
  wire n_stab_now = n_stab && (n_sel == n_q);
  // ---- lock shadow: wide samples (pfd_up|pfd_dn at clk falling edge) ----
  reg        wide_s  = 1'b1;   // sample of the current reference cycle
  reg [15:0] hist    = 16'hFFFF; // samples closed by the last 16 clk rises
  reg [15:0] hist_q  = 16'hFFFF;

  reg vco_q = 1'b0, prep_q = 1'b0, fb_q = 1'b0, clk_q = 1'b0, obs_q = 1'b0;
  // A sample at a clock edge sees the value just BEFORE the edge, as a
  // flop does: a pulse that starts in the edge's own step is not sampled.
  reg wide_q = 1'b0;
  always @($global_clock) begin
    vco_q <= vco_out; prep_q <= clk_pre_obs; fb_q <= clk_fb; clk_q <= clk;
    obs_q <= obs_sel; wide_q <= pfd_up | pfd_dn;
    n_q <= n_sel;
  end
  wire vco_rise = past_valid && vco_out && !vco_q;
  // obs_out only shows clk_pre while obs_sel is 0; a rise in the step
  // obs_sel returns to 0 is the mux switching, not a clk_pre edge.
  wire pre_rise = past_valid && clk_pre_obs && !prep_q && !obs_sel && !obs_q;
  wire fb_rise  = past_valid && clk_fb && !fb_q;
  wire clk_rise = past_valid && clk && !clk_q;
  wire clk_fall = past_valid && !clk && clk_q;

  // hist including the cycle a clk rise closes in this very step: lock
  // updates on that rise in the same step, one step before hist does.
  wire [15:0] hist_now = clk_rise ? {hist[14:0], wide_s} : hist;

  always @($global_clock) begin : shadow
    hist_q <= hist;
    if (!rst_n) begin
      vco_cnt <= 4'd0; pre_seen <= 1'b0; pre_ok <= 1'b0;
      pre_cnt <= 4'd0; fb_seen <= 1'b0; fb_ok <= 1'b0; n_last <= n_sel;
      n_stab <= 1'b1;
      wide_s <= 1'b1; hist <= 16'hFFFF;
    end else begin
      if (pre_rise) begin
        vco_cnt <= 4'd0; pre_seen <= 1'b1; pre_ok <= !obs_sel;
      end else begin
        if (vco_rise && vco_cnt != 4'hF) vco_cnt <= vco_cnt + 4'd1;
        if (obs_sel) pre_ok <= 1'b0;
      end
      if (fb_rise) begin
        pre_cnt <= 4'd0; fb_seen <= 1'b1; n_last <= n_sel;
        fb_ok <= !obs_sel && n_stab_now; n_stab <= 1'b1;
      end else begin
        if (pre_rise && pre_cnt != 4'hF) pre_cnt <= pre_cnt + 4'd1;
        if (obs_sel || n_sel != n_last) fb_ok <= 1'b0;
        if (n_sel != n_q) n_stab <= 1'b0;
      end
      if (clk_fall) wide_s <= wide_q;
      if (clk_rise) begin
        hist <= {hist[14:0], wide_s};
        wide_s <= 1'b0;
      end
    end
  end

  always @($global_clock) begin : props
    if (past_valid) begin
      // REQ-RST-ASYNC: rst_n low forces every listed output to 0 in the
      // same step, with no clock edge needed.
      if (!rst_n)
        reset_forces_outputs: assert (!pfd_up && !pfd_dn && !lock &&
                                      !clk_fb && !obs_out && !pll_en &&
                                      !cp_trim0 && !cp_trim1);

      // REQ-PRE-DIV8: clk_pre rises exactly once per 8 vco_out rises -
      // never more than 8 rises between clk_pre rises, and exactly 8 at
      // each clk_pre rise after the first. The 8th vco_out rise lands in
      // the same step as the clk_pre rise it causes, so it is counted as
      // vco_rise on top of vco_cnt (N = 1 in divider_ratio is the same).
      if (rst_n && $past(rst_n) && pre_ok && !obs_sel)
        prescaler_div8: assert (({1'b0, vco_cnt} + {4'd0, vco_rise} <= 5'd8) &&
                                (!pre_rise || !pre_seen ||
                                 {1'b0, vco_cnt} + {4'd0, vco_rise} == 5'd8));

      // REQ-DIV-N: clk_fb rises exactly once per N clk_pre rises (N = 1
      // passes clk_pre through: clk_fb rises in the same step).
      if (rst_n && $past(rst_n) && fb_ok && !obs_sel && n_sel == n_last)
        divider_ratio: assert (({1'b0, pre_cnt} + {4'd0, pre_rise} <= {2'b0, n_eff}) &&
                               (!fb_rise || !fb_seen ||
                                pre_cnt + {3'd0, pre_rise} == {1'b0, n_eff}));

      // REQ-PFD-NO-OVERLAP: UP and DN never both high at a sample point.
      pfd_never_both_high: assert (!(pfd_up && pfd_dn));

      // REQ-PFD-RESET: an edge that would set the second flop while the
      // other is high fires the reset - both are low after it. A recovery
      // overrides the PFD's outputs (REQ-RECOVER-OUT), so it is excluded.
      if (rst_n && $past(rst_n) && !recover && !$past(recover) &&
          (($past(pfd_up) && fb_rise) || ($past(pfd_dn) && clk_rise)))
        pfd_reset_on_both: assert (!pfd_up && !pfd_dn);

      // REQ-LOCK-NO-WIDE: lock is never high when a wide pulse was sampled
      // in the reference cycle it covers or the 15 before (one-step skew
      // between the clk rise and lock's update is tolerated).
      if (rst_n)
        lock_not_while_wide: assert (!lock || hist_now == 16'h0 || hist_q == 16'h0);
    end
  end

  always @($global_clock) begin : covers
    if (past_valid && rst_n) begin
      COVER_PFD_UP:     cover (pfd_up);
      COVER_PFD_DN:     cover (pfd_dn);
      COVER_PFD_RESET:  cover ($past(pfd_up) && fb_rise && !pfd_up && !pfd_dn);
      COVER_PRE_PERIOD: cover (pre_rise && pre_seen && pre_ok &&
                               {1'b0, vco_cnt} + {4'd0, vco_rise} == 5'd8);
      COVER_FB_N5:      cover (fb_rise && fb_seen && fb_ok && n_sel == 3'd5);
      COVER_FB_ILLEGAL: cover (fb_rise && fb_seen && fb_ok && n_sel == 3'd0);
      COVER_LOCK:       cover (lock);
      COVER_LOCK_DROP:  cover ($past(lock) && !lock && $past(rst_n));
    end
  end
`endif
endmodule

// ece298a_counter_formal.sv - the formal harness for blocks/counter: a plain
// structural wrapper (never `bind`, see corpus/vde/counter8's harness), the
// DUT unmodified, every input a free top-level port.
//
// The async reset turns sby's multiclock on (check_formal.py), so a solver
// step is one change of clk, not a cycle, and rst_n may fall between edges.
// The asserts read the solver's own steps: p_* hold each signal as it was
// the step before ($global_clock), a rising edge is clk now high after low,
// and the value a flop took on it is the one its inputs had the step before.
//
// The harness counts edges since rst_n was last low on its own, from the
// ports, rather than reading the synchroniser's flops: `released` is the
// spec's claim, not the design's state.
module ece298a_counter_formal (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  ece298a_counter dut (
      .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out),
      .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n));

`ifdef FORMAL
  wire load = ui_in[0], enable = ui_in[1], oe = ui_in[2];

  // the step before, kept by hand ($past is only allowed in clocked blocks)
  reg       past_valid = 0;
  reg       p_rst_n, p_clk, p_load, p_enable, p_oe;
  reg [7:0] p_uio_in, p_count;
  // rising edges up to the step before with rst_n high since it was last low,
  // saturating at 2. An edge in the step rst_n rises is that release's recovery
  // race, so it doesn't count. seen_low: rst_n has been low at all, so the
  // state is known.
  reg [1:0] edges = 0;
  reg       seen_low = 0;
  always @($global_clock) begin
    past_valid <= 1;
    p_rst_n <= rst_n;
    p_clk <= clk;
    p_load <= load;
    p_enable <= enable;
    p_oe <= oe;
    p_uio_in <= uio_in;
    p_count <= uo_out;
    if (!rst_n) begin
      edges <= 0;
      seen_low <= 1;
    end else if (past_valid && p_rst_n && clk && !p_clk && edges != 2) begin
      edges <= edges + 1;
    end
  end

  // rst_n high and two edges passed since it was low: the release reached the
  // count before this step
  wire run = past_valid && seen_low && rst_n && edges == 2;
  wire edge_ = past_valid && clk && !p_clk;
  wire p_do_load = p_load && !p_oe;

  always @* begin
    // REQ-RESET: rst_n low is a 0 count, in the very step it falls
    if (!rst_n)
      REQ_RESET: assert (uo_out == 8'h00);

    // REQ-RELEASE: from rst_n's fall until the second edge after its rise has
    // gone by, the count is 0; the first edge that can move it is the third
    if (seen_low && edges != 2)
      REQ_RELEASE: assert (uo_out == 8'h00);

    // REQ-LOAD: an edge that sampled LOAD with the bus released loads D
    if (run && edge_ && p_do_load)
      REQ_LOAD: assert (uo_out == p_uio_in);

    // REQ-LOAD-OE: LOAD with OE high is no load: hold, or count with ENABLE
    if (run && edge_ && p_load && p_oe)
      REQ_LOAD_OE: assert (uo_out == (p_enable ? p_count + 8'd1 : p_count));

    // REQ-COUNT: an edge that sampled ENABLE and no load adds one, wrapping
    if (run && edge_ && p_enable && !p_do_load)
      REQ_COUNT: assert (uo_out == p_count + 8'd1);

    // REQ-HOLD: no edge, or an edge with neither, leaves the count alone
    if (run && (!edge_ || (!p_enable && !p_do_load)))
      REQ_HOLD: assert (uo_out == p_count);

    // REQ-BUS: the enables follow OE and the bus carries the count, always
    REQ_BUS: assert (uio_oe == {8{oe}} && uio_out == uo_out);

    COVER_WRAP: cover (run && edge_ && p_count == 8'hFF && uo_out == 8'h00);
    COVER_LOAD_OE: cover (run && edge_ && p_load && p_oe && p_enable);
    COVER_ASYNC_CLEAR: cover (past_valid && !clk && !p_clk && p_count != 0
                              && uo_out == 0);
  end
`endif
endmodule

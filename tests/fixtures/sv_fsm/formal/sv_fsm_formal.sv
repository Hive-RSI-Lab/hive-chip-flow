// The formal harness for the sv_fsm fixture: a plain wrapper, as the
// corpus harnesses are.
module sv_fsm_formal (input logic clk, input logic rst, input logic start,
                      output logic busy, output logic done,
                      output logic [3:0] count);
  sv_fsm dut (.clk(clk), .rst(rst), .start(start), .busy(busy),
              .done(done), .count(count));

`ifdef FORMAL
  // The invariant holds in every state reachable from a reset: once rst
  // has been seen (registered, so the clock after it), never before.
  reg reset_seen = 0;
  always @(posedge clk) if (rst) reset_seen <= 1;
  always @(posedge clk)
    if (reset_seen)
      REQ_FORMAL_EXCL: assert (!(busy && done) && count <= 4'd10);
  always @(posedge clk)
    COVER_DONE: cover (done);
`endif
endmodule

// sv_fsm.sv - a SystemVerilog fixture (#91): start runs ten counted clocks,
// then one clock of done. Package import in the header, typedef enum,
// packed struct, always_ff and always_comb: what lint, sim, synth and
// formal must all read.
module sv_fsm
  import sv_fsm_pkg::*;
(
  input  logic       clk,
  input  logic       rst,
  input  logic       start,
  output logic       busy,
  output logic       done,
  output logic [3:0] count
);
  ctx_t cur, nxt;

  always_ff @(posedge clk) begin
    if (rst) cur <= '0;
    else     cur <= nxt;
  end

  always_comb begin
    nxt = cur;
    case (cur.state)
      IDLE: if (start) begin
        nxt.state = RUN;
        nxt.count = 4'd0;
      end
      RUN: begin
        nxt.count = cur.count + 4'd1;
        if (cur.count == LAST) nxt.state = DONE;
      end
      default: nxt.state = IDLE;
    endcase
  end

  assign busy  = (cur.state == RUN);
  assign done  = (cur.state == DONE);
  assign count = cur.count;
endmodule

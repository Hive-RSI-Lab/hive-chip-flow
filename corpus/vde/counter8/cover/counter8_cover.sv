// SV cover points for the cover gate (#80b): bound into counter8 and built
// only into its Verilator coverage run. spec.yaml's REQ-WRAP names WRAP.
module counter8_cover (
    input wire       clk,
    input wire       rst,
    input wire [7:0] count
);
  WRAP: cover property (@(posedge clk) disable iff (rst)
                        count == 8'd255 ##1 count == 8'd0);
endmodule

bind counter8 counter8_cover u_cover (.clk(clk), .rst(rst), .count(count));

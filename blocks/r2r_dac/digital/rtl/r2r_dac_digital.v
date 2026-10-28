`default_nettype none
// r2r_dac_digital - digital front end of the 8-bit R-2R DAC.
// mode 00 register (capture ui_in), 01 ramp, 10 sine, 11 hold. dac_code registered.
module r2r_dac_digital (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,
    input  wire [7:0] ui_in,
    input  wire [1:0] mode,
    input  wire       slow,
    output reg  [7:0] dac_code,
    output wire [7:0] uo_out
);
  reg [7:0] code_reg;
  reg [7:0] cnt;
  reg [7:0] presc;
  reg [7:0] sine_val;

  wire unused_ena = ena;

  always @(*) begin
    case (cnt)
      8'd0: sine_val = 8'd128;
      8'd1: sine_val = 8'd131;
      8'd2: sine_val = 8'd134;
      8'd3: sine_val = 8'd137;
      8'd4: sine_val = 8'd140;
      8'd5: sine_val = 8'd143;
      8'd6: sine_val = 8'd146;
      8'd7: sine_val = 8'd149;
      8'd8: sine_val = 8'd152;
      8'd9: sine_val = 8'd155;
      8'd10: sine_val = 8'd158;
      8'd11: sine_val = 8'd162;
      8'd12: sine_val = 8'd165;
      8'd13: sine_val = 8'd167;
      8'd14: sine_val = 8'd170;
      8'd15: sine_val = 8'd173;
      8'd16: sine_val = 8'd176;
      8'd17: sine_val = 8'd179;
      8'd18: sine_val = 8'd182;
      8'd19: sine_val = 8'd185;
      8'd20: sine_val = 8'd188;
      8'd21: sine_val = 8'd190;
      8'd22: sine_val = 8'd193;
      8'd23: sine_val = 8'd196;
      8'd24: sine_val = 8'd198;
      8'd25: sine_val = 8'd201;
      8'd26: sine_val = 8'd203;
      8'd27: sine_val = 8'd206;
      8'd28: sine_val = 8'd208;
      8'd29: sine_val = 8'd211;
      8'd30: sine_val = 8'd213;
      8'd31: sine_val = 8'd215;
      8'd32: sine_val = 8'd218;
      8'd33: sine_val = 8'd220;
      8'd34: sine_val = 8'd222;
      8'd35: sine_val = 8'd224;
      8'd36: sine_val = 8'd226;
      8'd37: sine_val = 8'd228;
      8'd38: sine_val = 8'd230;
      8'd39: sine_val = 8'd232;
      8'd40: sine_val = 8'd234;
      8'd41: sine_val = 8'd235;
      8'd42: sine_val = 8'd237;
      8'd43: sine_val = 8'd238;
      8'd44: sine_val = 8'd240;
      8'd45: sine_val = 8'd241;
      8'd46: sine_val = 8'd243;
      8'd47: sine_val = 8'd244;
      8'd48: sine_val = 8'd245;
      8'd49: sine_val = 8'd246;
      8'd50: sine_val = 8'd248;
      8'd51: sine_val = 8'd249;
      8'd52: sine_val = 8'd250;
      8'd53: sine_val = 8'd250;
      8'd54: sine_val = 8'd251;
      8'd55: sine_val = 8'd252;
      8'd56: sine_val = 8'd253;
      8'd57: sine_val = 8'd253;
      8'd58: sine_val = 8'd254;
      8'd59: sine_val = 8'd254;
      8'd60: sine_val = 8'd254;
      8'd61: sine_val = 8'd255;
      8'd62: sine_val = 8'd255;
      8'd63: sine_val = 8'd255;
      8'd64: sine_val = 8'd255;
      8'd65: sine_val = 8'd255;
      8'd66: sine_val = 8'd255;
      8'd67: sine_val = 8'd255;
      8'd68: sine_val = 8'd254;
      8'd69: sine_val = 8'd254;
      8'd70: sine_val = 8'd254;
      8'd71: sine_val = 8'd253;
      8'd72: sine_val = 8'd253;
      8'd73: sine_val = 8'd252;
      8'd74: sine_val = 8'd251;
      8'd75: sine_val = 8'd250;
      8'd76: sine_val = 8'd250;
      8'd77: sine_val = 8'd249;
      8'd78: sine_val = 8'd248;
      8'd79: sine_val = 8'd246;
      8'd80: sine_val = 8'd245;
      8'd81: sine_val = 8'd244;
      8'd82: sine_val = 8'd243;
      8'd83: sine_val = 8'd241;
      8'd84: sine_val = 8'd240;
      8'd85: sine_val = 8'd238;
      8'd86: sine_val = 8'd237;
      8'd87: sine_val = 8'd235;
      8'd88: sine_val = 8'd234;
      8'd89: sine_val = 8'd232;
      8'd90: sine_val = 8'd230;
      8'd91: sine_val = 8'd228;
      8'd92: sine_val = 8'd226;
      8'd93: sine_val = 8'd224;
      8'd94: sine_val = 8'd222;
      8'd95: sine_val = 8'd220;
      8'd96: sine_val = 8'd218;
      8'd97: sine_val = 8'd215;
      8'd98: sine_val = 8'd213;
      8'd99: sine_val = 8'd211;
      8'd100: sine_val = 8'd208;
      8'd101: sine_val = 8'd206;
      8'd102: sine_val = 8'd203;
      8'd103: sine_val = 8'd201;
      8'd104: sine_val = 8'd198;
      8'd105: sine_val = 8'd196;
      8'd106: sine_val = 8'd193;
      8'd107: sine_val = 8'd190;
      8'd108: sine_val = 8'd188;
      8'd109: sine_val = 8'd185;
      8'd110: sine_val = 8'd182;
      8'd111: sine_val = 8'd179;
      8'd112: sine_val = 8'd176;
      8'd113: sine_val = 8'd173;
      8'd114: sine_val = 8'd170;
      8'd115: sine_val = 8'd167;
      8'd116: sine_val = 8'd165;
      8'd117: sine_val = 8'd162;
      8'd118: sine_val = 8'd158;
      8'd119: sine_val = 8'd155;
      8'd120: sine_val = 8'd152;
      8'd121: sine_val = 8'd149;
      8'd122: sine_val = 8'd146;
      8'd123: sine_val = 8'd143;
      8'd124: sine_val = 8'd140;
      8'd125: sine_val = 8'd137;
      8'd126: sine_val = 8'd134;
      8'd127: sine_val = 8'd131;
      8'd128: sine_val = 8'd128;
      8'd129: sine_val = 8'd124;
      8'd130: sine_val = 8'd121;
      8'd131: sine_val = 8'd118;
      8'd132: sine_val = 8'd115;
      8'd133: sine_val = 8'd112;
      8'd134: sine_val = 8'd109;
      8'd135: sine_val = 8'd106;
      8'd136: sine_val = 8'd103;
      8'd137: sine_val = 8'd100;
      8'd138: sine_val = 8'd97;
      8'd139: sine_val = 8'd93;
      8'd140: sine_val = 8'd90;
      8'd141: sine_val = 8'd88;
      8'd142: sine_val = 8'd85;
      8'd143: sine_val = 8'd82;
      8'd144: sine_val = 8'd79;
      8'd145: sine_val = 8'd76;
      8'd146: sine_val = 8'd73;
      8'd147: sine_val = 8'd70;
      8'd148: sine_val = 8'd67;
      8'd149: sine_val = 8'd65;
      8'd150: sine_val = 8'd62;
      8'd151: sine_val = 8'd59;
      8'd152: sine_val = 8'd57;
      8'd153: sine_val = 8'd54;
      8'd154: sine_val = 8'd52;
      8'd155: sine_val = 8'd49;
      8'd156: sine_val = 8'd47;
      8'd157: sine_val = 8'd44;
      8'd158: sine_val = 8'd42;
      8'd159: sine_val = 8'd40;
      8'd160: sine_val = 8'd37;
      8'd161: sine_val = 8'd35;
      8'd162: sine_val = 8'd33;
      8'd163: sine_val = 8'd31;
      8'd164: sine_val = 8'd29;
      8'd165: sine_val = 8'd27;
      8'd166: sine_val = 8'd25;
      8'd167: sine_val = 8'd23;
      8'd168: sine_val = 8'd21;
      8'd169: sine_val = 8'd20;
      8'd170: sine_val = 8'd18;
      8'd171: sine_val = 8'd17;
      8'd172: sine_val = 8'd15;
      8'd173: sine_val = 8'd14;
      8'd174: sine_val = 8'd12;
      8'd175: sine_val = 8'd11;
      8'd176: sine_val = 8'd10;
      8'd177: sine_val = 8'd9;
      8'd178: sine_val = 8'd7;
      8'd179: sine_val = 8'd6;
      8'd180: sine_val = 8'd5;
      8'd181: sine_val = 8'd5;
      8'd182: sine_val = 8'd4;
      8'd183: sine_val = 8'd3;
      8'd184: sine_val = 8'd2;
      8'd185: sine_val = 8'd2;
      8'd186: sine_val = 8'd1;
      8'd187: sine_val = 8'd1;
      8'd188: sine_val = 8'd1;
      8'd189: sine_val = 8'd0;
      8'd190: sine_val = 8'd0;
      8'd191: sine_val = 8'd0;
      8'd192: sine_val = 8'd0;
      8'd193: sine_val = 8'd0;
      8'd194: sine_val = 8'd0;
      8'd195: sine_val = 8'd0;
      8'd196: sine_val = 8'd1;
      8'd197: sine_val = 8'd1;
      8'd198: sine_val = 8'd1;
      8'd199: sine_val = 8'd2;
      8'd200: sine_val = 8'd2;
      8'd201: sine_val = 8'd3;
      8'd202: sine_val = 8'd4;
      8'd203: sine_val = 8'd5;
      8'd204: sine_val = 8'd5;
      8'd205: sine_val = 8'd6;
      8'd206: sine_val = 8'd7;
      8'd207: sine_val = 8'd9;
      8'd208: sine_val = 8'd10;
      8'd209: sine_val = 8'd11;
      8'd210: sine_val = 8'd12;
      8'd211: sine_val = 8'd14;
      8'd212: sine_val = 8'd15;
      8'd213: sine_val = 8'd17;
      8'd214: sine_val = 8'd18;
      8'd215: sine_val = 8'd20;
      8'd216: sine_val = 8'd21;
      8'd217: sine_val = 8'd23;
      8'd218: sine_val = 8'd25;
      8'd219: sine_val = 8'd27;
      8'd220: sine_val = 8'd29;
      8'd221: sine_val = 8'd31;
      8'd222: sine_val = 8'd33;
      8'd223: sine_val = 8'd35;
      8'd224: sine_val = 8'd37;
      8'd225: sine_val = 8'd40;
      8'd226: sine_val = 8'd42;
      8'd227: sine_val = 8'd44;
      8'd228: sine_val = 8'd47;
      8'd229: sine_val = 8'd49;
      8'd230: sine_val = 8'd52;
      8'd231: sine_val = 8'd54;
      8'd232: sine_val = 8'd57;
      8'd233: sine_val = 8'd59;
      8'd234: sine_val = 8'd62;
      8'd235: sine_val = 8'd65;
      8'd236: sine_val = 8'd67;
      8'd237: sine_val = 8'd70;
      8'd238: sine_val = 8'd73;
      8'd239: sine_val = 8'd76;
      8'd240: sine_val = 8'd79;
      8'd241: sine_val = 8'd82;
      8'd242: sine_val = 8'd85;
      8'd243: sine_val = 8'd88;
      8'd244: sine_val = 8'd90;
      8'd245: sine_val = 8'd93;
      8'd246: sine_val = 8'd97;
      8'd247: sine_val = 8'd100;
      8'd248: sine_val = 8'd103;
      8'd249: sine_val = 8'd106;
      8'd250: sine_val = 8'd109;
      8'd251: sine_val = 8'd112;
      8'd252: sine_val = 8'd115;
      8'd253: sine_val = 8'd118;
      8'd254: sine_val = 8'd121;
      8'd255: sine_val = 8'd124;
      default: sine_val = 8'd128;
    endcase
  end

  // presc_tc is a registered copy of (presc == 8'hFF): it is loaded with
  // (presc == 8'hFE), i.e. the condition one cycle early, and presc always
  // increments by one (wrapping), so presc_tc == (presc == 8'hFF) holds on
  // every cycle. Reset clears both (presc = 0 -> tc = 0). This takes the
  // 8-bit compare off the step -> cnt -> sine path (timing at ss_125C).
  // cnt_p1 likewise holds cnt + 1 at all times (reset 1 with cnt 0, and both
  // advance together on step), so the incrementer is off that path too.
  reg       presc_tc;
  reg [7:0] cnt_p1;

  wire step = ~slow | presc_tc;

  // rst_n is registered once before use (synchronised reset): the pad's
  // input delay then sees only one flop, not the sine/mux logic, which is
  // what kept ss_125C short of setup. Reset takes effect one edge later.
  reg rst_q;
  always @(posedge clk) rst_q <= rst_n;

  always @(posedge clk) begin
    if (!rst_q) begin
      code_reg <= 8'd0;
      cnt      <= 8'd0;
      presc    <= 8'd0;
      presc_tc <= 1'b0;
      cnt_p1   <= 8'd1;
      dac_code <= 8'd0;
    end else begin
      presc_tc <= (presc == 8'hFE);
      case (mode)
        2'b00: dac_code <= ui_in;
        2'b01: dac_code <= cnt;
        2'b10: dac_code <= sine_val;
        default: dac_code <= code_reg;
      endcase
      if (mode == 2'b00)
        code_reg <= ui_in;
      presc <= presc + 8'd1;
      if (step) begin
        cnt    <= cnt_p1;
        cnt_p1 <= cnt_p1 + 8'd1;
      end
    end
  end

  assign uo_out = dac_code;
endmodule
`default_nettype wire

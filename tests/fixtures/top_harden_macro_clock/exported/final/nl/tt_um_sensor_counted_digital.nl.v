module tt_um_sensor_counted_digital (clk);
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _195_ (.D(_000_), .RN(net38), .CLK(\clknet_1_1__leaf_u_sensor_counted_digital.osc_out_regs ), .Q(\u_sensor_counted_digital.osc_div[0] ));
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _196_ (.D(_001_), .RN(net38), .CLK(\clknet_1_1__leaf_u_sensor_counted_digital.osc_out_regs ), .Q(\u_sensor_counted_digital.osc_div[1] ));
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _197_ (.D(_002_), .RN(net38), .CLK(\clknet_1_0__leaf_u_sensor_counted_digital.osc_out_regs ), .Q(\u_sensor_counted_digital.osc_div[2] ));
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _198_ (.D(_003_), .RN(net38), .CLK(\clknet_1_0__leaf_u_sensor_counted_digital.osc_out_regs ), .Q(\u_sensor_counted_digital.osc_div[3] ));
gf180mcu_fd_sc_mcu7t5v0__buf_1 _214_ (.I(\clknet_1_0__leaf_u_sensor_counted_digital.osc_out ), .Z(uio_out[1]));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 \clkbuf_0_u_sensor_counted_digital.osc_out  (.I(\u_sensor_counted_digital.osc_out ), .Z(\clknet_0_u_sensor_counted_digital.osc_out ));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 \clkbuf_0_u_sensor_counted_digital.osc_out_regs  (.I(\u_sensor_counted_digital.osc_out_regs ), .Z(\clknet_0_u_sensor_counted_digital.osc_out_regs ));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 \clkbuf_1_0__f_u_sensor_counted_digital.osc_out  (.I(\clknet_0_u_sensor_counted_digital.osc_out ), .Z(\clknet_1_0__leaf_u_sensor_counted_digital.osc_out ));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 \clkbuf_1_0__f_u_sensor_counted_digital.osc_out_regs  (.I(\clknet_0_u_sensor_counted_digital.osc_out_regs ), .Z(\clknet_1_0__leaf_u_sensor_counted_digital.osc_out_regs ));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 \clkbuf_1_1__f_u_sensor_counted_digital.osc_out_regs  (.I(\clknet_0_u_sensor_counted_digital.osc_out_regs ), .Z(\clknet_1_1__leaf_u_sensor_counted_digital.osc_out_regs ));
gf180mcu_fd_sc_mcu7t5v0__clkbuf_8 clkbuf_regs_0_osc (.I(\u_sensor_counted_digital.osc_out ), .Z(\u_sensor_counted_digital.osc_out_regs ));
sensor_counted_analog u_analog (.osc_en(net21), .osc_out(\u_sensor_counted_digital.osc_out ));
endmodule

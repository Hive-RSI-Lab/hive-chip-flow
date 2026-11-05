module tt_um_sensor_counted_digital (clk);
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _195_ ( .CLK(\u_sensor_counted_digital.osc_out ), .D(_000_), .Q(\u_sensor_counted_digital.osc_div[0] ), .RN(rst_n) );
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _196_ ( .CLK(\u_sensor_counted_digital.osc_out ), .D(_001_), .Q(\u_sensor_counted_digital.osc_div[1] ), .RN(rst_n) );
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _197_ ( .CLK(\u_sensor_counted_digital.osc_out ), .D(_002_), .Q(\u_sensor_counted_digital.osc_div[2] ), .RN(rst_n) );
gf180mcu_fd_sc_mcu7t5v0__dffrnq_1 _198_ ( .CLK(\u_sensor_counted_digital.osc_out ), .D(_003_), .Q(\u_sensor_counted_digital.osc_div[3] ), .RN(rst_n) );
gf180mcu_fd_sc_mcu7t5v0__buf_1 _214_ ( .I(\u_sensor_counted_digital.osc_out ), .Z(uio_out[1]) );
sensor_counted_analog u_analog ( .osc_en(ui_in[0]), .osc_out(\u_sensor_counted_digital.osc_out ) );
endmodule

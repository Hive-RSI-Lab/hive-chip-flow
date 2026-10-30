# Design SDC for tt_um_ihsan_sa_pll, sourced after LibreLane's base.sdc,
# which already defines the primary clock clk (spec clock.period_ns, 80 ns).
# Names are the flat (post-synth) netlist's: yosys flattening keeps one of
# a net's hierarchical aliases, so each clock net is looked up by every
# alias it can carry, and the clock goes on the driving flop's output pin.
# No leading wildcard: CTS adds clknet_*/clkbuf_* nets that end in the same
# names, and a pattern matching them gives a generated clock several masters.

proc pll_driver_pin {aliases} {
    foreach a $aliases {
        set nets [get_nets -quiet $a]
        if {[llength $nets] > 0} {
            set pins [get_pins -quiet -of_objects $nets -filter "direction == output"]
            if {[llength $pins] > 0} { return $pins }
        }
    }
    error "constraints.sdc: no net matches any of {$aliases}"
}

# vco_out: the VCO's buffered output on ui_in[3]; spec timing_notes 2.7 ns
# (370 MHz: the analog VCO's ceiling at every corner, ~363 MHz at ff, -40 C,
# 2.97 V with vctrl at the rail).
create_clock -name vco_out -period 2.7 [get_ports {ui_in[3]}]

# The /8 ripple prescaler: each stage clocks the next. Stage 0's divided
# clock is defined after its own buffer (u_pre_ckbuf0, net pre_c0), not on
# its Q: the Q net is then the toggle loop's data alone, so CTS neither
# buffers it nor pulls the loop's inverter into a clock tree.
set pll_pre_q0 [pll_driver_pin {u_ihsan_sa_pll.u_pre.pre_c0}]
set pll_pre_q1 [pll_driver_pin {u_ihsan_sa_pll.u_pre.pre_q1}]
set pll_pre_q2 [pll_driver_pin {u_ihsan_sa_pll.clk_pre u_ihsan_sa_pll.u_pre.pre_q2}]
create_generated_clock -name pre_q0 -source [get_ports {ui_in[3]}] -divide_by 2 $pll_pre_q0
create_generated_clock -name pre_q1 -source $pll_pre_q0 -divide_by 2 $pll_pre_q1
# clk_pre = vco_out/8 (21.6 ns at the 2.7 ns vco_out period). It has to be a
# generated clock: a create_clock on the stage-2 Q pin gives that flop's
# own toggle loop zero launch latency against a propagated capture clock,
# a false hold violation. REQ-TIM-PRE's 20 ns is tighter; harden run 2
# (clk_pre at 20 ns) met it with 3.3 ns of setup slack at ss.
# clk_fb = clk_pre & fb_en is the same clock passed through a gate, so
# STA propagates clk_pre through it to the divider's and PFD's flops.
create_generated_clock -name clk_pre -source $pll_pre_q1 -divide_by 2 $pll_pre_q2

# clk and the VCO-derived clocks are unrelated: the PFD reset AND, the lock
# detector's sample of pfd_dn and every rst_n recovery path cross them on
# purpose (REQ-PFD-*, REQ-LOCK-*).
set_clock_groups -asynchronous \
    -group [get_clocks clk] \
    -group [get_clocks {vco_out pre_q0 pre_q1 clk_pre}]

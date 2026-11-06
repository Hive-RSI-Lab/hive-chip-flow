# cdc_async_contradicts_interface (split)

`digital/spec/cdc.yaml` declares a port `async`, but `interface.yaml`
puts that signal in a clocked domain (such as `clk_sys`). The two files
disagree about whether the crossing needs a synchroniser.

**Cheapest fix first:** check what the analog side actually drives. If the
output changes only on the digital clock (like a SAR comparator that is
latched on `clk_sys`), remove the port from `cdc.yaml`'s `ports`. If it
has no clock, change `interface.yaml`'s `domain` to `clk_free`, and
change both side specs to match, so `split` stays clean.

**Trap:** do not leave both as they are and waive the finding. Whichever
file is wrong gives the wrong answer to the side that reads it: the
digital `cdc` gate, or `cosim`'s view of the timing.

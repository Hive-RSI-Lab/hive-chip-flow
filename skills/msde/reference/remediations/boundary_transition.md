# boundary_transition (boundary)

An a2d pin's edge into its declared load is over the library's max_transition
at the named corner, or never completed a 10-90% swing in the window.

**Cheapest fix first:** size up the output driver or add a buffer stage. Check
first that the declared load is what the digital side really presents. An
incomplete edge can also mean `window_ns` is too short for a running output.

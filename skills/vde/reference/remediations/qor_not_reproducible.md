# qor_not_reproducible (qor repro)

The recorded harden ran on a LibreLane, yosys, OpenROAD or PDK version that
is not this environment's, or recorded none. Re-running it here may give
different QoR. Routes to `harden`.

**Cheapest fix first:** re-harden on this environment, so the record names
the versions it ran on, then `qor diff` it against the old recorded run to
see what the tool change moved.

**Trap:** a pass recorded on other tools is still that tool's pass; don't
edit the recorded versions to match.

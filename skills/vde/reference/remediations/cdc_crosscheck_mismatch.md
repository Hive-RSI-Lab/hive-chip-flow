# cdc_crosscheck_mismatch (cdc)

OpenSTA, with a clock on each root port, counts different flops on it than the
netlist walk does, so one of the two misread the clock network and no crossing
result can be trusted. Compare the report's `crosscheck` with its `clocks`. An
engine bug is as likely as a design bug: do not change the RTL to satisfy it.

# macro_clock_undeclared (top_harden)

The SDC creates a clock on the analog macro's pin, and `macro_timing.clocks` in
`analog/spec/spec.yaml` names no record for it, so its period is only a typed
number. No harden ran.

**Fix:** declare it, with the record and measure the analog side wrote:
`clocks: {<pin>: {record: reports/recorded/pex_pvt.json, measure: <fmax measure>,
divide: <any divider after the measure>}}`, then run `macro_lib`. With no
`macro_timing` at all, declare the pins' limits and arcs too; `macro_lib` refuses
and names what is missing.

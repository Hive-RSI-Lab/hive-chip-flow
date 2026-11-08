# ua_pad_not_inserted (cosim)

`interface.yaml` maps pins under `ua_pins`, so `cosim` runs twice, `no_pad`
(the analog netlist as written) and `pad` (the netlist's block wrapped so
each ua port goes through the Tiny Tapeout ua pad model,
`engine/lib/padlib.py`). The pad could not be put in, so the gate refuses:
with no pad, a "pad" run would only be a second bare run.

The message names why:

- the analog spec has no `supply.vdd`, which the pad's ESD diodes return to;
- no `tb/test_*.py` names `analog_netlist`, so there is no netlist to wrap;
- no single top-level subckt in that netlist has every ua pin as a port
  (none, or more than one).

Fix the cosim bench or the analog netlist so one block subckt, instantiated
by nothing else in the file, carries every `ua_pins` entry as a port.
Triage it to the integrator.

**Trap:** do not remove the pin from `ua_pins` or rename the port to dodge
the wrap. The pad is on the shuttle whether the bench models it or not.

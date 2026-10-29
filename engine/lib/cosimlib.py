"""cosimlib.py - cocotbext-ams's MixedSignalBridge with two fixes a cosim
bench needs (the cosim gate, engine/scripts/check_cosim.py, which puts this
directory on the bench's PYTHONPATH):

    from cosimlib import CosimBridge
    bridge = CosimBridge(dut, [block], max_sync_interval_ns=1.0,
                         simulator_lib=os.environ["LIBNGSPICE_PATH"])

Neither changes what the bridge measures. Both were found on the PLL's
transistor cosim (ece298a-pll blocks/pll/tb), with the ideal and the
transistor model alike:

1. Sync rounding (SyncSafeNgspice). The stock sync callback clamps
   ngspice's next step to next_sync - ckttime. When ckttime lands a float
   rounding short of the sync point (3.9999999999999996e-09 against 4e-09)
   that remainder is ~1e-25 s, far below ngspice's minimum step, and the
   transient aborts with "Timestep too small" at the first sync
   (docs/spikes/dcosim.md's open shared-library quirk). A remainder under
   SYNC_EPS_S (1 fs) counts as reached.

2. Write on change (CosimBridge). The stock bridge Forces every analog
   output onto its HDL signal at every sync point; at 1 ns syncs that
   crashed vvp (SIGSEGV in the simulator thread, no Python frame) after a
   few hundred to a thousand forces. CosimBridge writes an output only when
   its digitized value changes, as a plain value, never a Force - so the
   bench's top must carry each a2d crossing net as a `reg` nothing in the
   Verilog drives (a wire driven elsewhere would win over the write). The
   value and the time it is written at are the stock bridge's own
   (digitized at the sync point, before cocotb advances).

Optionally, `ref_grid = (t0_s, period_s, lag_s)` adds a sync lag_s after
every rising edge of a reference clock that starts at t0_s: a digital
change reaches ngspice only at the next sync, so without it a PFD edge
launched by a cocotb Clock reaches the pump up to max_sync_interval late.
Set it on the bridge before `start()`.
"""
from __future__ import annotations

import math

from cocotbext.ams import MixedSignalBridge
from cocotbext.ams._ngspice import NgspiceInterface


class SyncSafeNgspice(NgspiceInterface):
    """NgspiceInterface whose sync callback treats a remainder under
    SYNC_EPS_S as the sync point reached (module docstring, fix 1), and
    adds ref_grid's syncs when set."""

    SYNC_EPS_S = 1e-15
    ref_grid: tuple[float, float, float] | None = None

    def next_ref_sync(self, ckttime: float) -> float | None:
        """The first ref_grid sync after ckttime (more than SYNC_EPS_S
        after it), or None with no ref_grid."""
        if self.ref_grid is None:
            return None
        t0, per, lag = self.ref_grid
        k = math.floor((ckttime - t0 - lag) / per + 1e-9) + 1
        t_next = t0 + lag + k * per
        if t_next - ckttime <= self.SYNC_EPS_S:
            t_next += per
        return t_next

    def _on_get_sync_data(self, ckttime, p_delta, old_delta, redostep,
                          ident, location, userdata):
        if not self._simulation_done:
            t_ref = self.next_ref_sync(ckttime)
            if t_ref is not None and t_ref < self._next_sync_time:
                self._next_sync_time = t_ref
            if 0.0 < self._next_sync_time - ckttime < self.SYNC_EPS_S:
                self._next_sync_time = ckttime
        return super()._on_get_sync_data(ckttime, p_delta, old_delta,
                                         redostep, ident, location, userdata)


class CosimBridge(MixedSignalBridge):
    """MixedSignalBridge on SyncSafeNgspice, writing each analog output to
    the HDL only when its digitized value changes (module docstring)."""

    ref_grid: tuple[float, float, float] | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._written: dict[tuple[str, str], int] = {}

    def _create_simulator(self, simulator):
        if simulator == "ngspice":
            sim = SyncSafeNgspice(self._simulator_lib)
            sim.ref_grid = self.ref_grid
            return sim
        return super()._create_simulator(simulator)

    def _read_analog_outputs(self, block):
        outs = self._block_output_nodes.get(block.name, {})
        for name, pin in block.digital_pins.items():
            if pin.direction != "output" or not outs.get(name):
                continue
            volts = [self._sim.get_node_voltage(n) for n in outs[name]]
            prev = self._sim._prev_digital_values.get(name)
            val = pin.analog_to_digital(volts, prev_value=prev)
            key = (block.name, name)
            if self._written.get(key) != val:
                self._resolve_signal(block.name, name).value = val
                self._written[key] = val

    def _release_outputs(self, block):
        pass  # nothing is forced, so nothing to release

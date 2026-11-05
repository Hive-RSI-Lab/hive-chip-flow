# removal_violation (timing)

OpenSTA's removal check failed at a flop's asynchronous reset (or set) pin:
the reset releases early, too close to the clock edge, inside the liberty's removal
window, so the flop may or may not leave reset on that edge and the
register's bits can leave reset on different cycles. The finding names the
flop instance and its slack. Routes to `rtl`.

**Cheapest fix first:** release the reset through a synchroniser on that
flop's clock (assert async, release on the clock), as `rdc_unsynced_release`
shows, and reset the flops from its last stage. If the release already comes
from a synchroniser, its last stage is on the wrong clock or edge, or the
reset net is buffered so heavily the release arrives before the removal window closes; check the path
OpenSTA reports.

**Trap:** do not cure it in `harden/constraints.sdc` with a false path or a
looser input delay on the reset port. That hides a real reset race; a
constraint that waives it is the person's call.

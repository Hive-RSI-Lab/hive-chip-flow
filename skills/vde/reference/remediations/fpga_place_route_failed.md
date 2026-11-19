# fpga_place_route_failed (fpga)

nextpnr-ice40 could not place or route the design on the board's device.
The message quotes nextpnr's error. Routes to `rtl`.

**Cheapest fix first:** read `log/fpga/nextpnr.log`. "Unable to place" or
a cell count above the device's (`utilisation` in the gate's report)
means the design is too big for the iCE40UP5K's 5280 logic cells:
share logic or shrink a memory. A pin error means the board top could not
put a port on its pin; tell the person, that is the flow's to fix.

**Trap:** do not cut features to fit the FPGA without asking: the FPGA is
a prototype of the chip, and the chip is what the spec describes.

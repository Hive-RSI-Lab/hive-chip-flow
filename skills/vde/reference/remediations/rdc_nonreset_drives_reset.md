# rdc_nonreset_drives_reset (cdc)

A flop with no async reset of its own drives another flop's async pin, directly
or through logic. Until that flop's first clock the net is unknown, so the reset
may be asserted, or stuck, at power-up. Routes to `rtl`.

**Fix:** give the driving flop a reset, or use a reset synchroniser. A waiver is
`{reset, domain?, reason}` in `spec/cdc.yaml`, naming the driving flop's net.

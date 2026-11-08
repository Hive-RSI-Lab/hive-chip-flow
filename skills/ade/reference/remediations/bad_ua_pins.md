# bad_ua_pins

spec.yaml's `ua_pins` is not a list of distinct port names (strings). It
names the block ports that leave the tile on Tiny Tapeout's analog `ua[k]`
pins, and each one gets the ua pad model in every bench.

Write it as a list, for example `ua_pins: [vout]`, with each name once.
Leave the key out for a block with no ua pin.

**Trap:** an empty or malformed list is not read as "no pins". It is
refused, so a typo can never quietly drop the pad.

# min_pulse_width_violation (timing)

A clock pin's high or low phase is shorter than the cell's liberty
`min_pulse_width`, even though setup and hold may be met. Routes to
`review`: the period and duty cycle come from spec.yaml's clock.

**Cheapest fix first:** check the clock's `-waveform` (a distorted duty
cycle from an analog divider or ring oscillator) and the period against
the cell's limit at the fast corner; a clock gate shortens the phase
too. If the duty cycle is real, the spec's clock requirement is the
thing to change, or the flop must be a cell with a smaller limit.

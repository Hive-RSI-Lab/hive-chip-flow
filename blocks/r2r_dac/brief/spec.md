# r2r_dac - 8-bit R-2R DAC on a Tiny Tapeout GF180 analog tile

Task brief, verbatim from the owner's board row (2026-09-25):

DESIGN: an 8-bit R-2R ladder from the GF180 PDK's poly resistors (pick R so the ladder settles within one 50 MHz period into the analog pad's load, and state the pad load you assumed), switched by standard-cell drivers at 3.3 V. One analog output pin. A small digital front end in the same tile: a code register loaded from ui_in, a free-running counter (ramp) mode and a 256-entry sine-table mode, selected by pins. GF180 gf180mcu_fd_sc_mcu7t5v0 at 3.3 V.

SETUP: TinyTapeout/ttgf-analog-template (the analog flow; import, don't fork), cocotb 2.0.1 pinned for the digital front end. Use the minimum analog pins (1).

QUANTIFY in ngspice, across tt/ff/ss and -40/25/125 C: DNL and INL (target +-0.5 LSB typical, and report the worst corner), monotonicity, full-scale range, full-scale settling to 0.5 LSB, and glitch energy at the mid-code transition. Include Monte-Carlo resistor mismatch if the PDK models support it; if they don't, say so. netgen LVS must be clean.

DONE WHEN: reports/checks.json shows every applicable gate fresh and passed, the TT precheck passes locally, and LVS is clean. Nothing is submitted.

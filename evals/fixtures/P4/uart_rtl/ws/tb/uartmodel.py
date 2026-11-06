"""Cycle-accurate reference model of the uart transmitter, written from
spec/spec.md alone (registered outputs; a start sampled on edge N gives
busy=1 / tx=0 after edge N; frame is 11*CLKS_PER_BIT clocks)."""

CPB = 4
FRAME = 11 * CPB


def frame_bits(byte):
    """11 frame bits: start, data[0..7], even parity, stop."""
    data = [(byte >> i) & 1 for i in range(8)]
    parity = sum(data) & 1
    return [0] + data + [parity, 1]


class UartModel:
    def __init__(self, cpb=CPB):
        self.cpb = cpb
        self.bits = None
        self.k = 0

    def outputs(self):
        if self.bits is None:
            return 1, 0
        return self.bits[self.k // self.cpb], 1

    def step(self, rst, start, data):
        """One rising edge with the given sampled inputs; returns the
        (tx, busy) visible after that edge."""
        if rst:
            self.bits = None
        elif self.bits is None:
            if start:
                self.bits = frame_bits(data & 0xFF)
                self.k = 0
        else:
            self.k += 1
            if self.k == 11 * self.cpb:
                self.bits = None
        return self.outputs()

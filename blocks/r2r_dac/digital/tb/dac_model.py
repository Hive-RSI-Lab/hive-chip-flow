"""Reference model for r2r_dac_digital, written from spec/spec.md alone."""
import math

SINE = [int(math.floor(127.5 + 127.5 * math.sin(2 * math.pi * i / 256) + 0.5)) for i in range(256)]


class Model:
    def __init__(self, pad_latency=0):
        self.code = 0
        self.cnt = 0
        self.presc = 0
        self.dac = 0
        # rst_n is registered once inside the block, after pad_latency flops
        # of the tile's pad synchroniser (engine/lib/padreset.latency); rst_n
        # low clears those pad flops at once, so only the release is delayed
        self.rst_chain = [0] * (1 + pad_latency)

    @property
    def rst_q(self):
        """the reset the block acts on at the next edge"""
        return self.rst_chain[-1]

    def sources(self, ui):
        return {0: ui, 1: self.cnt, 2: SINE[self.cnt], 3: self.code}

    def edge(self, rst_n, ui, mode, slow):
        rst_eff = self.rst_chain[-1]
        self.rst_chain = [rst_n] + self.rst_chain[:-1]
        if not rst_n:
            self.rst_chain = [0] * len(self.rst_chain)
        if not rst_eff:
            self.code = self.cnt = self.presc = self.dac = 0
            return self.dac
        self.dac = self.sources(ui)[mode]
        if mode == 0:
            self.code = ui
        old = self.presc
        self.presc = (self.presc + 1) % 256
        if slow == 0 or old == 255:
            self.cnt = (self.cnt + 1) % 256
        return self.dac

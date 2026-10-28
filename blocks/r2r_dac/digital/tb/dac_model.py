"""Reference model for r2r_dac_digital, written from spec/spec.md alone."""
import math

SINE = [int(math.floor(127.5 + 127.5 * math.sin(2 * math.pi * i / 256) + 0.5)) for i in range(256)]


class Model:
    def __init__(self):
        self.code = 0
        self.cnt = 0
        self.presc = 0
        self.dac = 0
        self.rst_q = 0  # rst_n is registered once inside the block

    def sources(self, ui):
        return {0: ui, 1: self.cnt, 2: SINE[self.cnt], 3: self.code}

    def edge(self, rst_n, ui, mode, slow):
        rst_eff, self.rst_q = self.rst_q, rst_n
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

"""Reference model for counter8: the spec's counter, one step per rising
edge. A plain module (not collected as a test)."""


class Counter8Model:
    def __init__(self):
        self.count = 0

    def step(self, rst=0):
        """One rising clk edge; returns the count after it."""
        self.count = 0 if rst else (self.count + 1) % 256
        return self.count

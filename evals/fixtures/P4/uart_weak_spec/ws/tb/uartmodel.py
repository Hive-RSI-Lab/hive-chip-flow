"""Reference model for the uart frame (spec.md "Frame timing"). Plain module,
not a test file. Clock index i is counted from the first clock of the frame;
bit k = i // CLKS_PER_BIT. Bit 9 is undefined in this revision -> None."""

CLKS_PER_BIT = 4  # spec.yaml params.CLKS_PER_BIT default


def expected_tx(byte, i, cpb=CLKS_PER_BIT):
    """Expected tx on frame clock i (0 <= i < 11*cpb); None where undefined."""
    k = i // cpb
    if k == 0:
        return 0
    if 1 <= k <= 8:
        return (byte >> (k - 1)) & 1
    if k == 9:
        return None
    return 1


def frame_len(cpb=CLKS_PER_BIT):
    return 11 * cpb

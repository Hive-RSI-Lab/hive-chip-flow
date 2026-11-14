import pytest


@pytest.mark.slow
def test_planted_failure_turns_slow_rest_red():
    assert False, "planted (#343): must turn slow (rest) red"

"""Tests for the pure martingale-detection core (martingale_service/detection.py).

The core defaults nothing; these tests fix the knobs once (ESC/FLOOR/CHAIN) and pass them
explicitly through the pm()/tm() helpers.
"""
from martingale_service.detection import (
    Step, pair_is_martingale, pair_martingale_chain, pair_martingale_triggers, trader_is_martingale)

MAX = 100.0                       # pair max size; floor = 10% = 10.0
ESC, FLOOR, CHAIN = 2.0, 0.10, 3  # standard knobs for these tests (explicit, one place)


def steps(*rows):
    """rows of (exposure, pnl); timestamps auto-increment."""
    return [Step(t_ms=i, exposure=e, pnl=p) for i, (e, p) in enumerate(rows)]


def pm(step_list, max_size):
    return pair_is_martingale(step_list, max_size,
                              escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)


def tm(steps_by_pair, max_size_by_pair):
    return trader_is_martingale(steps_by_pair, max_size_by_pair,
                                escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)


# ------------------------------------------------------------------ core: flagging

def test_clean_doubling_chain_underwater_flags():
    # exposures 15 -> 30 -> 60 (each 2x), all underwater
    assert pm(steps((15, -1), (30, -2), (60, -3)), MAX) is True


def test_two_levels_only_does_not_flag():
    # 15 -> 30 is only one escalation step (chain length 2); need length 3
    assert pm(steps((15, -1), (30, -2)), MAX) is False


def test_escalation_but_not_underwater_does_not_flag():
    # perfect doubling but P&L never < 0 -> no underwater streak
    assert pm(steps((15, 1), (30, 2), (60, 3)), MAX) is False


def test_underwater_but_flat_size_does_not_flag():
    assert pm(steps((20, -1), (20, -2), (20, -3)), MAX) is False


def test_breakeven_resets_streak_and_breaks_chain():
    # 15 -> 30 underwater, then breakeven (pnl>=0) resets, then 60 in a NEW streak.
    # No single streak has a length-3 chain.
    rows = steps((15, -1), (30, -2), (45, 0.5), (60, -1))
    assert pm(rows, MAX) is False


def test_non_adjacent_chain_flags():
    # underwater throughout; escalating levels are 10 -> 25 -> 60 with noise in between
    rows = steps((10, -1), (12, -1), (25, -2), (11, -2), (60, -3))
    assert pm(rows, MAX) is True


def test_floor_prevents_trivial_multiples():
    # raw 1 -> 3 -> 9 would be 3x each, but all are below the 10.0 floor -> clipped to 10 -> flat
    assert pm(steps((1, -1), (3, -2), (9, -3)), MAX) is False


def test_near_doubling_factor_catches_1_75():
    rows = steps((15, -1), (27, -2), (48, -3))          # ~1.8x each: misses 2.0, catches 1.75
    assert pair_is_martingale(rows, MAX, escalation_factor=2.0, floor_fraction=FLOOR, chain_length=CHAIN) is False
    assert pair_is_martingale(rows, MAX, escalation_factor=1.75, floor_fraction=FLOOR, chain_length=CHAIN) is True


def test_zero_max_size_is_not_martingale():
    assert pm(steps((15, -1), (30, -2), (60, -3)), 0.0) is False


def test_steps_are_ordered_by_t_ms():
    # same doubling chain, passed out of order; the core sorts by t_ms before analyzing
    shuffled = [Step(2, 60, -3), Step(0, 15, -1), Step(1, 30, -2)]
    assert pm(shuffled, MAX) is True


def test_signed_exposure_uses_magnitude():
    # negative exposures (e.g. shorts) escalate in magnitude 15 -> 30 -> 60 -> flagged
    shorts = [Step(0, -15, -1), Step(1, -30, -2), Step(2, -60, -3)]
    assert pm(shorts, MAX) is True


def test_pair_martingale_chain_returns_the_offending_chain():
    # the chain-returning primitive: escalating steps in time order (last = trigger); None if clean
    chain = pair_martingale_chain(steps((15, -1), (30, -2), (60, -3)), MAX,
                                  escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)
    assert [s.exposure for s in chain] == [15, 30, 60]
    assert pair_martingale_chain(steps((20, -1), (20, -2), (20, -3)), MAX,
                                 escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN) is None


def test_pair_martingale_triggers_lists_all_triggering_orders():
    # a doubling run of 5 levels -> a trigger at levels 3, 4 and 5 (each completes a >=3 chain)
    rows = steps((10, -1), (20, -2), (40, -3), (80, -4), (160, -5))
    trigs = pair_martingale_triggers(rows, MAX, escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)
    assert [s.exposure for s in trigs] == [40, 80, 160]
    assert pair_martingale_triggers(steps((20, -1), (20, -2), (20, -3)), MAX,
                                    escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN) == []


# --------------------------------------------------------------- floor_mode="exclude"

def pmx(step_list, max_size):
    return pair_is_martingale(step_list, max_size, escalation_factor=ESC,
                              floor_fraction=FLOOR, chain_length=CHAIN, floor_mode="exclude")


def test_exclude_mode_close_is_not_a_free_first_level():
    # clip: close (0 -> floored 10) + 25 + 55 = a chain; exclude: the close can't be a level
    rows = steps((0, -1), (25, -2), (55, -3))
    assert pm(rows, MAX) is True
    assert pmx(rows, MAX) is False


def test_exclude_mode_dust_still_cannot_make_trivial_multiples():
    # the floor's original purpose survives: 1 -> 3 -> 9 are all dust (< 10) -> no levels at all
    assert pmx(steps((1, -1), (3, -2), (9, -3)), MAX) is False


def test_exclude_mode_real_escalation_still_flags():
    assert pmx(steps((15, -1), (30, -2), (60, -3)), MAX) is True


def test_exclude_mode_chain_skips_ineligible_middle_steps():
    # dust and closes sit INSIDE the streak without breaking the chain around them
    rows = steps((15, -1), (0, -2), (5, -2), (30, -3), (60, -4))
    assert pmx(rows, MAX) is True


# --------------------------------------------------------------- trader-level OR

def test_trader_flagged_if_any_pair_martingale():
    clean = steps((20, -1), (20, -2), (20, -3))
    dirty = steps((15, -1), (30, -2), (60, -3))
    by_pair = {"BTCUSD": clean, "ETHUSD": dirty}
    max_by_pair = {"BTCUSD": MAX, "ETHUSD": MAX}
    assert tm(by_pair, max_by_pair) is True


def test_trader_not_flagged_if_no_pair_martingale():
    by_pair = {"BTCUSD": steps((20, -1), (20, -2)), "ETHUSD": steps((15, 1), (30, 2), (60, 3))}
    max_by_pair = {"BTCUSD": MAX, "ETHUSD": MAX}
    assert tm(by_pair, max_by_pair) is False

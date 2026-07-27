"""Tests for the service-added core helper ``pair_trigger_chain`` -- the audit-chain
reconstruction for one specific triggering order. Its trigger/no-trigger verdict must agree
exactly with ``pair_martingale_triggers`` (same DP, same eligibility rules)."""
from martingale_service.detection import Step, pair_martingale_triggers, pair_trigger_chain

MAX = 100.0                       # pair max size; floor = 10% = 10.0
ESC, FLOOR, CHAIN = 2.0, 0.10, 3


def steps(*rows):
    """rows of (exposure, pnl, order_id); timestamps auto-increment."""
    return [Step(t_ms=i, exposure=e, pnl=p, order_id=oid) for i, (e, p, oid) in enumerate(rows)]


def chain_for(step_list, order_id, floor_mode="clip"):
    return pair_trigger_chain(step_list, MAX, ESC, FLOOR, CHAIN, floor_mode, order_id=order_id)


def test_agrees_with_pair_martingale_triggers_on_every_order():
    rows = steps((10, -1, "a"), (20, -2, "b"), (40, -3, "c"), (80, -4, "d"), (160, -5, "e"))
    trigger_ids = {s.order_id for s in pair_martingale_triggers(
        rows, MAX, escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)}
    assert trigger_ids == {"c", "d", "e"}
    for s in rows:
        chain = chain_for(rows, s.order_id)
        assert (chain is not None) == (s.order_id in trigger_ids)


def test_chain_ends_at_the_requested_order_and_escalates():
    rows = steps((10, -1, "a"), (20, -2, "b"), (40, -3, "c"), (80, -4, "d"))
    chain = chain_for(rows, "d")
    assert [s.order_id for s in chain] == ["a", "b", "c", "d"]
    assert chain[-1].order_id == "d"


def test_later_trigger_gets_its_own_chain_not_the_first_one():
    # "c" completes the FIRST chain; asking for "e" must return a chain ending at "e".
    rows = steps((10, -1, "a"), (20, -2, "b"), (40, -3, "c"), (80, -4, "d"), (160, -5, "e"))
    chain = chain_for(rows, "e")
    assert chain[-1].order_id == "e"
    assert len(chain) >= CHAIN


def test_order_not_underwater_has_no_chain():
    # "c" is at breakeven-or-better, so it belongs to no underwater streak
    rows = steps((10, -1, "a"), (20, -2, "b"), (40, 1.0, "c"), (80, -1, "d"))
    assert chain_for(rows, "c") is None


def test_exclude_mode_skips_ineligible_levels():
    # dust ("x", below the 10.0 floor) sits inside the streak; the chain skips it and dust
    # itself can never be a trigger
    rows = steps((15, -1, "a"), (5, -1, "x"), (30, -2, "b"), (60, -3, "c"))
    chain = chain_for(rows, "c", floor_mode="exclude")
    assert [s.order_id for s in chain] == ["a", "b", "c"]
    assert chain_for(rows, "x", floor_mode="exclude") is None


def test_unknown_order_id_and_zero_max_size_return_none():
    rows = steps((10, -1, "a"), (20, -2, "b"), (40, -3, "c"))
    assert chain_for(rows, "nope") is None
    assert pair_trigger_chain(rows, 0.0, ESC, FLOOR, CHAIN, order_id="c") is None

"""Tests for the Vanta -> martingale-detector adapter (martingale_service/vanta_adapter.py).

Uses lightweight fakes duck-typed to the Vanta Position/Order fields the adapter reads, so
the tests need no vanta-network import. Prices in the martingale scenarios fall while the
trader is long, so the position is genuinely underwater (unrealized < 0). Note the opening
order of a fresh position is never underwater (unrealized == 0 at entry), so a single
position needs an open plus THREE escalating underwater orders to form a length-3 chain.

The adapter has no default parameters; tests fix the detection knobs once below (ESC/FLOOR/
CHAIN/WEEK2) and pass them explicitly via the mg()/mg_pairs() helpers.
"""
from dataclasses import dataclass
from typing import List, Optional

import pytest

from martingale_service.vanta_adapter import (
    build_trader_inputs,
    trader_is_martingale_from_positions,
    martingale_pairs,
    martingale_chains,
    martingale_triggers,
)


@dataclass
class FakeTP:
    trade_pair_id: str
    lot_size: float = 1.0
    max_leverage: float = 10.0


@dataclass
class FakeOrder:
    processed_ms: int
    quantity: Optional[float]     # signed delta, in lots
    leverage: Optional[float]     # signed (None for quantity-native orders)
    price: float
    slippage: float = 0.0
    quote_usd_rate: float = 1.0
    order_id: Optional[str] = None


@dataclass
class FakePosition:
    trade_pair: FakeTP
    orders: List[FakeOrder]
    close_ms: Optional[int] = None   # None => open


DAY = 24 * 60 * 60 * 1000
NOW = 1_700_000_000_000              # fixed "as-of" so tests are deterministic

# detection knobs for these tests -- explicit, one place (the adapter defaults nothing)
ESC, FLOOR, CHAIN = 2.0, 0.10, 3
WEEK2 = 14 * DAY


def pos(pair, orders, close_ms=None, **tp):
    return FakePosition(trade_pair=FakeTP(pair, **tp), orders=orders, close_ms=close_ms)


def mg(positions, as_of_ms, lookback_ms):
    return trader_is_martingale_from_positions(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms,
        escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)


def mg_pairs(positions, as_of_ms, lookback_ms):
    return martingale_pairs(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms,
        escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)


# --------------------------------------------------------- single-position flagging

def test_single_position_quantity_martingale_flags():
    # open + 3 escalating underwater orders (net qty 20 -> 40 -> 80). leverage tracks size.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),   # open, net 10 (pnl 0)
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0),    # net 20, underwater
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0),    # net 40, underwater
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0),    # net 80, underwater
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    assert mg([p], NOW, WEEK2) is True


def test_flat_leverage_but_quantity_escalates_flags():
    # SAME leverage on every order, but price keeps halving so QUANTITY doubles -> the
    # martingale shows up in quantity even though net leverage (2,4,6,8) never doubles.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=5, leverage=2.0, price=100.0),    # open,  net 5  (pnl 0)
        FakeOrder(t + 1, quantity=10, leverage=2.0, price=50.0),    # net 15, underwater
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=25.0),    # net 35, underwater
        FakeOrder(t + 3, quantity=40, leverage=2.0, price=12.5),    # net 75, underwater
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    assert mg([p], NOW, WEEK2) is True


def test_short_side_martingale_flags():
    # Shorts: quantity/leverage negative, price rises against the position -> underwater.
    # Escalating short size (net -20 -> -40 -> -80) is a martingale via |net_quantity|.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=-10, leverage=-1.0, price=100.0),   # open short, net -10 (pnl 0)
        FakeOrder(t + 1, quantity=-10, leverage=-1.0, price=105.0),   # net -20, underwater
        FakeOrder(t + 2, quantity=-20, leverage=-2.0, price=110.0),   # net -40, underwater
        FakeOrder(t + 3, quantity=-40, leverage=-4.0, price=115.0),   # net -80, underwater
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    assert mg([p], NOW, WEEK2) is True


def test_losing_hold_not_flagged():
    # A plain losing trade (open, then close at a loss) is not a martingale: no escalation.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=50, leverage=5.0, price=100.0),
        FakeOrder(t + 1, quantity=-50, leverage=-5.0, price=80.0),  # close at 80 < entry -> loss
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    assert mg([p], NOW, WEEK2) is False


# --------------------------------------------- cross-position (close+reopen) evasion

def test_cross_position_close_reopen_martingale_flags():
    # The reason underwater P&L is window-cumulative: position A has two escalating
    # underwater levels and closes at a loss; the trader immediately reopens position B with
    # a third, bigger level. Flattened across the close it is a length-3 escalating chain;
    # neither position ALONE is.
    t = NOW - 3 * DAY
    a = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),    # open, net 10
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0),     # net 20, underwater
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0),     # net 40, underwater
        FakeOrder(t + 3, quantity=-40, leverage=-4.0, price=88.0),   # flat: fill 88 < avg -> realized loss
    ]
    pos_a = pos("ETHUSD", a, close_ms=t + 3)
    b = [
        FakeOrder(t + DAY + 0, quantity=80, leverage=8.0, price=85.0),      # net 80 (still underwater via A's loss)
        FakeOrder(t + DAY + 1, quantity=-80, leverage=-8.0, price=84.0),    # flat
    ]
    pos_b = pos("ETHUSD", b, close_ms=t + DAY + 1)

    assert mg([pos_a, pos_b], NOW, WEEK2) is True
    # Proof it is the cross-position flattening that flags: neither position alone does.
    assert mg([pos_a], NOW, WEEK2) is False
    assert mg([pos_b], NOW, WEEK2) is False


def test_realized_pnl_computed_from_fills_not_read_from_order():
    # Realized P&L is derived from the fills, not read off the order. A closed WINNER carries
    # forward as POSITIVE realized, so a later position is not dragged underwater at its open.
    t = NOW - 2 * DAY
    winner = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),    # long @100
        FakeOrder(t + 1, quantity=-10, leverage=-1.0, price=120.0),  # close @120 -> +200 realized (from fills)
    ]
    later = [FakeOrder(t + DAY, quantity=10, leverage=1.0, price=100.0)]  # reopens flat
    ps = [pos("BTCUSD", winner, close_ms=t + 1), pos("BTCUSD", later, close_ms=None)]
    steps = build_trader_inputs(ps, NOW, WEEK2)[0]["BTCUSD"]
    assert steps[-1].pnl > 0.0     # opening order of `later`: +200 carried realized keeps pnl > 0


# ------------------------------------------------------------- scope / window / open

def test_open_position_is_always_included():
    # A still-open (never-closed) martingale is always scored -- there is no opt-out.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0),
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0),
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0),
    ]
    open_pos = pos("BTCUSD", orders, close_ms=None)   # still running
    assert mg([open_pos], NOW, WEEK2) is True


def test_position_closed_before_window_is_excluded():
    old = NOW - WEEK2 - DAY   # closed just before the 2-week window
    orders = [
        FakeOrder(old - 3, quantity=10, leverage=1.0, price=100.0),
        FakeOrder(old - 2, quantity=10, leverage=1.0, price=95.0),
        FakeOrder(old - 1, quantity=20, leverage=2.0, price=90.0),
        FakeOrder(old - 0, quantity=40, leverage=4.0, price=85.0),
    ]
    p = pos("BTCUSD", orders, close_ms=old)
    assert mg([p], NOW, WEEK2) is False
    assert mg([p], NOW, WEEK2 + 2 * DAY) is True   # ...but in scope with a longer look-back


def test_position_open_as_of_historical_asof_is_scoped_as_open():
    # Faithful history replay: a position whose real close is AFTER as_of must be treated as
    # OPEN as of that moment (post-as_of orders excluded), so a still-running martingale is
    # caught mid-build-up rather than hidden until it closes.
    t = NOW - 5 * DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0),
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0),
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0),
        FakeOrder(t + 100, quantity=-80, leverage=-8.0, price=999.0),  # closes later, after as_of
    ]
    p = pos("BTCUSD", orders, close_ms=t + 100)
    assert mg([p], t + 3, WEEK2) is True   # as-of t+3: open (close is later) -> scored mid-build-up


# -------------------------------------------------------------- trader-level / shape

def test_martingale_pairs_reports_only_the_flagged_pair():
    t = NOW - DAY
    dirty = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0),
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0),
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0),
    ]
    clean = [
        FakeOrder(t + 0, quantity=20, leverage=2.0, price=100.0),
        FakeOrder(t + 1, quantity=-20, leverage=-2.0, price=90.0),
    ]
    ps = [pos("ETHUSD", dirty, close_ms=NOW - DAY), pos("BTCUSD", clean, close_ms=NOW - DAY)]
    assert mg_pairs(ps, NOW, WEEK2) == ["ETHUSD"]


def test_martingale_chains_carries_order_ids_and_chain():
    # martingale_chains returns the escalating chain per flagged pair with order_ids attached;
    # the last step is the triggering order.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0, order_id="o0"),
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0, order_id="o1"),
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0, order_id="o2"),
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0, order_id="o3"),
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    chains = martingale_chains([p], as_of_ms=NOW, lookback_ms=WEEK2,
                               escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)
    assert list(chains) == ["BTCUSD"]
    chain = chains["BTCUSD"]
    assert [abs(s.exposure) for s in chain] == [20, 40, 80]   # the escalating underwater levels
    assert chain[-1].order_id == "o3"                          # trigger = order that completed the chain


def test_martingale_triggers_lists_all_triggering_orders_with_ids():
    # open + 5 escalating underwater orders -> triggers at the 3rd, 4th, 5th underwater level
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0, order_id="o0"),   # open (pnl 0)
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=95.0, order_id="o1"),    # net 20 uw
        FakeOrder(t + 2, quantity=20, leverage=2.0, price=90.0, order_id="o2"),    # net 40 uw
        FakeOrder(t + 3, quantity=40, leverage=4.0, price=85.0, order_id="o3"),    # net 80 uw  -> trigger
        FakeOrder(t + 4, quantity=80, leverage=8.0, price=80.0, order_id="o4"),    # net 160 uw -> trigger
        FakeOrder(t + 5, quantity=160, leverage=16.0, price=78.0, order_id="o5"),  # net 320 uw -> trigger
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    by_pair = martingale_triggers([p], as_of_ms=NOW, lookback_ms=WEEK2,
                                  escalation_factor=ESC, floor_fraction=FLOOR, chain_length=CHAIN)
    assert [s.order_id for s in by_pair["BTCUSD"]] == ["o3", "o4", "o5"]


def test_build_inputs_exposure_is_running_net_quantity():
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0),
        FakeOrder(t + 1, quantity=20, leverage=2.0, price=90.0),
        FakeOrder(t + 2, quantity=-30, leverage=-3.0, price=80.0),  # flat
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    steps_by_pair, max_by = build_trader_inputs([p], NOW, WEEK2)
    exposures = [s.exposure for s in steps_by_pair["BTCUSD"]]
    assert exposures == [10, 30, 0]                     # cumulative net qty, back to 0 at close
    assert max_by["BTCUSD"] == pytest.approx(100.0)     # 10 * median(10,10,10)


def test_opening_order_pnl_is_zero_even_with_slippage():
    # pnl(o) is measured the instant o is placed, so the window's first opening order is
    # pnl == 0 exactly -- slippage/spread must NOT make the open look underwater.
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=1.0, price=100.0, slippage=0.01),
        FakeOrder(t + 1, quantity=10, leverage=1.0, price=99.0, slippage=0.01),
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    steps_by_pair, _ = build_trader_inputs([p], NOW, WEEK2)
    steps = steps_by_pair["BTCUSD"]
    assert steps[0].pnl == 0.0                       # opening order: exactly flat
    assert steps[1].pnl < 0.0                        # first follow-on order: position now underwater


def test_quantity_native_orders_do_not_crash_when_leverage_is_none():
    # Current-schema quantity-native orders can carry leverage=None. Must not raise
    # (previously _entry_price did `leverage > 0` -> TypeError). Exposure comes from the
    # quantity sign; max_size falls back to 0 when NO order carries leverage, so such a pair
    # is conservatively left un-flagged (documented limitation).
    t = NOW - DAY
    orders = [
        FakeOrder(t + 0, quantity=10, leverage=None, price=100.0),
        FakeOrder(t + 1, quantity=10, leverage=None, price=95.0),
        FakeOrder(t + 2, quantity=20, leverage=None, price=90.0),
        FakeOrder(t + 3, quantity=40, leverage=None, price=85.0),
    ]
    p = pos("BTCUSD", orders, close_ms=NOW - DAY)
    steps_by_pair, max_by = build_trader_inputs([p], NOW, WEEK2)   # must not raise
    assert [s.exposure for s in steps_by_pair["BTCUSD"]] == [10, 20, 40, 80]
    assert max_by["BTCUSD"] == 0.0                                 # no leverage -> size cap unknown
    assert mg([p], NOW, WEEK2) is False


def test_missing_quantity_raises():
    t = NOW - DAY
    p = pos("BTCUSD", [FakeOrder(t, quantity=None, leverage=1.0, price=100.0)], close_ms=NOW - DAY)
    with pytest.raises(ValueError):
        build_trader_inputs([p], NOW, WEEK2)

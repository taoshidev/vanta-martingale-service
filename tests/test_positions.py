"""Tests for rebuilding adapter-ready positions from the two REST payload shapes."""
import pytest

from martingale_service.pairs import PairCatalog
from martingale_service.positions import positions_from_flat_orders, positions_from_snapshot


def catalog():
    c = PairCatalog()
    c.update_from_payload({"allowed": [
        {"trade_pair_id": "EURUSD", "trade_pair_category": "forex", "max_leverage": 10}]})
    return c


def od(ms, pair, qty, uuid, price=1.0, lev=1.0):
    return {"processed_ms": ms, "trade_pair_id": pair, "quantity": qty, "leverage": lev,
            "price": price, "slippage": 0.0, "quote_usd_rate": 1.0, "order_uuid": uuid}


def test_flat_orders_split_into_positions_at_net_zero():
    orders = [od(1, "EURUSD", 10, "a"), od(2, "EURUSD", -10, "b"),   # position 1, closed at t=2
              od(3, "EURUSD", 5, "c")]                                # position 2, still open
    by_pair, skipped = positions_from_flat_orders(orders, catalog())
    assert skipped == set()
    p1, p2 = by_pair["EURUSD"]
    assert [o.order_id for o in p1.orders] == ["a", "b"] and p1.close_ms == 2
    assert [o.order_id for o in p2.orders] == ["c"] and p2.close_ms is None
    assert p1.trade_pair.lot_size == 100_000.0         # the catalog spec rides on the position


def test_flat_orders_are_time_sorted_before_splitting():
    orders = [od(3, "EURUSD", 5, "c"), od(1, "EURUSD", 10, "a"), od(2, "EURUSD", -10, "b")]
    by_pair, _ = positions_from_flat_orders(orders, catalog())
    p1, p2 = by_pair["EURUSD"]
    assert [o.order_id for o in p1.orders] == ["a", "b"]
    assert [o.order_id for o in p2.orders] == ["c"]


def test_unknown_pair_is_deferred_not_guessed():
    by_pair, skipped = positions_from_flat_orders([od(1, "MYSTERY", 1, "a")], catalog())
    assert by_pair == {} and skipped == {"MYSTERY"}


def test_snapshot_positions_keep_the_validator_grouping():
    pos_dicts = [{
        "trade_pair": ["EURUSD", "EUR/USD", 7e-05, 0.1, 10],
        "orders": [od(1, "EURUSD", 10, "a"), od(2, "EURUSD", -10, "b")],
        "is_closed_position": True, "close_ms": 2,
    }, {
        "trade_pair": ["EURUSD", "EUR/USD", 7e-05, 0.1, 10],
        "orders": [od(3, "EURUSD", 5, "c")],
        "is_closed_position": False,
    }]
    by_pair, skipped = positions_from_snapshot(pos_dicts, catalog())
    assert skipped == set()
    p1, p2 = by_pair["EURUSD"]
    assert p1.close_ms == 2 and p2.close_ms is None
    assert [o.order_id for o in p2.orders] == ["c"]


def test_snapshot_unknown_pair_is_deferred():
    pos_dicts = [{"trade_pair": ["MYSTERY", "M/X", 0.001, 0.1, 10],
                  "orders": [od(1, "MYSTERY", 1, "a")], "is_closed_position": False}]
    by_pair, skipped = positions_from_snapshot(pos_dicts, catalog())
    assert by_pair == {} and skipped == {"MYSTERY"}


def test_missing_quantity_raises():
    with pytest.raises(ValueError):
        positions_from_flat_orders([od(1, "EURUSD", None, "a")], catalog())

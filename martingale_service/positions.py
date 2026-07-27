"""Rebuild adapter-ready positions (see ``vanta_adapter`` for the duck-typed shape) from the
two REST payload shapes:

- ``/miner-positions`` (sweep): positions already grouped by the validator; ``trade_pair`` is
  a >=5-element list whose ``[0]`` is the trade_pair_id.
- ``/orders/<hotkey>`` (doorbell): a flat filled-order list; positions are re-derived by
  splitting each pair's chronological orders wherever the running net quantity returns to zero.

Orders on pairs missing from the catalog are returned as skipped pair ids and deferred, not
evaluated on a guessed spec.
"""
from types import SimpleNamespace

_EPS = 1e-9   # |net quantity| below this counts as flat (a fresh position starts here)


def _order_ns(od):
    quantity = od.get("quantity")
    if quantity is None:
        raise ValueError(f"order {od.get('order_uuid')} has no quantity -- current-schema orders "
                         "always carry quantity; refusing to guess")
    return SimpleNamespace(
        processed_ms=od["processed_ms"], quantity=quantity, leverage=od.get("leverage"),
        price=od.get("price", 0.0), slippage=od.get("slippage") or 0.0,
        quote_usd_rate=od.get("quote_usd_rate") or 0.0, order_id=od.get("order_uuid"))


def positions_from_snapshot(position_dicts, catalog):
    """``/miner-positions`` position dicts -> ``({pair_id: [position, ...]}, skipped_pair_ids)``."""
    by_pair, skipped = {}, set()
    for pd in position_dicts:
        pair_id = pd["trade_pair"][0]
        spec = catalog.get(pair_id)
        if spec is None:
            skipped.add(pair_id)
            continue
        orders = [_order_ns(od) for od in pd["orders"]]
        if not orders:
            continue
        close_ms = pd.get("close_ms") if pd.get("is_closed_position") else None
        by_pair.setdefault(pair_id, []).append(
            SimpleNamespace(trade_pair=spec, orders=orders, close_ms=close_ms))
    return by_pair, skipped


def positions_from_flat_orders(order_dicts, catalog):
    """``/orders/<hotkey>`` flat filled-order dicts -> same shape as ``positions_from_snapshot``."""
    grouped = {}
    for od in order_dicts:
        grouped.setdefault(od["trade_pair_id"], []).append(od)

    by_pair, skipped = {}, set()
    for pair_id, ods in grouped.items():
        spec = catalog.get(pair_id)
        if spec is None:
            skipped.add(pair_id)
            continue
        ods.sort(key=lambda o: o["processed_ms"])
        net, group, positions = 0.0, [], []
        for od in ods:
            o = _order_ns(od)
            group.append(o)
            net += o.quantity
            if abs(net) < _EPS:                        # position closed here
                positions.append(SimpleNamespace(trade_pair=spec, orders=group,
                                                 close_ms=o.processed_ms))
                net, group = 0.0, []
        if group:                                      # leftover with net != 0 -> still open
            positions.append(SimpleNamespace(trade_pair=spec, orders=group, close_ms=None))
        if positions:
            by_pair[pair_id] = positions
    return by_pair, skipped

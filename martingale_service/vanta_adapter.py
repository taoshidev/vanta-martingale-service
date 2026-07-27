"""Builds the detector's ``(steps_by_pair, max_size_by_pair)`` from a trader's Vanta
``Position`` objects. Read-only and duck-typed on the fields below; never imports
vanta-network. Loading the positions is a separate concern (see ``positions.py``).

Two deliberate choices (rationale in the README): exposure is QUANTITY, not leverage (a
falling price hides escalation in leverage but not in size); and underwater P&L is cumulative
ACROSS a pair's positions, so closing and reopening can't evade detection.

Field contract (current Vanta schema)
--------------------------------------
Position: ``.trade_pair`` (``.trade_pair_id`` / ``.lot_size`` / ``.max_leverage``), ``.orders``,
          ``.close_ms`` (``None`` while open).
Order:    ``.processed_ms``, ``.quantity`` (signed lots, this order's delta), ``.price``,
          ``.slippage``, ``.quote_usd_rate``; ``.leverage`` (signed, optional -- max-size estimate
          only) and ``.order_id`` (optional, carried for reporting). P&L is computed from fills
          here; ``.realized_pnl`` is not read.
"""
from statistics import median

from .detection import (
    Step, pair_is_martingale, pair_martingale_chain, pair_martingale_triggers, trader_is_martingale)

_EPS = 1e-9   # |quantity| below this counts as flat: opening-order detection + dust filter in max_size

# NOTE: no tuning parameter is defaulted in this module. Every knob (window length, escalation
# factor, floor fraction, chain length, as-of time) must be passed in by the caller, so a run's
# numbers have exactly one visible source (see config.py's DETECTION PARAMETERS block).


def _in_scope(position, window_start_ms, as_of_ms):
    """In scope if the position closed within ``[window_start, as_of]`` -- OR it is still open as
    of ``as_of`` (no close, or a close AFTER ``as_of``, which matters when evaluating at a
    historical ``as_of``). The still-open position is ALWAYS included."""
    close_ms = getattr(position, "close_ms", None)
    if close_ms is None or close_ms > as_of_ms:   # not yet closed as of as_of -> open, always in
        return True
    return window_start_ms <= close_ms            # closed within [window_start, as_of]


def _entry_price(order, q):
    """Fill price adjusted for slippage, matching Vanta: a buy pays up, a sell receives less.

    Direction comes from the sign of the (signed) quantity ``q`` rather than ``leverage``:
    current-schema quantity-native orders can carry ``leverage=None`` (which would blow up
    ``leverage > 0``), and for an opening/increasing order ``sign(quantity) == sign(leverage)``.
    """
    return order.price * (1 + order.slippage) if q > 0 else order.price * (1 - order.slippage)


def _pair_steps(order_groups, lot_size):
    """Replay one pair's positions (each group = one position's orders) into ``Step``s.

    Net quantity and average entry reset per position; realized P&L carries ACROSS positions,
    so a close-then-reopen stays in one underwater streak. Per step, ``exposure`` is net
    quantity AFTER the order and ``pnl`` is measured the instant BEFORE it executes (so an
    opening order is exactly 0). P&L is computed from the fills here, not read from
    ``order.realized_pnl``.
    """
    steps = []
    cum_realized = 0.0
    for orders in order_groups:
        net_qty = 0.0        # net quantity BEFORE the current order
        avg_entry = 0.0      # weighted-average entry of that pre-order position
        for o in sorted(orders, key=lambda x: x.processed_ms):
            q = o.quantity
            if q is None:
                raise ValueError(
                    "order.quantity is None -- current-schema orders always carry quantity; "
                    "refusing to guess it. Investigate this order/position."
                )
            rate = o.quote_usd_rate or 0.0

            # pnl the instant o is placed (before it executes): realized so far + the pre-order
            # position's unrealized marked at o's price.
            unrealized = (o.price - avg_entry) * net_qty * lot_size * rate
            pnl = cum_realized + unrealized

            # apply o: advance the position (exposure = size AFTER o); realize P&L on a reduction.
            prev_net = net_qty
            new_net = prev_net + q
            fill = _entry_price(o, q)                     # slippage-adjusted fill: buy pays up, sell less
            if abs(prev_net) < _EPS:                      # opening order of this position
                avg_entry = fill
            elif (prev_net > 0) == (q > 0):               # increasing in the same direction
                avg_entry = (avg_entry * prev_net + fill * q) / new_net
            else:                                         # reducing/closing: realize on the closed qty
                cum_realized += -(fill - avg_entry) * q * lot_size * rate
                # weighted-average entry is unchanged by a reduction (Vanta rule)
            net_qty = new_net
            steps.append(Step(t_ms=o.processed_ms, exposure=net_qty, pnl=pnl,
                              order_id=getattr(o, "order_id", None)))
    return steps


def _pair_max_size(orders, max_leverage):
    """Estimated pair max size in lots: ``max_leverage * median(|quantity| / |leverage|)``.
    ``|quantity|/|leverage|`` is the size one leverage-unit buys; the median is outlier-robust
    and scaling by ``max_leverage`` gives the cap in the same unit as ``exposure``. Orders with
    ~zero leverage or quantity are ignored."""
    per_unit = [abs(o.quantity) / abs(o.leverage)
                for o in orders
                if o.quantity and o.leverage and abs(o.leverage) > _EPS]
    if not per_unit:
        return 0.0
    return max_leverage * median(per_unit)


def build_trader_inputs(positions, as_of_ms, lookback_ms):
    """One trader's Vanta ``positions`` -> ``(steps_by_pair, max_size_by_pair)``.

    ``as_of_ms`` -- window anchor (UTC epoch ms). In scope: positions closed with ``close_ms``
    in ``[as_of_ms - lookback_ms, as_of_ms]``, plus the position still open as of ``as_of_ms``
    (always included). Every position contributes its orders up to ``as_of_ms``.
    """
    window_start_ms = as_of_ms - lookback_ms

    groups_by_pair = {}   # pair_id -> list of positions, each a list of that position's in-scope orders
    tp_by_pair = {}
    for pos in positions:
        if not _in_scope(pos, window_start_ms, as_of_ms):
            continue
        orders = [o for o in pos.orders if o.processed_ms <= as_of_ms]
        if not orders:
            continue
        tp = pos.trade_pair
        pair = tp.trade_pair_id
        tp_by_pair.setdefault(pair, tp)
        groups_by_pair.setdefault(pair, []).append(orders)

    steps_by_pair = {}
    max_size_by_pair = {}
    for pair, groups in groups_by_pair.items():
        tp = tp_by_pair[pair]
        groups.sort(key=lambda g: min(o.processed_ms for o in g))   # positions are disjoint in time
        steps_by_pair[pair] = _pair_steps(groups, tp.lot_size)
        max_size_by_pair[pair] = _pair_max_size([o for g in groups for o in g], tp.max_leverage)
    return steps_by_pair, max_size_by_pair


def trader_is_martingale_from_positions(positions, *, as_of_ms, lookback_ms,
                                        escalation_factor, floor_fraction, chain_length,
                                        floor_mode="clip"):
    """True iff any pair of this trader is a martingale strategy over the window. Builds the
    inputs and defers to ``detection.trader_is_martingale``."""
    steps_by_pair, max_size_by_pair = build_trader_inputs(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms)
    return trader_is_martingale(steps_by_pair, max_size_by_pair,
                                escalation_factor, floor_fraction, chain_length, floor_mode)


def martingale_pairs(positions, *, as_of_ms, lookback_ms,
                     escalation_factor, floor_fraction, chain_length, floor_mode="clip"):
    """The trader's ``pair_id``s that are martingale strategies over the window (for
    reporting / drill-down). Empty list means the trader is not flagged."""
    steps_by_pair, max_size_by_pair = build_trader_inputs(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms)
    return [pair for pair, steps in steps_by_pair.items()
            if pair_is_martingale(steps, max_size_by_pair.get(pair, 0.0),
                                  escalation_factor, floor_fraction, chain_length, floor_mode)]


def martingale_chains(positions, *, as_of_ms, lookback_ms,
                      escalation_factor, floor_fraction, chain_length, floor_mode="clip"):
    """``{pair_id: [Step, ...]}`` for each flagged pair -- the escalating underwater chain that
    triggered it, in time order (the last element is the triggering order). Non-martingale pairs
    are omitted. Each chain ``Step`` carries ``order_id`` / ``t_ms`` / ``exposure`` for reporting."""
    steps_by_pair, max_size_by_pair = build_trader_inputs(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms)
    out = {}
    for pair, steps in steps_by_pair.items():
        chain = pair_martingale_chain(steps, max_size_by_pair.get(pair, 0.0),
                                      escalation_factor, floor_fraction, chain_length, floor_mode)
        if chain is not None:
            out[pair] = chain
    return out


def martingale_triggers(positions, *, as_of_ms, lookback_ms,
                        escalation_factor, floor_fraction, chain_length, floor_mode="clip"):
    """``{pair_id: [Step, ...]}`` -- EVERY triggering order per flagged pair (each escalating
    underwater level from the ``chain_length``-th on), in time order. Non-martingale pairs omitted.
    Each Step carries ``order_id`` / ``t_ms`` / ``exposure``. This is the count-all-triggers view
    (``martingale_chains`` only gives the first chain)."""
    steps_by_pair, max_size_by_pair = build_trader_inputs(
        positions, as_of_ms=as_of_ms, lookback_ms=lookback_ms)
    out = {}
    for pair, steps in steps_by_pair.items():
        trigs = pair_martingale_triggers(steps, max_size_by_pair.get(pair, 0.0),
                                         escalation_factor, floor_fraction, chain_length, floor_mode)
        if trigs:
            out[pair] = trigs
    return out

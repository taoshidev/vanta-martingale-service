"""Action-on-arrival evaluation: each order is judged once, the first time the service sees
it, and the verdict is frozen (see the README's "Guarantees" and "Warning, then elimination").

A triggering order's outcome is decided once here, from the subaccount's stored warning row,
and never recomputed:

    no warning row yet                              -> 'warning'
    >= grace_period_days after the warning's order  -> 'eliminate_eligible'
    otherwise                                       -> 'grace_period' (recorded, absorbed)

Orders before ``monitoring_started_ms`` are never judged and get no row; they are read only as
context for later orders' windows.
"""
import logging

from .detection import pair_trigger_chain
from .vanta_adapter import build_trader_inputs

log = logging.getLogger(__name__)

MS_PER_DAY = 24 * 60 * 60 * 1000


def window_lookback_ms(order_processed_ms, lookback_days):
    """Length of an order's detection window: ``lookback_days`` before the UTC midnight
    preceding the order, up to the order itself."""
    midnight_ms = (order_processed_ms // MS_PER_DAY) * MS_PER_DAY
    return lookback_days * MS_PER_DAY + (order_processed_ms - midnight_ms)


def evaluate_hotkey(store, params, hotkey, positions_by_pair, now_ms):
    """Evaluate every not-yet-seen order of one subaccount, in ``processed_ms`` order.

    ``positions_by_pair`` is the subaccount's full in-hand history from either payload shape
    (see ``positions``) -- context may predate monitoring, but rows are only written for
    orders at/after ``monitoring_started_ms``. Returns ``(n_evaluated, n_triggering)``.
    """
    state = store.ensure_subaccount(hotkey, now_ms)

    candidates = []
    for pair_id, positions in positions_by_pair.items():
        for position in positions:
            for o in position.orders:
                if o.processed_ms >= state.monitoring_started_ms and o.order_id:
                    candidates.append((o, pair_id))
    if not candidates:
        return 0, 0

    seen = store.seen_order_uuids([o.order_id for o, _ in candidates])
    unseen = [(o, pair_id) for o, pair_id in candidates if str(o.order_id) not in seen]
    unseen.sort(key=lambda t: (t[0].processed_ms, str(t[0].order_id)))

    n_evaluated = n_triggering = 0
    for o, pair_id in unseen:
        chain = _trigger_chain(o, pair_id, positions_by_pair[pair_id], params)
        outcome = chain_json = params_json = None
        if chain is not None:
            outcome = _decide_outcome(store, params, hotkey, o.processed_ms)
            price_by_id = {x.order_id: x.price
                           for p in positions_by_pair[pair_id] for x in p.orders}
            chain_json = [{"order_uuid": str(s.order_id), "exposure": s.exposure,
                           "price": price_by_id.get(s.order_id), "pnl": s.pnl} for s in chain]
            params_json = {"escalation_factor": params.escalation_factor,
                           "floor_fraction": params.floor_fraction,
                           "chain_length": params.chain_length,
                           "floor_mode": params.floor_mode,
                           "window_days": params.lookback_days,
                           "grace_period_days": params.grace_period_days}
        inserted = store.record_evaluation(
            order_uuid=str(o.order_id), synthetic_hotkey=hotkey, pair_id=pair_id,
            order_processed_ms=o.processed_ms, triggering=chain is not None,
            outcome=outcome, chain=chain_json, detector_params=params_json)
        if not inserted:
            continue                       # already evaluated by an earlier run: nothing applied
        n_evaluated += 1
        if chain is not None:
            n_triggering += 1
            log.info("TRIGGER %s %s order=%s outcome=%s chain_len=%d",
                     hotkey, pair_id, o.order_id, outcome, len(chain))
    return n_evaluated, n_triggering


def _trigger_chain(order, pair_id, pair_positions, params):
    """The escalating chain ``order`` completes at its own arrival (the audit trail), or
    ``None`` if it doesn't trigger. Window pinned to the order's own UTC day (see
    ``window_lookback_ms``); only this order itself can fire."""
    steps_by_pair, max_size_by_pair = build_trader_inputs(
        pair_positions, as_of_ms=order.processed_ms,
        lookback_ms=window_lookback_ms(order.processed_ms, params.lookback_days))
    return pair_trigger_chain(
        steps_by_pair.get(pair_id, []), max_size_by_pair.get(pair_id, 0.0),
        params.escalation_factor, params.floor_fraction, params.chain_length,
        params.floor_mode, order_id=order.order_id)


def _decide_outcome(store, params, hotkey, order_processed_ms):
    """The frozen outcome for a triggering order (see module docstring). Keyed off the stored
    warning row (written once, never updated) rather than the status column, so the
    at-most-one-warning invariant is enforced by the same fact the grace check reads."""
    warning_ms = store.warning_order_ms(hotkey)
    if warning_ms is None:
        return "warning"
    if order_processed_ms >= warning_ms + params.grace_period_days * MS_PER_DAY:
        return "eliminate_eligible"
    return "grace_period"

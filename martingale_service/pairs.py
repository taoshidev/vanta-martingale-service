"""Per-pair specs (lot size, max leverage) from the validator's ``GET /trade-pairs`` endpoint.

The detector needs ``max_leverage`` (anchors the level floor) and ``lot_size`` (scales P&L to
real units) per pair. Position payloads don't carry the category and so can't give lot size;
``/trade-pairs`` lists every pair's ``max_leverage`` and ``trade_pair_category``, and lot size
is a fixed function of the category (mirrored from the validator's ``TradePair.lot_size``).

Refreshed at the start of every sweep; a failed refresh keeps the previous catalog. An order
on a pair not in the catalog is deferred, not evaluated on a guessed spec.
"""
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Mirrors the validator's TradePair.lot_size: a constant per category...
LOT_SIZE_BY_CATEGORY = {
    "crypto": 1.0,
    "forex": 100_000.0,
    "indices": 1.0,
    "equities": 1.0,
    "commodities": 1.0,
}
# ...with two per-pair exceptions (both currently blocked pairs; kept for exactness).
LOT_SIZE_OVERRIDES = {"XAUUSD": 100.0, "XAGUSD": 5_000.0}


@dataclass(frozen=True)
class PairSpec:
    """Exactly what the detector's adapter reads off ``position.trade_pair`` (duck-typed)."""
    trade_pair_id: str
    lot_size: float
    max_leverage: float


class PairCatalog:
    """``trade_pair_id -> PairSpec``, built from ``/trade-pairs`` responses. A refresh
    replaces/extends entries but never removes them -- an order on a since-delisted pair must
    still be evaluable."""

    def __init__(self):
        self._specs = {}

    def get(self, trade_pair_id):
        return self._specs.get(trade_pair_id)

    def __len__(self):
        return len(self._specs)

    def update_from_payload(self, payload):
        """Ingest a ``GET /trade-pairs`` response (both ``allowed`` and ``disabled`` lists).
        Returns how many entries were ingested; malformed entries and unknown categories are
        skipped loudly (their pairs stay unevaluable rather than mis-specified)."""
        n = 0
        for entry in (payload.get("allowed") or []) + (payload.get("disabled") or []):
            pair_id = entry.get("trade_pair_id")
            category = entry.get("trade_pair_category")
            max_leverage = entry.get("max_leverage")
            if not pair_id or max_leverage is None:
                log.warning("malformed /trade-pairs entry skipped: %r", entry)
                continue
            lot_size = LOT_SIZE_OVERRIDES.get(pair_id, LOT_SIZE_BY_CATEGORY.get(category))
            if lot_size is None:
                log.warning("pair %s has unknown category %r -- skipped (its orders will not be "
                            "evaluated until this service learns the category)", pair_id, category)
                continue
            self._specs[pair_id] = PairSpec(pair_id, float(lot_size), float(max_leverage))
            n += 1
        return n

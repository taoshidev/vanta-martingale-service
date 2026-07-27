"""Tests for the pair catalog built from ``GET /trade-pairs`` payloads."""
from martingale_service.pairs import PairCatalog, PairSpec

PAYLOAD = {
    "allowed": [
        {"trade_pair_id": "EURUSD", "trade_pair_category": "forex", "max_leverage": 10},
        {"trade_pair_id": "BTCUSDC", "trade_pair_category": "crypto", "max_leverage": 1.0},
    ],
    "disabled": [
        {"trade_pair_id": "XAUUSD", "trade_pair_category": "forex", "max_leverage": 2},
    ],
}


def test_catalog_builds_specs_with_category_lot_size():
    catalog = PairCatalog()
    assert catalog.update_from_payload(PAYLOAD) == 3
    assert catalog.get("EURUSD") == PairSpec("EURUSD", 100_000.0, 10.0)
    assert catalog.get("BTCUSDC") == PairSpec("BTCUSDC", 1.0, 1.0)


def test_blocked_pairs_are_included_and_lot_size_overrides_apply():
    catalog = PairCatalog()
    catalog.update_from_payload(PAYLOAD)
    # XAUUSD comes from the "disabled" list and has a per-pair lot-size override
    assert catalog.get("XAUUSD") == PairSpec("XAUUSD", 100.0, 2.0)


def test_unknown_category_is_skipped_not_guessed():
    catalog = PairCatalog()
    n = catalog.update_from_payload({"allowed": [
        {"trade_pair_id": "NEWTHING", "trade_pair_category": "bonds", "max_leverage": 5}]})
    assert n == 0
    assert catalog.get("NEWTHING") is None


def test_malformed_entry_is_skipped():
    catalog = PairCatalog()
    n = catalog.update_from_payload({"allowed": [
        {"trade_pair_category": "forex", "max_leverage": 5},          # no id
        {"trade_pair_id": "EURUSD", "trade_pair_category": "forex"},  # no max_leverage
    ]})
    assert n == 0 and len(catalog) == 0


def test_refresh_updates_and_never_removes():
    catalog = PairCatalog()
    catalog.update_from_payload(PAYLOAD)
    catalog.update_from_payload({"allowed": [
        {"trade_pair_id": "EURUSD", "trade_pair_category": "forex", "max_leverage": 5}]})
    assert catalog.get("EURUSD").max_leverage == 5.0
    assert catalog.get("BTCUSDC") is not None          # absent from the refresh, still known

"""Evaluation semantics: the pinned window (pure) and the warn -> grace -> eliminate-eligible
state machine (DB-backed; needs TEST_DATABASE_URL, see conftest -- skipped otherwise).

The scenario is a textbook martingale on one pair: an open plus escalating orders while the
price falls, each taking the net position to 1.5x the previous level. Every order carries
|quantity| / |leverage| = 100, so the adapter's estimated max size is max_leverage(10) x 100
= 1000 and the floor is 3% of that = 30 -- all levels comfortably above it.
"""
import os
from types import SimpleNamespace

from martingale_service.config import DetectionParams
from martingale_service.evaluation import MS_PER_DAY, evaluate_hotkey, window_lookback_ms
from martingale_service.pairs import PairSpec

PARAMS = DetectionParams(escalation_factor=1.5, floor_fraction=0.03, chain_length=4,
                         lookback_days=10, floor_mode="exclude", grace_period_days=3)

BASE = 20_600 * MS_PER_DAY          # an exact UTC midnight; the absolute date is irrelevant
HOUR = 60 * 60 * 1000
HK = "5FakeEntityHotkey_7"          # synthetic hotkey (subaccount)


def uuid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def order(ms, qty, n, price):
    return SimpleNamespace(processed_ms=ms, quantity=qty, leverage=qty / 100.0, price=price,
                           slippage=0.0, quote_usd_rate=1.0, order_id=uuid(n))


def by_pair(orders):
    spec = PairSpec("EURUSD", lot_size=1.0, max_leverage=10.0)
    return {"EURUSD": [SimpleNamespace(trade_pair=spec, orders=list(orders), close_ms=None)]}


O1 = order(BASE + 1 * HOUR, 100.0, 1, 1.10)       # open: net 100, pnl 0 (not underwater)
O2 = order(BASE + 2 * HOUR, 50.0, 2, 1.09)        # net 150, underwater (level 1)
O3 = order(BASE + 3 * HOUR, 75.0, 3, 1.08)        # net 225   = 1.5 x 150 (level 2)
O4 = order(BASE + 4 * HOUR, 112.5, 4, 1.07)       # net 337.5 = 1.5 x 225 (level 3)
O5 = order(BASE + 5 * HOUR, 168.75, 5, 1.06)      # net 506.25 -> 4th level: TRIGGER (warning)
O6 = order(BASE + 1 * MS_PER_DAY, 253.125, 6, 1.05)             # trigger 19h after O5: grace
O7 = order(BASE + 3 * MS_PER_DAY + 6 * HOUR, 379.6875, 7, 1.04)  # >= 3d after O5: eliminate-eligible


def _rows(hotkey):
    import psycopg
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return conn.execute(
            "SELECT order_uuid::text, triggering, outcome::text, chain, detector_params "
            "FROM martingale_order_evaluations WHERE synthetic_hotkey = %s "
            "ORDER BY order_processed_ms", (hotkey,)).fetchall()


# ------------------------------------------------------------------------- pure window math

def test_window_is_pinned_to_the_prior_utc_midnight():
    assert window_lookback_ms(BASE, 10) == 10 * MS_PER_DAY               # exactly at midnight
    assert window_lookback_ms(BASE + 5 * HOUR, 10) == 10 * MS_PER_DAY + 5 * HOUR


# ------------------------------------------------------------------------ state machine (DB)

def test_first_trigger_warns_and_rows_are_frozen(store):
    n_evaluated, n_triggering = evaluate_hotkey(
        store, PARAMS, HK, by_pair([O1, O2, O3, O4, O5]), now_ms=BASE)
    assert (n_evaluated, n_triggering) == (5, 1)

    state = store.ensure_subaccount(HK, BASE)
    assert state.status == "warned"
    assert state.elimination_action == "none"
    assert store.warning_order_ms(HK) == O5.processed_ms

    rows = _rows(HK)
    assert [r[0] for r in rows] == [uuid(n) for n in (1, 2, 3, 4, 5)]
    assert [r[1] for r in rows] == [False, False, False, False, True]
    assert [r[2] for r in rows] == [None, None, None, None, "warning"]

    _, _, _, chain, detector_params = rows[-1]
    assert [c["order_uuid"] for c in chain] == [uuid(n) for n in (2, 3, 4, 5)]
    assert chain[-1]["price"] == 1.06
    assert all(c["pnl"] < 0 for c in chain)            # what made the streak underwater
    assert detector_params["window_days"] == 10
    assert rows[0][3] is None and rows[0][4] is None   # non-triggering rows carry no chain


def test_grace_then_eliminate_eligible(store):
    evaluate_hotkey(store, PARAMS, HK, by_pair([O1, O2, O3, O4, O5]), now_ms=BASE)

    # a trigger inside the 3-day grace window is recorded but absorbed
    n_evaluated, n_triggering = evaluate_hotkey(
        store, PARAMS, HK, by_pair([O1, O2, O3, O4, O5, O6]), now_ms=BASE)
    assert (n_evaluated, n_triggering) == (1, 1)
    state = store.ensure_subaccount(HK, BASE)
    assert state.status == "warned" and state.elimination_action == "none"
    assert _rows(HK)[-1][2] == "grace_period"

    # a trigger >= 3 days after the warning's order becomes eliminate-eligible
    evaluate_hotkey(store, PARAMS, HK, by_pair([O1, O2, O3, O4, O5, O6, O7]), now_ms=BASE)
    state = store.ensure_subaccount(HK, BASE)
    assert state.status == "warned"                    # elimination is a human decision
    assert state.elimination_action == "pending_review"
    assert _rows(HK)[-1][2] == "eliminate_eligible"
    assert store.warning_order_ms(HK) == O5.processed_ms   # still exactly one warning row


def test_reprocessing_is_idempotent(store):
    payload = by_pair([O1, O2, O3, O4, O5])
    evaluate_hotkey(store, PARAMS, HK, payload, now_ms=BASE)
    assert evaluate_hotkey(store, PARAMS, HK, payload, now_ms=BASE) == (0, 0)
    assert len(_rows(HK)) == 5
    assert store.warning_order_ms(HK) == O5.processed_ms


def test_pre_monitoring_orders_are_context_not_judged(store):
    # Monitoring starts between O2 and O3: O1/O2 never get rows, but they still provide the
    # streak/chain context that lets O5 trigger -- and O5 is the order that gets judged.
    started = BASE + 2 * HOUR + 30 * 60 * 1000
    n_evaluated, n_triggering = evaluate_hotkey(
        store, PARAMS, HK, by_pair([O1, O2, O3, O4, O5]), now_ms=started)
    assert (n_evaluated, n_triggering) == (3, 1)
    rows = _rows(HK)
    assert [r[0] for r in rows] == [uuid(n) for n in (3, 4, 5)]
    assert store.warning_order_ms(HK) == O5.processed_ms
    # the audit chain may cite pre-monitoring orders as evidence -- they were context
    assert _rows(HK)[-1][3][0]["order_uuid"] == uuid(2)


def test_subaccount_with_no_new_orders_still_gets_a_state_row(store):
    assert evaluate_hotkey(store, PARAMS, "5AnotherEntity_1", {}, now_ms=BASE) == (0, 0)
    state = store.ensure_subaccount("5AnotherEntity_1", BASE)
    assert state.status == "clean" and state.monitoring_started_ms == BASE


def test_non_uuid_order_ids_are_handled(store):
    # Vanta bracket / flat-all orders carry a suffixed id, not a clean UUID -- these must round
    # through the store as plain text and never be cast to a Postgres uuid.
    oids = ["6122f3f1-b622-4f57-85d2-190e6fa703f9-bracket-0",
            "629fa61a-c147-4624-a8e5-921908351b7f_flat_all"]
    store.ensure_subaccount(HK, BASE)
    assert store.seen_order_uuids(oids) == set()       # must not raise on non-uuid ids
    store.record_evaluation(order_uuid=oids[0], synthetic_hotkey=HK, pair_id="EURUSD",
                            order_processed_ms=BASE, triggering=False, outcome=None,
                            chain=None, detector_params=None)
    assert store.seen_order_uuids(oids) == {oids[0]}

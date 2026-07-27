"""Postgres persistence (psycopg 3). The two tables are defined in ``schema.sql``; in
production their DDL is owned by the Vanta UI project's migration flow -- this module only
reads and writes rows, it never runs DDL.

All writes are idempotent: the evaluation insert is keyed on ``order_uuid`` with
``ON CONFLICT DO NOTHING``, and state transitions apply only when that insert actually
landed, so restarts and websocket/sweep overlaps can never double-apply an outcome.
"""
import logging
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubaccountState:
    synthetic_hotkey: str
    status: str
    monitoring_started_ms: int
    elimination_action: str


class Store:
    """One connection, lazily (re)opened, in autocommit mode with explicit transaction blocks
    for the atomic groups. The single evaluator worker is the only writer, so there is no
    in-process concurrency to manage; transactions exist for atomicity only."""

    def __init__(self, database_url):
        self._url = database_url
        self._conn = None

    def _c(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._url, autocommit=True)
        return self._conn

    def recycle(self):
        """Drop the connection after an error; the next call reconnects."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def close(self):
        self.recycle()

    def ping(self):
        self._c().execute("SELECT 1")

    def ensure_subaccount(self, hotkey, now_ms):
        """Fetch the subaccount's state row, creating it on first sight -- monitoring starts
        NOW, so the account's pre-existing orders are context, never judged."""
        conn = self._c()
        with conn.transaction():
            conn.execute(
                "INSERT INTO martingale_subaccount_state (synthetic_hotkey, monitoring_started_ms) "
                "VALUES (%s, %s) ON CONFLICT (synthetic_hotkey) DO NOTHING", (hotkey, now_ms))
            row = conn.execute(
                "SELECT status, monitoring_started_ms, elimination_action "
                "FROM martingale_subaccount_state WHERE synthetic_hotkey = %s",
                (hotkey,)).fetchone()
        return SubaccountState(hotkey, row[0], row[1], row[2])

    def seen_order_uuids(self, order_uuids):
        """The subset of ``order_uuids`` that already have an evaluation row (as strings)."""
        if not order_uuids:
            return set()
        rows = self._c().execute(
            "SELECT order_uuid FROM martingale_order_evaluations "
            "WHERE order_uuid = ANY(%s::text[])",
            ([str(u) for u in order_uuids],)).fetchall()
        return {r[0] for r in rows}

    def warning_order_ms(self, hotkey):
        """``order_processed_ms`` of the subaccount's warning row, or None if never warned.
        Safe to read anytime: ``outcome`` is written once and never updated, so there is at
        most one such row and its answer never changes."""
        row = self._c().execute(
            "SELECT order_processed_ms FROM martingale_order_evaluations "
            "WHERE synthetic_hotkey = %s AND outcome = 'warning'", (hotkey,)).fetchone()
        return row[0] if row else None

    def record_evaluation(self, *, order_uuid, synthetic_hotkey, pair_id, order_processed_ms,
                          triggering, outcome, chain, detector_params):
        """Insert the evaluation row and apply its state transition atomically. Returns whether
        the row was actually inserted (False = already evaluated; nothing is re-applied)."""
        conn = self._c()
        with conn.transaction():
            cur = conn.execute(
                "INSERT INTO martingale_order_evaluations "
                "(order_uuid, synthetic_hotkey, pair_id, order_processed_ms, triggering, "
                " outcome, chain, detector_params) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (order_uuid) DO NOTHING",
                (order_uuid, synthetic_hotkey, pair_id, order_processed_ms, triggering, outcome,
                 Jsonb(chain) if chain is not None else None,
                 Jsonb(detector_params) if detector_params is not None else None))
            if cur.rowcount != 1:
                return False
            if outcome == "warning":
                conn.execute(
                    "UPDATE martingale_subaccount_state "
                    "SET status = 'warned', updated_at = now() "
                    "WHERE synthetic_hotkey = %s AND status = 'clean'", (synthetic_hotkey,))
            elif outcome == "eliminate_eligible":
                conn.execute(
                    "UPDATE martingale_subaccount_state "
                    "SET elimination_action = 'pending_review', updated_at = now() "
                    "WHERE synthetic_hotkey = %s AND elimination_action = 'none'",
                    (synthetic_hotkey,))
        return True

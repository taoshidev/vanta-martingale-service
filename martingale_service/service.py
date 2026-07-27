"""Service wiring: the reconcile sweep and the websocket doorbell both feed one evaluator
worker, which writes to Postgres (architecture and failure model in the README). The sweep
alone is correct and every write is idempotent, so any transient failure is safely retried.
The service only records -- it never eliminates automatically.
"""
import logging
import queue
import signal
import threading
import time

from .config import DETECTION, load_runtime_config
from .db import Store
from .evaluation import evaluate_hotkey
from .pairs import PairCatalog
from .positions import positions_from_flat_orders, positions_from_snapshot
from .rest import VantaRest
from .ws import start_doorbell

log = logging.getLogger(__name__)


def _now_ms():
    return int(time.time() * 1000)


def sweep_loop(rest, catalog, jobs, stop, interval_s):
    """Every ``interval_s``: refresh the pair catalog, download the snapshot, and enqueue
    every subaccount (synthetic hotkey = contains ``_``; entity parents and regular miners
    are out of scope). Waits for the queue to drain so at most one snapshot is in memory."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            n_pairs = catalog.update_from_payload(rest.trade_pairs())
            log.debug("pair catalog refreshed: %d entries", n_pairs)
        except Exception as e:
            log.warning("pair catalog refresh failed (%s); keeping previous %d entries",
                        e, len(catalog))
        try:
            snapshot = rest.miner_positions()
            n = 0
            for hotkey, payload in snapshot.items():
                if "_" not in hotkey:
                    continue
                position_dicts = payload["positions"] if isinstance(payload, dict) else payload
                jobs.put(("sweep", hotkey, position_dicts))
                n += 1
            log.info("sweep enqueued %d subaccounts", n)
            jobs.join()
        except Exception as e:
            log.error("sweep failed: %s", e)
        stop.wait(max(0.0, interval_s - (time.monotonic() - started)))


def worker_loop(store, rest, catalog, jobs, stop, params):
    """The single evaluator. Jobs are ``("sweep", hotkey, position_dicts)`` or
    ``("ws", hotkey, None)`` -- a websocket job fetches the account's filled orders itself.
    Per-job failures are logged and dropped; the next sweep retries anything that mattered."""
    while not stop.is_set() or not jobs.empty():
        try:
            source, hotkey, position_dicts = jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if source == "ws":
                by_pair, skipped = positions_from_flat_orders(rest.orders(hotkey), catalog)
            else:
                by_pair, skipped = positions_from_snapshot(position_dicts, catalog)
            if skipped:
                log.warning("%s: pairs missing from catalog, their orders deferred: %s",
                            hotkey, sorted(skipped))
            n_evaluated, n_triggering = evaluate_hotkey(store, params, hotkey, by_pair, _now_ms())
            if n_evaluated:
                log.info("%s: evaluated %d new orders (%d triggering) [%s]",
                         hotkey, n_evaluated, n_triggering, source)
        except Exception:
            log.exception("failed evaluating %s (from %s); a later sweep will retry",
                          hotkey, source)
            store.recycle()
        finally:
            jobs.task_done()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_runtime_config()
    log.info("starting: %s | sweep_interval=%ss doorbell=%s",
             DETECTION, cfg.sweep_interval_s, "on" if cfg.ws_url else "off (sweep only)")

    store = Store(cfg.database_url)
    store.ping()                                       # fail fast on a bad DATABASE_URL
    rest = VantaRest(cfg.rest_base_url, cfg.api_key)
    catalog = PairCatalog()
    jobs = queue.Queue()
    stop = threading.Event()

    def shutdown(signum, _frame):
        log.info("signal %s received; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if cfg.ws_url:
        start_doorbell(cfg.ws_url, cfg.api_key,
                       lambda hotkey: jobs.put(("ws", hotkey, None)), stop)
    sweeper = threading.Thread(target=sweep_loop, name="sweep", daemon=True,
                               args=(rest, catalog, jobs, stop, cfg.sweep_interval_s))
    sweeper.start()

    worker_loop(store, rest, catalog, jobs, stop, DETECTION)   # runs until stop is set
    store.close()
    log.info("stopped")
    return 0

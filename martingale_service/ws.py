"""Websocket doorbell (latency path only): subscribes to the validator's position broadcast
and rings a callback with a subaccount's hotkey whenever it trades, so the service can fetch
and evaluate that account immediately instead of waiting for the next sweep. Losing the
connection never loses detections (the sweep re-discovers everything), so failure handling is
just log-backoff-reconnect.
"""
import asyncio
import json
import logging
import threading

import websockets

log = logging.getLogger(__name__)

_MAX_BACKOFF_S = 60


def hotkey_from_message(raw):
    """Subaccount hotkey from one broadcast frame, or ``None`` for anything else (auth acks,
    subscription acks, pongs, dashboard payloads, entity/miner updates...). Position frames
    look like ``{"sequence": n, "timestamp": ms, "data": {"position": {...,
    "miner_hotkey": hk}}}``; synthetic (subaccount) hotkeys are
    ``{entity_hotkey}_{subaccount_id}``, and a bare SS58 hotkey never contains ``_``."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    position = data.get("position")
    if not isinstance(position, dict):
        return None
    hotkey = position.get("miner_hotkey")
    if isinstance(hotkey, str) and "_" in hotkey:
        return hotkey
    return None


async def _listen(ws_url, api_key, ring, stop):
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(
                    ws_url, additional_headers={"Authorization": f"Bearer {api_key}"},
                    open_timeout=15, proxy=None) as ws:   # connect directly; ignore any ambient
                                                          # proxy env (matches the REST client)
                await ws.send(json.dumps({"type": "subscribe", "all": True}))
                log.info("doorbell connected to %s", ws_url)
                backoff = 1
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue                       # idle; just re-check the stop flag
                    hotkey = hotkey_from_message(raw)
                    if hotkey:
                        ring(hotkey)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if stop.is_set():
                break
            log.warning("doorbell connection lost (%s); reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)


def start_doorbell(ws_url, api_key, ring, stop):
    """Run the listener on its own daemon thread (own asyncio loop); returns the thread.
    ``ring(hotkey)`` must be thread-safe -- the service passes a ``queue.Queue.put``."""
    def runner():
        asyncio.run(_listen(ws_url, api_key, ring, stop))

    thread = threading.Thread(target=runner, name="doorbell", daemon=True)
    thread.start()
    return thread

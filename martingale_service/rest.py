"""Minimal Vanta validator REST client (stdlib urllib; no extra dependencies).

The service only ever reads, so every call here is a GET:

- ``GET /trade-pairs`` -- per-pair specs for the catalog (KB-sized).
- ``GET /miner-positions?tier=100`` -- the full-history snapshot the reconcile sweep diffs
  against the database (~10MB gzipped; served pre-gzipped, transparently decompressed here).
- ``GET /orders/<hotkey>?status=filled`` -- one subaccount's filled orders (doorbell path).
  404 means "never traded" and maps to an empty list, not an error.
"""
import gzip
import json
import urllib.error
import urllib.request


class VantaRest:
    def __init__(self, base_url, api_key):
        self._base = base_url.rstrip("/")
        self._key = api_key

    def _get(self, path, *, timeout_s):
        req = urllib.request.Request(self._base + path,
                                     headers={"Authorization": f"Bearer {self._key}"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
        if body[:2] == b"\x1f\x8b":                    # pre-gzipped payload (miner-positions)
            body = gzip.decompress(body)
        return json.loads(body)

    def trade_pairs(self):
        return self._get("/trade-pairs", timeout_s=30)

    def miner_positions(self):
        return self._get("/miner-positions?tier=100", timeout_s=300)

    def orders(self, hotkey):
        try:
            payload = self._get(f"/orders/{hotkey}?status=filled", timeout_s=30)
        except urllib.error.HTTPError as e:
            if e.code == 404:                          # never traded
                return []
            raise
        return payload.get("filled", []) if isinstance(payload, dict) else payload

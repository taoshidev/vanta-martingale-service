"""Tests for the doorbell's frame filter (pure -- no socket involved)."""
import json

from martingale_service.ws import hotkey_from_message


def frame(position):
    return json.dumps({"sequence": 1, "timestamp": 2, "data": {"position": position}})


def test_position_broadcast_for_a_subaccount_rings():
    assert hotkey_from_message(frame({"miner_hotkey": "5Abc_12", "orders": []})) == "5Abc_12"


def test_regular_miner_position_is_ignored():
    # bare SS58 hotkey (no "_") = regular miner or entity parent -> out of scope
    assert hotkey_from_message(frame({"miner_hotkey": "5PlainMinerHotkey"})) is None


def test_non_position_frames_are_ignored():
    assert hotkey_from_message(json.dumps({"status": "success", "tier": 200})) is None
    assert hotkey_from_message(json.dumps({"type": "pong", "timestamp": 3})) is None
    assert hotkey_from_message(json.dumps({"data": {"dashboard": {}}})) is None
    assert hotkey_from_message(json.dumps(["not", "a", "dict"])) is None
    assert hotkey_from_message("not json at all") is None

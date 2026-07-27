"""Tests for runtime-config loading (env first, git-ignored secrets.json fallback)."""
import json

import pytest

from martingale_service.config import load_runtime_config

ALL_VARS = ("VANTA_REST_BASE_URL", "VANTA_API_KEY", "DATABASE_URL",
            "VANTA_WS_URL", "SWEEP_INTERVAL_SECONDS")


def _clear_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_missing_required_settings_exit_with_a_clear_message(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit, match="VANTA_REST_BASE_URL"):
        load_runtime_config(secrets_path=str(tmp_path / "does-not-exist.json"))


def test_secrets_file_fallback_and_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({
        "VANTA_REST_BASE_URL": "https://validator.example/",
        "VANTA_API_KEY": "k",
        "DATABASE_URL": "postgresql://localhost/x",
    }))
    cfg = load_runtime_config(secrets_path=str(secrets))
    assert cfg.rest_base_url == "https://validator.example"   # trailing slash stripped
    assert cfg.ws_url is None                                 # doorbell off by default
    assert cfg.sweep_interval_s == 60.0


def test_environment_wins_over_the_secrets_file(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({
        "VANTA_REST_BASE_URL": "https://file.example",
        "VANTA_API_KEY": "k",
        "DATABASE_URL": "postgresql://localhost/x",
        "SWEEP_INTERVAL_SECONDS": 60,
    }))
    monkeypatch.setenv("VANTA_REST_BASE_URL", "https://env.example")
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "120")
    cfg = load_runtime_config(secrets_path=str(secrets))
    assert cfg.rest_base_url == "https://env.example"
    assert cfg.sweep_interval_s == 120.0

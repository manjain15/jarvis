"""Live spend bind address and constant-time token check."""

import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import live_spend


def test_bind_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("JARVIS_SPEND_HOST", raising=False)
    assert live_spend.bind_host() == "127.0.0.1"
    monkeypatch.setenv("JARVIS_SPEND_HOST", "0.0.0.0")
    assert live_spend.bind_host() == "0.0.0.0"
    monkeypatch.setenv("JARVIS_SPEND_HOST", "  ")
    assert live_spend.bind_host() == "127.0.0.1"


def test_spend_token_compare_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(live_spend, "DATA_DIR", tmp_path)
    monkeypatch.setattr(live_spend, "SPEND_FILE", tmp_path / "live_spend.jsonl")
    monkeypatch.setattr(live_spend, "TOKEN_FILE", tmp_path / ".spend_token")
    secret = "s" * 32
    (tmp_path / ".spend_token").write_text(secret + "\n")
    client = live_spend.create_app().test_client()

    missing = client.post("/spend", json={"amount": 1, "category": "Other"})
    assert missing.status_code == 401
    short = client.post(
        "/spend",
        headers={"X-Jarvis-Token": "nope"},
        json={"amount": 1, "category": "Other"},
    )
    assert short.status_code == 401
    wrong = client.post(
        "/spend",
        headers={"X-Jarvis-Token": "t" * 32},
        json={"amount": 1, "category": "Other"},
    )
    assert wrong.status_code == 401
    ok = client.post(
        "/spend",
        headers={"X-Jarvis-Token": secret},
        json={"amount": 4.5, "category": "Transport", "note": "bus"},
    )
    assert ok.status_code == 200
    assert ok.get_json()["ok"] is True
    assert "4.50" in ok.get_json()["logged"]

"""Agent API auth, brief persistence, and route validation.

Handlers that talk to Claude, Google, or Mem0 are stubbed. The SSH-target
loader is exercised against a temp .env so the real host never enters the test.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import agent_api

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _client():
    return agent_api.create_app(token=TOKEN).test_client()


def test_bearer_matches_and_rejects():
    assert agent_api.bearer_matches("Bearer secret", "secret") is True
    assert agent_api.bearer_matches("bearer secret", "secret") is True
    assert agent_api.bearer_matches("Bearer wrong", "secret") is False
    assert agent_api.bearer_matches("Bearer ", "secret") is False
    assert agent_api.bearer_matches("Token secret", "secret") is False
    assert agent_api.bearer_matches("Bearer secret", "") is False
    assert agent_api.bearer_matches("", "secret") is False
    # Different lengths must not raise.
    assert agent_api.bearer_matches("Bearer short", "a-much-longer-secret") is False


def test_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        agent_api.create_app()


def test_every_route_requires_bearer():
    client = _client()
    checks = [
        ("get", "/health", None),
        ("post", "/ask", {"question": "hi"}),
        ("get", "/flags", None),
        ("get", "/context", None),
        ("get", "/memory/search", None),
        ("get", "/brief", None),
        ("post", "/spend", {"amount": 1, "category": "Other"}),
    ]
    for method, path, body in checks:
        kwargs = {}
        if body is not None:
            kwargs["json"] = body
        naked = getattr(client, method)(path, **kwargs)
        assert naked.status_code == 401, path
        bad = getattr(client, method)(path, headers={"Authorization": "Bearer no"}, **kwargs)
        assert bad.status_code == 401, path


def test_health_ok():
    res = _client().get("/health", headers=AUTH)
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert res.headers["Cache-Control"] == "no-store"


def test_ask_validation_and_read_only_brain(monkeypatch):
    calls = {}

    def fake_chat(message, context, turns, tools=None):
        calls["message"] = message
        calls["context"] = context
        calls["turns"] = turns
        calls["tools"] = tools
        return "prioritise the assignment"

    fake = type(sys)("jarvis_telegram")
    fake.build_static_context = lambda: "ctx"
    fake.chat_with_claude = fake_chat
    monkeypatch.setitem(sys.modules, "jarvis_telegram", fake)

    assert agent_api.answer_question("  what's due? ") == "prioritise the assignment"
    assert calls == {
        "message": "what's due?",
        "context": "ctx",
        "turns": [],
        "tools": [],
    }
    with pytest.raises(ValueError):
        agent_api.answer_question("   ")

    client = _client()
    empty = client.post("/ask", json={}, headers=AUTH)
    assert empty.status_code == 400
    ok = client.post("/ask", json={"question": "what's due?"}, headers=AUTH)
    assert ok.status_code == 200
    assert ok.get_json()["answer"] == "prioritise the assignment"


def test_memory_route_rejects_blank_query():
    res = _client().get("/memory/search", headers=AUTH)
    assert res.status_code == 400
    assert "q is required" in res.get_json()["error"]


def test_flags_context_memory_spend_are_wired(monkeypatch):
    with pytest.raises(ValueError):
        agent_api.search_memory_payload("  ")

    monkeypatch.setattr(agent_api, "get_flags_payload", lambda: {"flags": ["due soon"], "academic_alerts": [], "unavailable": []})
    monkeypatch.setattr(agent_api, "get_context_payload", lambda days_ahead: {"days_ahead": days_ahead, "deadlines": []})
    monkeypatch.setattr(agent_api, "search_memory_payload", lambda query, limit=5: {"query": query, "limit": limit, "text": "remembered"})
    monkeypatch.setattr(agent_api, "log_spend_entry", lambda amount, category, note="": {
        "ok": True, "logged": "$4.00 Other", "week_total": "$4.00",
        "entry": {"amount": amount, "category": category, "note": note},
    })

    client = _client()
    assert client.get("/flags", headers=AUTH).get_json()["flags"] == ["due soon"]

    ctx = client.get("/context?days=14", headers=AUTH)
    assert ctx.status_code == 200
    assert ctx.get_json()["days_ahead"] == 14
    assert client.get("/context?days=0", headers=AUTH).status_code == 400

    mem = client.get("/memory/search?q=mentor&limit=2", headers=AUTH)
    assert mem.get_json()["text"] == "remembered"

    spend = client.post("/spend", json={"amount": 4, "category": "Other", "note": "bus"}, headers=AUTH)
    assert spend.status_code == 200
    assert spend.get_json()["entry"]["note"] == "bus"


def test_brief_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_api, "BRIEF_FILE", tmp_path / "latest_brief.json")
    client = _client()
    missing = client.get("/brief", headers=AUTH)
    assert missing.status_code == 404

    agent_api.save_latest_brief("<h2>Monday</h2><p>Ship the assignment.</p>", "morning", "Monday")
    stored = client.get("/brief", headers=AUTH).get_json()
    assert stored["kind"] == "morning"
    assert stored["label"] == "Monday"
    assert "Ship the assignment." in stored["text"]
    assert "<p>" not in stored["text"]


def test_sync_scripts_have_no_ipv4_literal():
    for rel in (
        "deploy/sync-data-down.sh",
        "deploy/sync-finance-up.sh",
        "deploy/load-vps-target.sh",
        "deploy/vps-deploy.sh",
        ".github/workflows/deploy.yml",
        ".env.example",
    ):
        text = (REPO_ROOT / rel).read_text()
        assert _IPV4.search(text) is None, rel
        if rel.startswith("deploy/sync-"):
            assert "JARVIS_VPS_HOST" in text


def test_vps_target_loader(tmp_path):
    script = REPO_ROOT / "deploy" / "load-vps-target.sh"
    env_file = tmp_path / ".env"
    env_file.write_text('export JARVIS_VPS_USER="deploy"\nJARVIS_VPS_HOST=\'vps.example\'\n')

    out = subprocess.check_output(
        ["bash", "-c", f'set -euo pipefail; unset JARVIS_VPS_USER JARVIS_VPS_HOST; REPO_ROOT="{tmp_path}"; source "{script}"; printf "%s" "$VPS"'],
        text=True,
    )
    assert out == "deploy@vps.example"

    overridden = subprocess.check_output(
        ["bash", "-c", f'set -euo pipefail; REPO_ROOT="{tmp_path}"; source "{script}"; printf "%s" "$VPS"'],
        text=True,
        env={**os.environ, "JARVIS_VPS_USER": "other", "JARVIS_VPS_HOST": "host.example"},
    )
    assert overridden == "other@host.example"

    env_file.unlink()
    missing = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; unset JARVIS_VPS_USER JARVIS_VPS_HOST; REPO_ROOT="{tmp_path}"; source "{script}"'],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in ("JARVIS_VPS_USER", "JARVIS_VPS_HOST")},
    )
    assert missing.returncode != 0
    assert "JARVIS_VPS_USER" in missing.stderr

    bad = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; REPO_ROOT="{tmp_path}"; source "{script}"'],
        capture_output=True,
        text=True,
        env={**os.environ, "JARVIS_VPS_USER": "bad user", "JARVIS_VPS_HOST": "host.example"},
    )
    assert bad.returncode != 0

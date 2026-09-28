"""Agent API auth, brief persistence, route validation, and term-context writes.

Handlers that talk to Claude, Google, or Mem0 are stubbed. The SSH-target
loader is exercised against a temp .env so the real host never enters the test.
"""

import datetime
import json
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
import term_context

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
        ("patch", "/mentor", {"actor": "career", "awaiting_response": False}),
        ("patch", "/internships", {"actor": "career", "company": "Canva", "status": "rejected"}),
        ("post", "/internships", {
            "actor": "career", "company": "Atlassian", "role": "SWE", "status": "applied",
        }),
        ("get", "/finance", None),
        ("get", "/finance/spending", None),
        ("get", "/finance/savings", None),
        ("get", "/finance/subscriptions", None),
        ("get", "/finance/reselling", None),
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


def _today():
    """A date that is today for flag math and not in the future for Sydney validation."""
    local = datetime.date.today()
    sydney = datetime.datetime.now(agent_api.TIMEZONE).date()
    return min(local, sydney)


def _iso(days_ago):
    return (_today() - datetime.timedelta(days=days_ago)).isoformat()


def _seed(tmp_path, monkeypatch, mentor=None, internships=None):
    """Point term context and the audit log at temp files and return the context path."""
    ctx = {}
    if mentor is not None:
        ctx["mentor"] = mentor
    if internships is not None:
        ctx["internships"] = internships
    path = tmp_path / "term_context.json"
    path.write_text(json.dumps(ctx))
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "agent_api_audit.jsonl")
    return path


def _mentor():
    return {
        "name": "Emily",
        "email": "emily@example.com",
        "last_contact": _iso(40),
        "last_topic": "intro",
        "awaiting_response": True,
    }


def _canva():
    return {
        "company": "Canva",
        "role": "Software Engineer Intern",
        "status": "applied",
        "last_update": _iso(40),
        "next_action": "wait",
        "notes": "applied in May",
        "url": "https://www.canva.com/careers/",
    }


def _flags(client):
    res = client.get("/flags", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["unavailable"] == []
    return body["flags"]


def _audit_lines(tmp_path):
    path = tmp_path / "agent_api_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_mentor_patch_clears_flag_and_writes_audit(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    monkeypatch.setattr(agent_api, "upcoming_calendar_events", lambda days_ahead: [])
    client = _client()

    assert any("Google Mentor" in flag for flag in _flags(client))

    recent = _today().isoformat()
    res = client.patch("/mentor", headers=AUTH, json={
        "actor": "  career  ",
        "reason": "Check-in sent today",
        "last_contact": recent,
        "last_topic": "Check-in after the 7 Sep email",
        "awaiting_response": True,
        "next_action": "Wait for a reply",
        "notes": "Emailed earlier\nstill waiting",
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["changed"] is True
    assert body["audit_logged"] is True
    assert body["mentor"]["email"] == "emily@example.com"
    assert body["mentor"]["name"] == "Emily"
    assert body["mentor"]["last_contact"] == recent
    assert body["mentor"]["notes"] == "Emailed earlier\nstill waiting"
    assert body["changes"]["last_contact"]["old"] == _iso(40)
    assert body["changes"]["last_contact"]["new"] == recent

    assert not any("Google Mentor" in flag for flag in _flags(client))
    stored = json.loads(path.read_text())
    assert stored["mentor"]["email"] == "emily@example.com"
    assert stored["internships"][0]["company"] == "Canva"

    context = client.get("/context", headers=AUTH)
    assert context.status_code == 200
    assert context.get_json()["term"]["mentor"]["last_topic"] == "Check-in after the 7 Sep email"

    again = client.patch("/mentor", headers=AUTH, json={
        "actor": "career",
        "last_contact": recent,
        "awaiting_response": True,
    })
    assert again.status_code == 200
    assert again.get_json()["changed"] is False
    assert again.get_json()["audit_logged"] is False

    lines = _audit_lines(tmp_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["actor"] == "career"
    assert entry["action"] == "mentor_update"
    assert entry["target"] == {"record": "mentor"}
    assert entry["reason"] == "Check-in sent today"
    assert entry["old"]["last_contact"] == _iso(40)
    assert entry["new"]["last_contact"] == recent
    assert "awaiting_response" not in entry["new"]
    assert "T" in entry["ts"]

    silent = client.patch("/mentor", headers=AUTH, json={
        "actor": "career",
        "last_contact": _iso(40),
        "awaiting_response": False,
    })
    assert silent.status_code == 200
    assert not any("Google Mentor" in flag for flag in _flags(client))


def test_internship_patch_changes_flags(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    client = _client()
    assert any("Canva" in flag and "no update" in flag for flag in _flags(client))

    recent = _today().isoformat()
    refreshed = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "canva",
        "last_update": recent,
        "notes": "Still open",
    })
    assert refreshed.status_code == 200
    assert refreshed.get_json()["internship"]["url"] == "https://www.canva.com/careers/"
    assert refreshed.get_json()["internship"]["status"] == "applied"
    assert not any("Canva" in flag for flag in _flags(client))

    waiting = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Canva",
        "status": "OA_completed",
    })
    assert waiting.status_code == 200
    flags = _flags(client)
    assert any("OA completed" in flag for flag in flags)
    assert not any("no update" in flag for flag in flags)

    interviewing = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Canva",
        "role": "software engineer intern",
        "status": "interview",
    })
    assert interviewing.status_code == 200
    assert not any("Canva" in flag for flag in _flags(client))

    closed = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Canva",
        "status": "rejected",
        "last_update": _iso(40),
        "next_action": "No further action",
    })
    assert closed.status_code == 200
    assert not any("Canva" in flag for flag in _flags(client))
    stored = json.loads(path.read_text())["internships"][0]
    assert stored["status"] == "rejected"
    assert stored["url"] == "https://www.canva.com/careers/"
    assert stored["last_update"] == _iso(40)

    audit = _audit_lines(tmp_path)
    assert audit[0]["action"] == "internship_update"
    assert audit[0]["target"]["company"] == "Canva"
    assert audit[0]["target"]["role"] == "Software Engineer Intern"
    assert audit[0]["old"]["last_update"] == _iso(40)
    assert audit[0]["new"]["last_update"] == recent
    assert audit[-1]["new"]["status"] == "rejected"


def test_add_internship_is_visible_to_flags(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    client = _client()

    created = client.post("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Dolby",
        "role": "Audio Intern",
        "status": "applied",
        "last_update": _iso(30),
        "next_action": "Check the portal",
        "notes": "No reply yet",
        "reason": "Record was missing",
    })
    assert created.status_code == 201
    assert created.get_json()["internship"]["company"] == "Dolby"
    assert any("Dolby" in flag for flag in _flags(client))

    amazon = client.post("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Amazon",
        "role": "SDE Intern",
        "status": "withdrawn",
    })
    assert amazon.status_code == 201
    row = amazon.get_json()["internship"]
    assert row["status"] == "withdrawn"
    assert row["last_update"] == datetime.datetime.now(agent_api.TIMEZONE).date().isoformat()
    assert not any("Amazon" in flag for flag in _flags(client))

    duplicate = client.post("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "dolby",
        "role": "audio intern",
        "status": "applied",
    })
    assert duplicate.status_code == 409
    companies = [app["company"] for app in json.loads(path.read_text())["internships"]]
    assert companies.count("Dolby") == 1

    audit = _audit_lines(tmp_path)
    added = [line for line in audit if line["action"] == "internship_add"]
    assert added[0]["old"] is None
    assert added[0]["new"]["company"] == "Dolby"
    assert added[0]["actor"] == "career"
    assert added[0]["reason"] == "Record was missing"


def test_write_validation_and_misses_leave_the_file_unchanged(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    before = path.read_text()
    client = _client()
    future = (datetime.datetime.now(agent_api.TIMEZONE).date() + datetime.timedelta(days=2)).isoformat()
    cases = [
        ("patch", "/mentor", {"last_contact": _today().isoformat()}),
        ("patch", "/mentor", {"actor": "career agent", "awaiting_response": False}),
        ("patch", "/mentor", {"actor": "career", "email": "other@example.com", "notes": "x"}),
        ("patch", "/mentor", {"actor": "career", "awaiting_response": "false"}),
        ("patch", "/mentor", {"actor": "career", "last_contact": "28/09/2026"}),
        ("patch", "/mentor", {"actor": "career", "last_contact": "2026-02-31"}),
        ("patch", "/mentor", {"actor": "career", "last_contact": future}),
        ("patch", "/mentor", {"actor": "career", "notes": "bad\x00note"}),
        ("patch", "/mentor", {"actor": "career", "last_topic": None}),
        ("patch", "/mentor", []),
        ("patch", "/mentor", {"actor": "career"}),
        ("patch", "/internships", {"actor": "career", "company": "Canva", "status": "Applied"}),
        ("patch", "/internships", {"actor": "career", "company": "Canva", "status": "hired"}),
        ("patch", "/internships", {"actor": "career", "status": "rejected"}),
        ("patch", "/internships", {"actor": "career", "company": "Netflix", "status": "rejected"}),
        ("patch", "/internships", {"actor": "career", "company": "Canva", "salary": "1"}),
        ("post", "/internships", {"actor": "career", "company": "Atlassian", "role": "SWE"}),
        ("post", "/internships", {
            "actor": "career", "company": "Atlassian", "role": "SWE", "status": "applied", "level": "intern",
        }),
    ]
    for method, route, payload in cases:
        res = getattr(client, method)(route, headers=AUTH, json=payload)
        assert res.status_code in (400, 404), (route, payload, res.status_code, res.get_json())
        assert path.read_text() == before
    assert _audit_lines(tmp_path) == []

    empty = client.patch(
        "/mentor",
        headers={**AUTH, "Content-Type": "application/json"},
        data="not-json",
    )
    assert empty.status_code == 400
    assert path.read_text() == before

    missing_mentor = _seed(tmp_path, monkeypatch, internships=[_canva()])
    untouched = missing_mentor.read_text()
    gone = _client().patch("/mentor", headers=AUTH, json={"actor": "career", "notes": "hi"})
    assert gone.status_code == 404
    assert missing_mentor.read_text() == untouched


def test_ambiguous_internship_does_not_write(tmp_path, monkeypatch):
    rows = [
        _canva(),
        {
            "company": "Canva",
            "role": "Product Intern",
            "status": "applied",
            "last_update": _iso(40),
            "next_action": "wait",
        },
    ]
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=rows)
    before = path.read_text()
    client = _client()
    ambiguous = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Canva",
        "status": "withdrawn",
    })
    assert ambiguous.status_code == 409
    assert "role" in ambiguous.get_json()["error"]
    assert path.read_text() == before

    picked = client.patch("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Canva",
        "role": "Product Intern",
        "status": "withdrawn",
    })
    assert picked.status_code == 200
    stored = json.loads(path.read_text())["internships"]
    by_role = {row["role"]: row["status"] for row in stored}
    assert by_role["Product Intern"] == "withdrawn"
    assert by_role["Software Engineer Intern"] == "applied"


def test_writes_skip_the_proposal_queue(tmp_path, monkeypatch):
    watched = [
        REPO_ROOT / "memory" / "term_proposed_updates.json",
        REPO_ROOT / "memory" / "proposed_updates.json",
        REPO_ROOT / "memory" / "proposal_trust.json",
    ]
    before = {path: path.read_bytes() if path.exists() else None for path in watched}
    _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    client = _client()
    assert client.patch("/mentor", headers=AUTH, json={
        "actor": "career",
        "awaiting_response": False,
    }).status_code == 200
    assert client.post("/internships", headers=AUTH, json={
        "actor": "career",
        "company": "Dolby",
        "role": "Audio Intern",
        "status": "offer",
    }).status_code == 201
    for path, snapshot in before.items():
        assert (path.read_bytes() if path.exists() else None) == snapshot


def test_audit_failure_leaves_term_context_unchanged(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch, mentor=_mentor(), internships=[_canva()])
    before = path.read_text()

    def fail(_entry):
        raise OSError("disk full")

    monkeypatch.setattr(agent_api, "append_external_audit", fail)
    res = _client().patch("/mentor", headers=AUTH, json={
        "actor": "career",
        "awaiting_response": False,
    })
    assert res.status_code == 500
    assert res.get_json()["error"] == "mentor update failed"
    assert path.read_text() == before
    assert not (tmp_path / "agent_api_audit.jsonl").exists()

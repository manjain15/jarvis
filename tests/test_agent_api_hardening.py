"""Hardening added after review: data-loss guard, redaction, per-bot tokens,
throttling, daily caps, spend audit/dedupe, brief fallback, watchdog probe."""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import agent_api
import finance_tracker
import term_context

LONG = "x" * 40


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(agent_api, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    return tmp_path


# ── H1: unreadable term_context.json is never overwritten ─────────────────────

def test_corrupt_context_is_not_overwritten(paths):
    ctx = paths / "term_context.json"
    original = '{"subjects": [1, 2, 3], "internships": [{"company": "A"}'  # torn write
    ctx.write_text(original)
    with pytest.raises(RuntimeError):
        term_context.add_internship("Foo", "Intern", "applied")
    assert ctx.read_text() == original


def test_write_keeps_a_backup_of_the_previous_file(paths):
    ctx = paths / "term_context.json"
    ctx.write_text(json.dumps({"internships": []}))
    term_context.add_internship("Foo", "Intern", "applied")
    bak = json.loads((paths / "term_context.json.bak").read_text())
    assert bak == {"internships": []}


def test_missing_context_file_still_starts_empty(paths):
    term_context.add_internship("Foo", "Intern", "applied")
    assert json.loads((paths / "term_context.json").read_text())["internships"][0]["company"] == "Foo"


# ── M1: redaction ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Transfer 062000 12345678",
    "Card 4111 1111 1111 1111",
    "Card 4111-1111-1111-1111",
    "Osko to 0412 345 678",
    "Acct 123 456 789",
    "Visa xx1234",
    "PayID jo@example.com",
])
def test_redaction_covers_separated_numbers_tails_and_emails(text):
    out = agent_api.redact_account_numbers(text)
    assert "[redacted]" in out
    assert not any(ch.isdigit() for ch in out)


def test_redaction_keeps_dates_and_amounts():
    assert agent_api.redact_account_numbers("2026-09-28") == "2026-09-28"
    assert agent_api.redact_account_numbers("Ref 2026-09-28 2026-09-29") == "Ref 2026-09-28 2026-09-29"
    assert agent_api.redact_account_numbers("$1,234.56 on 01/09/2026") == "$1,234.56 on 01/09/2026"


def test_number_straddling_the_truncation_point_is_redacted():
    day = datetime.date(2026, 9, 1)
    txn = {"date": day, "debit": 200.0, "category": "Other",
           "description": "x" * 73 + " 123456789012"}
    out = finance_tracker.summarise_spending(
        [txn], day, day, 75, redact=agent_api.redact_account_numbers)
    assert not any(ch.isdigit() for ch in out["flagged"][0]["description"])


# ── H2/H3: per-bot tokens, scopes, throttling, caps ───────────────────────────

def _scoped_app(monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKENS", f"career:{'c' * 40},money:{'m' * 40},study:{'s' * 40}")
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    return agent_api.create_app().test_client()


def test_scoped_tokens_only_reach_their_routes(monkeypatch, paths):
    client = _scoped_app(monkeypatch)
    assert client.get("/finance/savings", headers=_bearer("c" * 40)).status_code == 403
    assert client.post("/ask", json={"question": "hi"}, headers=_bearer("c" * 40)).status_code == 403
    assert client.get("/brief", headers=_bearer("m" * 40)).status_code == 403
    assert client.patch("/mentor", json={"notes": "x"}, headers=_bearer("s" * 40)).status_code == 403
    assert client.get("/health", headers=_bearer("m" * 40)).status_code == 200


def test_actor_comes_from_the_token_not_the_body(monkeypatch, paths):
    (paths / "term_context.json").write_text(json.dumps({"mentor": {"name": "E", "notes": ""}}))
    client = _scoped_app(monkeypatch)
    res = client.patch("/mentor", json={"notes": "hi", "actor": "someone-else"},
                       headers=_bearer("c" * 40))
    assert res.status_code == 200
    line = json.loads((paths / "audit.jsonl").read_text().splitlines()[-1])
    assert line["actor"] == "career"


def test_named_token_must_be_long_and_known(monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKENS", "career:short")
    with pytest.raises(RuntimeError):
        agent_api.create_app()
    monkeypatch.setenv("JARVIS_API_TOKENS", f"nobody:{LONG}")
    with pytest.raises(RuntimeError):
        agent_api.create_app()


def test_failed_logins_are_throttled_per_client():
    client = agent_api.create_app(token=LONG).test_client()
    for _ in range(agent_api.AUTH_FAIL_LIMIT):
        assert client.get("/health", headers=_bearer("wrong")).status_code == 401
    assert client.get("/health", headers=_bearer("wrong")).status_code == 429
    # The blocked client is refused even with the right token; another client is not.
    assert client.get("/health", headers=_bearer(LONG)).status_code == 429
    other = client.get("/health", headers={**_bearer(LONG), "X-Forwarded-For": "203.0.113.9"})
    assert other.status_code == 200


def test_unknown_routes_and_bad_headers_need_auth():
    client = agent_api.create_app(token=LONG).test_client()
    assert client.get("/nope").status_code == 401
    assert client.delete("/health").status_code == 401
    for header in ("Bearer", "bearer", "Basic abc", "Bearer   "):
        assert client.get("/health", headers={"Authorization": header}).status_code == 401


def test_daily_cap_on_ask_and_memory(monkeypatch, paths):
    monkeypatch.setenv("JARVIS_API_ASK_DAILY", "2")
    def _answer(question):
        if not question.strip():
            raise ValueError("question is required")
        return "ok"

    monkeypatch.setattr(agent_api, "answer_question", _answer)
    client = agent_api.create_app(token=LONG).test_client()
    codes = [client.post("/ask", json={"question": "hi"}, headers=_bearer(LONG)).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    # A blank question is a 400 and does not use the allowance.
    assert client.post("/ask", json={"question": " "}, headers=_bearer(LONG)).status_code == 400


def test_non_object_and_oversized_bodies(paths):
    client = agent_api.create_app(token=LONG).test_client()
    for path in ("/ask", "/spend", "/mentor"):
        method = client.patch if path == "/mentor" else client.post
        assert method(path, json=[1, 2], headers=_bearer(LONG)).status_code == 400
    big = client.post("/ask", data="x" * (agent_api.MAX_BODY_BYTES + 10),
                      content_type="application/json", headers=_bearer(LONG))
    assert big.status_code == 413


# ── M4: /spend audit and dedupe ───────────────────────────────────────────────

def test_spend_is_audited_and_retries_are_not_double_counted(monkeypatch, paths):
    import live_spend
    monkeypatch.setattr(live_spend, "SPEND_FILE", paths / "live_spend.jsonl")
    monkeypatch.setattr(live_spend, "DATA_DIR", paths)
    monkeypatch.setenv("JARVIS_API_TOKENS", f"money:{'m' * 40}")
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    client = agent_api.create_app().test_client()
    body = {"amount": 12.5, "category": "Food & dining", "note": "lunch"}
    first = client.post("/spend", json=body, headers=_bearer("m" * 40)).get_json()
    again = client.post("/spend", json=body, headers=_bearer("m" * 40)).get_json()
    assert first["duplicate"] is False and again["duplicate"] is True
    assert len((paths / "live_spend.jsonl").read_text().splitlines()) == 1
    audit = [json.loads(l) for l in (paths / "audit.jsonl").read_text().splitlines()]
    assert len(audit) == 1 and audit[0]["actor"] == "money" and audit[0]["action"] == "spend"


# ── watchdog probe and workout schedule (replaces the deleted PPLRUL test) ────

def test_watchdog_reports_a_dead_agent_api(monkeypatch):
    import watchdog
    monkeypatch.setattr(watchdog, "_systemctl", lambda *a: "active")
    monkeypatch.setattr(watchdog, "_agent_api_responds", lambda: False)
    assert any("agent-api" in p and "not responding" in p for p in watchdog.check_problems())
    monkeypatch.setattr(watchdog, "_agent_api_responds", lambda: True)
    assert not any("agent-api" in p for p in watchdog.check_problems())


def test_weekday_schedule_lookup_and_validation(paths):
    (paths / "term_context.json").write_text("{}")
    assert term_context.get_pplrul_day(datetime.date(2026, 9, 28)) == "Pull"  # Monday
    assert term_context.get_pplrul_day(datetime.date(2026, 9, 27)) == "Push"  # Sunday
    full = dict(term_context.DEFAULT_WORKOUT_SCHEDULE, Monday="Legs")
    term_context.update_workout_schedule(full)
    assert term_context.get_pplrul_day(datetime.date(2026, 9, 28)) == "Legs"
    with pytest.raises(ValueError):
        term_context.update_workout_schedule({"Monday": "Legs"})
    with pytest.raises(ValueError):
        term_context.update_workout_schedule(dict(full, Tuesday="Cardio"))

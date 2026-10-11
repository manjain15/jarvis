"""Daily ledger: day building, debt carry, phone events, and the API routes."""

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
import ledger

MONDAY = datetime.date(2026, 10, 12)
PHONE_TOKEN = "p" * 40
COACH_TOKEN = "k" * 40
OWNER_TOKEN = "test-token"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Ledger files in a temp dir; Monday is Push, every other day is Rest."""
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(ledger, "_split_label", lambda d: "Push" if d == MONDAY else "Rest")
    return tmp_path / "ledger"


def _at(hour, minute=0, date=MONDAY):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, datetime.time(hour, minute)))


def test_gym_day_has_gym_and_steps(store):
    day = ledger.build_day(MONDAY)
    assert [i["id"] for i in day["items"]] == ["gym", "steps"]
    gym = ledger.find_item(day, "gym")
    assert gym["title"] == "Push"
    assert gym["slot"] == ledger.DEFAULT_GYM_SLOT
    assert gym["minutes"] == ledger.DEFAULT_GYM_MINUTES
    assert ledger.find_item(day, "steps")["target"] == 10_000
    assert day["debt"] == {"gym_minutes": 0, "steps": 0}


def test_rest_day_has_only_steps(store):
    day = ledger.build_day(MONDAY + datetime.timedelta(days=1))
    assert [i["id"] for i in day["items"]] == ["steps"]
    assert day["split"] == "Rest"


def test_debt_carries_and_step_debt_is_capped(store):
    previous = {"debt": {"gym_minutes": 10, "steps": 4_500}}
    day = ledger.build_day(MONDAY, previous)
    assert day["debt"] == {"gym_minutes": 10, "steps": 4_500}
    assert ledger.find_item(day, "gym")["minutes"] == 70
    assert ledger.find_item(day, "steps")["target"] == 13_000


def test_get_or_create_reads_previous_day_from_disk(store):
    sunday = MONDAY - datetime.timedelta(days=1)
    ledger.LEDGER_DIR.mkdir(parents=True)
    (ledger.LEDGER_DIR / f"{sunday.isoformat()}.json").write_text(
        json.dumps({"debt": {"gym_minutes": 0, "steps": 1_160}}))
    day = ledger.get_or_create_day(MONDAY)
    assert ledger.find_item(day, "steps")["target"] == 11_160
    assert (store / "2026-10-12.json").exists()
    # A second call returns the stored file, not a rebuild.
    assert ledger.get_or_create_day(MONDAY) == day


def test_events_apply_and_repeats_are_harmless(store):
    ledger.record_event({"type": "wake"}, now=_at(6, 58))
    ledger.record_event({"type": "wake"}, now=_at(7, 30))
    ledger.record_event({"type": "gym_arrive"}, now=_at(7, 41))
    ledger.record_event({"type": "gym_arrive"}, now=_at(8, 50))
    ledger.record_event({"type": "steps", "count": 4_210.6}, now=_at(13))
    ledger.record_event({"type": "steps", "count": 3_000}, now=_at(13, 5))
    day = ledger.record_event({"type": "sleep", "hours": 7.1}, now=_at(7))

    assert day["woke_at"] == _at(6, 58).isoformat(timespec="seconds")
    assert ledger.find_item(day, "gym")["arrived_at"] == _at(7, 41).isoformat(timespec="seconds")
    steps = ledger.find_item(day, "steps")
    assert steps["count"] == 4_210
    assert steps["status"] == "pending"
    assert day["sleep_hours"] == 7.1
    assert len(day["events"]) == 7


def test_steps_reaching_target_marks_done(store):
    day = ledger.record_event({"type": "steps", "count": 10_050}, now=_at(20))
    assert ledger.find_item(day, "steps")["status"] == "done"


def test_gym_arrival_on_rest_day_is_logged_but_changes_nothing(store):
    tuesday = MONDAY + datetime.timedelta(days=1)
    day = ledger.record_event({"type": "gym_arrive"}, now=_at(9, date=tuesday))
    assert ledger.find_item(day, "gym") is None
    assert day["events"][0]["type"] == "gym_arrive"


@pytest.mark.parametrize("body", [
    None,
    [],
    {},
    {"type": "nap"},
    {"type": "steps"},
    {"type": "steps", "count": "4000"},
    {"type": "steps", "count": True},
    {"type": "steps", "count": -1},
    {"type": "steps", "count": 100_001},
    {"type": "sleep", "hours": 20},
    {"type": "wake", "count": 1},
    {"type": "steps", "count": 10, "ts": "2026-10-12T07:00"},
])
def test_bad_event_bodies_are_rejected(store, body):
    with pytest.raises(ValueError):
        ledger.record_event(body, now=_at(9))
    assert not (store / "2026-10-12.json").exists()


def _client():
    return agent_api.create_app(
        token=OWNER_TOKEN,
        bot_tokens={"phone": PHONE_TOKEN, "coach": COACH_TOKEN},
    ).test_client()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_phone_can_post_events_only(store, monkeypatch):
    monkeypatch.setattr(ledger, "now_local", lambda: _at(10))
    client = _client()
    phone = _auth(PHONE_TOKEN)

    ok = client.post("/events", headers=phone, json={"type": "steps", "count": 2_600})
    assert ok.status_code == 200
    assert ok.get_json() == {"ok": True, "date": "2026-10-12", "type": "steps"}

    bad = client.post("/events", headers=phone, json={"type": "steps", "count": "lots"})
    assert bad.status_code == 400

    assert client.get("/today", headers=phone).status_code == 403
    assert client.get("/flags", headers=phone).status_code == 403
    assert client.post("/spend", headers=phone, json={"amount": 1, "category": "Other"}).status_code == 403


def test_today_returns_ledger_without_event_log(store, monkeypatch):
    monkeypatch.setattr(ledger, "now_local", lambda: _at(10))
    client = _client()
    client.post("/events", headers=_auth(PHONE_TOKEN), json={"type": "wake"})

    res = client.get("/today", headers=_auth(COACH_TOKEN))
    assert res.status_code == 200
    body = res.get_json()
    assert body["date"] == "2026-10-12"
    assert body["split"] == "Push"
    assert body["woke_at"] == _at(10).isoformat(timespec="seconds")
    assert body["event_count"] == 1
    assert "events" not in body

    assert client.post("/events", headers=_auth(COACH_TOKEN), json={"type": "wake"}).status_code == 403
    assert client.get("/today", headers=_auth(OWNER_TOKEN)).status_code == 200


def test_new_routes_require_bearer(store):
    client = _client()
    assert client.post("/events", json={"type": "wake"},
                       headers={"X-Forwarded-For": "203.0.113.50"}).status_code == 401
    assert client.get("/today", headers={"X-Forwarded-For": "203.0.113.51"}).status_code == 401


def test_phone_and_coach_tokens_parse_from_env():
    parsed = agent_api.parse_bot_tokens(f"phone:{PHONE_TOKEN},coach:{COACH_TOKEN}")
    assert parsed == [("phone", PHONE_TOKEN), ("coach", COACH_TOKEN)]

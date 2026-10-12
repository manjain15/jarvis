"""Coach check-in: facts, questions, answers → actions and files, and the API routes."""

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
import areas
import evening_checkin as ec
import ledger

TUE = datetime.date(2026, 10, 13)
COACH = "k" * 40
CAREER = "c" * 40


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(ledger, "_split_label", lambda d: "Legs" if d == TUE else "Rest")
    monkeypatch.setattr(areas, "AREAS_FILE", tmp_path / "areas.json")
    monkeypatch.setattr(areas, "DATA_DIR", tmp_path)
    monkeypatch.setattr(areas, "_today", lambda: TUE)
    monkeypatch.setattr(areas, "_term_evidence", lambda: {"career": TUE, "networking": TUE})
    monkeypatch.setattr(areas, "_github_last_commit", lambda repo: TUE)
    monkeypatch.setattr(ec, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "get_tomorrow_pplrul", lambda: "Rest")
    monkeypatch.setattr(ec, "get_pplrul_day", lambda date=None: "Legs" if date == TUE else "Rest")
    import term_context
    monkeypatch.setattr(term_context, "get_workout_schedule", lambda: dict(term_context.DEFAULT_WORKOUT_SCHEDULE))
    return tmp_path


def at(h, m=0):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(TUE, datetime.time(h, m)))


def _seed_day():
    def _add(d):
        d["items"] += [
            {"id": "study-1", "kind": "study", "course": "MATH2931", "type": "study", "task": "W5 Q1-6",
             "start": at(9).isoformat(), "end": at(10).isoformat(), "minutes": 60, "status": "partial"},
            {"id": "study-2", "kind": "study", "course": "COMP3121", "type": "lecture", "task": "W5 L9",
             "start": at(11).isoformat(), "end": at(12).isoformat(), "minutes": 60, "status": "done"},
        ]
        ledger.find_item(d, "steps")["count"] = 8_400
    ledger.update_day(TUE, _add)


def test_facts_and_questions():
    _seed_day()
    data = ec.today_checkin(at(21))
    assert data["facts"]["gym"]["split"] == "Legs"
    assert data["facts"]["steps"] == {"count": 8_400, "target": 10_000}
    ids = [q["id"] for q in data["questions"]]
    assert ids == ["left:study-1", "gym_reason", "area:project", "tomorrow_focus", "weight"]
    assert data["submitted"] is False


def test_no_area_question_when_everything_is_recent():
    _seed_day()
    areas.patch("project", {"touched": "2026-10-12"})
    ids = [q["id"] for q in ec.today_checkin(at(21))["questions"]]
    assert not any(i.startswith("area:") for i in ids)


def test_submit_applies_actions_and_writes_files(store):
    _seed_day()
    seen = {}

    def actions(facts, answers, notes):
        seen.update(answers=answers, notes=notes)
        return {"summary": "Legs missed; Q5-6 carry.",
                "areas": {"project": {"next_action": "Draft the one-pager headings", "worked_today": False}}}

    out = ec.submit_checkin({
        "answers": {"left:study-1": "Q5-6", "gym_reason": "ran late at uni", "area:project": "no idea yet",
                    "tomorrow_focus": "Finish Q5-6", "weight": "72.4"},
        "notes": "tired",
    }, now=at(21, 10), actions_fn=actions)

    assert out["ok"] and out["summary"] == "Legs missed; Q5-6 carry."
    assert "Tomorrow's plan carries: Q5-6" in out["actions"]
    assert 'Passion project: next step is "Draft the one-pager headings"' in out["actions"]
    assert seen["answers"]["weight"] == 72.4 and seen["notes"] == "tired"

    item = ledger.find_item(ledger.load_day(TUE), "study-1")
    assert item["left"] == "Q5-6"
    assert ledger.load_day(TUE)["checkin"]
    assert areas.load()["project"]["next_action"] == "Draft the one-pager headings"
    assert areas.load()["weight"]["last_touched"] == "2026-10-13"

    saved = json.loads((store / "checkin_2026-10-13.json").read_text())
    assert saved["source"] == "coach" and saved["weight_today"] == 72.4
    assert saved["tomorrow_focus"] == "Finish Q5-6" and saved["workout_skip_reason"] == "ran late at uni"
    assert saved["area_project"] == "no idea yet"
    assert (store / "summary_2026-10-13.txt").read_text() == "Legs missed; Q5-6 carry."
    assert ec.today_checkin(at(21, 20))["submitted"] is True


def test_claude_down_still_saves_with_a_plain_summary(store):
    _seed_day()
    out = ec.submit_checkin({"answers": {"area:project": "Sketch the data model", "tomorrow_focus": "Q5-6"}},
                            now=at(21, 10), actions_fn=lambda f, a, n: None)
    assert "CARRY-FORWARD" in out["summary"] and "Q5-6" in out["summary"]
    assert areas.load()["project"]["next_action"] == "Sketch the data model"


@pytest.mark.parametrize("body", [
    [], {"answers": []}, {"answers": {"nope": "x"}}, {"answers": {"weight": "heavy"}},
    {"answers": {"weight": 300}}, {"answers": {"tomorrow_focus": "x" * 501}}, {"extra": 1},
])
def test_bad_bodies(body):
    _seed_day()
    with pytest.raises(ValueError):
        ec.submit_checkin(body, now=at(21), actions_fn=lambda f, a, n: None)


def test_skip_answers_are_dropped():
    _seed_day()
    out = ec.submit_checkin({"answers": {"weight": "skip", "tomorrow_focus": ""}},
                            now=at(21), actions_fn=lambda f, a, n: {"summary": "s", "areas": {}})
    assert out["actions"] == []


def test_keepup_on_the_weeks_last_day(monkeypatch):
    import day_plan
    import term_context
    saturday = datetime.date(2026, 10, 17)
    monkeypatch.setattr(term_context, "get_study_plan", lambda: {"targets": {"MATH2931": {"lectures": 4}}})
    monkeypatch.setattr(day_plan, "minutes_done", lambda s, e: {("MATH2931", "lecture"): 120})
    assert ec._keepup(saturday) == {"keepup_MATH2931": False}
    assert ec._keepup(TUE) == {}


# ── API routes ───────────────────────────────────────────────────────────────

def _client():
    return agent_api.create_app(token="owner-token", bot_tokens={"coach": COACH, "career": CAREER}).test_client()


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_routes_and_scopes(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_api, "get_checkin_payload", lambda: {"questions": []})
    monkeypatch.setattr(agent_api, "apply_checkin", lambda body: {"ok": True})
    monkeypatch.setattr(agent_api, "get_areas_payload", lambda: {"areas": []})
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "audit.jsonl")
    c = _client()
    assert c.get("/checkin/today", headers=_auth(COACH)).status_code == 200
    assert c.post("/checkin", headers=_auth(COACH), json={"answers": {}}).status_code == 200
    assert c.get("/areas", headers=_auth(COACH)).status_code == 200
    assert c.get("/areas", headers=_auth(CAREER)).status_code == 200
    assert c.get("/checkin/today", headers=_auth(CAREER)).status_code == 403

    ok = c.patch("/areas/career", headers=_auth(CAREER), json={"next_action": "Apply to Canva"})
    assert ok.status_code == 200 and ok.get_json()["next_action"] == "Apply to Canva"
    assert json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])["actor"] == "career"
    assert c.patch("/areas/music", headers=_auth(CAREER), json={"next_action": "x"}).status_code == 404
    assert c.patch("/areas/career", headers=_auth(CAREER), json={"paused": "no"}).status_code == 400


def test_schedule_change_from_the_checkin():
    import day_plan
    _seed_day()

    def actions(facts, answers, notes):
        return {"summary": "s", "areas": {}, "schedule_changes": [
            {"date": "2026-10-14", "add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]},
            {"date": "2026-09-01", "remove": ["Automation work"]},          # in the past: ignored
            {"date": "2026-10-15", "add": [{"start": "nine", "end": "5"}]},  # invalid: ignored
        ]}

    out = ec.submit_checkin({"answers": {"tomorrow_focus": "lecture after work"}}, now=at(21), actions_fn=actions)
    assert "Schedule Wed 14 Oct: Automation work 09:30–17:30 added" in out["actions"]
    assert list(day_plan.load_overrides()) == ["2026-10-14"]


def test_tomorrow_fact_has_the_gym_slot():
    _seed_day()
    assert ec.today_checkin(at(21))["facts"]["tomorrow"]["gym_slot"] == ledger.DEFAULT_GYM_SLOT


def test_schedule_route(monkeypatch, tmp_path):
    import day_plan
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(ledger, "now_local", lambda: at(9, 44))
    replans = []
    monkeypatch.setattr(day_plan, "replan_today", lambda **kw: replans.append(1) or [
        {"start": "2026-10-13T17:45", "title": "Lecture · MATH2931", "task": "Watch W5 lecture 1 (Tue 11:00)"}])
    c = _client()
    body = {"date": "2026-10-13", "add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}],
            "reason": "work moved from Monday"}

    # Not planned yet: stored, no re-plan.
    r = c.post("/schedule", headers=_auth(COACH), json=body)
    assert r.status_code == 200 and r.get_json()["replanned"] is False

    ledger.update_day(TUE, lambda d: d.update(planned="2026-10-13T05:30:00+11:00"))
    r = c.post("/schedule", headers=_auth(COACH), json=body)
    assert r.get_json()["replanned"] is True and r.get_json()["blocks"][0]["start"] == "17:45"
    assert replans == [1]

    assert c.post("/schedule", headers=_auth(COACH), json={**body, "date": "2026-10-01"}).status_code == 400
    assert c.post("/schedule", headers=_auth(COACH), json={"date": "2026-10-14"}).status_code == 400
    assert c.post("/schedule", headers=_auth(CAREER), json=body).status_code == 403
    got = c.get("/schedule", headers=_auth(COACH)).get_json()
    assert got["changes"]["2026-10-13"]["add"][0]["label"] == "Automation work"

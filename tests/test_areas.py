"""Areas: evidence, neglect order, pauses, and PATCH validation."""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import areas

TODAY = datetime.date(2026, 10, 13)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(areas, "AREAS_FILE", tmp_path / "areas.json")
    monkeypatch.setattr(areas, "DATA_DIR", tmp_path)
    monkeypatch.setattr(areas, "_today", lambda: TODAY)
    monkeypatch.setattr(areas, "_term_evidence", lambda: {"career": datetime.date(2026, 10, 8),
                                                          "networking": datetime.date(2026, 9, 30)})
    import term_context
    monkeypatch.setattr(term_context, "get_workout_schedule", lambda: dict(term_context.DEFAULT_WORKOUT_SCHEDULE))
    return tmp_path


def _now():
    return areas.TIMEZONE.localize(datetime.datetime.combine(TODAY, datetime.time(9)))


def test_evidence_sets_last_touched_and_github_is_cached():
    calls = []
    github = lambda repo: calls.append(repo) or datetime.date(2026, 10, 12)
    got = areas.refresh_evidence(_now(), github=github)
    assert got["portfolio"]["last_touched"] == "2026-10-12"
    assert got["career"]["last_touched"] == "2026-10-08"
    assert got["networking"]["last_touched"] == "2026-09-30"
    assert got["project"]["last_touched"] is None
    areas.refresh_evidence(_now() + datetime.timedelta(minutes=10), github=github)
    assert calls == ["manjain15/jarvis"]


def test_most_neglected_skips_paused_and_unblockable():
    got = areas.refresh_evidence(_now(), github=lambda r: TODAY)
    order = [a["name"] for a in areas.most_neglected(got, TODAY, blockable_only=True)]
    assert order[0] == "project"            # never touched
    assert "weight" not in order
    areas.patch("project", {"paused": True, "paused_reason": "exams"})
    order = [a["name"] for a in areas.most_neglected(areas.load(), TODAY, blockable_only=True)]
    assert order[0] == "networking"


def test_summary_marks_the_pick():
    areas.refresh_evidence(_now(), github=lambda r: None)
    data = areas.summary(today=TODAY)
    assert data["most_neglected"] == "project"
    project = next(a for a in data["areas"] if a["name"] == "project")
    assert project["days_since"] == areas.NEVER_TOUCHED_DAYS


def test_patch_sets_and_touches():
    out = areas.patch("career", {"next_action": "Apply to Atlassian grad", "touched": "2026-10-13"})
    assert out["next_action"] == "Apply to Atlassian grad" and out["last_touched"] == "2026-10-13"
    assert areas.this_week(areas.load()["career"], datetime.date(2026, 10, 11)) == 1


@pytest.mark.parametrize("body", [
    [], {"colour": "red"}, {"paused": "yes"}, {"next_action": 4}, {"next_action": "x" * 301},
    {"touched": "tomorrow"}, {"touched": "2026-12-01"}, {"target_per_week": 9},
])
def test_patch_rejects_bad_bodies(body):
    with pytest.raises(ValueError):
        areas.patch("career", body)


def test_patch_unknown_area():
    with pytest.raises(KeyError):
        areas.patch("music", {"next_action": "x"})


def test_weight_evidence_from_checkins(store):
    (store / "checkin_2026-10-12.json").write_text(json.dumps({"weight_today": 71.8}))
    (store / "checkin_2026-10-13.json").write_text(json.dumps({"weight_today": None}))
    assert areas._weight_evidence() == datetime.date(2026, 10, 12)

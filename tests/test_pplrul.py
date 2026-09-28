"""Weekday workout schedule. The old anchor-date rotation is gone."""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import term_context

# 2026-09-27 is a Sunday. The default map is Sun Push through Sat Rest.
_SUNDAY = datetime.date(2026, 9, 27)


def test_get_pplrul_day_follows_the_weekday_names(tmp_path, monkeypatch):
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    expected = term_context.DEFAULT_WORKOUT_SCHEDULE
    for offset in range(7):
        day = _SUNDAY + datetime.timedelta(days=offset)
        assert term_context.get_pplrul_day(day) == expected[day.strftime("%A")]


def test_update_workout_schedule_rejects_a_missing_weekday(tmp_path, monkeypatch):
    path = tmp_path / "term_context.json"
    path.write_text(json.dumps({"mentor": {"name": "Emily"}}))
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    schedule = dict(term_context.DEFAULT_WORKOUT_SCHEDULE)
    schedule.pop("Wednesday")
    with pytest.raises(ValueError, match="Wednesday"):
        term_context.update_workout_schedule(schedule)
    assert "workout_schedule" not in json.loads(path.read_text())


def test_update_workout_schedule_rejects_an_unknown_label(tmp_path, monkeypatch):
    path = tmp_path / "term_context.json"
    path.write_text("{}")
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    schedule = dict(term_context.DEFAULT_WORKOUT_SCHEDULE)
    schedule["Friday"] = "Cardio"
    with pytest.raises(ValueError, match="Cardio"):
        term_context.update_workout_schedule(schedule)
    assert json.loads(path.read_text()) == {}


def test_update_workout_schedule_writes_the_weekday_map(tmp_path, monkeypatch):
    path = tmp_path / "term_context.json"
    path.write_text(json.dumps({"mentor": {"name": "Emily"}}))
    monkeypatch.setattr(term_context, "CONTEXT_FILE", path)
    schedule = dict(term_context.DEFAULT_WORKOUT_SCHEDULE)
    schedule["Monday"] = "Rest"
    term_context.update_workout_schedule(schedule)
    assert term_context.get_pplrul_day(_SUNDAY + datetime.timedelta(days=1)) == "Rest"
    stored = json.loads(path.read_text())
    assert stored["mentor"]["name"] == "Emily"
    assert stored["workout_schedule"]["Monday"] == "Rest"
    assert stored["workout_schedule"]["Sunday"] == "Push"
    previous = json.loads((tmp_path / "term_context.json.bak").read_text())
    assert previous["mentor"]["name"] == "Emily"
    assert "workout_schedule" not in previous

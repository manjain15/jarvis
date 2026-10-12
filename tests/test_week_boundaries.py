"""Training-week boundaries follow the split, and Hevy session assignment."""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import hevy
import term_context

SUN = datetime.date(2026, 10, 11)
SPLIT = {"Sunday": "Push", "Monday": "Pull", "Tuesday": "Legs", "Wednesday": "Rest",
         "Thursday": "Upper", "Friday": "Sharms", "Saturday": "Rest"}


def split(d):
    return SPLIT[d.strftime("%A")]


def test_week_starts_on_the_schedules_first_day(monkeypatch):
    monkeypatch.setattr(term_context, "get_workout_schedule", lambda: SPLIT)
    assert term_context.week_start(SUN) == SUN
    assert term_context.week_start(SUN + datetime.timedelta(days=6)) == SUN
    assert term_context.week_start(SUN + datetime.timedelta(days=7)) == SUN + datetime.timedelta(days=7)
    monday_first = {"Monday": "Push", **{k: v for k, v in SPLIT.items() if k != "Monday"}}
    monkeypatch.setattr(term_context, "get_workout_schedule", lambda: monday_first)
    assert term_context.week_start(SUN) == SUN - datetime.timedelta(days=6)


def _w(local):
    """A Hevy workout logged at a Sydney local time."""
    dt = hevy.TIMEZONE.localize(local)
    return {"start_time": dt.astimezone(datetime.timezone.utc).isoformat(), "title": "w"}


def test_saturday_night_push_fills_sunday():
    sat_night = _w(datetime.datetime(2026, 10, 10, 20, 45))
    monday = _w(datetime.datetime(2026, 10, 12, 8, 52))
    got = hevy.assign_sessions([monday, sat_night], SUN, SUN + datetime.timedelta(days=1), split_fn=split)
    assert set(got) == {SUN, SUN + datetime.timedelta(days=1)}
    assert got[SUN] is sat_night


def test_rest_day_session_does_not_fill_a_day_that_has_its_own():
    sat = _w(datetime.datetime(2026, 10, 10, 20, 0))
    sun = _w(datetime.datetime(2026, 10, 11, 9, 0))
    got = hevy.assign_sessions([sun, sat], SUN, SUN, split_fn=split)
    assert got[SUN] is sun


def test_session_on_the_day_after_is_not_moved_back():
    monday = _w(datetime.datetime(2026, 10, 12, 8, 0))   # Monday's own Pull
    got = hevy.assign_sessions([monday], SUN, SUN, split_fn=split)
    assert SUN not in got

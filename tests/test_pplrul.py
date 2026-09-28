"""PPLRUL training-cycle math in evening_checkin — pure date arithmetic."""

import datetime

import pytest

import evening_checkin as ec
import term_context


@pytest.fixture(autouse=True)
def _default_split(monkeypatch):
    """Ignore a local term_context.json so the weekday default is what we assert."""
    monkeypatch.setattr(
        term_context,
        "get_workout_schedule",
        lambda: term_context.DEFAULT_WORKOUT_SCHEDULE,
    )


def test_wednesday_is_rest():
    # 2026-05-13 is a Wednesday. The default weekday schedule maps it to Rest.
    assert ec.get_pplrul_day(datetime.date(2026, 5, 13)) == "Rest"


def test_cycle_advances_daily():
    # Default weekday schedule, starting Wednesday 13 May 2026.
    base = datetime.date(2026, 5, 13)
    expected = ["Rest", "Upper", "Sharms", "Rest", "Push", "Pull", "Legs"]
    got = [ec.get_pplrul_day(base + datetime.timedelta(days=i)) for i in range(7)]
    assert got == expected


def test_cycle_wraps_after_seven_days():
    base = datetime.date(2026, 5, 13)
    assert ec.get_pplrul_day(base) == ec.get_pplrul_day(base + datetime.timedelta(days=7))


def test_works_before_anchor():
    # Tuesday 12 May 2026 is Legs on the default weekday schedule.
    assert ec.get_pplrul_day(datetime.date(2026, 5, 12)) == "Legs"


def test_tomorrow_helper(monkeypatch):
    monkeypatch.setattr(ec, "now_sydney",
                        lambda: datetime.datetime(2026, 5, 13, 9, 0))
    # Tomorrow (Thursday 14th) is Upper on the default weekday schedule.
    assert ec.get_tomorrow_pplrul() == "Upper"

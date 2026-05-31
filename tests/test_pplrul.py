"""PPLRUL training-cycle math in evening_checkin — pure date arithmetic."""

import datetime
import evening_checkin as ec


def test_anchor_day_is_legs():
    # ANCHOR_DATE (2026-05-13) is defined as a Legs day.
    assert ec.get_pplrul_day(datetime.date(2026, 5, 13)) == "Legs"


def test_cycle_advances_daily():
    # PPLRUL = [Push, Pull, Legs, Rest, Upper, Sharms, Rest]; anchor index 2 (Legs).
    base = datetime.date(2026, 5, 13)
    expected = ["Legs", "Rest", "Upper", "Sharms", "Rest", "Push", "Pull"]
    got = [ec.get_pplrul_day(base + datetime.timedelta(days=i)) for i in range(7)]
    assert got == expected


def test_cycle_wraps_after_seven_days():
    base = datetime.date(2026, 5, 13)
    assert ec.get_pplrul_day(base) == ec.get_pplrul_day(base + datetime.timedelta(days=7))


def test_works_before_anchor():
    # Day before anchor (index 2) → index 1 → "Pull".
    assert ec.get_pplrul_day(datetime.date(2026, 5, 12)) == "Pull"


def test_tomorrow_helper(monkeypatch):
    monkeypatch.setattr(ec, "now_sydney",
                        lambda: datetime.datetime(2026, 5, 13, 9, 0))
    # Tomorrow (14th) is the day after Legs → Rest.
    assert ec.get_tomorrow_pplrul() == "Rest"

"""Step pace nudges and step debt at day close."""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import enforcer
import ledger
import pace

TUESDAY = datetime.date(2026, 10, 13)   # Rest in these tests
WEDNESDAY = TUESDAY + datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(ledger, "_split_label", lambda d: "Rest")
    monkeypatch.setattr(enforcer, "_last_close_attempt", {})


def at(hour, minute=0, date=TUESDAY):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, datetime.time(hour, minute)))


def steps_at(count, hour, minute=0, date=TUESDAY):
    ledger.record_event({"type": "steps", "count": count}, now=at(hour, minute, date))


def free_all_day():
    return []


def no_calendar():
    raise AssertionError("calendar should not be read")


def test_on_pace_says_nothing():
    steps_at(2_700, 10, 0)
    assert pace.tick(at(10, 1), busy_fn=no_calendar) == []


def test_behind_pace_nudges_with_a_free_window():
    steps_at(1_400, 10, 0)
    busy = [(at(10, 30), at(12, 0))]
    out = pace.tick(at(10, 1), busy_fn=lambda: busy)
    assert out == ["1,100 behind 2,500 pace. 11 min walk. Free until 10:30."]
    # Judged once only.
    assert pace.tick(at(10, 5), busy_fn=no_calendar) == []


def test_busy_now_points_to_the_next_gap():
    steps_at(3_000, 13, 0)
    busy = [(at(12, 0), at(14, 0)), (at(15, 0), at(17, 0))]
    out = pace.tick(at(13, 1), busy_fn=lambda: busy)
    assert out == ["2,000 behind 5,000 pace. 20 min walk. Next free 14:00–15:00."]


def test_calendar_unknown_still_nudges():
    steps_at(3_000, 13, 0)
    assert pace.tick(at(13, 1), busy_fn=lambda: None) == ["2,000 behind 5,000 pace. 20 min walk."]


def test_no_free_block_left():
    steps_at(5_000, 19, 0)
    busy = [(at(19, 0), at(22, 0))]
    out = pace.tick(at(19, 2), busy_fn=lambda: busy)
    assert out == ["3,500 behind 8,500 pace. 35 min walk. No free block left today, so walk between things."]


def test_final_checkpoint_wording():
    steps_at(8_840, 21, 0)
    out = pace.tick(at(21, 1), busy_fn=free_all_day)
    assert out == ["8,840 / 10,000. 1,160 short. 12 min walk. Free until 22:00."]


def test_waits_for_the_checkpoint_reading():
    steps_at(1_000, 9, 50)          # app-open reading just before 10:00
    assert pace.tick(at(10, 0), busy_fn=no_calendar) == []
    steps_at(2_600, 10, 1)          # the 10:00 Shortcut lands
    assert pace.tick(at(10, 2), busy_fn=no_calendar) == []


def test_pre_checkpoint_reading_is_used_after_the_wait():
    steps_at(1_000, 9, 50)
    assert pace.tick(at(10, 10), busy_fn=no_calendar) == []
    assert pace.tick(at(10, 21), busy_fn=free_all_day) == [
        "1,500 behind 2,500 pace. 15 min walk. Free until 22:00."]


def test_missing_reading_is_unknown_not_behind():
    steps_at(500, 8, 0)             # stale: more than 15 min before 10:00
    assert pace.tick(at(10, 25), busy_fn=no_calendar) == []
    assert "10:00" in ledger.find_item(ledger.load_day(TUESDAY), "steps")["pace"]["checked"]


def test_stale_checkpoints_after_downtime_are_skipped():
    steps_at(1_000, 10, 0)
    # The daemon comes back at 14:30: 10:00 and 13:00 are over an hour old.
    assert pace.tick(at(14, 30), busy_fn=no_calendar) == []


def test_several_due_at_once_send_one_message():
    steps_at(1_000, 10, 0)
    steps_at(1_200, 13, 0)
    out = pace.tick(at(13, 5), busy_fn=free_all_day)
    assert out == ["3,800 behind 5,000 pace. 38 min walk. Free until 22:00."]


def test_debt_raises_pace():
    previous = {"debt": {"gym_minutes": 0, "steps": 2_000}}
    ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger.update_day(TUESDAY, lambda day: ledger.apply_carried_debt(day, previous))
    steps_at(2_700, 10, 0)
    assert pace.tick(at(10, 1), busy_fn=free_all_day) == [
        "300 behind 3,000 pace. 3 min walk. Free until 22:00."]


def test_next_free():
    busy = [(at(9), at(10)), (at(10), at(10, 10)), (at(10, 20), at(11))]
    assert pace.next_free(busy, at(9, 30), at(22)) == (at(11), at(22))
    assert pace.next_free([], at(21, 50), at(22)) is None


# ── Step debt at close ──────────────────────────────────────────────────────

def test_shortfall_becomes_tomorrows_debt():
    steps_at(8_840, 21, 0)
    out = enforcer.tick(at(23, 0), fetch=lambda d: (_ for _ in ()).throw(AssertionError("no hevy")))
    assert out == ["Steps 8,840 / 10,000. +1,160 tomorrow."]
    tomorrow = ledger.get_or_create_day(WEDNESDAY)
    assert ledger.find_item(tomorrow, "steps")["target"] == 11_160


def test_shortfall_is_capped():
    steps_at(4_000, 21, 0)
    enforcer.tick(at(23, 0), fetch=lambda d: 0)
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 3_000


def test_target_hit_clears_step_debt():
    previous = {"debt": {"gym_minutes": 0, "steps": 1_160}}
    ledger.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger.update_day(TUESDAY, lambda day: ledger.apply_carried_debt(day, previous))
    steps_at(11_500, 21, 0)
    assert enforcer.tick(at(23, 0), fetch=lambda d: 0) == []
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 0


def test_no_readings_carries_nothing():
    ledger.get_or_create_day(TUESDAY)
    assert enforcer.tick(at(23, 0), fetch=lambda d: 0) == []
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 0


def test_closed_day_gets_no_more_nudges():
    steps_at(8_000, 21, 0)
    enforcer.tick(at(23, 0), fetch=lambda d: 0)
    steps_at(8_100, 23, 10)
    assert pace.tick(at(23, 11), busy_fn=no_calendar) == []


def test_zero_reading_is_a_locked_phone_not_zero_steps():
    steps_at(0, 10, 0)
    assert pace.tick(at(10, 1), busy_fn=no_calendar) == []
    assert pace.tick(at(10, 25), busy_fn=no_calendar) == []
    enforcer.tick(at(23, 0), fetch=lambda d: 0)
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 0

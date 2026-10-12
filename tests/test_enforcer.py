"""Gym enforcer: wake sequence, excuse gate, Hevy verification, and debt."""

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

MONDAY = datetime.date(2026, 10, 12)      # Pull
TUESDAY = MONDAY + datetime.timedelta(days=1)   # Rest
WEDNESDAY = MONDAY + datetime.timedelta(days=2)  # Rest
THURSDAY = MONDAY + datetime.timedelta(days=3)  # Upper


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Temp ledger dir, a fixed split, and a clean close-attempt memo."""
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    splits = {MONDAY: "Pull", THURSDAY: "Upper"}
    monkeypatch.setattr(ledger, "_split_label", lambda d: splits.get(d, "Rest"))
    monkeypatch.setattr(enforcer, "_last_close_attempt", {})
    monkeypatch.setattr(enforcer, "TRAVEL_MINUTES", 10)
    monkeypatch.setattr(ledger, "DEFAULT_GYM_SLOT", "07:15")
    return tmp_path


def at(hour, minute=0, date=MONDAY):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, datetime.time(hour, minute)))


GYM_DAYS = {MONDAY, THURSDAY}


def no_hevy(date):
    """Rest-day lookups (early-session check) find nothing; gym-day lookups are too early."""
    if date not in GYM_DAYS:
        return 0
    raise AssertionError("Hevy should not be called yet")


def gym(date=MONDAY):
    return ledger.find_item(ledger.load_day(date), "gym")


def debt(date):
    return ledger.load_day(date)["debt"]


# ── Sequence ─────────────────────────────────────────────────────────────────

def test_full_morning_sequence():
    ledger.record_event({"type": "wake"}, now=at(6, 40))

    assert enforcer.tick(at(6, 40), fetch=no_hevy) == ["06:40 · Pull · leave by 07:05\nReply up."]
    assert enforcer.tick(at(6, 41), fetch=no_hevy) == []
    assert enforcer.tick(at(6, 43), fetch=no_hevy) == ["3 min. Reply up."]
    assert enforcer.tick(at(6, 44), fetch=no_hevy) == []

    assert enforcer.handle_text("up", now=at(6, 45)) == "Clothes on. Photo, 10 min."
    assert enforcer.tick(at(6, 55), fetch=no_hevy) == ["Photo."]
    assert enforcer.handle_photo(now=at(6, 56)) == "Leave by 07:05."
    assert enforcer.handle_photo(now=at(6, 57)) is None

    assert enforcer.tick(at(7, 5), fetch=no_hevy) == ["Leave now."]
    assert enforcer.tick(at(7, 45), fetch=no_hevy) == ["No arrival. Reason, or go."]
    assert enforcer.tick(at(7, 50), fetch=no_hevy) == []


def test_no_wake_starts_at_slot_minus_30():
    assert enforcer.tick(at(6, 30), fetch=no_hevy) == []
    assert enforcer.tick(at(6, 45), fetch=no_hevy) == ["06:45 · Pull · leave by 07:05\nReply up."]


def test_late_wake_sends_one_message():
    ledger.record_event({"type": "wake"}, now=at(8, 10))
    assert enforcer.tick(at(8, 10), fetch=no_hevy) == ["08:10 · Pull · late. Go now, or skip <reason>."]
    assert enforcer.tick(at(8, 20), fetch=no_hevy) == []


def test_arrival_silences_leave_and_arrival_checks():
    ledger.record_event({"type": "wake"}, now=at(6, 40))
    enforcer.tick(at(6, 40), fetch=no_hevy)
    enforcer.handle_text("up", now=at(6, 41))
    ledger.record_event({"type": "gym_arrive"}, now=at(7, 0))
    assert enforcer.tick(at(7, 5), fetch=no_hevy) == []
    assert enforcer.tick(at(7, 50), fetch=no_hevy) == []


def test_up_and_skip_are_ignored_off_gym_days():
    assert enforcer.handle_text("up", now=at(7, 0, TUESDAY)) is None
    assert enforcer.handle_text("skip tired", now=at(7, 0, TUESDAY)) is None
    assert enforcer.handle_text("what's on today", now=at(7, 0)) is None
    assert enforcer.tick(at(7, 0, TUESDAY), fetch=no_hevy) == []


# ── Hevy verification ────────────────────────────────────────────────────────

def test_verification_after_slot_plus_two_hours_is_throttled():
    calls = []

    def hevy(date):
        calls.append(date)
        return 0 if len(calls) == 1 else 58

    enforcer.tick(at(6, 45), fetch=no_hevy)
    assert enforcer.tick(at(9, 15), fetch=hevy) == []
    assert enforcer.tick(at(9, 30), fetch=hevy) == []
    assert len(calls) == 1
    assert enforcer.tick(at(9, 45), fetch=hevy) == ["Logged · Pull · 58 min."]
    assert gym()["status"] == "done"
    assert enforcer.tick(at(10, 30), fetch=hevy) == []


def test_partial_session():
    enforcer.tick(at(6, 45), fetch=no_hevy)
    assert enforcer.tick(at(9, 15), fetch=lambda d: 30) == ["Logged · Pull · 30 min. Partial."]
    assert gym()["status"] == "partial"


def test_hevy_down_is_not_a_miss():
    enforcer.tick(at(6, 45), fetch=no_hevy)
    assert enforcer.tick(at(23, 0), fetch=lambda d: None) == []
    assert gym()["status"] == "pending"
    assert ledger.load_day(MONDAY)["closed"] is False


# ── Excuse gate ──────────────────────────────────────────────────────────────

def _started():
    enforcer.tick(at(6, 45), fetch=no_hevy)


def test_skip_needs_a_reason():
    _started()
    assert enforcer.handle_text("skip", now=at(6, 50)) == "Reason required: skip <reason>."


def test_skip_sick_is_excused():
    _started()
    reply = enforcer.handle_text("skip sick, fever", now=at(6, 50),
                                 sleep_fn=no_hevy, free_fn=no_hevy)
    assert reply == "Excused. Logged for the weekly review."
    assert gym()["status"] == "excused"
    assert enforcer.tick(at(7, 5), fetch=no_hevy) == []


def test_low_sleep_moves_to_evening_once():
    _started()
    reply = enforcer.handle_text("skip wrecked", now=at(6, 50),
                                 sleep_fn=lambda day: 5.4, free_fn=no_hevy)
    assert reply == "Slept 5.4h. Moved to 18:00. Leave by 17:50."
    assert gym()["slot"] == "18:00"
    assert enforcer.tick(at(7, 5), fetch=no_hevy) == []
    assert enforcer.tick(at(17, 0), fetch=no_hevy) == ["18:00 · Pull · leave by 17:50"]
    assert enforcer.tick(at(17, 50), fetch=no_hevy) == ["Leave now."]

    again = enforcer.handle_text("skip still wrecked", now=at(17, 51),
                                 sleep_fn=lambda day: 5.4, free_fn=lambda now: 300)
    assert again == "Not a reason. Go now."


def test_full_calendar_allows_the_short_version():
    _started()
    reply = enforcer.handle_text("skip packed day", now=at(6, 50),
                                 sleep_fn=lambda day: 7.5, free_fn=lambda now: 50)
    assert reply == "Longest free block is 50 min. 35-min version. Still going."
    assert gym()["minutes"] == 35
    assert enforcer.tick(at(9, 15), fetch=lambda d: 30) == ["Logged · Pull · 30 min."]


def test_anything_else_is_rejected():
    _started()
    reply = enforcer.handle_text("skip don't feel like it", now=at(6, 50),
                                 sleep_fn=lambda day: 7.5, free_fn=lambda now: 300)
    assert reply == "Not a reason. Leave by 07:05."
    assert gym()["status"] == "pending"
    kinds = [e["type"] for e in ledger.load_day(MONDAY)["events"]]
    assert "enforcer.rejected" in kinds


def test_unknown_sleep_and_calendar_reject():
    _started()
    reply = enforcer.handle_text("skip tired", now=at(6, 50),
                                 sleep_fn=lambda day: None, free_fn=lambda now: None)
    assert reply.startswith("Not a reason.")


# ── Close and debt ───────────────────────────────────────────────────────────

def test_miss_creates_a_makeup_cleared_by_a_rest_day_session():
    _started()
    assert enforcer.tick(at(23, 0), fetch=lambda d: 0) == ["Pull missed. Make-up: 70 min by Wed."]
    assert gym()["status"] == "missed"
    assert debt(MONDAY)["makeup"]["minutes"] == 70

    # Tuesday carries it. A 60-min rest-day session clears it (70 − 15 slack).
    ledger.get_or_create_day(TUESDAY)
    assert enforcer.tick(at(23, 0, TUESDAY), fetch=lambda d: 60) == ["Make-up done · 60 min. Cleared."]
    assert debt(TUESDAY)["makeup"] is None


def test_expired_makeup_adds_minutes_to_the_next_session():
    _started()
    enforcer.tick(at(23, 0), fetch=lambda d: 0)
    ledger.get_or_create_day(TUESDAY)
    assert enforcer.tick(at(23, 0, TUESDAY), fetch=lambda d: 0) == []
    ledger.get_or_create_day(WEDNESDAY)
    assert enforcer.tick(at(23, 0, WEDNESDAY), fetch=lambda d: 0) == [
        "Make-up not done. +20 min on the next session until it's hit."]

    thursday = ledger.get_or_create_day(THURSDAY)
    assert ledger.find_item(thursday, "gym")["minutes"] == 80
    # 70 min is within the 15-min slack of 80: done, and the debt clears.
    enforcer.tick(at(6, 45, THURSDAY), fetch=no_hevy)
    assert enforcer.tick(at(9, 15, THURSDAY), fetch=lambda d: 70) == ["Logged · Upper · 70 min."]
    enforcer.tick(at(23, 0, THURSDAY), fetch=lambda d: 70)
    assert debt(THURSDAY)["gym_minutes"] == 0


def test_excused_day_closes_without_debt():
    _started()
    enforcer.handle_text("skip sick", now=at(6, 50))
    assert enforcer.tick(at(23, 0), fetch=lambda d: 0) == []
    assert debt(MONDAY)["makeup"] is None


def test_yesterday_closes_late_and_recarries_into_today():
    enforcer.tick(at(6, 45), fetch=no_hevy)
    # The daemon was down at 23:00. Tuesday's file is created before the close.
    ledger.record_event({"type": "wake"}, now=at(7, 0, TUESDAY))
    assert debt(TUESDAY)["makeup"] is None

    out = enforcer.tick(at(7, 1, TUESDAY), fetch=lambda d: 0)
    assert out == ["Pull missed. Make-up: 70 min by Wed."]
    assert debt(TUESDAY)["makeup"]["minutes"] == 70
    assert ledger.load_day(MONDAY)["closed"] is True


def test_longest_gap():
    def t(h, m=0):
        return at(h, m)
    busy = [(t(9), t(11)), (t(10), t(12)), (t(13), t(14))]
    assert enforcer._longest_gap(busy, t(8), t(21)) == 420
    assert enforcer._longest_gap(busy, t(9), t(14)) == 60
    assert enforcer._longest_gap([], t(20), t(21)) == 60


def test_a_day_the_sequence_never_ran_is_not_a_miss():
    # Built before go-live (or the daemon was down all morning): no sequence state.
    ledger.get_or_create_day(MONDAY)
    assert enforcer.tick(at(23, 0), fetch=lambda d: 0) == []
    assert gym()["status"] == "unenforced"
    assert debt(MONDAY)["makeup"] is None


def test_a_session_still_counts_on_an_unenforced_day():
    ledger.get_or_create_day(MONDAY)
    enforcer.tick(at(23, 0), fetch=lambda d: 62)
    assert gym()["status"] == "done"



# ── Early sessions ───────────────────────────────────────────────────────────

SUNDAY = MONDAY - datetime.timedelta(days=1)   # Rest in these tests


def test_rest_day_session_the_night_before_counts():
    calls = []

    def hevy(date):
        calls.append(date)
        return 54 if date == SUNDAY else 0

    assert enforcer.tick(at(6, 0), fetch=hevy) == []
    assert enforcer.tick(at(6, 45), fetch=hevy) == ["Pull logged yesterday · 54 min. Nothing to do this morning."]
    assert gym()["status"] == "done" and gym()["early"] is True
    # No sequence, no repeat check, no miss at close.
    assert enforcer.tick(at(7, 5), fetch=hevy) == []
    assert calls == [SUNDAY]
    assert enforcer.tick(at(23, 0), fetch=hevy) == []
    assert debt(MONDAY)["makeup"] is None


def test_no_early_session_runs_the_normal_sequence():
    assert enforcer.tick(at(6, 45), fetch=no_hevy) == ["06:45 · Pull · leave by 07:05\nReply up."]


def test_wake_triggers_the_early_check_too():
    ledger.record_event({"type": "wake"}, now=at(6, 0))
    out = enforcer.tick(at(6, 0), fetch=lambda d: 60 if d == SUNDAY else 0)
    assert out == ["Pull logged yesterday · 60 min. Nothing to do this morning."]


def test_session_that_cleared_a_makeup_is_not_counted_twice():
    ledger.update_day(SUNDAY, lambda d: d.update(makeup_cleared=True))
    out = enforcer.tick(at(6, 45), fetch=lambda d: (_ for _ in ()).throw(AssertionError("no hevy")))
    assert out == ["06:45 · Pull · leave by 07:05\nReply up."]


def test_early_check_finding_nothing_falls_back_to_the_sequence():
    ledger.get_or_create_day(WEDNESDAY)            # Rest, nothing logged
    out = enforcer.tick(at(6, 45, THURSDAY), fetch=lambda d: 0)
    assert out[-1].startswith("06:45 · Upper")
    assert gym(THURSDAY)["early_checked"] is True


def test_no_early_credit_after_a_training_day(monkeypatch):
    # Make Sunday a gym day: Monday then follows a training day, so no lookup.
    monkeypatch.setattr(ledger, "_split_label", lambda d: "Push" if d == SUNDAY else
                        {MONDAY: "Pull", THURSDAY: "Upper"}.get(d, "Rest"))
    ledger.update_day(SUNDAY, lambda d: d.update(closed=True))  # already settled
    out = enforcer.tick(at(6, 45), fetch=lambda d: (_ for _ in ()).throw(AssertionError("no hevy")))
    assert out == ["06:45 · Pull · leave by 07:05\nReply up."]

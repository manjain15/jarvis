"""Step counts reconcile the phone shortcut with Google Health before grading."""

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
import morning_brief
import pace

OCT16 = datetime.date(2026, 10, 16)  # the day Fitbit and the phone disagreed
OCT17 = OCT16 + datetime.timedelta(days=1)
TUESDAY = datetime.date(2026, 10, 13)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(ledger, "_split_label", lambda d: "Rest")
    monkeypatch.setattr(enforcer, "_last_close_attempt", {})
    ledger._health_cache.clear()
    ledger._health_warned.clear()


def at(hour, minute=0, date=OCT16):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, datetime.time(hour, minute)))


def pull_returning(value):
    """A pull_health_steps stand-in. Close asks with max_age_s=0."""
    def _pull(date, max_age_s=0):
        return value
    return _pull


def steps(date):
    return ledger.find_item(ledger.load_day(date), "steps")


def close(date, fetch=None):
    fetch = fetch or (lambda d: 0)
    return enforcer.tick(at(23, 0, date), fetch=fetch)


# ── Google Health client ────────────────────────────────────────────────────

def test_fetch_health_steps_reads_the_daily_rollup(monkeypatch):
    import google_health
    monkeypatch.setattr(google_health, "get_access_token", lambda: "tok")
    monkeypatch.setattr(google_health, "fetch_steps", lambda token, date: {"steps": 13_000})
    assert ledger.fetch_health_steps(OCT16) == 13_000


def test_pull_returns_none_and_warns_when_health_errors(monkeypatch, capsys):
    def boom(date):
        raise RuntimeError("no token")
    monkeypatch.setattr(ledger, "fetch_health_steps", boom)
    assert ledger.pull_health_steps(OCT16) is None
    assert "unavailable" in capsys.readouterr().out
    assert ledger.pull_health_steps(OCT16) is None
    assert capsys.readouterr().out == ""


def test_pull_returns_none_and_warns_when_health_has_no_data(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "fetch_health_steps", lambda date: None)
    assert ledger.pull_health_steps(OCT16) is None
    assert "no data" in capsys.readouterr().out


# ── During the day ──────────────────────────────────────────────────────────

def test_shortcut_keeps_the_higher_health_total(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(9_000))
    day = ledger.record_event({"type": "steps", "count": 4_000}, now=at(10))
    item = ledger.find_item(day, "steps")
    assert item["count"] == 9_000
    assert item["evidence"] == "shortcut+google_health"
    assert item["status"] == "pending"

    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(8_000))
    day = ledger.record_event({"type": "steps", "count": 5_000}, now=at(13))
    item = ledger.find_item(day, "steps")
    assert item["count"] == 9_000
    assert item["evidence"] == "shortcut+google_health"
    assert [e["type"] for e in day["events"]].count("steps.health") == 1


def test_shortcut_wins_when_health_is_lower(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(3_000))
    day = ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    item = ledger.find_item(day, "steps")
    assert item["count"] == 7_140
    assert item["evidence"] == "shortcut+google_health"


def test_shortcut_stands_when_health_errors(monkeypatch):
    def boom(date, max_age_s=0):
        raise RuntimeError("down")
    monkeypatch.setattr(ledger, "pull_health_steps", boom)
    day = ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    item = ledger.find_item(day, "steps")
    assert item["count"] == 7_140
    assert item["evidence"] == "shortcut"
    assert item["status"] == "pending"


def test_pace_judges_the_reconciled_count_and_does_not_poll(monkeypatch):
    calls = []

    def pull(date, max_age_s=0):
        calls.append((date, max_age_s))
        return None if len(calls) == 1 else 4_000

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    # A reading just before 10:00 cannot satisfy the checkpoint yet.
    ledger.record_event({"type": "steps", "count": 1_000}, now=at(9, 50, TUESDAY))
    assert pace.tick(at(10, 5, TUESDAY), busy_fn=lambda: []) == []
    assert len(calls) == 1

    assert pace.tick(at(10, 21, TUESDAY), busy_fn=lambda: []) == []
    assert steps(TUESDAY)["count"] == 4_000
    assert len(calls) == 2
    assert pace.tick(at(10, 22, TUESDAY), busy_fn=lambda: []) == []
    assert len(calls) == 2


def test_pace_nudge_uses_the_health_gap(monkeypatch):
    calls = {"n": 0}

    def pull(date, max_age_s=0):
        calls["n"] += 1
        return None if calls["n"] == 1 else 1_500

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    ledger.record_event({"type": "steps", "count": 1_000}, now=at(10, 0, TUESDAY))
    out = pace.tick(at(10, 1, TUESDAY), busy_fn=lambda: [])
    assert out == ["1,000 behind 2,500 pace. 10 min walk. Free until 22:00."]
    assert steps(TUESDAY)["count"] == 1_500


# ── Close grades the reconciled count ───────────────────────────────────────

def test_close_prefers_health_when_it_is_higher(monkeypatch):
    def pull(date, max_age_s=0):
        return 13_000 if max_age_s == 0 else None

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    assert steps(OCT16)["count"] == 7_140
    assert close(OCT16) == []
    item = steps(OCT16)
    assert item["count"] == 13_000
    assert item["status"] == "done"
    assert item["evidence"] == "shortcut+google_health"
    assert ledger.load_day(OCT16)["debt"]["steps"] == 0
    tomorrow = ledger.get_or_create_day(OCT17)
    assert ledger.find_item(tomorrow, "steps")["target"] == 10_000


def test_close_debt_uses_health_when_still_short(monkeypatch):
    """8,000 from Health, not the 5,000 shortcut, is what the debt is cut from."""
    def pull(date, max_age_s=0):
        return 8_000 if max_age_s == 0 else None

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    ledger.record_event({"type": "steps", "count": 5_000}, now=at(21))
    ledger.get_or_create_day(OCT17)
    assert close(OCT16) == ["Steps 8,000 / 10,000. +2,000 tomorrow."]
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_000
    assert steps(OCT17)["target"] == 12_000


def test_close_keeps_the_shortcut_when_health_is_lower(monkeypatch):
    def pull(date, max_age_s=0):
        return 5_000 if max_age_s == 0 else None

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    assert close(OCT16) == ["Steps 7,140 / 10,000. +2,860 tomorrow."]
    item = steps(OCT16)
    assert item["count"] == 7_140
    assert item["evidence"] == "shortcut+google_health"
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860


def test_close_keeps_the_shortcut_when_health_is_missing(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    assert close(OCT16) == ["Steps 7,140 / 10,000. +2,860 tomorrow."]
    assert steps(OCT16)["evidence"] == "shortcut"
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860


def test_close_keeps_the_shortcut_when_health_errors(monkeypatch):
    def pull(date, max_age_s=0):
        if max_age_s == 0:
            raise RuntimeError("down")
        return None

    monkeypatch.setattr(ledger, "pull_health_steps", pull)
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    assert close(OCT16) == ["Steps 7,140 / 10,000. +2,860 tomorrow."]
    assert steps(OCT16)["count"] == 7_140
    assert steps(OCT16)["evidence"] == "shortcut"
    assert ledger.load_day(OCT16)["closed"] is True


def test_health_alone_is_enough_to_grade(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.get_or_create_day(OCT16)
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(8_000))
    assert close(OCT16) == ["Steps 8,000 / 10,000. +2,000 tomorrow."]
    assert steps(OCT16)["evidence"] == "google_health"


# ── Regrade 16 Oct ──────────────────────────────────────────────────────────

def _closed_short_day():
    """Oct 16 closed on the phone's 7,140, and Oct 17 already carrying +2,860."""
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    close(OCT16)
    ledger.get_or_create_day(OCT17)
    ledger.record_event({"type": "steps", "count": 11_000}, now=at(12, date=OCT17))
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860
    assert steps(OCT17)["target"] == 12_860
    assert steps(OCT17)["status"] == "pending"


def test_regrade_clears_the_16_oct_debt_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    _closed_short_day()
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(13_000))

    result = ledger.regrade_steps(OCT16)
    assert result["ok"] is True
    assert result["count_before"] == 7_140
    assert result["count_after"] == 13_000
    assert result["debt_before"] == 2_860
    assert result["debt_after"] == 0
    assert result["evidence"] == "shortcut+google_health"
    assert result["next_target_before"] == 12_860
    assert result["next_target_after"] == 10_000
    assert steps(OCT17)["status"] == "done"
    assert steps(OCT17)["count"] == 11_000

    again = ledger.regrade_steps(OCT16)
    assert again["count_after"] == 13_000
    assert again["debt_after"] == 0
    assert again["next_target_after"] == 10_000
    assert [e["type"] for e in ledger.load_day(OCT16)["events"]].count("steps.health") == 1


def test_regrade_leaves_a_closed_later_day_gym_debt_alone(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    close(OCT16)

    def _wednesday(day):
        day["closed"] = True
        day["debt"]["gym_minutes"] = 20
        day["debt"]["steps"] = 3_000
        item = ledger.find_item(day, "steps")
        item["count"] = 9_000
        item["target"] = 12_860
        day["events"].append({"ts": at(18, date=OCT17).isoformat(timespec="seconds"),
                              "type": "steps", "value": 9_000})

    ledger.update_day(OCT17, _wednesday)
    ledger.get_or_create_day(OCT17 + datetime.timedelta(days=1))
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(13_000))

    ledger.regrade_steps(OCT16)
    wed = ledger.load_day(OCT17)
    assert wed["debt"]["gym_minutes"] == 20
    assert wed["debt"]["steps"] == 1_000
    assert steps(OCT17)["target"] == 10_000
    thursday = OCT17 + datetime.timedelta(days=1)
    assert steps(thursday)["target"] == 11_000


def test_regrade_command_reports_the_change_and_refuses_a_missing_day(capsys):
    assert ledger.regrade_steps_main("16 Oct") == 2
    assert ledger.regrade_steps_main("2026-10-16") == 1
    assert "No ledger" in capsys.readouterr().out


def test_regrade_command_leaves_the_file_when_health_has_no_data(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    close(OCT16)
    assert ledger.regrade_steps_main("2026-10-16") == 1
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860
    assert "unchanged" in capsys.readouterr().out


def test_regrade_command_prints_the_cleared_debt(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    _closed_short_day()
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(13_000))
    assert ledger.regrade_steps_main("2026-10-16") == 0
    out = capsys.readouterr().out
    assert "2026-10-16 steps 7,140 → 13,000 (shortcut+google_health)" in out
    assert "2026-10-16 step debt 2,860 → 0" in out
    assert "2026-10-17 step target 12,860 → 10,000" in out


# ── Brief agrees with the ledger ────────────────────────────────────────────

def test_brief_yesterday_shows_the_reconciled_count(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    _closed_short_day()
    morning_brief.sync_yesterday_steps(OCT16, 13_000)
    prev = ledger.load_day(OCT16)
    cells = morning_brief.yesterday_stats(prev, health_steps=13_000)
    assert cells[1] == {"value": "13,000", "label": "Steps", "miss": False}
    today = ledger.load_day(OCT17)
    assert steps(OCT17)["target"] == 10_000
    assert morning_brief.owed_rows(today, OCT17) == []


def test_brief_keeps_the_shortcut_when_health_is_lower(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    close(OCT16)
    ledger.get_or_create_day(OCT17)
    morning_brief.sync_yesterday_steps(OCT16, 5_000)
    prev = ledger.load_day(OCT16)
    cells = morning_brief.yesterday_stats(prev, health_steps=5_000)
    assert cells[1]["value"] == "7,140"
    assert cells[1]["miss"] is True
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860
    assert steps(OCT17)["target"] == 12_860


def test_brief_sync_swallows_a_reconcile_error(monkeypatch):
    monkeypatch.setattr(ledger, "pull_health_steps", pull_returning(None))
    ledger.record_event({"type": "steps", "count": 7_140}, now=at(21))
    close(OCT16)

    def boom(*args, **kwargs):
        raise RuntimeError("disk")
    monkeypatch.setattr(ledger, "reconcile_steps", boom)
    morning_brief.sync_yesterday_steps(OCT16, 13_000)
    assert ledger.load_day(OCT16)["debt"]["steps"] == 2_860

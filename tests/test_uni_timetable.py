"""uni_timetable .ics handling — the layer where this term's silent 403 bug
lived. Tests recurrence math, the marker filter, webcal normalisation, and that
the fetch sends a browser User-Agent (the actual 403 fix)."""

import datetime
import pytest

import uni_timetable as ut

ICAL = pytest.importorskip("icalendar")  # skip cleanly if icalendar absent


# ── URL normalisation ─────────────────────────────────────────────────────────

def test_webcal_normalised_to_https():
    assert ut._normalise_url("webcal://x/y.ics") == "https://x/y.ics"


def test_https_left_alone():
    assert ut._normalise_url("https://x/y.ics") == "https://x/y.ics"


# ── Recurrence: _event_occurs_on ──────────────────────────────────────────────

def _vevent(start_date, rrule=None):
    ev = ICAL.Event()
    ev.add("summary", "Class")
    ev.add("dtstart", datetime.datetime.combine(start_date, datetime.time(9, 0)))
    ev.add("dtend", datetime.datetime.combine(start_date, datetime.time(10, 0)))
    if rrule:
        ev.add("rrule", rrule)
    return ev


def test_oneoff_matches_only_its_date():
    ev = _vevent(datetime.date(2026, 6, 1))
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 1))
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 8))


def test_weekly_until_bounds():
    # Weekly Mondays until 2026-06-15.
    ev = _vevent(datetime.date(2026, 6, 1),
                 {"freq": "weekly", "until": datetime.datetime(2026, 6, 15, 0, 0)})
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 1))
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 8))
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 15))
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 22))  # past UNTIL
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 4))   # wrong weekday


def test_weekly_count_bounds():
    ev = _vevent(datetime.date(2026, 6, 1), {"freq": "weekly", "count": 3})
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 1))       # occ 0
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 15))      # occ 2 (3rd)
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 22))  # occ 3 → past COUNT


def test_fortnightly_interval():
    ev = _vevent(datetime.date(2026, 6, 1),
                 {"freq": "weekly", "interval": 2, "count": 5})
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 1))       # on-week
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 8))   # off-week
    assert ut._event_occurs_on(ev, datetime.date(2026, 6, 15))      # on-week


def test_before_first_occurrence():
    ev = _vevent(datetime.date(2026, 6, 8), {"freq": "weekly", "count": 5})
    assert not ut._event_occurs_on(ev, datetime.date(2026, 6, 1))


# ── get_events_for: parsing, marker filter (monkeypatched fetch) ──────────────

def _calendar_bytes(*events):
    cal = ICAL.Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//test//EN")
    for e in events:
        cal.add_component(e)
    return cal.to_ical()


def test_get_events_for_returns_todays_class(monkeypatch):
    ev = _vevent(datetime.date(2026, 6, 1))
    monkeypatch.setattr(ut, "ICS_URL", "https://stub")
    monkeypatch.setattr(ut, "_fetch_ics", lambda url, timeout=15: _calendar_bytes(ev))
    out = ut.get_events_for(datetime.date(2026, 6, 1))
    assert len(out) == 1
    assert out[0]["title"] == "Class"


def test_zero_duration_marker_filtered(monkeypatch):
    marker = ICAL.Event()
    marker.add("summary", "Start of Term 2")
    marker.add("dtstart", datetime.datetime(2026, 6, 1, 9, 0))
    marker.add("dtend", datetime.datetime(2026, 6, 1, 9, 0))  # zero duration
    monkeypatch.setattr(ut, "ICS_URL", "https://stub")
    monkeypatch.setattr(ut, "_fetch_ics", lambda url, timeout=15: _calendar_bytes(marker))
    assert ut.get_events_for(datetime.date(2026, 6, 1)) == []


def test_dormant_when_no_url(monkeypatch):
    monkeypatch.setattr(ut, "ICS_URL", "")
    assert ut.get_events_for(datetime.date(2026, 6, 1)) == []


def test_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(ut, "ICS_URL", "https://stub")
    monkeypatch.setattr(ut, "_fetch_ics", lambda url, timeout=15: None)
    assert ut.get_events_for(datetime.date(2026, 6, 1)) == []


# ── The 403 fix: fetch must send a browser User-Agent ─────────────────────────

def test_fetch_sends_user_agent(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"OK"

    def fake_urlopen(req, timeout=15):
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(ut, "urlopen", fake_urlopen)
    data = ut._fetch_ics("https://my.unsw.edu.au/x.ics")
    assert data == b"OK"
    assert captured["ua"] and "Mozilla" in captured["ua"]

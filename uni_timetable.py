"""
Jarvis — UNSW Timetable (subscribed .ics feed)
==============================================
Manav's uni timetable lives in Apple Calendar as a *subscribed* calendar, which
means there's an underlying .ics feed URL. Jarvis runs on a Linux VPS, so it
fetches that feed directly over HTTPS — no Apple/iCloud login, no app-specific
password, and it auto-reflects timetable changes the moment UNSW updates them.

The feed URL goes in .env as TIMETABLE_ICS_URL (webcal:// is normalised to
https://). If it's unset, every function here returns empty — Jarvis behaves
exactly as before, so this integration is safe to ship before the URL is added.

The UNSW feed expresses classes as weekly/fortnightly recurrences bounded by
COUNT (e.g. RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=5), so the recurrence handling
below honours FREQ=WEEKLY, INTERVAL, COUNT and UNTIL — otherwise expired
classes from a finished term would show forever.

Output shapes (matching the two existing calendar consumers):
  - get_today_events()       → [{title, time, location, start, end}]  (rich; start/end tz-aware)
  - get_today_events_brief() → [{title, time, location, description}] (matches morning_brief.fetch_calendar_events)
  - get_busy_blocks()        → [(start, end), ...]                    (for free-block logic)
  - get_tomorrow_summary()   → str                                   (for the evening check-in)

Run directly to smoke-test the feed:
    python uni_timetable.py
"""

import datetime
from urllib.request import urlopen, Request

import pytz

import config

try:
    from icalendar import Calendar
    ICAL_AVAILABLE = True
except Exception:
    ICAL_AVAILABLE = False

TIMEZONE = pytz.timezone(config.TIMEZONE)

# Feed URL from .env (empty string if unset → integration stays dormant).
ICS_URL = getattr(config, "TIMETABLE_ICS_URL", "") or ""


def _normalise_url(url):
    """webcal:// is just http(s) under the hood — swap the scheme for urlopen."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def _fetch_ics(url, timeout=15):
    """
    Fetch the raw .ics bytes. Returns None on any network/HTTP error.
    A browser User-Agent is required: UNSW's feed returns HTTP 403 to the
    default urllib agent.
    """
    try:
        req = Request(_normalise_url(url), headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _to_local_dt(value):
    """
    Convert an icalendar DTSTART/DTEND value to a tz-aware datetime in the local
    zone. All-day events arrive as a date; anchor them to local midnight.
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = TIMEZONE.localize(value)
        return value.astimezone(TIMEZONE)
    # date (all-day) → local midnight
    return datetime.datetime.combine(value, datetime.time.min).astimezone(TIMEZONE)


def _rrule_scalar(rule, key, default=None):
    """icalendar vRecur stores values as lists ([10]); pull the first, safely."""
    if rule is None:
        return default
    val = rule.get(key)
    if not val:
        return default
    return val[0] if isinstance(val, (list, tuple)) else val


def _event_occurs_on(comp, target):
    """
    True if a VEVENT (one-off or weekly-recurring) has an occurrence on `target`
    (a date). Honours FREQ=WEEKLY with INTERVAL, COUNT and UNTIL so that classes
    from a finished term correctly stop appearing.
    """
    dtstart = comp.get("dtstart")
    if dtstart is None:
        return False
    first = _to_local_dt(dtstart.dt).date()

    # Non-recurring: simple date match.
    if "rrule" not in comp:
        return first == target

    rule = comp.get("rrule")
    freq = str(_rrule_scalar(rule, "FREQ", "WEEKLY")).upper()
    if freq != "WEEKLY":
        # A class timetable only uses weekly recurrence; ignore anything else.
        return False

    if target < first:
        return False

    interval = int(_rrule_scalar(rule, "INTERVAL", 1) or 1)
    step = 7 * interval
    delta = (target - first).days
    if delta % step != 0:
        return False  # falls between recurrence weeks (e.g. off-week of a fortnightly class)

    occurrence_index = delta // step  # 0-based: which occurrence `target` would be

    count = _rrule_scalar(rule, "COUNT")
    if count is not None and occurrence_index >= int(count):
        return False  # past the COUNT-th (last) occurrence

    until = _rrule_scalar(rule, "UNTIL")
    if until is not None:
        until_date = _to_local_dt(until).date() if isinstance(until, datetime.datetime) else until
        if target > until_date:
            return False

    return True


def get_events_for(target_date=None):
    """
    Returns timetable events for `target_date` (default today) with tz-aware
    start/end datetimes. Shape matches jarvis_calendar.get_today_events:
        [{title, time, location, start, end}]  sorted by start.
    Empty list if the feed is unset, unreachable, or icalendar is missing.
    """
    if not ICS_URL or not ICAL_AVAILABLE:
        return []
    raw = _fetch_ics(ICS_URL)
    if not raw:
        return []

    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return []

    target = target_date or datetime.datetime.now(TIMEZONE).date()
    events = []
    for comp in cal.walk("VEVENT"):
        try:
            if not _event_occurs_on(comp, target):
                continue
            dtstart = comp.get("dtstart").dt
            dtend_field = comp.get("dtend")
            dtend = dtend_field.dt if dtend_field is not None else dtstart

            start_dt = _to_local_dt(dtstart)
            end_dt   = _to_local_dt(dtend)
            # Skip zero-duration marker events (e.g. "Start of Term 2"), which
            # the UNSW feed includes as AcadCalendar pins, not real classes.
            if end_dt <= start_dt and isinstance(dtstart, datetime.datetime):
                continue
            # For a recurring match, shift the (time-of-day) onto the target date.
            if start_dt.date() != target:
                shift = target - start_dt.date()
                start_dt = start_dt + shift
                end_dt   = end_dt + shift

            all_day = not isinstance(dtstart, datetime.datetime)
            if all_day:
                time_str = "All day"
            else:
                time_str = start_dt.strftime("%-I:%M %p") + "–" + end_dt.strftime("%-I:%M %p")

            events.append({
                "title":    str(comp.get("summary", "Class")),
                "time":     time_str,
                "location": str(comp.get("location", "")),
                "start":    start_dt,
                "end":      end_dt,
            })
        except Exception:
            # One malformed VEVENT shouldn't sink the whole feed.
            continue

    return sorted(events, key=lambda x: x["start"])


def get_today_events():
    """Today's timetable events (rich shape). See get_events_for."""
    return get_events_for(None)


def get_today_events_brief():
    """
    Today's classes in the shape morning_brief.fetch_calendar_events returns:
        [{title, time, location, description}].
    `description` is tagged so Claude knows these are uni classes.
    """
    return [
        {"title": e["title"], "time": e["time"],
         "location": e["location"], "description": "UNSW timetable"}
        for e in get_today_events()
    ]


def get_tomorrow_summary():
    """
    A one-line-per-class text summary of tomorrow's timetable, for the evening
    check-in's tomorrow-context. Empty string if no classes / feed dormant.
    """
    tomorrow = datetime.datetime.now(TIMEZONE).date() + datetime.timedelta(days=1)
    evs = get_events_for(tomorrow)
    if not evs:
        return ""
    lines = []
    for e in evs:
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"  {e['time']}: {e['title']}{loc}")
    return "TOMORROW'S UNSW CLASSES:\n" + "\n".join(lines)


def get_busy_blocks():
    """
    Returns today's class periods as (start, end) tz-aware tuples, for merging
    into free-block / daily-plan logic. All-day items are skipped.
    """
    return [(e["start"], e["end"]) for e in get_today_events() if e["time"] != "All day"]


if __name__ == "__main__":
    if not ICAL_AVAILABLE:
        print("⚠️  icalendar not installed — run: pip install icalendar")
    elif not ICS_URL:
        print("⚠️  TIMETABLE_ICS_URL not set in .env — integration is dormant.")
        print("    Add it (webcal:// or https://...ics) and re-run.")
    else:
        evs = get_today_events()
        print(f"UNSW timetable — {len(evs)} class(es) today "
              f"({datetime.datetime.now(TIMEZONE):%A %d %b %Y}):\n")
        for e in evs:
            loc = f"  @ {e['location']}" if e["location"] else ""
            print(f"  {e['time']:>20}  {e['title']}{loc}")
        if not evs:
            print("  (none today)")
        tom = get_tomorrow_summary()
        if tom:
            print("\n" + tom)

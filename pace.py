"""
Jarvis — Step Pace
==================
Nudges on Telegram when Manav is behind his step pace. Readings come from
the iOS Shortcut (POST /events steps) into the ledger. The shortfall at day
close becomes tomorrow's step debt (enforcer._settle). See docs/REWORK.md §6.

CHECKPOINTS (share of the day's target, which includes step debt):
  10:00 25%   13:00 50%   16:00 70%   19:00 85%   21:00 100%

A checkpoint waits for a reading taken at or after it, for up to
WAIT_AFTER minutes. It is then judged on the latest count, provided some
reading arrived within FRESH_BEFORE minutes before it or later. Otherwise
it is skipped silently: no reading means unknown, not behind.
Only a behind result sends a message. The fix is sized at ~100 steps/min
and placed in the next free calendar gap.

No Anthropic calls. The calendar is read only when a nudge is sent.
"""

import datetime
import math

import ledger

CHECKPOINTS = (
    (datetime.time(10, 0), 0.25),
    (datetime.time(13, 0), 0.50),
    (datetime.time(16, 0), 0.70),
    (datetime.time(19, 0), 0.85),
    (datetime.time(21, 0), 1.00),
)
FRESH_BEFORE = 15
WAIT_AFTER = 20
STEPS_PER_MIN = 100
MIN_GAP_MIN = 15
FREE_UNTIL = datetime.time(22, 0)


def _at(date, t):
    """A tz-aware datetime on date at time t in the configured timezone."""
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, t))


def busy_periods():
    """Today's busy (start, end) periods, or None when the calendar can't be read."""
    try:
        from jarvis_calendar import get_busy_periods
        return get_busy_periods()
    except Exception:
        return None


def next_free(busy, now, end):
    """
    The free window to walk in: (start, end) of the gap containing now, or
    the next gap of at least MIN_GAP_MIN before end. None when there isn't one.
    """
    cursor = now
    for b_start, b_end in sorted(busy):
        if b_end <= cursor:
            continue
        if b_start > cursor and (b_start - cursor).total_seconds() >= MIN_GAP_MIN * 60:
            return cursor, min(b_start, end)
        cursor = max(cursor, b_end)
        if cursor >= end:
            return None
    if (end - cursor).total_seconds() >= MIN_GAP_MIN * 60:
        return cursor, end
    return None


def _fix(gap, now, busy):
    """The 'walk N min, when' part of a nudge."""
    minutes = math.ceil(gap / STEPS_PER_MIN)
    walk = f"{minutes} min walk"
    if busy is None:
        return f"{walk}."
    window = next_free(busy, now, _at(now.date(), FREE_UNTIL))
    if window is None:
        return f"{walk}. No free block left today, so walk between things."
    start, end = window
    if start <= now:
        return f"{walk}. Free until {end:%H:%M}."
    return f"{walk}. Next free {start:%H:%M}–{end:%H:%M}."


def is_reading(event):
    """
    A usable steps reading. A count of 0 is what the Shortcut sends when the
    phone is locked and Health can't be read, so it counts as no reading.
    """
    return event.get("type") == "steps" and (event.get("value") or 0) > 0


def _readings_since(day, since):
    """True when a usable steps reading arrived at or after since."""
    for event in day.get("events", []):
        if is_reading(event) and datetime.datetime.fromisoformat(event["ts"]) >= since:
            return True
    return False


def _due(day, now):
    """
    Checkpoints to judge now, as (label, pace, judged) where judged is False
    when the checkpoint is being skipped for lack of a fresh reading.
    Marks each one checked on the steps item.
    """
    steps = ledger.find_item(day, "steps")
    if steps is None:
        return []
    state = steps.setdefault("pace", {"checked": []})
    date = datetime.date.fromisoformat(day["date"])
    result = []
    for t, share in CHECKPOINTS:
        label = t.strftime("%H:%M")
        cp = _at(date, t)
        if label in state["checked"] or now < cp:
            continue
        if not _readings_since(day, cp) and now < cp + datetime.timedelta(minutes=WAIT_AFTER):
            continue  # The checkpoint's own Shortcut reading may still be on its way.
        fresh = _readings_since(day, cp - datetime.timedelta(minutes=FRESH_BEFORE))
        state["checked"].append(label)
        # A checkpoint first seen an hour late (daemon down) is stale: skip it.
        judged = fresh and now < cp + datetime.timedelta(hours=1)
        result.append((label, round(steps["target"] * share), judged))
    return result


def tick(now=None, busy_fn=None):
    """Judge any checkpoint that's due. Returns messages to send."""
    now = now or ledger.now_local()
    busy_fn = busy_fn or busy_periods

    def _check(day):
        steps = ledger.find_item(day, "steps")
        if steps is None or day.get("closed"):
            return []
        behind = []
        for label, pace, judged in _due(day, now):
            if judged and steps["count"] < pace:
                behind.append((label, pace))
        if behind:
            label, pace = behind[-1]  # Several due at once: only the latest matters.
            day["events"].append({"ts": now.isoformat(timespec="seconds"),
                                  "type": "pace.behind", "checkpoint": label,
                                  "count": steps["count"], "pace": pace})
            return [(label, pace, steps["count"], steps["target"])]
        return []

    found = ledger.update_day(now.date(), _check)
    if not found:
        return []
    label, pace, count, target = found[0]
    gap = pace - count
    busy = busy_fn()
    if pace >= target:
        head = f"{count:,} / {target:,}. {gap:,} short."
    else:
        head = f"{gap:,} behind {pace:,} pace."
    return [f"{head} {_fix(gap, now, busy)}"]

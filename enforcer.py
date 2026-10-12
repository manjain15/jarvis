"""
Jarvis — Gym Enforcer
=====================
Runs the morning gym sequence over Telegram and settles gym debt. State
lives on the ledger's gym item ("seq"), so a daemon restart picks up
where it left off. See docs/REWORK.md §5 and §7.

The Telegram daemon calls:
  tick(now)              every loop (~25s). Returns messages to send.
  handle_text(text, now) before commands / Claude. Returns a reply, or None
                         when the text isn't for the enforcer.
  handle_photo(now)      when a photo arrives. Returns a reply or None.

SEQUENCE (gym day, status pending):
  start      wake event, or slot − 30 min with no wake   "07:04 · Pull · leave by 07:05 / Reply up."
  up +3      no "up" reply                               "3 min. Reply up."
  up         reply "up"                                  "Clothes on. Photo, 10 min."
  photo +10  no photo                                    "Photo."
  leave-by   not arrived                                 "Leave now."
  slot +30   not arrived                                 "No arrival. Reason, or go."
  slot +2h   Hevy checked every 30 min until 23:00       done / partial / missed

EXCUSES ("skip <reason>"):
  sick / ill / injured   excused, logged for the weekly review
  slept < 6h             moved to EVENING_SLOT (once a day)
  < 75 min free          35-min version (once a day)
  anything else          rejected, sequence continues

DEBT (settled when the day closes, 23:00):
  done      Hevy minutes >= target − 15. Clears gym_minutes debt.
  partial   >= 25 min. No new debt.
  unenforced  the sequence never ran that day (before go-live, daemon down).
            No debt.
  missed    owes a make-up: the session + 10 min, within 2 days, as an extra
            workout (a rest-day session, or minutes beyond that day's target).
  make-up not done by its due date: +20 min on the next gym session, which
            stays until a session hits the longer target.

No Anthropic calls. Hevy is called at most every 30 min, and only after
slot + 2h. Calendar and Google Health are read only when a skip needs it.
"""

import datetime
import os
import re

import ledger

TRAVEL_MINUTES     = int(os.environ.get("JARVIS_GYM_TRAVEL_MIN", "10"))
EVENING_SLOT       = os.environ.get("JARVIS_GYM_EVENING_SLOT", "18:00")
FALLBACK_LEAD      = 30
EVENING_LEAD       = 60
UP_REMIND_AFTER    = 3
PHOTO_REMIND_AFTER = 10
ARRIVAL_GRACE      = 30
VERIFY_AFTER       = 120
VERIFY_EVERY       = 30
DAY_CLOSE          = datetime.time(23, 0)
DONE_SLACK         = 15
MIN_PARTIAL        = 25
SHORT_MINUTES      = 35
FULL_DAY_FREE      = 75
FULL_DAY_UNTIL     = datetime.time(21, 0)
LOW_SLEEP_HOURS    = 6
MAKEUP_EXTRA       = 10
MAKEUP_DAYS        = 2
EXPIRED_PENALTY    = 20
SICK_WORDS         = ("sick", "ill", "injured", "injury", "fever")


# ── Evidence sources (replaced in tests) ─────────────────────────────────────

def workout_minutes(date):
    """Total Hevy minutes logged on date, or None when Hevy can't be reached."""
    try:
        from hevy import fetch_recent_workouts, parse_workout_date
    except Exception:
        return None
    workouts = fetch_recent_workouts(page_size=10)
    if workouts is None:
        return None
    total = 0
    for w in workouts:
        start = parse_workout_date(w)
        if start is None or start.date() != date:
            continue
        try:
            end = datetime.datetime.fromisoformat(w["end_time"].replace("Z", "+00:00"))
            total += max(0, int((end - start).total_seconds() // 60))
        except Exception:
            continue
    return total


def sleep_hours(day):
    """Last night's sleep: the phone report on the ledger, else Google Health, else None."""
    if day.get("sleep_hours") is not None:
        return float(day["sleep_hours"])
    try:
        from google_health import fetch_sleep, get_access_token
        night = datetime.date.fromisoformat(day["date"]) - datetime.timedelta(days=1)
        sleep = fetch_sleep(get_access_token(), night)
        if sleep and sleep.get("total_minutes"):
            return round(sleep["total_minutes"] / 60, 1)
    except Exception:
        pass
    return None


def longest_free_minutes(now):
    """Longest free calendar gap between now and FULL_DAY_UNTIL, or None if unknown."""
    try:
        from jarvis_calendar import get_busy_periods
        busy = get_busy_periods()
    except Exception:
        return None
    return _longest_gap(busy, now, _at(now.date(), FULL_DAY_UNTIL, now))


def _longest_gap(busy, start, end):
    """Longest gap in minutes between start and end, given (start, end) busy periods."""
    cursor, best = start, 0
    for b_start, b_end in sorted(busy):
        if b_end <= cursor:
            continue
        if b_start > cursor:
            best = max(best, (min(b_start, end) - cursor).total_seconds() / 60)
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if cursor < end:
        best = max(best, (end - cursor).total_seconds() / 60)
    return int(best)


# ── Time helpers ─────────────────────────────────────────────────────────────

def _at(date, t, _like=None):
    """A tz-aware datetime on date at time t in the configured timezone."""
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, t))


def _hm(text):
    """'07:15' → datetime.time(7, 15)."""
    hours, minutes = text.split(":")
    return datetime.time(int(hours), int(minutes))


def _ts(now):
    return now.isoformat(timespec="seconds")


def _parse(ts):
    return datetime.datetime.fromisoformat(ts) if ts else None


def slot_dt(day, gym, now):
    """The gym slot as a datetime on the ledger's date."""
    return _at(datetime.date.fromisoformat(day["date"]), _hm(gym["slot"]), now)


def leave_by(day, gym, now):
    """When Manav has to leave to make the slot."""
    return slot_dt(day, gym, now) - datetime.timedelta(minutes=TRAVEL_MINUTES)


def _seq(gym):
    """The gym item's sequence state, created on first use."""
    return gym.setdefault("seq", {
        "started": None, "stage": None, "stage_at": None,
        "sent": [], "last_verify": None, "moved": False, "short": False,
    })


def _log(day, kind, now, **extra):
    """Append an enforcer entry to the ledger's event log."""
    day["events"].append({"ts": _ts(now), "type": f"enforcer.{kind}", **extra})


def _is_sick(reason):
    """True when reason names illness or injury as a whole word."""
    return any(w in SICK_WORDS for w in re.findall(r"[a-z]+", reason.lower()))


def _owed_line(day):
    """One line describing what's owed, or '' when nothing is."""
    debt = day.get("debt") or {}
    parts = []
    makeup = debt.get("makeup")
    if makeup:
        due = datetime.date.fromisoformat(makeup["due"]).strftime("%a")
        parts.append(f"make-up {makeup['minutes']} min by {due}")
    if debt.get("gym_minutes"):
        parts.append(f"+{debt['gym_minutes']} min on today's session")
    return ("Owed: " + ", ".join(parts)) if parts else ""


# ── Sequence ─────────────────────────────────────────────────────────────────

def _active_gym(day):
    """The gym item if the sequence applies to it today, else None."""
    gym = ledger.find_item(day, "gym")
    if gym is None or gym.get("status") != "pending" or day.get("closed"):
        return None
    return gym


def _start_message(day, gym, now):
    seq = _seq(gym)
    leave = leave_by(day, gym, now)
    if seq["moved"]:
        head = f"{gym['slot']} · {gym['title']} · leave by {leave:%H:%M}"
    else:
        head = f"{now:%H:%M} · {gym['title']} · leave by {leave:%H:%M}\nReply up."
    owed = _owed_line(day)
    return f"{head}\n{owed}" if owed else head


def _sequence_messages(day, gym, now):
    """Advance the clock-driven parts of the sequence. Returns messages to send."""
    seq = _seq(gym)
    out = []
    slot = slot_dt(day, gym, now)
    leave = slot - datetime.timedelta(minutes=TRAVEL_MINUTES)

    verify_from = slot + datetime.timedelta(minutes=VERIFY_AFTER)

    def once(key, text, until):
        """Send text once. Past `until` it's stale: mark it sent, say nothing."""
        if key not in seq["sent"]:
            seq["sent"].append(key)
            if now < until:
                out.append(text)

    if not seq["started"]:
        woke = _parse(day.get("woke_at"))
        lead = EVENING_LEAD if seq["moved"] else FALLBACK_LEAD
        due = (woke is not None and not seq["moved"]) or now >= slot - datetime.timedelta(minutes=lead)
        if not due or now >= verify_from:
            return out
        seq["started"] = _ts(now)
        seq["stage_at"] = _ts(now)
        _log(day, "start", now, trigger="wake" if woke and not seq["moved"] else "clock")
        if gym.get("arrived_at"):
            seq["stage"] = "ready"
            return out
        if now >= leave:
            # Late start (woke after leave-by, or the daemon was down): one message, not three.
            seq["stage"] = "ready"
            seq["sent"] += [f"leave:{gym['slot']}", f"arrival:{gym['slot']}"]
            owed = _owed_line(day)
            text = f"{now:%H:%M} · {gym['title']} · late. Go now, or skip <reason>."
            out.append(f"{text}\n{owed}" if owed else text)
            return out
        seq["stage"] = "ready" if seq["moved"] else "up"
        out.append(_start_message(day, gym, now))
        return out

    stage_at = _parse(seq["stage_at"])
    arrival_at = slot + datetime.timedelta(minutes=ARRIVAL_GRACE)
    if seq["stage"] == "up" and now >= stage_at + datetime.timedelta(minutes=UP_REMIND_AFTER):
        once("up_remind", "3 min. Reply up.", leave)
    if seq["stage"] == "photo" and now >= stage_at + datetime.timedelta(minutes=PHOTO_REMIND_AFTER):
        once("photo_remind", "Photo.", leave)

    if gym.get("arrived_at") is None:
        if now >= leave:
            once(f"leave:{gym['slot']}", "Leave now.", arrival_at)
        if now >= arrival_at:
            once(f"arrival:{gym['slot']}", "No arrival. Reason, or go.", verify_from)
    return out


def _verify_due(day, gym, now):
    """True when Hevy should be checked for this gym item now."""
    if gym.get("status") not in ("pending", "partial") or day.get("closed"):
        return False
    if now < slot_dt(day, gym, now) + datetime.timedelta(minutes=VERIFY_AFTER):
        return False
    last = _parse(_seq(gym).get("last_verify"))
    return last is None or now - last >= datetime.timedelta(minutes=VERIFY_EVERY)


def _grade(gym, minutes):
    """'done', 'partial' or None for a Hevy total against the gym target."""
    if minutes >= max(MIN_PARTIAL, gym["minutes"] - DONE_SLACK):
        return "done"
    if minutes >= MIN_PARTIAL:
        return "partial"
    return None


def _apply_workout(day, gym, minutes, now):
    """Record a Hevy check on the gym item. Returns a message when status changes."""
    _seq(gym)["last_verify"] = _ts(now)
    grade = _grade(gym, minutes)
    if grade is None or grade == gym["status"]:
        return None
    gym["status"] = grade
    gym["logged_minutes"] = minutes
    _log(day, "verified", now, status=grade, minutes=minutes)
    if grade == "done":
        return f"Logged · {gym['title']} · {minutes} min."
    return f"Logged · {gym['title']} · {minutes} min. Partial."


# ── Day close and debt ───────────────────────────────────────────────────────

def _settle(day, minutes, now):
    """
    Close a ledger: grade the gym item, settle make-ups and gym debt.
    minutes is the day's Hevy total. Returns messages to send.
    """
    out = []
    date = datetime.date.fromisoformat(day["date"])
    debt = day["debt"] = day.get("debt") or {"gym_minutes": 0, "steps": 0, "makeup": None}
    debt.setdefault("makeup", None)
    gym = ledger.find_item(day, "gym")
    needed = 0

    if gym is not None:
        # A day the sequence never ran (before go-live, or the daemon was down
        # all morning) is not held against Manav. A logged session still counts.
        if gym["status"] == "pending" and not (gym.get("seq") or {}).get("started"):
            grade = _grade(gym, minutes)
            gym["status"] = grade or "unenforced"
            if grade:
                gym["logged_minutes"] = minutes
        if gym["status"] in ("pending", "partial"):
            grade = _grade(gym, minutes)
            if grade:
                gym["status"] = grade
                gym["logged_minutes"] = minutes
            elif gym["status"] == "pending":
                gym["status"] = "missed"
        needed = gym["minutes"] if gym["status"] in ("done", "partial") else 0

        if gym["status"] == "done":
            debt["gym_minutes"] = 0
        elif gym["status"] == "missed":
            if debt["makeup"] is None:
                due = date + datetime.timedelta(days=MAKEUP_DAYS)
                debt["makeup"] = {
                    "split": gym["title"],
                    "minutes": gym.get("base_minutes", gym["minutes"]) + MAKEUP_EXTRA,
                    "due": due.isoformat(),
                    "from": day["date"],
                }
                out.append(f"{gym['title']} missed. Make-up: "
                           f"{debt['makeup']['minutes']} min by {due:%a}.")
            else:
                debt["gym_minutes"] += MAKEUP_EXTRA
                out.append(f"{gym['title']} missed with a make-up still owed. "
                           f"+{MAKEUP_EXTRA} min on the next session.")

    makeup = debt.get("makeup")
    if makeup and makeup.get("from") != day["date"]:
        extra = minutes - needed
        if extra >= makeup["minutes"] - DONE_SLACK:
            out.append(f"Make-up done · {extra} min. Cleared.")
            debt["makeup"] = None
        elif date >= datetime.date.fromisoformat(makeup["due"]):
            debt["gym_minutes"] += EXPIRED_PENALTY
            debt["makeup"] = None
            out.append(f"Make-up not done. +{EXPIRED_PENALTY} min on the next session "
                       "until it's hit.")

    day["closed"] = True
    _log(day, "closed", now, minutes=minutes)
    return out


def _close(date, now, fetch):
    """Close date's ledger if it exists and is still open. Returns messages."""
    stored = ledger.load_day(date)
    if stored is None or stored.get("closed"):
        return []
    minutes = fetch(date)
    if minutes is None:
        return []  # Hevy unreachable. The next tick retries.
    out = ledger.update_day(date, lambda day: [] if day.get("closed") else _settle(day, minutes, now))
    # The next day may already exist (built before this close). Re-carry the debt.
    following = date + datetime.timedelta(days=1)
    if ledger.load_day(following) is not None:
        closed = ledger.load_day(date)
        ledger.update_day(following, lambda day: ledger.apply_carried_debt(day, closed))
    return out


# ── Entry points for the Telegram daemon ─────────────────────────────────────

_last_close_attempt = {}


def tick(now=None, fetch=None):
    """
    Advance the enforcer. Returns messages to send, in order.
    fetch(date) returns Hevy minutes for date (default: workout_minutes).
    """
    now = now or ledger.now_local()
    fetch = fetch or workout_minutes
    today = now.date()
    out = []

    # Close yesterday first if the daemon was down at 23:00, at most every 30 min.
    yesterday = today - datetime.timedelta(days=1)
    last = _last_close_attempt.get(yesterday)
    if last is None or now - last >= datetime.timedelta(minutes=VERIFY_EVERY):
        stored = ledger.load_day(yesterday)
        if stored is not None and not stored.get("closed"):
            _last_close_attempt[yesterday] = now
            out += _close(yesterday, now, fetch)

    def _step(day):
        gym = _active_gym(day)
        msgs = _sequence_messages(day, gym, now) if gym else []
        g = ledger.find_item(day, "gym")
        return msgs, bool(g and _verify_due(day, g, now))

    msgs, verify = ledger.update_day(today, _step)
    out += msgs

    if verify:
        minutes = fetch(today)
        if minutes is not None:
            def _record(day):
                g = ledger.find_item(day, "gym")
                return _apply_workout(day, g, minutes, now) if g else None
            msg = ledger.update_day(today, _record)
            if msg:
                out.append(msg)
        else:
            ledger.update_day(today, lambda day: _seq(ledger.find_item(day, "gym"))
                              .__setitem__("last_verify", _ts(now)))

    if now.time() >= DAY_CLOSE:
        last = _last_close_attempt.get(today)
        if last is None or now - last >= datetime.timedelta(minutes=VERIFY_EVERY):
            _last_close_attempt[today] = now
            out += _close(today, now, fetch)
    return out


def handle_text(text, now=None, sleep_fn=None, free_fn=None):
    """
    Route a Telegram text to the enforcer. Returns the reply, or None when
    the text isn't 'up' / 'skip ...' during an active gym day.
    """
    now = now or ledger.now_local()
    words = text.strip().lower().split(None, 1)
    if not words or words[0] not in ("up", "skip"):
        return None
    sleep_fn = sleep_fn or sleep_hours
    free_fn = free_fn or longest_free_minutes

    if words[0] == "up":
        def _up(day):
            gym = _active_gym(day)
            if gym is None or _seq(gym)["stage"] != "up":
                return None
            seq = _seq(gym)
            seq["stage"], seq["stage_at"] = "photo", _ts(now)
            _log(day, "up", now)
            return "Clothes on. Photo, 10 min."
        return ledger.update_day(now.date(), _up)

    reason = words[1].strip() if len(words) > 1 else ""

    # Evidence is fetched before taking the lock (network calls).
    day = ledger.get_or_create_day(now.date())
    if _active_gym(day) is None:
        return None
    if not reason:
        return "Reason required: skip <reason>."
    slept = None if _is_sick(reason) else sleep_fn(day)
    free = None
    if not _is_sick(reason) and not (slept is not None and slept < LOW_SLEEP_HOURS):
        free = free_fn(now)

    def _skip(day):
        gym = _active_gym(day)
        if gym is None:
            return None
        seq = _seq(gym)
        if _is_sick(reason):
            gym["status"], gym["excuse"] = "excused", reason
            _log(day, "excused", now, reason=reason)
            return "Excused. Logged for the weekly review."
        if slept is not None and slept < LOW_SLEEP_HOURS and not seq["moved"] \
                and now.time() < _hm(EVENING_SLOT):
            gym["moved_from"], gym["slot"] = gym["slot"], EVENING_SLOT
            gym["excuse"] = reason
            seq.update(started=None, stage=None, stage_at=None, moved=True)
            _log(day, "moved", now, reason=reason, sleep=slept)
            leave = leave_by(day, gym, now)
            return f"Slept {slept:g}h. Moved to {EVENING_SLOT}. Leave by {leave:%H:%M}."
        if free is not None and free < FULL_DAY_FREE and not seq["short"]:
            gym["minutes"], gym["excuse"] = SHORT_MINUTES, reason
            seq["short"] = True
            _log(day, "short", now, reason=reason, free=free)
            return f"Longest free block is {free} min. {SHORT_MINUTES}-min version. Still going."
        _log(day, "rejected", now, reason=reason)
        leave = leave_by(day, gym, now)
        if now < leave:
            return f"Not a reason. Leave by {leave:%H:%M}."
        return "Not a reason. Go now."

    return ledger.update_day(now.date(), _skip)


def handle_photo(now=None):
    """A photo during the sequence counts as the clothes-on proof. Returns the reply or None."""
    now = now or ledger.now_local()

    def _photo(day):
        gym = _active_gym(day)
        if gym is None or _seq(gym)["stage"] not in ("up", "photo"):
            return None
        seq = _seq(gym)
        seq["stage"], seq["stage_at"] = "ready", _ts(now)
        _log(day, "photo", now)
        return f"Leave by {leave_by(day, gym, now):%H:%M}."

    return ledger.update_day(now.date(), _photo)

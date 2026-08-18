"""
Jarvis — Hevy Workout Integration
===================================
Fetches workout data from the Hevy API and produces a summary
for the morning brief.

What it tracks:
  - Did you train yesterday?
  - What exercises, sets, reps, and weights?
  - Any new personal bests?
  - Weekly training consistency (sessions this week)
  - Progressive overload — are key lifts trending up?

REQUIRES: Hevy Pro subscription + API key in config.py
  HEVY_API_KEY = "sk_live_..."

TEST: python hevy.py --test
"""

import json
import ssl
import certifi
import datetime
import argparse
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import pytz
import config

# ── SSL fix for Mac ───────────────────────────────────────────────────────────
_ssl_context = ssl.create_default_context(cafile=certifi.where())

def _urlopen_ssl(req):
    return urlopen(req, context=_ssl_context)

# ── Constants ─────────────────────────────────────────────────────────────────
API_BASE = "https://api.hevyapp.com/v1"
TIMEZONE = pytz.timezone(config.TIMEZONE)

# ── API helper ────────────────────────────────────────────────────────────────

def _get(endpoint, params=None):
    """GET request to Hevy API."""
    url = f"{API_BASE}/{endpoint}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)

    req = Request(url, headers={
        "api-key": config.HEVY_API_KEY,
        "Accept":  "application/json",
    })
    try:
        with _urlopen_ssl(req) as r:
            return json.loads(r.read())
    except HTTPError as e:
        print(f"⚠️   Hevy API error ({endpoint}): {e.code} — {e.read().decode()[:150]}")
        return None
    except URLError as e:
        print(f"⚠️   Hevy API error ({endpoint}): {e}")
        return None


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_recent_workouts(page=1, page_size=10):
    """
    Fetches recent workouts, most recent first.
    Returns a list of workout dicts, or None if the API call itself failed
    (rate limit, network error, etc). Callers must treat None as "unknown"
    — not the same as "confirmed zero workouts" — or a transient API
    failure gets silently misread as "nothing happened today".
    """
    data = _get("workouts", {"page": page, "pageSize": page_size})
    if data is None:
        return None
    return data.get("workouts", [])


def fetch_workout_count():
    """Returns total workout count and weekly/monthly stats."""
    data = _get("workouts/count")
    return data if data else {}


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_workout_date(workout):
    """
    Parses the start_time from a workout and returns a local datetime.
    Hevy returns ISO 8601 strings.
    """
    start_raw = workout.get("start_time", "")
    if not start_raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        return dt.astimezone(TIMEZONE)
    except Exception:
        return None


def format_workout_summary(workout):
    """
    Formats a single workout into a readable summary string.
    Shows workout title, duration, and key exercises with top sets.
    """
    title    = workout.get("title", "Workout")
    start_dt = parse_workout_date(workout)
    end_raw  = workout.get("end_time", "")

    # Duration
    duration_str = ""
    if start_dt and end_raw:
        try:
            end_dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(TIMEZONE)
            mins   = int((end_dt - start_dt).total_seconds() / 60)
            duration_str = f"{mins}min"
        except Exception:
            pass

    lines = [f"{title}" + (f" ({duration_str})" if duration_str else "")]

    exercises = workout.get("exercises", [])
    for ex in exercises[:8]:  # cap at 8 exercises
        ex_title = ex.get("title", "Unknown exercise")
        sets     = ex.get("sets", [])

        # Find the top set by weight
        weight_sets = [s for s in sets if s.get("weight_kg") and s.get("reps")]
        if weight_sets:
            top = max(weight_sets, key=lambda s: s["weight_kg"])
            weight_kg  = top["weight_kg"]
            reps       = top["reps"]
            set_count  = len([s for s in sets if s.get("weight_kg") and s.get("reps")])
            if set_count == 0:
                set_count = len(sets)
            lines.append(f"  {ex_title}: {set_count}x{reps} @ {weight_kg}kg")
        else:
            # Bodyweight or cardio
            set_count = len(sets)
            if set_count > 0:
                lines.append(f"  {ex_title}: {set_count} sets")

    return "\n".join(lines)


def find_pbs(workout):
    """
    Scans a workout for personal best markers.
    Hevy marks sets with is_personal_record = True.
    Returns list of (exercise_name, weight_kg, reps) tuples.
    """
    pbs = []
    for ex in workout.get("exercises", []):
        ex_title = ex.get("title", "Unknown")
        for s in ex.get("sets", []):
            if s.get("is_personal_record") is True:
                weight = s.get("weight_kg", 0)
                reps   = s.get("reps", 0)
                pbs.append((ex_title, weight, reps))
    return pbs


def get_weekly_sessions(workouts):
    """
    Counts training sessions in the current week (Mon–Sun).
    Returns (sessions_this_week, session_dates)
    """
    tz    = TIMEZONE
    today = datetime.datetime.now(tz).date()
    # Start of current week (Monday)
    week_start = today - datetime.timedelta(days=today.weekday())

    session_dates = []
    for w in workouts:
        dt = parse_workout_date(w)
        if dt and dt.date() >= week_start:
            session_dates.append(dt.date())

    return len(session_dates), session_dates


# ── PPLRUL helper ──────────────────────────────────────────────────────────────
# Schedule itself now lives in term_context.json (single source of truth,
# editable via proposals) instead of being hardcoded here.
try:
    from term_context import get_pplrul_day, VALID_SPLIT_LABELS as _VALID_SPLIT_LABELS
    PPLRUL = sorted(_VALID_SPLIT_LABELS)  # kept for backward-compat imports elsewhere
except Exception:
    PPLRUL = ["Push", "Pull", "Legs", "Rest", "Upper", "Sharms", "Rest"]
    def get_pplrul_day(date=None):
        return "Rest"


# ── Main summary ──────────────────────────────────────────────────────────────

def fetch_workout_data():
    """
    Fetches workout data and returns a formatted summary string
    for the morning brief.
    """
    if not hasattr(config, "HEVY_API_KEY") or not config.HEVY_API_KEY:
        return "WORKOUTS: Hevy API key not configured."

    workouts = fetch_recent_workouts(page_size=10)  # max 10 per page
    if workouts is None:
        return "WORKOUTS: Could not connect to Hevy API."
    if not workouts:
        return "WORKOUTS: No workouts found in Hevy."

    tz        = TIMEZONE
    today     = datetime.datetime.now(tz).date()
    yesterday = today - datetime.timedelta(days=1)

    today_split     = get_pplrul_day(today)
    yesterday_split = get_pplrul_day(yesterday)

    lines = []

    # ── Did they train yesterday? ─────────────────────────────────────────────
    yesterday_workouts = [
        w for w in workouts
        if parse_workout_date(w) and parse_workout_date(w).date() == yesterday
    ]

    is_rest_day = yesterday_split == "Rest"

    if is_rest_day:
        lines.append(f"YESTERDAY ({yesterday_split} day): Rest day — no training expected.")
    elif yesterday_workouts:
        lines.append(f"YESTERDAY ({yesterday_split} day): ✓ Trained")
        for w in yesterday_workouts:
            lines.append(format_workout_summary(w))
            pbs = find_pbs(w)
            if pbs:
                for ex, kg, reps in pbs:
                    lines.append(f"  🏆 NEW PB: {ex} — {kg}kg x {reps}")
    else:
        lines.append(f"YESTERDAY ({yesterday_split} day): ✗ No session logged — missed training day.")

    lines.append("")

    # ── Weekly consistency ────────────────────────────────────────────────────
    sessions_this_week, session_dates = get_weekly_sessions(workouts)

    # Expected sessions this week (non-rest days so far)
    today_weekday = today.weekday()  # 0=Mon
    week_start    = today - datetime.timedelta(days=today_weekday)
    expected = 0
    for i in range(today_weekday + 1):  # up to and including today
        d = week_start + datetime.timedelta(days=i)
        if get_pplrul_day(d) != "Rest":
            expected += 1

    lines.append(f"THIS WEEK: {sessions_this_week}/{expected} sessions completed")

    # Show which days were trained
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_display = []
    now_hour = datetime.datetime.now(tz).hour
    for i in range(today_weekday + 1):
        d        = week_start + datetime.timedelta(days=i)
        split    = get_pplrul_day(d)
        trained  = d in session_dates
        rest     = split == "Rest"
        is_today = (d == today)
        if rest:
            week_display.append(f"{day_names[i]}:Rest")
        elif trained:
            week_display.append(f"{day_names[i]}:✓")
        elif is_today:
            week_display.append(f"{day_names[i]}:pending")  # haven't trained yet today
        else:
            week_display.append(f"{day_names[i]}:✗")

    lines.append("  " + "  ".join(week_display))
    lines.append("")

    # ── Most recent workout (if not yesterday) ────────────────────────────────
    if not yesterday_workouts and workouts:
        most_recent    = workouts[0]
        most_recent_dt = parse_workout_date(most_recent)
        if most_recent_dt:
            days_ago = (today - most_recent_dt.date()).days
            lines.append(f"LAST WORKOUT: {days_ago} day(s) ago — {most_recent.get('title', 'Workout')}")

    # ── Today's plan ──────────────────────────────────────────────────────────
    lines.append(f"TODAY: {today_split} day")
    if today_split == "Rest":
        lines.append("  Rest day — recovery, mobility, or light activity only.")
    else:
        lines.append(f"  {today_split} session to complete today.")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Hevy Integration")
    parser.add_argument("--test", action="store_true", help="Print workout summary")
    args = parser.parse_args()

    print("\n🏋️   Fetching Hevy workout data...\n")
    print(fetch_workout_data())
    print()

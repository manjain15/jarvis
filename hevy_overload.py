"""
Jarvis — Hevy Progressive Overload Tracker
============================================
Analyses workout history to track whether key lifts are trending up,
stalling, or declining. Surfaces insights in the morning brief.

HOW IT WORKS:
  - Fetches last 10 pages of workouts from Hevy API
  - For each exercise, tracks the top set (max weight x reps) over time
  - Calculates trend: improving / stalling / declining
  - Feeds findings into Mem0 and morning brief

WHAT IT TRACKS:
  - Top set weight per exercise per session
  - Week-over-week and month-over-month trends
  - Personal bests and when they were set
  - Stalls (no progress in 3+ weeks on a lift)

CLI:
  python hevy_overload.py              -- print full analysis
  python hevy_overload.py --exercise "Bench Press"  -- single exercise
"""

import json
import datetime
import argparse
from pathlib import Path
from collections import defaultdict

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
TIMEZONE   = pytz.timezone(config.TIMEZONE)

# Exercises to track closely (your main compound + key isolation lifts)
# Add or remove based on what you actually train
KEY_EXERCISES = [
    "Hack Squat (Machine)",
    "Leg Extension (Machine)",
    "Lat Pulldown (Cable)",
    "Smith Machine Row",
    "smith t bar row",
    "Bench Press (Barbell)",
    "Incline Bench Press (Barbell)",
    "Overhead Press (Barbell)",
    "Seated Row (Cable)",
    "Romanian Deadlift (Barbell)",
    "Shoulder Press (Machine)",
    "Lateral Raise (Dumbbell)",
    "Bicep Curl (Barbell)",
    "Seated Incline Curl (Dumbbell)",
    "Hammer Curl (Cable)",
    "Tricep Pushdown (Cable)",
]

# How many weeks without progress = stall
STALL_WEEKS = 3


# ── Hevy API ──────────────────────────────────────────────────────────────────

def fetch_all_workouts(max_pages=10):
    """Fetches workout history from Hevy API. Returns list of workouts."""
    from hevy import _get, fetch_recent_workouts
    workouts = []
    for page in range(1, max_pages + 1):
        data = _get("workouts", {"page": page, "pageSize": 10})
        if not data or not data.get("workouts"):
            break
        workouts.extend(data["workouts"])
        # Stop if we've got all workouts
        if len(workouts) >= data.get("workout_count", 999):
            break
    return workouts


# ── Analysis ──────────────────────────────────────────────────────────────────

def parse_workout_date_local(workout):
    """Returns local date of workout."""
    start_raw = workout.get("start_time", "")
    if not start_raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        return dt.astimezone(TIMEZONE).date()
    except Exception:
        return None


def get_top_set(sets):
    """
    Returns the best set (highest weight, then most reps).
    Only counts normal sets (not warmup, dropset etc).
    """
    normal = [s for s in sets if s.get("weight_kg") and s.get("reps")]
    if not normal:
        return None
    return max(normal, key=lambda s: (s["weight_kg"], s["reps"]))


def build_exercise_history(workouts):
    """
    Returns dict: exercise_name -> sorted list of (date, weight_kg, reps, e1rm)
    e1rm = estimated 1 rep max using Epley formula: w * (1 + r/30)
    """
    history = defaultdict(list)

    for w in workouts:
        date = parse_workout_date_local(w)
        if not date:
            continue
        for ex in w.get("exercises", []):
            title   = ex.get("title", "").strip()
            top_set = get_top_set(ex.get("sets", []))
            if not top_set:
                continue
            weight = top_set["weight_kg"]
            reps   = top_set["reps"]
            e1rm   = round(weight * (1 + reps / 30), 1)  # Epley formula
            history[title].append((date, weight, reps, e1rm))

    # Sort each exercise by date ascending
    for title in history:
        history[title].sort(key=lambda x: x[0])

    return history


def analyse_exercise(title, sessions):
    """
    Analyses trend for a single exercise.
    Returns dict with trend info.
    """
    if len(sessions) < 2:
        return None

    tz    = TIMEZONE
    today = datetime.datetime.now(tz).date()

    # Recent sessions (last 8 weeks)
    cutoff  = today - datetime.timedelta(weeks=8)
    recent  = [(d, w, r, e) for d, w, r, e in sessions if d >= cutoff]

    if len(recent) < 2:
        return None

    # All-time best
    best_e1rm    = max(e for _, _, _, e in sessions)
    best_session = max(sessions, key=lambda x: x[3])

    # First and last recent sessions
    first_recent = recent[0]
    last_recent  = recent[-1]

    # Week-over-week change
    last_date    = last_recent[0]
    week_ago_cut = last_date - datetime.timedelta(weeks=1)
    prev_week    = [s for s in recent if s[0] <= week_ago_cut]
    wow_change   = None
    if prev_week:
        prev = prev_week[-1]
        wow_change = round(last_recent[3] - prev[3], 1)  # e1rm change

    # 4-week change
    four_weeks_cut = last_date - datetime.timedelta(weeks=4)
    four_weeks_ago = [s for s in recent if s[0] <= four_weeks_cut]
    monthly_change = None
    if four_weeks_ago:
        prev4          = four_weeks_ago[-1]
        monthly_change = round(last_recent[3] - prev4[3], 1)

    # Stall detection — no improvement in last N weeks
    stall_cut    = last_date - datetime.timedelta(weeks=STALL_WEEKS)
    stall_window = [s for s in recent if s[0] >= stall_cut]
    stalled      = False
    if len(stall_window) >= 2:
        e1rms_in_window = [e for _, _, _, e in stall_window]
        stalled = (max(e1rms_in_window) - min(e1rms_in_window)) < 1.0

    # Trend label
    if wow_change is not None:
        if wow_change > 2:
            trend = "improving"
        elif wow_change < -2:
            trend = "declining"
        elif stalled:
            trend = "stalling"
        else:
            trend = "stable"
    else:
        trend = "stable"

    return {
        "title":          title,
        "sessions":       len(sessions),
        "recent":         len(recent),
        "last_date":      last_recent[0],
        "last_weight":    last_recent[1],
        "last_reps":      last_recent[2],
        "last_e1rm":      last_recent[3],
        "best_e1rm":      best_e1rm,
        "best_date":      best_session[0],
        "wow_change":     wow_change,
        "monthly_change": monthly_change,
        "stalled":        stalled,
        "trend":          trend,
    }


# ── Summary for brief ──────────────────────────────────────────────────────────

def get_overload_summary():
    """
    Returns a formatted summary for the morning brief.
    Highlights stalls, PBs, and strong progress.
    """
    try:
        workouts = fetch_all_workouts(max_pages=10)
    except Exception as e:
        return f"PROGRESSIVE OVERLOAD: Could not fetch Hevy data — {e}"

    if not workouts:
        return "PROGRESSIVE OVERLOAD: No workout history found."

    history  = build_exercise_history(workouts)
    analyses = {}

    for title, sessions in history.items():
        result = analyse_exercise(title, sessions)
        if result:
            analyses[title] = result

    if not analyses:
        return "PROGRESSIVE OVERLOAD: Not enough data yet — train more consistently."

    lines = ["PROGRESSIVE OVERLOAD ANALYSIS:"]

    # Stalling lifts
    stalling = [a for a in analyses.values() if a["stalled"]]
    if stalling:
        lines.append(f"\n  ⚠️  STALLING ({len(stalling)} lift(s)):")
        for a in sorted(stalling, key=lambda x: x["last_e1rm"], reverse=True)[:4]:
            lines.append(
                f"  • {a['title']}: {a['last_weight']}kg × {a['last_reps']} "
                f"(e1RM {a['last_e1rm']}kg) — no progress in {STALL_WEEKS}+ weeks"
            )

    # Strong progress
    improving = [a for a in analyses.values() if a["trend"] == "improving"]
    if improving:
        lines.append(f"\n  ✅  IMPROVING ({len(improving)} lift(s)):")
        for a in sorted(improving, key=lambda x: x["wow_change"] or 0, reverse=True)[:4]:
            lines.append(
                f"  • {a['title']}: {a['last_weight']}kg × {a['last_reps']} "
                f"(+{a['wow_change']}kg e1RM this week)"
            )

    # Recent PBs (set in last 7 days)
    tz      = TIMEZONE
    today   = datetime.datetime.now(tz).date()
    pb_cut  = today - datetime.timedelta(days=7)
    new_pbs = [a for a in analyses.values() if a["best_date"] >= pb_cut]
    if new_pbs:
        lines.append(f"\n  🏆  NEW PBs THIS WEEK:")
        for a in new_pbs[:3]:
            lines.append(f"  • {a['title']}: {a['best_e1rm']}kg e1RM")

    # Key lift summary (compound movements)
    key     = ["Hack Squat", "Lat Pulldown", "Bench Press", "Overhead Press", "Romanian Deadlift"]
    tracked = [a for a in analyses.values() if any(k.lower() in a["title"].lower() for k in key)]
    if tracked:
        lines.append(f"\n  📊  KEY LIFTS (e1RM):")
        for a in sorted(tracked, key=lambda x: x["title"])[:6]:
            arrow = "↑" if a["trend"] == "improving" else "↓" if a["trend"] == "declining" else "→"
            lines.append(
                f"  {arrow} {a['title']}: {a['last_e1rm']}kg "
                f"(best: {a['best_e1rm']}kg)"
            )

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hevy Progressive Overload Tracker")
    parser.add_argument("--exercise", metavar="NAME", help="Analyse a specific exercise")
    parser.add_argument("--all",      action="store_true", help="Show all tracked exercises")
    parser.add_argument("--stalls",   action="store_true", help="Show only stalling lifts")
    args = parser.parse_args()

    print("\n🏋️   Fetching workout history...")
    workouts = fetch_all_workouts(max_pages=10)
    print(f"✅  {len(workouts)} workouts loaded")

    history  = build_exercise_history(workouts)
    analyses = {t: analyse_exercise(t, s) for t, s in history.items()}
    analyses = {t: a for t, a in analyses.items() if a}

    if args.exercise:
        # Find matching exercise (case-insensitive)
        matches = {t: a for t, a in analyses.items() if args.exercise.lower() in t.lower()}
        if not matches:
            print(f"❌  No exercise matching '{args.exercise}' found.")
            print(f"    Available: {', '.join(sorted(analyses.keys())[:10])}")
        else:
            for title, a in matches.items():
                print(f"\n📊  {title}")
                print(f"   Sessions tracked: {a['sessions']}")
                print(f"   Last session: {a['last_date']} — {a['last_weight']}kg × {a['last_reps']} (e1RM: {a['last_e1rm']}kg)")
                print(f"   All-time best: {a['best_e1rm']}kg e1RM on {a['best_date']}")
                print(f"   Week-over-week: {'+' if (a['wow_change'] or 0) > 0 else ''}{a['wow_change']}kg e1RM")
                print(f"   Monthly change: {'+' if (a['monthly_change'] or 0) > 0 else ''}{a['monthly_change']}kg e1RM")
                print(f"   Trend: {a['trend'].upper()}")
                if a["stalled"]:
                    print(f"   ⚠️  STALLING — no progress in {STALL_WEEKS}+ weeks")

    elif args.stalls:
        stalling = [a for a in analyses.values() if a["stalled"]]
        if not stalling:
            print("\n✅  No stalling lifts detected.")
        else:
            print(f"\n⚠️  {len(stalling)} stalling lift(s):\n")
            for a in sorted(stalling, key=lambda x: x["title"]):
                print(f"  • {a['title']}: {a['last_weight']}kg × {a['last_reps']} — stalled {STALL_WEEKS}+ weeks")

    elif args.all:
        print(f"\n📊  All exercises ({len(analyses)} tracked):\n")
        for title, a in sorted(analyses.items()):
            trend_icon = {"improving": "↑", "declining": "↓", "stalling": "⚠", "stable": "→"}.get(a["trend"], "?")
            print(f"  {trend_icon} {title}: {a['last_weight']}kg × {a['last_reps']} (e1RM {a['last_e1rm']}kg)")

    else:
        print()
        print(get_overload_summary())
        print()

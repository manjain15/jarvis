"""
Jarvis Evening Check-in (Telegram-driven)
=========================================
The check-in is a daily reflection across all life areas. Its two artifacts
drive the rest of Jarvis:
  - data/checkin_YYYY-MM-DD.json  → read by memory_system.py (nightly, 22:00)
  - data/summary_YYYY-MM-DD.txt   → read by morning_brief.py the next day

ARCHITECTURE (since the VPS migration):
  The terminal version is retired — systemd timers have no TTY. The flow now
  runs over Telegram: jarvis_telegram.py auto-starts it at 21:00 (or on /checkin),
  walks the question plan one message at a time, then calls the functions here to
  persist and summarise. This module owns the question plan, answer parsing, and
  artifact writing — it is the single source of truth for both.

  This file is import-only for the daemon. Run it directly to print the plan:
      python evening_checkin.py
"""

import json
import datetime
from pathlib import Path

import pytz
import anthropic
import config

# ── Setup ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
DATA_DIR     = SCRIPT_DIR / "data"
PROFILE_FILE = SCRIPT_DIR / "profile.md"

DATA_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)


def now_sydney():
    """Current time in the configured timezone."""
    return datetime.datetime.now(TIMEZONE)


# ── PPLRUL helper ─────────────────────────────────────────────────────────────
# PPLRUL cycle starts from a known anchor date — update ANCHOR_DATE and
# ANCHOR_DAY if you need to re-sync the cycle.

PPLRUL = ["Push", "Pull", "Legs", "Rest", "Upper", "Sharms", "Rest"]
ANCHOR_DATE = datetime.date(2026, 5, 13)
ANCHOR_DAY  = 2  # Legs on May 13 (Wednesday) — Push starts Monday


def get_pplrul_day(date=None):
    """Returns the PPLRUL label for the given date (default: today)."""
    if date is None:
        date = now_sydney().date()
    delta = (date - ANCHOR_DATE).days
    index = (ANCHOR_DAY + delta) % len(PPLRUL)
    return PPLRUL[index]


def get_tomorrow_pplrul():
    """Returns tomorrow's PPLRUL label."""
    tomorrow = now_sydney().date() + datetime.timedelta(days=1)
    return get_pplrul_day(tomorrow)


# ── Question plan (single source of truth) ─────────────────────────────────────
# Each step is a dict:
#   key       — the response-dict key (must stay stable: memory_system + the
#               summary prompt read these)
#   prompt    — the question text shown to Manav
#   type      — "yn" | "number" | "text"
#   min/max   — clamp bounds for number questions (optional)
#   followups — dict mapping a parsed answer (True/False) to a list of further
#               steps, spliced in only when that answer is given (optional)

def init_responses():
    """Returns the base response dict (the fields not asked as questions)."""
    today = now_sydney()
    return {
        "date": today.strftime("%Y-%m-%d"),
        "day_of_week": today.strftime("%A"),
        "pplrul_today": get_pplrul_day(),
        "pplrul_tomorrow": get_tomorrow_pplrul(),
    }


def build_checkin_steps(pplrul_today, pplrul_tomorrow):
    """
    Build the ordered list of check-in steps for the given PPLRUL context.
    The rest-day vs gym-day branch is decided here, at build time.
    """
    steps = []

    # ── Health & Fitness ──────────────────────────────────────────────────────
    if pplrul_today != "Rest":
        steps.append({
            "key": "workout_done",
            "prompt": f"Did you hit the gym today? ({pplrul_today} session)",
            "type": "yn",
            "followups": {
                True:  [{"key": "workout_notes",
                         "prompt": "Any notes on the session? (weight, reps, how it felt)",
                         "type": "text"}],
                False: [{"key": "workout_skip_reason",
                         "prompt": "What got in the way?",
                         "type": "text"}],
            },
        })
    else:
        steps.append({
            "key": "rest_day_active_recovery",
            "prompt": "Rest day today — did you do any active recovery or stretching?",
            "type": "yn",
        })

    steps.append({"key": "sleep_hours_last_night",
                  "prompt": "How many hours did you sleep last night?",
                  "type": "number", "min": 2, "max": 14})
    steps.append({"key": "sleep_target_tonight",
                  "prompt": f"What time are you aiming to be in bed tonight? "
                            f"(target: 7–8hrs before tomorrow's {pplrul_tomorrow} day)",
                  "type": "text"})
    steps.append({"key": "weight_today",
                  "prompt": "Did you weigh yourself today? If yes, what was it? (kg)",
                  "type": "number", "min": 40, "max": 150})
    steps.append({"key": "energy_level",
                  "prompt": "Energy level today — 1 (exhausted) to 10 (great)?",
                  "type": "number", "min": 1, "max": 10})

    # ── Career & Internship ───────────────────────────────────────────────────
    steps.append({"key": "application_sent",
                  "prompt": "Did you send any internship applications today?",
                  "type": "yn",
                  "followups": {True: [{"key": "application_details",
                                        "prompt": "Which company/role?", "type": "text"}]}})
    steps.append({"key": "google_mentor_contact",
                  "prompt": "Did you contact your Google mentor today or recently?",
                  "type": "yn",
                  "followups": {True: [{"key": "mentor_notes",
                                        "prompt": "What did you discuss or what was the outcome?",
                                        "type": "text"}]}})
    steps.append({"key": "career_work_done",
                  "prompt": "Did you do any career-building work today? "
                            "(LeetCode, portfolio, LinkedIn, networking)",
                  "type": "yn",
                  "followups": {True: [{"key": "career_work_details",
                                        "prompt": "What did you work on?", "type": "text"}]}})

    # ── Uni ───────────────────────────────────────────────────────────────────
    steps.append({"key": "uni_work_done",
                  "prompt": "Did you do any uni work today?",
                  "type": "yn",
                  "followups": {True: [{"key": "uni_work_details",
                                        "prompt": "What did you work on?", "type": "text"}]}})
    steps.append({"key": "upcoming_deadline",
                  "prompt": "Any uni deadlines coming up in the next 2 weeks you want Jarvis to track?",
                  "type": "text"})

    # ── Passion Project ───────────────────────────────────────────────────────
    steps.append({"key": "project_work_done",
                  "prompt": "Did you work on your passion project today?",
                  "type": "yn",
                  "followups": {
                      True:  [{"key": "project_details",
                               "prompt": "What did you work on or figure out?", "type": "text"}],
                      False: [{"key": "project_blocker",
                               "prompt": "What's blocking you or why didn't it happen?",
                               "type": "text"}],
                  }})

    # ── Finances ──────────────────────────────────────────────────────────────
    steps.append({"key": "pokemon_plan_progress",
                  "prompt": "Any progress on the Pokemon reselling plan today?",
                  "type": "yn",
                  "followups": {True: [{"key": "pokemon_plan_details",
                                        "prompt": "What did you figure out or decide?",
                                        "type": "text"}]}})
    steps.append({"key": "unusual_spending",
                  "prompt": "Any unusual spending today worth flagging?",
                  "type": "yn",
                  "followups": {True: [{"key": "spending_details",
                                        "prompt": "What was it?", "type": "text"}]}})

    # ── General ───────────────────────────────────────────────────────────────
    steps.append({"key": "day_rating",
                  "prompt": "Rate today overall — 1 (wrote-off) to 10 (crushed it)?",
                  "type": "number", "min": 1, "max": 10})
    steps.append({"key": "biggest_win",
                  "prompt": "What was the best thing that happened or that you did today?",
                  "type": "text"})
    steps.append({"key": "tomorrow_focus",
                  "prompt": "What's the ONE thing you most want to get done tomorrow?",
                  "type": "text"})
    steps.append({"key": "anything_else",
                  "prompt": "Anything else on your mind you want Jarvis to know or remember?",
                  "type": "text"})

    return steps


# Tokens that mean "skip this question" — kept small so they don't eat real answers.
_SKIP_TOKENS = {"skip", "-", ""}
_YES_TOKENS  = {"y", "yes", "1", "yep", "yeah", "yup"}
_NO_TOKENS   = {"n", "no", "0", "nope", "nah"}


def parse_answer(step, raw_text):
    """
    Convert a raw text reply into the typed value for `step`.
    Returns the parsed value, or None for a skip/blank/unparseable input
    (None preserves the old terminal behaviour: skipped → 'not provided').
    """
    text = (raw_text or "").strip()
    if text.lower() in _SKIP_TOKENS:
        return None

    kind = step["type"]
    if kind == "yn":
        low = text.lower()
        if low in _YES_TOKENS:
            return True
        if low in _NO_TOKENS:
            return False
        return None
    if kind == "number":
        try:
            val = float(text)
        except ValueError:
            return None
        if "min" in step and val < step["min"]:
            return step["min"]
        if "max" in step and val > step["max"]:
            return step["max"]
        return val
    return text  # free text


# ── Persistence ────────────────────────────────────────────────────────────────

def save_responses(responses):
    """Saves check-in responses to a dated JSON file. Returns the path."""
    path = DATA_DIR / f"checkin_{responses['date']}.json"
    with open(path, "w") as f:
        json.dump(responses, f, indent=2)
    return path


def save_summary(summary, date_str):
    """Saves the Claude summary to a dated text file. Returns the path."""
    path = DATA_DIR / f"summary_{date_str}.txt"
    with open(path, "w") as f:
        f.write(summary)
    return path


# ── Claude summary ──────────────────────────────────────────────────────────────

def generate_summary(responses):
    """
    Sends responses to Claude and returns a short summary + tomorrow's focus.
    This is what the morning brief reads the next day.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    profile_text = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else ""

    r = responses
    pplrul_today    = r.get("pplrul_today", "Unknown")
    pplrul_tomorrow = r.get("pplrul_tomorrow", "Unknown")

    def fmt(key, label):
        val = r.get(key)
        if val is None:
            return f"  {label}: not provided"
        if isinstance(val, bool):
            return f"  {label}: {'Yes' if val else 'No'}"
        return f"  {label}: {val}"

    checkin_text = f"""
DATE: {r.get('date')} ({r.get('day_of_week')})
PPLRUL today: {pplrul_today} | Tomorrow: {pplrul_tomorrow}

HEALTH & FITNESS:
{fmt('workout_done', 'Gym done')}
{fmt('workout_notes', 'Workout notes')}
{fmt('workout_skip_reason', 'Skip reason')}
{fmt('rest_day_active_recovery', 'Active recovery (rest day)')}
{fmt('sleep_hours_last_night', 'Sleep last night (hrs)')}
{fmt('sleep_target_tonight', 'Bed time target tonight')}
{fmt('weight_today', 'Weight today (kg)')}
{fmt('energy_level', 'Energy level (1-10)')}

CAREER & INTERNSHIP:
{fmt('application_sent', 'Application sent today')}
{fmt('application_details', 'Application details')}
{fmt('google_mentor_contact', 'Google mentor contact')}
{fmt('mentor_notes', 'Mentor notes')}
{fmt('career_work_done', 'Career building work')}
{fmt('career_work_details', 'Career work details')}

UNI:
{fmt('uni_work_done', 'Uni work done')}
{fmt('uni_work_details', 'Uni work details')}
{fmt('upcoming_deadline', 'Upcoming deadline flagged')}

PASSION PROJECT:
{fmt('project_work_done', 'Project work done')}
{fmt('project_details', 'Project details')}
{fmt('project_blocker', 'Blocker')}

FINANCES:
{fmt('pokemon_plan_progress', 'Pokemon plan progress')}
{fmt('pokemon_plan_details', 'Pokemon plan details')}
{fmt('unusual_spending', 'Unusual spending')}
{fmt('spending_details', 'Spending details')}

GENERAL:
{fmt('day_rating', 'Day rating (1-10)')}
{fmt('biggest_win', 'Biggest win today')}
{fmt('tomorrow_focus', 'Tomorrow focus')}
{fmt('anything_else', 'Anything else')}
"""

    prompt = f"""You are Jarvis — Manav's personal AI assistant. You know him well.

Here is his profile:
{profile_text}

Here is his evening check-in for today:
{checkin_text}

Write a SHORT end-of-day summary (max 200 words) that will be read by tomorrow morning's briefing.
Structure it as:

YESTERDAY SUMMARY:
[2-3 sentences covering what actually happened — workout, applications, project, energy. Be direct and factual.]

CARRY-FORWARD:
[Bullet points of anything unfinished, flagged, or that needs follow-up tomorrow. Max 5 bullets.]

TOMORROW CONTEXT:
[1-2 sentences setting up tomorrow — what Manav said he wants to focus on, what PPLRUL day it is, anything to be aware of.]

Be direct. No fluff. Write it as a handoff note from tonight-Jarvis to tomorrow-morning-Jarvis.
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


# ── CLI (plan dump only — terminal check-in is retired) ─────────────────────────

if __name__ == "__main__":
    today = now_sydney()
    steps = build_checkin_steps(get_pplrul_day(), get_tomorrow_pplrul())
    print("Terminal check-in is retired — the flow runs over Telegram "
          "(jarvis_telegram.py).")
    print(f"\nQuestion plan for {today:%A %d %b %Y} "
          f"(today={get_pplrul_day()}, tomorrow={get_tomorrow_pplrul()}):\n")
    n = 0
    for step in steps:
        n += 1
        print(f"  {n:2d}. [{step['type']:6}] {step['key']}: {step['prompt']}")
        for answer, followups in step.get("followups", {}).items():
            for f in followups:
                print(f"          ↳ if {answer}: [{f['type']:6}] {f['key']}: {f['prompt']}")

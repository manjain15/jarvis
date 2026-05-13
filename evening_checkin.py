"""
Jarvis Evening Check-in
=======================
Runs every evening at 9:30pm (via cron).
Asks Manav a series of questions across all four life areas,
saves the responses to a dated JSON log, and generates a
brief summary that the morning brief reads the next day.

HOW IT WORKS:
  1. Opens in terminal and asks questions one by one
  2. Saves all responses to data/checkin_YYYY-MM-DD.json
  3. Sends responses to Claude for a short reflection + tomorrow's focus
  4. Saves that summary to data/summary_YYYY-MM-DD.txt
  5. Morning brief automatically reads yesterday's summary if it exists

TO RUN MANUALLY:
  cd /Users/manavjain/jarvis
  source venv/bin/activate
  python evening_checkin.py

TO SCHEDULE (add to crontab -e):
  30 21 * * * cd /Users/manavjain/jarvis && venv/bin/python evening_checkin.py >> jarvis_evening.log 2>&1
"""

import os
import sys
import json
import datetime
import pytz
from pathlib import Path

import anthropic
import config

# ── Setup ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent
DATA_DIR    = SCRIPT_DIR / "data"
PROFILE_FILE = SCRIPT_DIR / "profile.md"

DATA_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_sydney():
    return datetime.datetime.now(TIMEZONE)

def ask(question, allow_skip=True):
    """
    Prints a question and returns the user's input.
    Empty input = skipped (returns None).
    """
    skip_hint = "  (press Enter to skip)" if allow_skip else ""
    print(f"\n  {question}{skip_hint}")
    print("  › ", end="", flush=True)
    try:
        response = input().strip()
        return response if response else None
    except (EOFError, KeyboardInterrupt):
        # Handle non-interactive / cron environments gracefully
        return None

def ask_yn(question):
    """
    Yes/no question. Returns True, False, or None (skipped).
    Accepts: y/yes/1/yep/yeah = True, n/no/0/nope/nah = False
    """
    print(f"\n  {question}  (y/n, or Enter to skip)")
    print("  › ", end="", flush=True)
    try:
        response = input().strip().lower()
        if response in ("y", "yes", "1", "yep", "yeah"):
            return True
        elif response in ("n", "no", "0", "nope", "nah"):
            return False
        return None
    except (EOFError, KeyboardInterrupt):
        return None

def ask_number(question, unit="", min_val=None, max_val=None):
    """
    Asks for a numeric input. Returns float or None.
    """
    print(f"\n  {question}  (press Enter to skip)")
    print("  › ", end="", flush=True)
    try:
        response = input().strip()
        if not response:
            return None
        val = float(response)
        if min_val is not None and val < min_val:
            return min_val
        if max_val is not None and val > max_val:
            return max_val
        return val
    except (ValueError, EOFError, KeyboardInterrupt):
        return None

def divider(title=""):
    width = 52
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n  {'─' * pad} {title} {'─' * pad}")
    else:
        print(f"\n  {'─' * width}")

def header():
    today = now_sydney()
    date_str = today.strftime("%A, %d %B %Y")
    print(f"""
╔══════════════════════════════════════════════════════╗
║           JARVIS  —  Evening Check-in               ║
║           {date_str:<42} ║
╚══════════════════════════════════════════════════════╝""")


# ── PPLRUL helper ─────────────────────────────────────────────────────────────
# PPLRUL = Push, Pull, Legs, Rest, Upper, Lower
# Cycle starts from a known anchor date — update ANCHOR_DATE and ANCHOR_DAY
# if you need to re-sync the cycle.

PPLRUL = ["Push", "Pull", "Legs", "Rest", "Upper", "Sharms", "Rest"]
ANCHOR_DATE = datetime.date(2026, 5, 13)
ANCHOR_DAY  = 2  # Legs on May 13 (Wednesday) — Push starts Monday

def get_pplrul_day(date=None):
    """Returns today's PPLRUL label."""
    if date is None:
        date = now_sydney().date()
    delta = (date - ANCHOR_DATE).days
    index = (ANCHOR_DAY + delta) % len(PPLRUL)
    return PPLRUL[index]

def get_tomorrow_pplrul():
    tomorrow = now_sydney().date() + datetime.timedelta(days=1)
    return get_pplrul_day(tomorrow)


# ── The check-in questions ────────────────────────────────────────────────────

def run_checkin():
    """
    Runs the full evening check-in across all four life areas.
    Returns a dict of all responses.
    """
    today      = now_sydney()
    today_str  = today.strftime("%A, %d %B %Y")
    pplrul_day = get_pplrul_day()
    tomorrow_split = get_tomorrow_pplrul()

    responses = {
        "date": today.strftime("%Y-%m-%d"),
        "day_of_week": today.strftime("%A"),
        "pplrul_today": pplrul_day,
        "pplrul_tomorrow": tomorrow_split,
    }

    header()

    print(f"""
  Good evening, Manav. Quick check-in before you wind down.
  Today was {pplrul_day} day. Tomorrow is {tomorrow_split}.
  Answer what you can — skip anything that doesn't apply.
""")

    # ── SECTION 1: Health & Fitness ───────────────────────────────────────────
    divider("HEALTH & FITNESS")

    if pplrul_day != "Rest":
        responses["workout_done"] = ask_yn(f"Did you hit the gym today? ({pplrul_day} session)")
        if responses["workout_done"]:
            responses["workout_notes"] = ask("Any notes on the session? (weight, reps, how it felt)")
        elif responses["workout_done"] is False:
            responses["workout_skip_reason"] = ask("What got in the way?")
    else:
        print("\n  Rest day — no gym today. Good.")
        responses["workout_done"] = None  # rest day, not applicable
        responses["rest_day_active_recovery"] = ask_yn("Did you do any active recovery or stretching?")

    responses["sleep_hours_last_night"] = ask_number(
        "How many hours did you sleep last night?", "hrs", min_val=2, max_val=14
    )

    responses["sleep_target_tonight"] = ask(
        f"What time are you aiming to be in bed tonight? (target: 7–8hrs before tomorrow's {tomorrow_split} day)"
    )

    responses["weight_today"] = ask_number(
        "Did you weigh yourself today? If yes, what was it? (kg)", "kg", min_val=40, max_val=150
    )

    responses["energy_level"] = ask_number(
        "Energy level today — 1 (exhausted) to 10 (great)?", "", min_val=1, max_val=10
    )

    # ── SECTION 2: Career & Internship ───────────────────────────────────────
    divider("CAREER & INTERNSHIP")

    responses["application_sent"] = ask_yn("Did you send any internship applications today?")
    if responses["application_sent"]:
        responses["application_details"] = ask("Which company/role?")

    responses["google_mentor_contact"] = ask_yn("Did you contact your Google mentor today or recently?")
    if responses["google_mentor_contact"]:
        responses["mentor_notes"] = ask("What did you discuss or what was the outcome?")

    responses["career_work_done"] = ask_yn(
        "Did you do any career-building work today? (LeetCode, portfolio, LinkedIn, networking)"
    )
    if responses["career_work_done"]:
        responses["career_work_details"] = ask("What did you work on?")

    # ── SECTION 3: Uni ────────────────────────────────────────────────────────
    divider("UNI")

    responses["uni_work_done"] = ask_yn("Did you do any uni work today?")
    if responses["uni_work_done"]:
        responses["uni_work_details"] = ask("What did you work on?")

    responses["upcoming_deadline"] = ask(
        "Any uni deadlines coming up in the next 2 weeks you want Jarvis to track?"
    )

    # ── SECTION 4: Passion Project ────────────────────────────────────────────
    divider("PASSION PROJECT")

    responses["project_work_done"] = ask_yn("Did you work on your passion project today?")
    if responses["project_work_done"]:
        responses["project_details"] = ask("What did you work on or figure out?")
    else:
        responses["project_blocker"] = ask("What's blocking you or why didn't it happen?")

    # ── SECTION 5: Finances ───────────────────────────────────────────────────
    divider("FINANCES")

    responses["pokemon_plan_progress"] = ask_yn(
        "Any progress on the Pokemon reselling plan today?"
    )
    if responses["pokemon_plan_progress"]:
        responses["pokemon_plan_details"] = ask("What did you figure out or decide?")

    responses["unusual_spending"] = ask_yn("Any unusual spending today worth flagging?")
    if responses["unusual_spending"]:
        responses["spending_details"] = ask("What was it?")

    # ── SECTION 6: General ────────────────────────────────────────────────────
    divider("GENERAL")

    responses["day_rating"] = ask_number(
        "Rate today overall — 1 (wrote-off) to 10 (crushed it)?", "", min_val=1, max_val=10
    )

    responses["biggest_win"] = ask("What was the best thing that happened or that you did today?")

    responses["tomorrow_focus"] = ask(
        "What's the ONE thing you most want to get done tomorrow?"
    )

    responses["anything_else"] = ask(
        "Anything else on your mind you want Jarvis to know or remember?"
    )

    return responses


# ── Save responses ────────────────────────────────────────────────────────────

def save_responses(responses):
    """Saves check-in responses to a dated JSON file."""
    date_str = responses["date"]
    path = DATA_DIR / f"checkin_{date_str}.json"
    with open(path, "w") as f:
        json.dump(responses, f, indent=2)
    return path


# ── Generate Claude summary ───────────────────────────────────────────────────

def generate_summary(responses):
    """
    Sends responses to Claude and gets a short summary + tomorrow's focus.
    This is what the morning brief reads the next day.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Load profile for context
    profile_text = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else ""

    # Format responses into readable text
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


def save_summary(summary, date_str):
    """Saves the Claude summary to a dated text file."""
    path = DATA_DIR / f"summary_{date_str}.txt"
    with open(path, "w") as f:
        f.write(summary)
    return path


# ── Print closing screen ──────────────────────────────────────────────────────

def print_closing(summary, responses):
    tomorrow_split = responses.get("pplrul_tomorrow", "")
    divider()
    print(f"""
  ✅  Check-in saved. Here's Jarvis's read on today:

{summary}

  Tomorrow is {tomorrow_split} day. Get to bed.
""")
    divider()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        # Run the check-in
        responses = run_checkin()

        print("\n\n  💾  Saving responses...")
        checkin_path = save_responses(responses)

        print("  🧠  Generating summary with Claude...")
        summary = generate_summary(responses)

        summary_path = save_summary(summary, responses["date"])

        print_closing(summary, responses)

        print(f"  Files saved:")
        print(f"    {checkin_path}")
        print(f"    {summary_path}\n")

    except KeyboardInterrupt:
        print("\n\n  Check-in cancelled. See you tomorrow.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

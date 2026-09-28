"""
Jarvis Morning Brief
====================
Runs every morning at 7am (via cron or Task Scheduler).
Pulls Gmail + Google Calendar, sends everything to Claude,
and emails you a personalised daily briefing.

HOW IT WORKS (plain English):
  1. Connects to your Gmail and Calendar using Google OAuth
  2. Fetches today's events and unread emails from the last 18 hours
  3. Loads your Jarvis profile (the "you" document)
  4. Sends it all to Claude with a prompt to write your daily brief
  5. Emails the brief to you via Gmail

FILES YOU NEED:
  - config.py          — your personal settings (edit this first)
  - credentials.json   — downloaded from Google Cloud Console
  - profile.md         — your Jarvis profile document

FIRST-TIME SETUP:
  1. Edit config.py with your details
  2. Follow SETUP_GUIDE.md to get credentials.json from Google
  3. Run: python morning_brief.py --setup   (authenticates with Google)
  4. Run: python morning_brief.py --test    (sends a test brief right now)
  5. Schedule it: python morning_brief.py --schedule  (sets up 7am daily)
"""
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


import os
import re
import sys
import json
import base64
import argparse
import datetime
import pytz
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Google API imports ────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Anthropic import ──────────────────────────────────────────────────────────
import anthropic

# ── Local config ─────────────────────────────────────────────────────────────
import config

# ── Google Health integration (optional) ─────────────────────────────────────
try:
    from google_health import fetch_health_data as fetch_fitbit_data
    FITBIT_AVAILABLE = True
except Exception:
    FITBIT_AVAILABLE = False

# ── Finance tracker (optional) ────────────────────────────────────────────────
try:
    from finance_tracker import get_finance_summary
    FINANCE_AVAILABLE = True
except Exception:
    FINANCE_AVAILABLE = False

# ── UNSW timetable (optional) ─────────────────────────────────────────────────
try:
    import uni_timetable
    TIMETABLE_AVAILABLE = True
except Exception:
    TIMETABLE_AVAILABLE = False

# ── Memory system (optional) ──────────────────────────────────────────────────
try:
    from jarvis_mem0 import load_memory_for_prompt as load_memory
    MEMORY_AVAILABLE = True
except Exception:
    try:
        from memory_system import load_memory
        MEMORY_AVAILABLE = True
    except Exception:
        MEMORY_AVAILABLE = False

# ── Weekly review (optional) ──────────────────────────────────────────────────
try:
    from weekly_review import run_weekly_review
    WEEKLY_REVIEW_AVAILABLE = True
except Exception:
    WEEKLY_REVIEW_AVAILABLE = False

# ── Job search (optional) ─────────────────────────────────────────────────────
try:
    from job_search import get_links_for_brief
    JOB_SEARCH_AVAILABLE = True
except Exception:
    JOB_SEARCH_AVAILABLE = False

# ── Hevy workout integration (optional) ──────────────────────────────────────
try:
    from hevy import fetch_workout_data
    HEVY_AVAILABLE = True
except Exception:
    HEVY_AVAILABLE = False

# ── Calendar & task management (optional) ────────────────────────────────────
try:
    from jarvis_calendar import get_tasks_summary, generate_daily_plan
    CALENDAR_WRITE = True
except Exception:
    CALENDAR_WRITE = False

# ── Pokemon reselling tracker (optional) ─────────────────────────────────────
try:
    from reselling_tracker import get_reselling_summary
    RESELLING_AVAILABLE = True
except Exception:
    RESELLING_AVAILABLE = False

# ── Hevy progressive overload (optional) ─────────────────────────────────────
try:
    from hevy_overload import get_overload_summary
    OVERLOAD_AVAILABLE = True
except Exception:
    OVERLOAD_AVAILABLE = False

# ── Term context integration (optional) ─────────────────────────────────────
try:
    from term_context import get_term_summary, get_flags, get_finance_goals
    TERM_CONTEXT_AVAILABLE = True
except Exception:
    TERM_CONTEXT_AVAILABLE = False
    def get_finance_goals():
        return {"savings_goal": 35000.00, "savings_deadline": "2027-01-01",
                "monthly_income": 2800.00, "monthly_budget": 300.00, "weekly_budget": 75.00}

try:
    from followups import maybe_draft_mentor_followup
    FOLLOWUPS_AVAILABLE = True
except Exception:
    FOLLOWUPS_AVAILABLE = False

# ── Course schedule — this week's topics from course outlines (optional) ──────
try:
    import course_schedule
    COURSE_SCHEDULE_AVAILABLE = True
except Exception:
    COURSE_SCHEDULE_AVAILABLE = False

# ── Study tracker — assessment ramp-up + revision alerts (optional) ───────────
try:
    import study_tracker
    STUDY_TRACKER_AVAILABLE = True
except Exception:
    STUDY_TRACKER_AVAILABLE = False

# Google OAuth scopes — these are the exact permissions we request
# Gmail: read emails + send the brief back to you
# Calendar: read your events
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts.create for mentor follow-ups
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",  # write: create/update events
    "https://www.googleapis.com/auth/tasks",             # Google Tasks read/write
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    # Note: Google Health scopes are in a SEPARATE token (health_token.json)
    # due to a Google API bug — mixing health + consumer scopes causes 403 errors
]

SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE  = SCRIPT_DIR / "token.json"       # saved after first login
CREDS_FILE  = SCRIPT_DIR / "credentials.json" # from Google Cloud Console
PROFILE_FILE = SCRIPT_DIR / "profile.md"      # your Jarvis profile doc
DATA_DIR     = SCRIPT_DIR / "data"             # evening check-in data


def load_last_checkin():
    """
    Loads last night's check-in summary if it exists.
    Looks for summary_YYYY-MM-DD.txt from yesterday.
    Returns the summary text, or None if not found.
    """
    if not DATA_DIR.exists():
        return None
    tz = pytz.timezone("Australia/Sydney")
    yesterday = (datetime.datetime.now(tz) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    summary_path = DATA_DIR / f"summary_{yesterday}.txt"
    if summary_path.exists():
        return summary_path.read_text()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — GOOGLE AUTHENTICATION
# ─────────────────────────────────────────────────────────────────────────────
# This handles logging in to Google. The first time you run --setup, it opens
# a browser window. After that, it silently refreshes your token automatically.

def get_google_credentials():
    """
    Returns valid Google credentials.
    - First run: opens browser for OAuth consent
    - Subsequent runs: loads saved token and refreshes if expired
    - If SCOPES grew (e.g. gmail.compose) beyond what the token grants,
      falls back to the token's existing scopes so cron never opens a browser.
      Re-run --setup to grant new scopes.
    """
    from google_auth import load_credentials

    if TOKEN_FILE.exists():
        creds = load_credentials(TOKEN_FILE, SCOPES)
        if creds.valid:
            return creds

    # No token or no refresh token — first-time browser login
    if not CREDS_FILE.exists():
        print("\n❌  credentials.json not found.")
        print("    Follow SETUP_GUIDE.md to download it from Google Cloud Console.\n")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Atomic write — other cron jobs read this same file concurrently
    tmp_file = TOKEN_FILE.parent / (TOKEN_FILE.name + ".tmp")
    tmp_file.write_text(creds.to_json())
    tmp_file.replace(TOKEN_FILE)
    print("✅  Google authentication saved.")

    return creds


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — FETCH TODAY'S CALENDAR EVENTS
# ─────────────────────────────────────────────────────────────────────────────
# Pulls all events from today. Returns a clean list of dicts with the
# essential info Claude needs to write a useful briefing.

def fetch_calendar_events(creds):
    """
    Fetches today's Google Calendar events.
    Returns a list of event dicts: title, time, location, description.
    """
    service = build("calendar", "v3", credentials=creds)

    # Define "today" in Sydney time
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # The API requires ISO format with timezone
    time_min = start_of_day.isoformat()
    time_max = end_of_day.isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,       # expand recurring events
        orderBy="startTime",     # sorted chronologically
        maxResults=20,
    ).execute()

    events = []
    for e in result.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        end   = e["end"].get("dateTime", e["end"].get("date", ""))

        # Format time nicely: "9:00 AM – 10:00 AM" or "All day"
        if "T" in start:
            start_dt = datetime.datetime.fromisoformat(start)
            end_dt   = datetime.datetime.fromisoformat(end)
            time_str = (
                start_dt.astimezone(tz).strftime("%-I:%M %p")
                + " – "
                + end_dt.astimezone(tz).strftime("%-I:%M %p")
            )
        else:
            time_str = "All day"

        events.append({
            "title":       e.get("summary", "Untitled event"),
            "time":        time_str,
            "location":    e.get("location", ""),
            "description": e.get("description", "")[:200],  # truncate long descriptions
        })

    # Merge in today's UNSW timetable classes (subscribed .ics feed). Dormant
    # and harmless if the feed URL is unset or the module is unavailable.
    if TIMETABLE_AVAILABLE:
        try:
            events.extend(uni_timetable.get_today_events_brief())
        except Exception:
            pass

    return events


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — FETCH RECENT EMAILS
# ─────────────────────────────────────────────────────────────────────────────
# Grabs unread emails from the last 18 hours. We extract sender, subject,
# and a snippet — enough for Claude to assess urgency without reading full bodies.

def fetch_emails(creds, hours_back=18, max_emails=15):
    """
    Fetches unread emails from the last `hours_back` hours.
    Returns a list of email dicts: sender, subject, snippet, date.
    """
    service = build("gmail", "v1", credentials=creds)

    # Build the Gmail search query
    # "is:unread" + "newer_than:1d" catches overnight emails
    query = f"is:unread newer_than:{hours_back}h -category:promotions -category:social"

    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_emails,
    ).execute()

    messages = result.get("messages", [])
    emails = []

    for msg in messages:
        # Fetch with retry + timeout to handle flaky network at 7am
        msg_data = None
        for attempt in range(3):
            try:
                req = service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                # httplib2 socket timeout (seconds)
                import socket
                old_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(10)
                try:
                    msg_data = req.execute()
                finally:
                    socket.setdefaulttimeout(old_timeout)
                break  # success, exit retry loop
            except Exception as e:
                if attempt == 2:
                    print(f"    ⚠️  Skipping message {msg['id']} after 3 attempts: {e}")
                else:
                    import time
                    time.sleep(2 ** attempt)  # 1s, 2s backoff

        if msg_data is None:
            continue

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        snippet = msg_data.get("snippet", "")

        sender_raw = headers.get("From", "Unknown")
        sender = sender_raw.split("<")[0].strip().strip('"')

        emails.append({
            "sender":  sender,
            "subject": headers.get("Subject", "(no subject)"),
            "snippet": snippet[:200],
            "date":    headers.get("Date", ""),
        })

    return emails

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — BUILD THE PROMPT FOR CLAUDE
# ─────────────────────────────────────────────────────────────────────────────
# This is the heart of Jarvis. We construct a detailed prompt that includes:
#   - Your profile document (who you are, your goals)
#   - Today's calendar data
#   - Your recent emails
#   - Exact instructions for how to write the brief

# Do not raise this. A brief that needs more tokens is too long; cap the
# prompt and salvage a truncated reply instead.
BRIEF_MAX_TOKENS = 2500

# Input pasted into the prompt. The model echoes what it is given, and the
# finance block is kept long enough that the savings lines are not the part
# that gets cut.
PROMPT_SECTION_CAPS = {
    "profile": 3200,
    "calendar": 900,
    "emails": 1400,
    "checkin": 700,
    "fitbit": 700,
    "finance": 2600,
    "hevy": 700,
    "memory": 1000,
    "jobs": 700,
    "overload": 700,
    "reselling": 800,
    "tasks": 700,
    "plan": 800,
    "term": 1200,
    "academic": 700,
    "flags": 700,
    "followup": 400,
}

_SHORT_BRIEF_SUFFIX = (
    "\n\nThe previous reply was cut off. Rewrite the COMPLETE brief in under "
    "280 words. At most 2 sentences per section. Omit profile updates. "
    "End with the mindset sentence. Finished HTML only."
)


def cap_section(text, limit):
    """
    Trim a prompt section to limit characters.

    Cuts on a line boundary when that still keeps most of the section, and
    marks the cut so the model does not treat the fragment as complete data.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0].rstrip()
    if len(cut) < limit // 2:
        cut = text[:limit].rstrip()
    return cut + "\n…(section shortened)"


def build_prompt(profile_text, events, emails, today_str, checkin_summary=None, fitbit_data=None, finance_data=None, hevy_data=None, memory_data=None, jobs_data=None, overload_data=None, pokemon_data=None, tasks_data=None, daily_plan=None, proposals_text=None, term_data=None, term_flags=None, course_topics=None, academic_alerts=None, followup_note=None):
    """
    Constructs the full prompt sent to Claude.
    Returns a string.
    """

    # Format calendar events as readable text
    if events:
        calendar_section = "\n".join([
            f"  • {e['time']}: {e['title']}"
            + (f" @ {e['location']}" if e["location"] else "")
            for e in events
        ])
    else:
        calendar_section = "  No events scheduled today."

    # Format emails as readable text. Cap count and snippet so triage stays short.
    if emails:
        shown = []
        for e in emails[:6]:
            snippet = (e.get("snippet") or "")[:140]
            shown.append(
                f"  • From: {e.get('sender', '')} | Subject: {e.get('subject', '')}\n"
                f"    Preview: {snippet}"
            )
        extra = len(emails) - len(shown)
        if extra > 0:
            shown.append(f"  • +{extra} more unread (not listed)")
        email_section = "\n".join(shown)
    else:
        email_section = "  No unread emails in the last 18 hours."

    # Format last night's check-in if available
    if checkin_summary:
        checkin_section = checkin_summary
    else:
        checkin_section = "  No check-in data from last night."

    if fitbit_data:
        fitbit_section = fitbit_data
    else:
        fitbit_section = "  Fitbit not connected or no data available."

    if finance_data:
        finance_section = finance_data
    else:
        finance_section = "  No finance data available."

    if hevy_data:
        hevy_section = hevy_data
    else:
        hevy_section = "  No Hevy workout data available."

    if memory_data:
        memory_section = memory_data
    else:
        memory_section = "  No memory data yet — memory system will build over time."

    # Append proposals to memory section if any exist
    if proposals_text:
        memory_section = memory_section + "\n\n" + proposals_text

    if jobs_data:
        jobs_section = jobs_data
    else:
        jobs_section = "  Job search not run today."

    if overload_data:
        overload_section = overload_data
    else:
        overload_section = "  No overload data available."

    if pokemon_data:
        pokemon_section = pokemon_data
    else:
        pokemon_section = "  Reselling tracker unavailable."

    tasks_section = tasks_data or "  Google Tasks not connected."
    plan_section  = daily_plan or "  Could not generate plan today."

    # ── Term / uni / internship context ──
    if term_data:
        lines = []
        if term_data.get("term_name"):
            lines.append(f"  Term: {term_data['term_name']} — Week {term_data.get('term_week', '?')}")
        if term_data.get("subjects"):
            lines.append(f"  Subjects: {', '.join(term_data['subjects'])}")

        assessments = term_data.get("assessments") or []
        if assessments:
            lines.append("  Upcoming assessments (next 21 days):")
            for a in assessments:
                weight = f" [{a['weight']}%]" if a.get("weight") else ""
                lines.append(f"    • {a['subject']} — {a['name']}{weight} → due {a['due']} ({a['days_left']}d)")
        else:
            lines.append("  No assessment due dates filled in yet (update term_context.json when Moodle releases them).")

        # This week's lecture/lab topics from the course outlines (course_schedule).
        if course_topics:
            for tline in course_topics.splitlines():
                lines.append(f"  {tline}")

        internships = term_data.get("internships") or []
        if internships:
            lines.append("  Internship pipeline:")
            for app in internships:
                lines.append(
                    f"    • {app.get('company')} ({app.get('role','')}) — {app.get('status','')} "
                    f"| next: {app.get('next_action','')}"
                )

        mentor = term_data.get("mentor") or {}
        if mentor:
            lines.append(
                f"  Mentor ({mentor.get('name','')}): last contact {mentor.get('last_contact','?')} — "
                f"{mentor.get('last_topic','')}"
                + (" [awaiting reply]" if mentor.get("awaiting_response") else "")
            )

        portfolio_targets = term_data.get("portfolio_targets") or []
        if portfolio_targets:
            lines.append("  Portfolio targets:")
            for p in portfolio_targets:
                lines.append(f"    • {p}")

        exch = term_data.get("exchange_target") or {}
        if exch.get("savings_goal"):
            lines.append(
                f"  US Exchange target: {exch['savings_deadline']} — "
                f"savings goal ${exch['savings_goal']:,.0f}"
            )

        term_section = "\n".join(lines) if lines else "  No term context loaded."
    else:
        term_section = "  Term context module not available."

    if term_flags:
        flags_section = "\n".join(f"  • {f}" for f in term_flags)
    else:
        flags_section = "  No urgent term/internship/mentor flags today."

    followup_section = followup_note if followup_note else "  No mentor follow-up draft created today."

    academic_section = academic_alerts if academic_alerts else "  No assessment or revision alerts right now."

    _fin_goals = get_finance_goals()
    _fin_deadline_str = datetime.date.fromisoformat(_fin_goals["savings_deadline"]).strftime("%B %Y")

    profile_text = cap_section(profile_text, PROMPT_SECTION_CAPS["profile"])
    calendar_section = cap_section(calendar_section, PROMPT_SECTION_CAPS["calendar"])
    email_section = cap_section(email_section, PROMPT_SECTION_CAPS["emails"])
    checkin_section = cap_section(checkin_section, PROMPT_SECTION_CAPS["checkin"])
    fitbit_section = cap_section(fitbit_section, PROMPT_SECTION_CAPS["fitbit"])
    finance_section = cap_section(finance_section, PROMPT_SECTION_CAPS["finance"])
    hevy_section = cap_section(hevy_section, PROMPT_SECTION_CAPS["hevy"])
    memory_section = cap_section(memory_section, PROMPT_SECTION_CAPS["memory"])
    jobs_section = cap_section(jobs_section, PROMPT_SECTION_CAPS["jobs"])
    overload_section = cap_section(overload_section, PROMPT_SECTION_CAPS["overload"])
    pokemon_section = cap_section(pokemon_section, PROMPT_SECTION_CAPS["reselling"])
    tasks_section = cap_section(tasks_section, PROMPT_SECTION_CAPS["tasks"])
    plan_section = cap_section(plan_section, PROMPT_SECTION_CAPS["plan"])
    term_section = cap_section(term_section, PROMPT_SECTION_CAPS["term"])
    academic_section = cap_section(academic_section, PROMPT_SECTION_CAPS["academic"])
    flags_section = cap_section(flags_section, PROMPT_SECTION_CAPS["flags"])
    followup_section = cap_section(followup_section, PROMPT_SECTION_CAPS["followup"])

    prompt = f"""You are Jarvis — a highly intelligent personal assistant who knows this person deeply.
You speak directly, concisely, and with genuine intelligence. No fluff. No filler.
You push them toward their goals. You're the voice in their ear that keeps them sharp.

Today is {today_str} (Sydney time).

────────────────────────────
THEIR PROFILE (everything you know about them):
────────────────────────────
{profile_text}

────────────────────────────
TODAY'S CALENDAR:
────────────────────────────
{calendar_section}

────────────────────────────
RECENT EMAILS (last 18 hours, unread):
────────────────────────────
{email_section}

────────────────────────────
LAST NIGHT'S CHECK-IN SUMMARY:
────────────────────────────
{checkin_section}

────────────────────────────
FITBIT HEALTH DATA (objective, from wearable):
────────────────────────────
{fitbit_section}

────────────────────────────
FINANCE DATA (from bank CSV):
────────────────────────────
{finance_section}

────────────────────────────
HEVY WORKOUT DATA:
────────────────────────────
{hevy_section}

────────────────────────────
JARVIS MEMORY (patterns and history):
────────────────────────────
{memory_section}

────────────────────────────
NEW JOB POSTINGS FOUND TODAY:
────────────────────────────
{jobs_section}

────────────────────────────
PROGRESSIVE OVERLOAD ANALYSIS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{overload_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POKEMON / RESELLING P&L:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{pokemon_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PENDING TASKS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tasks_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TODAY'S PLAN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{plan_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TERM / UNI / INTERNSHIP CONTEXT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{term_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STUDY & ASSESSMENT ALERTS (ramp-up + revision — act on these):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{academic_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TERM FLAGS (urgent nudges for today):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{flags_section}

MENTOR FOLLOW-UP DRAFT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{followup_section}

────────────────────────────
YOUR TASK — write their morning briefing:
────────────────────────────

Write a sharp morning briefing in clean HTML for email.
Return ONLY raw HTML. No markdown, no code fences. Start with the h2.

Hard limits — a cut-off brief is a failed brief:
- Under 320 words total.
- At most 2 sentences per section, or 4 short bullets for schedule and email.
- Use only numbers and facts from the sections above. Do not invent drafts, balances, or emails.
- Omit a section that has nothing to say (mentor, and profile updates, in particular).

<h2>Good morning. Here's your day.</h2>

<h3>📅 Today's schedule</h3>
At most 4 bullets. If the day is empty, one sentence.

<h3>📬 Email triage</h3>
At most 4 lines. One line each: who, what, and whether it needs action today. If nothing urgent, one sentence.

<h3>🎯 Your #1 priority today</h3>
One specific action, two sentences max. Rotate internship search, health, and the passion project across days.

<h3>📚 Uni check</h3>
Term and week. The single most urgent assessment inside 7 days (subject, name, days left, weight), or say nothing is due. Two sentences.

<h3>💼 Internship pulse</h3>
Name any stale or OA-waiting company from the flags, then one concrete action. Two sentences.

<h3>🤝 Mentor</h3>
One or two sentences. If a Gmail draft is waiting, give its subject and say it is in Drafts. Do not invent a draft. Omit the section if there is nothing to do.

<h3>💪 Health check</h3>
Sleep versus a 7–8h target, whether they trained, today's split, and one stalling lift if any. Two sentences.

<h3>💰 Finance flag</h3>
Weekly spend versus the ~${_fin_goals['weekly_budget']:.0f}/week budget (~${_fin_goals['monthly_budget']:.0f}/month), savings versus ${_fin_goals['savings_goal']:,.0f} by {_fin_deadline_str}, and reselling net if the data has it. Two sentences. On Sunday, one extra sentence: export the St. George CSVs (everyday, savings, investing) into jarvis/finance/ and run deploy/sync-finance-up.sh.

<h3>🔄 Profile & term updates</h3>
Only if proposals are pending. One line each, plus the command: python update_profile.py for profile, python term_updates.py for term context. If there are no proposals, omit this section entirely.

<h3>⚡ Today's mindset</h3>
One sentence, specific to this week, then stop.

Under 320 words. No corporate speak. No "Great news!" or "Here's a summary of...".
Just start. Be Jarvis."""

    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — CALL CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

def salvage_truncated_html(html):
    """
    Turn a max_tokens cutoff into HTML that still closes.

    Drops a trailing unfinished tag, trims a cut-off word, closes tags the
    brief opened, and appends one sentence so the email is not left mid-tag.
    """
    text = (html or "").strip()
    last_lt = text.rfind("<")
    last_gt = text.rfind(">")
    if last_lt > last_gt:
        text = text[:last_lt].rstrip()
    if text and text[-1].isalnum():
        boundary = max(text.rfind(". "), text.rfind("! "), text.rfind("? "), text.rfind("\n"))
        if boundary > len(text) * 0.5:
            text = text[:boundary + 1].rstrip()
        else:
            space = text.rfind(" ")
            if space > 0:
                text = text[:space].rstrip()

    tag_re = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?/?>", re.DOTALL)
    stack = []
    void = {"br", "hr", "img", "meta", "link"}
    for match in tag_re.finditer(text):
        raw = match.group(0)
        name = match.group(1).lower()
        if raw.startswith("</"):
            if name in stack:
                while stack and stack[-1] != name:
                    stack.pop()
                if stack and stack[-1] == name:
                    stack.pop()
            continue
        if raw.endswith("/>") or name in void:
            continue
        stack.append(name)

    closers = "".join(f"</{name}>" for name in reversed(stack))
    note = "<p><em>Brief shortened to fit. Sections above stop at the cutoff.</em></p>"
    return (text + closers + note).strip()


def generate_brief(prompt):
    """
    Send the prompt to Claude and return the HTML brief.

    Retries overload (529) up to 5 times. If stop_reason is max_tokens, retries
    once with a shorter instruction. A second cutoff is closed with
    salvage_truncated_html instead of raising max_tokens. Does not send email.
    """
    import time
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt_in_use = prompt
    retried_short = False

    for attempt in range(5):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=BRIEF_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt_in_use}],
            )
            body = message.content[0].text if message.content else ""
            if message.stop_reason == "max_tokens" and not retried_short and attempt < 4:
                print("    ⚠️  Brief hit max_tokens — retrying once, shorter")
                retried_short = True
                prompt_in_use = prompt + _SHORT_BRIEF_SUFFIX
                continue
            if message.stop_reason == "max_tokens":
                print("    ⚠️  Brief still truncated — sending a closed fallback")
                return salvage_truncated_html(body)
            return body
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 4:
                wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s
                print(f"    ⚠️  Claude overloaded, retrying in {wait}s (attempt {attempt + 1}/5)...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("brief generation failed")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — SEND THE BRIEF VIA EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_email(creds, brief_html, today_str):
    """
    Sends the Jarvis brief to your email address via Gmail API.
    The brief is HTML — renders nicely in any email client.
    """
    service = build("gmail", "v1", credentials=creds)

    # Wrap the brief HTML in a clean email template
    full_html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 15px;
      line-height: 1.6;
      color: #1e293b;
      max-width: 620px;
      margin: 0 auto;
      padding: 24px 20px;
      background: #f8fafc;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 28px 32px;
      border: 1px solid #e2e8f0;
    }}
    h2 {{
      font-size: 22px;
      font-weight: 600;
      color: #0f172a;
      margin: 0 0 20px;
      padding-bottom: 16px;
      border-bottom: 2px solid #1A56DB;
    }}
    h3 {{
      font-size: 15px;
      font-weight: 600;
      color: #1e293b;
      margin: 20px 0 8px;
    }}
    p, li {{ color: #334155; margin: 4px 0; }}
    ul {{ padding-left: 18px; }}
    .footer {{
      text-align: center;
      font-size: 12px;
      color: #94a3b8;
      margin-top: 20px;
    }}
  </style>
</head>
<body>
  <div class="card">
    {brief_html}
  </div>
  <div class="footer">
    Jarvis · {today_str} · Sydney
  </div>
</body>
</html>"""

    # Build the email message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Jarvis — {today_str}"
    msg["From"]    = config.YOUR_EMAIL
    msg["To"]      = config.YOUR_EMAIL
    msg.attach(MIMEText(full_html, "html"))

    # Gmail API requires base64-encoded raw message
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    print(f"✅  Brief sent to {config.YOUR_EMAIL}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — ties everything together
# ─────────────────────────────────────────────────────────────────────────────

def run_brief():
    """Main function — runs the full pipeline."""

    tz = pytz.timezone(config.TIMEZONE)
    today = datetime.datetime.now(tz)
    today_str = today.strftime("%A, %d %B %Y")

    # On Sundays, run the weekly review instead of the standard brief
    if today.weekday() == 6 and WEEKLY_REVIEW_AVAILABLE:
        print(f"\n📋  Sunday detected — running weekly review instead of morning brief")
        run_weekly_review()
        return

    print(f"\n🤖  Jarvis morning brief — {today_str}")
    print("    ─────────────────────────────────")

    # Load profile
    if not PROFILE_FILE.exists():
        print("⚠️   profile.md not found — running without profile context.")
        profile_text = "No profile loaded yet."
    else:
        profile_text = PROFILE_FILE.read_text()
        print("✅  Profile loaded")

    # Authenticate with Google
    print("🔐  Authenticating with Google...")
    creds = get_google_credentials()

    # Fetch data
    print("📅  Fetching calendar events...")
    events = fetch_calendar_events(creds)
    print(f"    Found {len(events)} event(s) today")

    print("📬  Fetching emails...")
    try:
        emails = fetch_emails(creds)
        print(f"    Found {len(emails)} unread email(s)")
    except Exception as e:
        print(f"    ⚠️  Email fetch failed: {e} — continuing without emails")
        emails = []

    # Load last night's check-in summary
    checkin_summary = load_last_checkin()
    if checkin_summary:
        print("📋  Last night's check-in loaded")
    else:
        print("📋  No check-in data from last night")

    # Fetch Fitbit health data
    fitbit_data = None
    if FITBIT_AVAILABLE:
        print("🏃  Fetching Fitbit data...")
        try:
            fitbit_data = fetch_fitbit_data()
            print("✅  Fitbit data loaded")
        except Exception as e:
            print(f"⚠️   Fitbit fetch failed: {e}")
    else:
        print("⚠️   Fitbit not configured — skipping")

    # Load job links (curated list — no API cost)
    jobs_data = None
    if JOB_SEARCH_AVAILABLE:
        try:
            from job_search import get_links_for_brief
            jobs_data = get_links_for_brief()
            print("🔍  Job links loaded")
        except Exception as e:
            print(f"⚠️   Job links failed: {e}")

    # Load pending profile proposals
    proposals_text = ""
    try:
        from update_profile import format_proposals_for_brief
        proposals_text = format_proposals_for_brief()
        if proposals_text:
            print("💡  Pending profile proposals loaded")
    except Exception:
        pass

    # Load pending term-context proposals (append to same proposals_text block)
    try:
        from term_updates import format_proposals_for_brief as format_term_proposals
        term_proposals_text = format_term_proposals()
        if term_proposals_text:
            proposals_text = (proposals_text + "\n\n" + term_proposals_text).strip()
            print("📚  Pending term-context proposals loaded")
    except Exception:
        pass

    # Load memory
    memory_data = None
    if MEMORY_AVAILABLE:
        try:
            memory_data = load_memory(days_back=14)
            print("🧠  Memory loaded")
        except Exception as e:
            print(f"⚠️   Memory load failed: {e}")

    # Fetch Hevy workout data
    hevy_data = None
    if HEVY_AVAILABLE and hasattr(config, "HEVY_API_KEY") and config.HEVY_API_KEY:
        print("🏋️   Fetching Hevy data...")
        try:
            hevy_data = fetch_workout_data()
            print("✅  Hevy data loaded")
        except Exception as e:
            print(f"⚠️   Hevy fetch failed: {e}")

    # Fetch finance data
    finance_data = None
    if FINANCE_AVAILABLE:
        try:
            finance_data = get_finance_summary()
            print("💰  Finance data loaded")
        except Exception as e:
            print(f"⚠️   Finance fetch failed: {e}")

    pokemon_data = None
    if RESELLING_AVAILABLE:
        try:
            pokemon_data = get_reselling_summary()
            print("🃏  Reselling data loaded")
        except Exception as e:
            print(f"⚠️   Reselling fetch failed: {e}")

    # Fetch progressive overload analysis
    overload_data = None
    if OVERLOAD_AVAILABLE:
        try:
            overload_data = get_overload_summary()
            print("📈  Overload analysis loaded")
        except Exception as e:
            print(f"⚠️   Overload analysis failed: {e}")

    # Fetch Google Tasks + generate today's time-blocked plan
    tasks_data = None
    daily_plan = None
    if CALENDAR_WRITE:
        try:
            tasks_data = get_tasks_summary()
            print("📝  Tasks loaded")
        except Exception as e:
            print(f"⚠️   Tasks fetch failed: {e}")
        try:
            daily_plan = generate_daily_plan(memory_text=memory_data or "")
            print("🗓   Daily plan generated")
        except Exception as e:
            print(f"⚠️   Daily plan failed: {e}")


    # Fetch term data
    course_topics = ""
    if COURSE_SCHEDULE_AVAILABLE:
        try:
            course_topics = course_schedule.get_current_week_summary()
        except Exception:
            course_topics = ""

    academic_alerts = ""
    if STUDY_TRACKER_AVAILABLE:
        try:
            academic_alerts = study_tracker.get_academic_alerts_block()
        except Exception:
            academic_alerts = ""

    term_data  = {}
    term_flags = []
    if TERM_CONTEXT_AVAILABLE:
        try:
            term_data  = get_term_summary()
            term_flags = get_flags()
        except Exception:
            pass

    # Mentor follow-up draft (Gmail draft only — never sent). Fail-soft.
    followup_note = None
    if FOLLOWUPS_AVAILABLE:
        try:
            info = maybe_draft_mentor_followup(creds)
            followup_note = info.get("brief_note")
            if info.get("drafted"):
                print(f"✉️   Mentor follow-up draft created: {info.get('subject')}")
            elif info.get("brief_note"):
                print(f"✉️   Mentor draft already waiting: {info.get('subject')}")
        except Exception as e:
            print(f"⚠️   Mentor follow-up draft skipped: {e}")

    # Build prompt and call Claude
    print("🧠  Generating brief with Claude...")
    prompt = build_prompt(profile_text, events, emails, today_str, checkin_summary, fitbit_data, finance_data, hevy_data, memory_data, jobs_data, overload_data, pokemon_data, tasks_data, daily_plan, proposals_text, term_data, term_flags, course_topics, academic_alerts, followup_note)
    brief  = generate_brief(prompt)
    print("✅  Brief generated")

    try:
        from agent_api import save_latest_brief
        save_latest_brief(brief, kind="morning", label=today_str)
        print("💾  Brief saved for the agent API")
    except Exception as e:
        print(f"⚠️   Could not save brief for the agent API: {e}")

    # Send email
    print("📤  Sending brief...")
    send_email(creds, brief, today_str)

    print("\n✅  Done. Check your inbox.\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI — handle command-line arguments
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Morning Brief")
    parser.add_argument("--setup",    action="store_true", help="Authenticate with Google (run this first)")
    parser.add_argument("--test",     action="store_true", help="Run the brief right now")
    parser.add_argument("--schedule", action="store_true", help="Print cron setup instructions")
    args = parser.parse_args()

    if args.setup:
        print("\n🔐  Starting Google authentication...")
        get_google_credentials()
        print("✅  Setup complete. Run --test to send your first brief.\n")

    elif args.schedule:
        print("""
┌─────────────────────────────────────────────────────┐
│  HOW TO SCHEDULE JARVIS AT 7AM DAILY                │
└─────────────────────────────────────────────────────┘

On Mac / Linux — add a cron job:

  1. Open terminal and run:
       crontab -e

  2. Add this line (adjust the path to where your files are):
       0 7 * * * cd /path/to/jarvis && python3 morning_brief.py >> jarvis.log 2>&1

  3. Save and close. Jarvis will now run at 7am every day.

On Windows — use Task Scheduler:

  1. Open Task Scheduler → Create Basic Task
  2. Name: "Jarvis Morning Brief"
  3. Trigger: Daily at 7:00 AM
  4. Action: Start a Program
       Program: python
       Arguments: C:\\path\\to\\jarvis\\morning_brief.py
  5. Save.

To verify it's running, check jarvis.log after 7am.
""")

    else:
        # Default: run the brief (also triggered by --test)
        run_brief()

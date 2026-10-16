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
    from jarvis_calendar import get_tasks_summary
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

# ── Daily ledger: today's commitments, debt, yesterday's evidence (optional) ──
try:
    import ledger
    LEDGER_AVAILABLE = True
except Exception:
    LEDGER_AVAILABLE = False

# ── Voice + email template (pure Python, no network) ─────────────────────────
from voice import VOICE_RULES, strip_emoji
from email_template import render_brief

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

# Claude writes only the note and the one push (JSON); everything else in the
# email is rendered from data. A short cap keeps it from drifting into a brief.
NOTE_MAX_TOKENS = 400

# Input pasted into the prompt. The model echoes what it is given, and the
# finance block is kept long enough that the savings-goal lines, including
# the fund breakdown, are not the part that gets cut.
PROMPT_SECTION_CAPS = {
    "profile": 3200,
    "calendar": 900,
    "emails": 1400,
    "checkin": 700,
    "fitbit": 700,
    "finance": 3000,
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


def build_prompt(profile_text, events, emails, today_str, checkin_summary=None, fitbit_data=None, finance_data=None, hevy_data=None, memory_data=None, jobs_data=None, overload_data=None, pokemon_data=None, tasks_data=None, daily_plan=None, proposals_text=None, term_data=None, term_flags=None, course_topics=None, academic_alerts=None, followup_note=None, areas_text=None):
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

    prompt = f"""{VOICE_RULES}

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

AREAS (days since last worked on; paused ones are never pushed):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{areas_text or "  Areas unavailable."}

────────────────────────────
YOUR TASK
────────────────────────────
The email around your text is built from the data above: today's timeline, what is
owed, deadlines and yesterday's numbers. Do not repeat those lists. Write two things.

"note": at most 2 sentences. The one thing about today the lists don't make obvious:
a deadline colliding with missed study, an email that needs action today, a stale
internship or mentor flag, spending off track (budget ~${_fin_goals['weekly_budget']:.0f}/week, savings
${_fin_goals['savings_goal']:,.0f} by {_fin_deadline_str}). If nothing stands out, one sentence on today's main study goal.

"push": 1 sentence. The next action of the area marked MOST NEGLECTED in AREAS, made
specific for today (today's plan has a 45-min block for it). Never push a paused area.
If a mentor draft is waiting in Gmail and networking isn't paused, you may name its subject instead.

The note and the push must be about different things. If the push covers the mentor
draft, the note covers something else.

Under 80 words in total. Return only JSON: {{"note": "...", "push": "..."}}"""

    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — CALL CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

def parse_note(text):
    """
    {"note", "push"} from Claude's reply. Anything unparseable gives empty
    strings, and the brief goes out without those two lines.
    """
    try:
        raw = text[text.index("{"):text.rindex("}") + 1]
        data = json.loads(raw)
    except Exception:
        return {"note": "", "push": ""}
    return {key: strip_emoji(str(data.get(key) or ""))[:400] for key in ("note", "push")}


def generate_note(prompt):
    """
    Ask Claude for the note and the one push. Retries overload (529) up to 5
    times. Any other failure returns empty strings so the brief still sends.
    """
    import time
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    for attempt in range(5):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=NOTE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            return parse_note(message.content[0].text if message.content else "")
        except Exception as e:
            overloaded = (
                isinstance(e, anthropic.APIStatusError)
                and e.status_code == 529
                and attempt < 4
            )
            if overloaded:
                wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s
                print(f"    ⚠️  Claude overloaded, retrying in {wait}s (attempt {attempt + 1}/5)...")
                time.sleep(wait)
                continue
            print(f"    ⚠️  Note generation failed: {e} — sending the brief without it")
            return {"note": "", "push": ""}
    return {"note": "", "push": ""}


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5b — BUILD THE EMAIL FROM DATA
# ─────────────────────────────────────────────────────────────────────────────
# Every number in the email comes from here, not from Claude. Each builder is a
# pure function over plain data so it can be tested without Google.

def _hm(dt):
    return dt.strftime("%H:%M")


def today_rows(day, calendar_items, classes, travel_minutes=5):
    """
    The Today timeline: gym, study blocks, weekly checks, calendar events and
    classes, in time order. calendar_items and classes are dicts with start
    (datetime or None for all-day), title and location. Jarvis's own study
    events must already be left out of calendar_items (the ledger has them).
    """
    rows = []
    for item in (day or {}).get("items", []):
        kind = item.get("kind")
        if kind == "gym":
            slot = datetime.datetime.strptime(item["slot"], "%H:%M")
            leave = (slot - datetime.timedelta(minutes=travel_minutes)).strftime("%H:%M")
            status = item.get("status")
            if status in ("done", "partial"):
                sub = f"Logged · {item.get('logged_minutes', item['minutes'])} min"
            elif status == "excused":
                sub = "Excused"
            elif status == "missed":
                sub = "Missed"
            else:
                sub = f"Leave by {leave}"
                if item.get("minutes") != item.get("base_minutes", item.get("minutes")):
                    sub += f" · {item['minutes']} min with debt"
                if item.get("moved_from"):
                    sub += f" · moved from {item['moved_from']}"
            rows.append((item["slot"], item["title"], sub, status == "missed"))
        elif kind == "study":
            start = datetime.datetime.fromisoformat(item["start"])
            rows.append((_hm(start), item["title"], f"{item['task']} · {item['minutes']} min", False))
        elif kind == "area":
            start = datetime.datetime.fromisoformat(item["start"])
            rows.append((_hm(start), item["title"], f"{item['task']} · {item['minutes']} min", False))
        elif kind == "check":
            ask = datetime.datetime.fromisoformat(item["ask_at"])
            rows.append((_hm(ask), item["label"], "Quick check", False))
    for e in list(calendar_items) + list(classes):
        start = e.get("start")
        rows.append(("All day" if start is None else _hm(start), e["title"], e.get("location") or None, False))
    rows.sort(key=lambda r: "" if r[0] == "All day" else r[0])
    return [{"time": t, "title": title, "sub": sub, "miss": miss} for t, title, sub, miss in rows]


def owed_rows(day, today):
    """The Owed section from the ledger's carried debt."""
    debt = (day or {}).get("debt") or {}
    rows = []
    makeup = debt.get("makeup")
    if makeup:
        due = datetime.date.fromisoformat(makeup["due"])
        rows.append({"label": f"Gym make-up · {makeup['split']} · by {due:%a}",
                     "value": f"{makeup['minutes']} min", "miss": due <= today + datetime.timedelta(days=1)})
    if debt.get("gym_minutes"):
        rows.append({"label": "Extra on every session until hit",
                     "value": f"+{debt['gym_minutes']} min", "miss": True})
    if debt.get("steps"):
        rows.append({"label": "Steps carried from yesterday", "value": f"+{debt['steps']:,}", "miss": False})
    return rows


def due_rows(upcoming, limit=4):
    """Due soon: the next few assessments, with days left. Within 3 days is red."""
    rows = []
    for a in upcoming[:limit]:
        due = datetime.date.fromisoformat(a["due"])
        days = a["days_left"]
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"{days}d"
        weight = f" ({a['weight']}%)" if a.get("weight") else ""
        rows.append({"label": f"{a['subject']} {a['name']}{weight}",
                     "value": f"{due:%a %-d %b} · {when}", "miss": days <= 3})
    return rows


def sync_yesterday_steps(yesterday, health_steps):
    """
    Write health_steps into yesterday's ledger so the brief and the ledger
    show the same total. When yesterday is already closed, step debt is
    recomputed and carried onto today. No ledger or no total: no change.
    Never raises.
    """
    if not LEDGER_AVAILABLE or not health_steps:
        return
    try:
        ledger.reconcile_steps(yesterday, health_count=health_steps, fetch=False)
    except Exception as e:
        print(f"⚠️   Couldn't reconcile yesterday's steps: {e}")


def yesterday_stats(prev_day, sleep_hours=None, health_steps=None, weight=None, weight_delta=None):
    """The four Yesterday cells: gym, steps, sleep, weight."""
    gym = ledger.find_item(prev_day, "gym") if (LEDGER_AVAILABLE and prev_day) else None
    if gym is None:
        gym_cell = {"value": "Rest", "label": "Gym", "miss": False}
    else:
        value = {"done": "Done", "partial": "Partial", "missed": "Missed",
                 "excused": "Excused"}.get(gym.get("status"), "—")
        gym_cell = {"value": value, "label": gym["title"], "miss": gym.get("status") == "missed"}

    steps_item = ledger.find_item(prev_day, "steps") if (LEDGER_AVAILABLE and prev_day) else None
    count = steps_item["count"] if steps_item and steps_item.get("count") else health_steps
    target = steps_item["target"] if steps_item else 10_000
    steps_cell = {"value": f"{count:,}" if count else "—", "label": "Steps",
                  "miss": bool(count) and count < target}

    sleep_cell = {"value": f"{sleep_hours:.1f}h" if sleep_hours else "—", "label": "Sleep",
                  "miss": bool(sleep_hours) and sleep_hours < 6}

    if weight:
        label = "kg" + (f" · {weight_delta:+.1f} wk" if weight_delta is not None else "")
        weight_cell = {"value": f"{weight:g}", "label": label, "miss": False}
    else:
        weight_cell = {"value": "—", "label": "Weight", "miss": False}
    return [gym_cell, steps_cell, sleep_cell, weight_cell]


def meta_line(week, split, class_count):
    """'Week 5 · Pull day · 2 classes'."""
    parts = []
    if week:
        parts.append(f"Week {week}")
    parts.append("Rest day" if split in (None, "Rest") else f"{split} day")
    if class_count:
        parts.append(f"{class_count} class{'es' if class_count != 1 else ''}")
    return " · ".join(parts)


def _ignore_words():
    try:
        from term_context import get_study_plan
        return [w.lower() for w in get_study_plan().get("ignore_busy", [])]
    except Exception:
        return []


def _calendar_items(creds, now):
    """Today's calendar events (not Jarvis's own blocks) as dicts with datetimes."""
    service = build("calendar", "v3", credentials=creds)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    result = service.events().list(calendarId="primary", timeMin=start.isoformat(),
                                   timeMax=end.isoformat(), singleEvents=True,
                                   orderBy="startTime", maxResults=30).execute()
    ignore = _ignore_words()
    items = []
    for e in result.get("items", []):
        if e.get("extendedProperties", {}).get("private", {}).get("jarvis"):
            continue
        title = e.get("summary", "Untitled event")
        if any(w in title.lower() for w in ignore):
            continue
        raw = e.get("start", {}).get("dateTime")
        items.append({"start": datetime.datetime.fromisoformat(raw).astimezone(now.tzinfo) if raw else None,
                      "title": title, "location": e.get("location", "")})
    return items


def _classes_today():
    """Today's timetable classes that Manav attends (ignore_busy titles left out)."""
    if not TIMETABLE_AVAILABLE:
        return []
    ignore = _ignore_words()
    try:
        return [{"start": c["start"], "title": c["title"], "location": c.get("location", "")}
                for c in uni_timetable.get_today_events()
                if c["time"] != "All day" and not any(w in c["title"].lower() for w in ignore)]
    except Exception:
        return []


def _health_yesterday(today_day, yesterday):
    """(sleep hours last night, Google Health steps yesterday). Either may be None."""
    sleep = (today_day or {}).get("sleep_hours")
    steps = None
    try:
        from google_health import fetch_sleep, fetch_steps, get_access_token
        token = get_access_token()
        if sleep is None:
            data = fetch_sleep(token, yesterday)
            if data and data.get("total_minutes"):
                sleep = data["total_minutes"] / 60
        data = fetch_steps(token, yesterday)
        steps = data["steps"] if data else None
    except Exception as e:
        print(f"⚠️   Google Health unavailable for the brief: {e}")
    return sleep, steps


def _weight(yesterday):
    """(weight from yesterday's check-in, change versus 7 days earlier)."""
    def read(date):
        try:
            return json.loads((DATA_DIR / f"checkin_{date.isoformat()}.json").read_text()).get("weight_today")
        except Exception:
            return None
    now_w = read(yesterday)
    then_w = read(yesterday - datetime.timedelta(days=7))
    delta = round(now_w - then_w, 1) if now_w and then_w else None
    return now_w, delta


def areas_context():
    """The AREAS prompt section: one line per area, most neglected marked. '' on failure."""
    try:
        import areas
        data = areas.summary(areas.refresh_evidence())
    except Exception as e:
        print(f"⚠️   Areas unavailable for the brief: {e}")
        return ""
    lines = []
    for a in data["areas"]:
        mark = "  ← MOST NEGLECTED" if a["name"] == data["most_neglected"] else ""
        state = f"PAUSED ({a['paused_reason'] or 'no reason'})" if a["paused"] else f"{a['days_since']}d since"
        lines.append(f"  • {a['label']}: {state}, {a['this_week']}/{a['target_per_week']} this week. "
                     f"Next: {a['next_action'] or '—'}{mark}")
    return "\n".join(lines)


def plan_text(day):
    """Today's ledger plan as prompt text, so the note can refer to it."""
    lines = []
    for r in today_rows(day, [], []):
        lines.append(f"  • {r['time']} {r['title']}" + (f" — {r['sub']}" if r["sub"] else ""))
    return "\n".join(lines) or "  No plan built today."


def collect_brief_data(creds, now, note):
    """The render_brief data dict for today."""
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    day = prev = None
    if LEDGER_AVAILABLE:
        try:
            day = ledger.get_or_create_day(today)
            prev = ledger.load_day(yesterday)
        except Exception as e:
            print(f"⚠️   Ledger unavailable for the brief: {e}")

    try:
        calendar_items = _calendar_items(creds, now)
    except Exception as e:
        print(f"⚠️   Calendar unavailable for the brief: {e}")
        calendar_items = []
    classes = _classes_today()

    upcoming, week = [], None
    if TERM_CONTEXT_AVAILABLE:
        try:
            import term_context
            ctx = term_context.load_context()
            upcoming = term_context.get_upcoming_assessments(ctx, days_ahead=14)
            week = term_context.current_term_week(ctx)
        except Exception:
            pass

    sleep, health_steps = _health_yesterday(day, yesterday)
    if prev is not None:
        sync_yesterday_steps(yesterday, health_steps)
        try:
            prev = ledger.load_day(yesterday)
            reloaded = ledger.load_day(today)
            if reloaded is not None:
                day = reloaded
        except Exception as e:
            print(f"⚠️   Ledger reload after step reconcile failed: {e}")
    weight, delta = _weight(yesterday)
    try:
        import enforcer
        travel = enforcer.TRAVEL_MINUTES
    except Exception:
        travel = 5

    return {
        "title": now.strftime("%A %-d %B"),
        "meta": meta_line(week, (day or {}).get("split"), len(classes)),
        "note": note.get("note"),
        "owed": owed_rows(day, today),
        "today": today_rows(day, calendar_items, classes, travel),
        "due": due_rows(upcoming),
        "yesterday": yesterday_stats(prev, sleep, health_steps, weight, delta),
        "push": note.get("push"),
        "footer": f"jarvis · {now:%H:%M}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — SEND THE BRIEF VIA EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_email(creds, html_body, text_body, subject):
    """Send the rendered brief (HTML + plain-text parts) to config.YOUR_EMAIL via the Gmail API."""
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.YOUR_EMAIL
    msg["To"]      = config.YOUR_EMAIL
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅  Brief sent to {config.YOUR_EMAIL}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN — ties everything together
# ─────────────────────────────────────────────────────────────────────────────

def run_brief(preview=None):
    """
    Main function — runs the full pipeline. preview is a file path: write the
    rendered email there instead of sending it (the weekly review still sends).
    """

    tz = pytz.timezone(config.TIMEZONE)
    today = datetime.datetime.now(tz)
    today_str = today.strftime("%A, %d %B %Y")

    # On Sundays, run the weekly review instead of the standard brief
    if today.weekday() == 6 and WEEKLY_REVIEW_AVAILABLE:
        print(f"\n📋  Sunday detected — running weekly review instead of morning brief")
        run_weekly_review(preview=preview)
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

    # Fetch Google Tasks. Today's plan comes from the ledger (day_plan.py builds
    # it at 05:30), so no separate Claude call generates one here.
    tasks_data = None
    if CALENDAR_WRITE:
        try:
            tasks_data = get_tasks_summary()
            print("📝  Tasks loaded")
        except Exception as e:
            print(f"⚠️   Tasks fetch failed: {e}")
    daily_plan = None
    if LEDGER_AVAILABLE:
        try:
            daily_plan = plan_text(ledger.load_day(today.date()))
        except Exception as e:
            print(f"⚠️   Ledger plan unavailable: {e}")


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

    # Claude writes the note and the one push; the rest is rendered from data.
    print("🧠  Writing the note with Claude...")
    prompt = build_prompt(profile_text, events, emails, today_str, checkin_summary, fitbit_data, finance_data, hevy_data, memory_data, jobs_data, overload_data, pokemon_data, tasks_data, daily_plan, proposals_text, term_data, term_flags, course_topics, academic_alerts, followup_note, areas_text=areas_context())
    note = generate_note(prompt)

    data = collect_brief_data(creds, today, note)
    if proposals_text:
        data["footer"] += " · proposals pending: /proposals"
    html_body, text_body = render_brief(data)
    print("✅  Brief rendered")

    if preview:
        Path(preview).write_text(html_body)
        print(f"\n👀  Preview written to {preview} (not sent, not saved).\n")
        return

    try:
        from agent_api import save_latest_brief
        save_latest_brief(html_body, kind="morning", label=today_str)
        print("💾  Brief saved for the agent API")
    except Exception as e:
        print(f"⚠️   Could not save brief for the agent API: {e}")

    print("📤  Sending brief...")
    send_email(creds, html_body, text_body, f"{today:%a %-d %b} · jarvis")

    print("\n✅  Done. Check your inbox.\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI — handle command-line arguments
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Morning Brief")
    parser.add_argument("--setup",    action="store_true", help="Authenticate with Google (run this first)")
    parser.add_argument("--test",     action="store_true", help="Run the brief right now")
    parser.add_argument("--schedule", action="store_true", help="Print cron setup instructions")
    parser.add_argument("--preview",  metavar="FILE", help="Write the email to FILE instead of sending it")
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
        run_brief(preview=args.preview)

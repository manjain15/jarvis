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

import os
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
    from pokemon_tracker import get_pokemon_summary
    POKEMON_AVAILABLE = True
except Exception:
    POKEMON_AVAILABLE = False

# ── Hevy progressive overload (optional) ─────────────────────────────────────
try:
    from hevy_overload import get_overload_summary
    OVERLOAD_AVAILABLE = True
except Exception:
    OVERLOAD_AVAILABLE = False

# Google OAuth scopes — these are the exact permissions we request
# Gmail: read emails + send the brief back to you
# Calendar: read your events
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",  # write: create/update events
    "https://www.googleapis.com/auth/tasks",             # Google Tasks read/write
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
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # If no valid credentials, kick off the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired — silently refresh it
            creds.refresh(Request())
        else:
            # First time — open browser for login
            if not CREDS_FILE.exists():
                print("\n❌  credentials.json not found.")
                print("    Follow SETUP_GUIDE.md to download it from Google Cloud Console.\n")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the token so we don't need to log in again
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
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
        # Fetch full message headers (not body — keeps it fast)
        msg_data = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        snippet = msg_data.get("snippet", "")

        # Clean up the From field: "John Smith <john@example.com>" → "John Smith"
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

def build_prompt(profile_text, events, emails, today_str, checkin_summary=None, fitbit_data=None, finance_data=None, hevy_data=None, memory_data=None, jobs_data=None, overload_data=None, pokemon_data=None, tasks_data=None, daily_plan=None):
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

    # Format emails as readable text
    if emails:
        email_section = "\n".join([
            f"  • From: {e['sender']} | Subject: {e['subject']}\n    Preview: {e['snippet']}"
            for e in emails
        ])
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
        pokemon_section = "  No inventory file found — copy Excel to jarvis/pokemon/inventory.xlsx"

    tasks_section = tasks_data or "  Google Tasks not connected."
    plan_section  = daily_plan or "  Could not generate plan today." 

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

────────────────────────────
YOUR TASK — write their morning briefing:
────────────────────────────

Write a sharp, personal morning briefing in clean HTML (for email).
Return ONLY raw HTML — no markdown, no code fences, no ```html wrapper. Start directly with the HTML.
Structure it exactly like this — use the section headers, keep each section tight:

<h2>Good morning. Here's your day.</h2>

<h3>📅 Today's schedule</h3>
[List their events. If there are none, say something motivating about having a clear day.]

<h3>📬 Email triage</h3>
[For each email: one line — who it's from, what it's about, and whether it needs action today.
Flag urgency clearly. If nothing urgent, say so.]

<h3>🎯 Your #1 priority today</h3>
[Based on their goals and what's on their plate, what is the SINGLE most important thing
they should do today? Be specific — not "work on your career" but something actionable.
Rotate focus across: internship search, health, and their passion project.]

<h3>💼 Internship pulse & new roles</h3>
[A direct, honest check-in on their internship goal. Have they applied recently?
What should they do today — even one small step? Be direct, not gentle.]

<h3>💪 Health check</h3>
[Use FITBIT DATA for sleep/HR/steps AND HEVY DATA for workout tracking AND PROGRESSIVE OVERLOAD data. State actual sleep vs 7-8hr target, resting HR. Confirm if they trained yesterday, any PBs. State today's split. If any lifts are stalling, flag the most important one. 5-6 lines max, direct and specific.]

<h3>💰 Finance flag</h3>
[Use the FINANCE DATA — be specific with real numbers. Call out: unusual transactions over $50, any category spending that seems high vs a ~$75/week budget (~$300/month total spending), savings progress vs $35k Jan 2027 goal, and the Pokemon reselling plan (keep asking until it exists). 3-4 lines max, direct. ALSO: if today is Sunday, remind Manav to export his St. George CSV (everyday + 2 savings accounts) and drop them in the jarvis/finance/ folder to keep finance tracking accurate for the week ahead.]

<h3>🔄 Profile updates</h3>
[ONLY include this section if there are pending profile update proposals in the data. List each proposal concisely — section, what would change, and why. Tell Manav to run 'python update_profile.py' to approve. If no proposals, omit this section entirely.]

<h3>⚡ Today's mindset</h3>
[ONE sentence. Sharp. Motivating. Personalised to where they are right now.
Not generic. Make it land.]

Keep the total brief under 400 words. Write like you know them well — because you do.
No corporate speak. No "Great news!" or "Here's a summary of...".
Just start. Be Jarvis."""

    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — CALL CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

def generate_brief(prompt):
    """
    Sends the prompt to Claude and returns the HTML brief as a string.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


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
    emails = fetch_emails(creds)
    print(f"    Found {len(emails)} unread email(s)")

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

    # Build prompt and call Claude
    print("🧠  Generating brief with Claude...")
    prompt = build_prompt(profile_text, events, emails, today_str, checkin_summary, fitbit_data, finance_data, hevy_data, memory_data, jobs_data, overload_data, pokemon_data, tasks_data, daily_plan)
    brief  = generate_brief(prompt)
    print("✅  Brief generated")

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

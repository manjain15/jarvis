"""
Jarvis — Calendar & Task Management
=====================================
Gives Jarvis the ability to:
  - Create, update, and delete Google Calendar events
  - Read and write Google Tasks
  - Generate a smart daily plan based on sleep, calendar, and priorities
  - Surface pending tasks in the morning brief

USAGE (voice via wake.py):
  "Hey Jarvis, block 9-11am tomorrow for deep work"
  "Add a task to follow up with my Google mentor"
  "What's on my calendar today?"
  "Plan my day"

USAGE (CLI):
  python jarvis_calendar.py --plan          # generate today's plan
  python jarvis_calendar.py --tasks         # show pending tasks
  python jarvis_calendar.py --add-task "Follow up with Google mentor"
  python jarvis_calendar.py --block "9am-11am tomorrow" "Deep work - internship apps"
"""

import datetime
import argparse
from pathlib import Path

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
TIMEZONE   = pytz.timezone(config.TIMEZONE)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token_file = SCRIPT_DIR / "token.json"
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tmp_file = token_file.parent / (token_file.name + ".tmp")
        tmp_file.write_text(creds.to_json())
        tmp_file.replace(token_file)
    return creds


def get_calendar_service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=get_credentials())


def get_tasks_service():
    from googleapiclient.discovery import build
    return build("tasks", "v1", credentials=get_credentials())


# ── Calendar reads ────────────────────────────────────────────────────────────

def get_today_events():
    """Returns today's calendar events."""
    tz      = TIMEZONE
    now     = datetime.datetime.now(tz)
    today   = now.date()
    start   = datetime.datetime.combine(today, datetime.time.min).astimezone(tz)
    end     = datetime.datetime.combine(today, datetime.time.max).astimezone(tz)

    service = get_calendar_service()
    result  = service.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for e in result.get("items", []):
        start_raw = e.get("start", {})
        end_raw   = e.get("end", {})
        if "dateTime" in start_raw:
            start_dt = datetime.datetime.fromisoformat(start_raw["dateTime"])
            end_dt   = datetime.datetime.fromisoformat(end_raw["dateTime"])
            start_str = start_dt.astimezone(tz).strftime("%-I:%M %p")
            end_str   = end_dt.astimezone(tz).strftime("%-I:%M %p")
            time_str  = f"{start_str}–{end_str}"
        else:
            time_str  = "All day"
            start_dt  = datetime.datetime.combine(today, datetime.time.min).astimezone(tz)

        events.append({
            "id":      e.get("id"),
            "title":   e.get("summary", "Untitled"),
            "time":    time_str,
            "start":   start_dt,
            "location": e.get("location", ""),
        })

    return sorted(events, key=lambda x: x["start"])


def get_free_blocks(min_duration_mins=45):
    """
    Returns free time blocks today (gaps between events).
    Only returns blocks during useful hours (7am-10pm).
    """
    tz     = TIMEZONE
    today  = datetime.datetime.now(tz).date()
    events = get_today_events()

    work_start = datetime.datetime.combine(today, datetime.time(7,  0)).astimezone(tz)
    work_end   = datetime.datetime.combine(today, datetime.time(22, 0)).astimezone(tz)

    # Build list of busy periods
    busy = []
    for e in events:
        if e["time"] != "All day":
            busy.append(e["start"])

    # Add today's UNSW classes so deep-work/gym aren't suggested during lectures.
    # Optional: silently skipped if the timetable module/feed isn't available.
    try:
        import uni_timetable
        for class_start, _class_end in uni_timetable.get_busy_blocks():
            busy.append(class_start)
    except Exception:
        pass

    # Simple gap finder
    busy_sorted = sorted(busy)
    free_blocks = []
    cursor      = work_start

    for event_start in busy_sorted:
        if event_start > cursor:
            gap_mins = (event_start - cursor).seconds // 60
            if gap_mins >= min_duration_mins:
                free_blocks.append({
                    "start":    cursor,
                    "end":      event_start,
                    "duration": gap_mins,
                    "label":    f"{cursor.strftime('%-I:%M %p')}–{event_start.strftime('%-I:%M %p')} ({gap_mins}m free)",
                })
        if event_start > cursor:
            cursor = event_start

    # Final block
    if cursor < work_end:
        gap_mins = (work_end - cursor).seconds // 60
        if gap_mins >= min_duration_mins:
            free_blocks.append({
                "start":    cursor,
                "end":      work_end,
                "duration": gap_mins,
                "label":    f"{cursor.strftime('%-I:%M %p')}–{work_end.strftime('%-I:%M %p')} ({gap_mins}m free)",
            })

    return free_blocks


# ── Calendar writes ───────────────────────────────────────────────────────────

def create_event(title, start_dt, end_dt, description="", colour_id=None):
    """
    Creates a Google Calendar event.
    colour_id: 1=lavender 2=sage 3=grape 4=flamingo 5=banana 6=tangerine
               7=peacock 8=graphite 9=blueberry 10=basil 11=tomato
    """
    tz      = TIMEZONE
    service = get_calendar_service()

    event = {
        "summary":     title,
        "description": description,
        "start":       {"dateTime": start_dt.isoformat(), "timeZone": str(tz)},
        "end":         {"dateTime": end_dt.isoformat(),   "timeZone": str(tz)},
    }
    if colour_id:
        event["colorId"] = str(colour_id)

    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")


def block_time(title, start_dt, end_dt):
    """Creates a focus block on the calendar (graphite colour)."""
    return create_event(title, start_dt, end_dt, colour_id=8)


def delete_event(event_id):
    """Deletes a calendar event by ID."""
    service = get_calendar_service()
    service.events().delete(calendarId="primary", eventId=event_id).execute()


# ── Tasks ─────────────────────────────────────────────────────────────────────

def get_task_list_id():
    """Returns the ID of the first (default) task list."""
    service  = get_tasks_service()
    lists    = service.tasklists().list().execute()
    items    = lists.get("items", [])
    return items[0]["id"] if items else "@default"


def get_tasks(include_completed=False):
    """Returns pending tasks from Google Tasks."""
    service  = get_tasks_service()
    list_id  = get_task_list_id()
    result   = service.tasks().list(
        tasklist=list_id,
        showCompleted=include_completed,
        showHidden=False,
    ).execute()

    tasks = []
    for t in result.get("items", []):
        if t.get("status") == "completed" and not include_completed:
            continue
        due = None
        if t.get("due"):
            try:
                due = datetime.datetime.fromisoformat(t["due"].replace("Z", "+00:00")).date()
            except Exception:
                pass
        tasks.append({
            "id":    t["id"],
            "title": t.get("title", ""),
            "notes": t.get("notes", ""),
            "due":   due,
            "done":  t.get("status") == "completed",
        })

    return tasks


def add_task(title, notes="", due_date=None):
    """Adds a task to Google Tasks."""
    service = get_tasks_service()
    list_id = get_task_list_id()

    task = {"title": title, "notes": notes}
    if due_date:
        task["due"] = datetime.datetime.combine(
            due_date, datetime.time.min
        ).strftime("%Y-%m-%dT00:00:00.000Z")

    result = service.tasks().insert(tasklist=list_id, body=task).execute()
    return result.get("id")


def complete_task(task_id):
    """Marks a task as completed."""
    service = get_tasks_service()
    list_id = get_task_list_id()
    service.tasks().patch(
        tasklist=list_id,
        task=task_id,
        body={"status": "completed"}
    ).execute()


# ── Smart daily plan ──────────────────────────────────────────────────────────

def generate_daily_plan(sleep_hours=None, profile_text="", memory_text=""):
    """
    Generates a smart time-blocked day plan using Claude.
    Takes into account: calendar events, free blocks, sleep quality,
    current priorities, and tasks.
    """
    import anthropic

    tz    = TIMEZONE
    now   = datetime.datetime.now(tz)
    today = now.date()

    # Gather context
    events      = get_today_events()
    free_blocks = get_free_blocks(min_duration_mins=30)
    tasks       = get_tasks()

    events_str = "\n".join(
        f"  {e['time']}: {e['title']}" for e in events
    ) or "  No events"

    free_str = "\n".join(
        f"  {b['label']}" for b in free_blocks
    ) or "  No significant free blocks"

    tasks_str = "\n".join(
        f"  • {t['title']}" + (f" (due {t['due']})" if t['due'] else "")
        for t in tasks[:10]
    ) or "  No pending tasks"

    sleep_str = f"{sleep_hours:.1f} hours" if sleep_hours else "unknown"

    prompt = f"""You are Jarvis, Manav's personal AI assistant. Generate a smart, realistic time-blocked day plan for today.

TODAY: {now.strftime('%A, %d %B %Y')}
CURRENT TIME: {now.strftime('%-I:%M %p')}
SLEEP LAST NIGHT: {sleep_str}

CALENDAR COMMITMENTS:
{events_str}

FREE BLOCKS AVAILABLE:
{free_str}

PENDING TASKS:
{tasks_str}

MANAV'S PRIORITIES (from profile):
- Internship applications and career building (highest priority during holidays)
- Gym training (PPLRUL split)
- Pokemon reselling (sell existing inventory)
- Savings goal: $35k by Jan 2027
- Building Jarvis

CONTEXT:
{memory_text[:500] if memory_text else 'No recent context.'}

Generate a practical time-blocked plan for the rest of today. Be specific with times.
Use the free blocks wisely based on his energy level (sleep: {sleep_str}).
If sleep was poor (<6hrs), front-load admin tasks, save deep work for afternoon.
If sleep was good (7+hrs), put the hardest/most important work first.
Flag if any pending tasks are overdue or time-sensitive.
Keep it concise — this is read in the morning brief. Max 15 lines."""

    client  = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


def get_tasks_summary():
    """Returns a formatted task list for the morning brief."""
    try:
        tasks = get_tasks()
        if not tasks:
            return "PENDING TASKS:\n  No pending tasks."

        tz    = TIMEZONE
        today = datetime.datetime.now(tz).date()
        lines = [f"PENDING TASKS ({len(tasks)}):"]

        overdue  = [t for t in tasks if t["due"] and t["due"] < today]
        due_soon = [t for t in tasks if t["due"] and today <= t["due"] <= today + datetime.timedelta(days=3)]
        rest     = [t for t in tasks if not t["due"] or t["due"] > today + datetime.timedelta(days=3)]

        if overdue:
            lines.append("  OVERDUE:")
            for t in overdue:
                lines.append(f"  ⚠️  {t['title']} (was due {t['due']})")
        if due_soon:
            lines.append("  DUE SOON:")
            for t in due_soon:
                lines.append(f"  • {t['title']} (due {t['due']})")
        if rest:
            for t in rest[:5]:
                lines.append(f"  • {t['title']}")
            if len(rest) > 5:
                lines.append(f"  ... and {len(rest)-5} more")

        return "\n".join(lines)
    except Exception as e:
        return f"PENDING TASKS: Could not load — {e}"


# ── Natural language event creation (called from voice) ───────────────────────

def parse_and_create_event(natural_language_input):
    """
    Takes a natural language request and creates a calendar event.
    E.g. "block 9-11am tomorrow for deep work on internship apps"
    Returns a confirmation string.
    """
    import anthropic

    tz  = TIMEZONE
    now = datetime.datetime.now(tz)

    prompt = f"""Parse this calendar request and return ONLY a JSON object, nothing else.

Request: "{natural_language_input}"
Current time: {now.strftime('%A, %d %B %Y %-I:%M %p')} AEST

Return JSON with these exact fields:
{{
  "title": "event title",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM",
  "description": "optional description"
}}

Rules:
- "tomorrow" = {(now.date() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')}
- "today" = {now.date().strftime('%Y-%m-%d')}
- Use 24-hour format for times
- If no end time given, assume 1 hour duration"""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg    = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )

    import json, re
    text      = msg.content[0].text.strip()
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        return "Sorry, I couldn't parse that event request."

    data = json.loads(json_match.group())

    date      = datetime.date.fromisoformat(data["date"])
    start_t   = datetime.time.fromisoformat(data["start_time"])
    end_t     = datetime.time.fromisoformat(data["end_time"])
    start_dt  = datetime.datetime.combine(date, start_t).astimezone(tz)
    end_dt    = datetime.datetime.combine(date, end_t).astimezone(tz)

    link = create_event(
        title=data["title"],
        start_dt=start_dt,
        end_dt=end_dt,
        description=data.get("description", "Created by Jarvis"),
        colour_id=8,  # graphite for Jarvis-created events
    )

    return (
        f"Done. '{data['title']}' added to your calendar on "
        f"{date.strftime('%A %d %b')} from "
        f"{start_dt.strftime('%-I:%M %p')} to {end_dt.strftime('%-I:%M %p')}."
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Calendar & Task Manager")
    parser.add_argument("--plan",       action="store_true", help="Generate today's smart day plan")
    parser.add_argument("--tasks",      action="store_true", help="Show pending tasks")
    parser.add_argument("--add-task",   metavar="TITLE",     help="Add a task")
    parser.add_argument("--complete",   metavar="TASK_ID",   help="Mark task complete by ID")
    parser.add_argument("--events",     action="store_true", help="Show today's events")
    parser.add_argument("--block",      metavar="REQUEST",   help="Create event from natural language")
    args = parser.parse_args()

    if args.plan:
        print("\n📅  Generating smart day plan...\n")
        plan = generate_daily_plan()
        print(plan)
        print()

    elif args.tasks:
        print()
        print(get_tasks_summary())
        print()

    elif args.add_task:
        task_id = add_task(args.add_task)
        print(f"\n✅  Task added: '{args.add_task}' (ID: {task_id})\n")

    elif args.complete:
        complete_task(args.complete)
        print(f"\n✅  Task {args.complete} marked complete.\n")

    elif args.events:
        print(f"\n📅  Today's events:\n")
        for e in get_today_events():
            print(f"  {e['time']}: {e['title']}")
        print()

    elif args.block:
        print(f"\n📅  Creating event: '{args.block}'...")
        result = parse_and_create_event(args.block)
        print(f"✅  {result}\n")

    else:
        parser.print_help()

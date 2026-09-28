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
  python jarvis_calendar.py --sync-deadlines  # upsert assessment/fee deadlines from term_context
"""

import datetime
import argparse
import json
from pathlib import Path

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
TIMEZONE   = pytz.timezone(config.TIMEZONE)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
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
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except Exception:
        creds = Credentials.from_authorized_user_file(str(token_file))
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = Credentials.from_authorized_user_file(str(token_file))
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
    try:
        from term_context import get_finance_goals
        _fin_goals = get_finance_goals()
        _savings_line = f"- Savings goal: ${_fin_goals['savings_goal']:,.0f} by {datetime.date.fromisoformat(_fin_goals['savings_deadline']).strftime('%B %Y')}"
    except Exception:
        _savings_line = "- Savings goal: see finance tracker"

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
{_savings_line}
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



# ── Term deadline sync (assessments + fee/census) ─────────────────────────────
# Upserts Google Calendar all-day events tagged with a [Jarvis] title prefix.
# Event IDs live in data/calendar_sync.json so re-runs update rather than duplicate.
# Only syncs dates that already exist in term_context — never invents fee/census dates.

TITLE_PREFIX = "[Jarvis]"
SYNC_FILE    = SCRIPT_DIR / "data" / "calendar_sync.json"
DAILY_FLAG   = SCRIPT_DIR / "data" / "calendar_sync_daily.json"


def _load_sync_store() -> dict:
    try:
        return json.loads(SYNC_FILE.read_text())
    except Exception:
        return {"events": {}}


def _save_sync_store(store: dict):
    SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    from json_store import atomic_write_json
    atomic_write_json(SYNC_FILE, store)


def _already_synced_today() -> bool:
    try:
        data = json.loads(DAILY_FLAG.read_text())
        today = datetime.datetime.now(TIMEZONE).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_synced_today():
    DAILY_FLAG.parent.mkdir(parents=True, exist_ok=True)
    from json_store import atomic_write_json
    today = datetime.datetime.now(TIMEZONE).date().isoformat()
    atomic_write_json(DAILY_FLAG, {"date": today})


def create_all_day_event(title, due_date, description="", colour_id=None):
    """
    Creates an all-day Google Calendar event on due_date (date or ISO str).
    Returns {"id": ..., "htmlLink": ...}.
    """
    if isinstance(due_date, str):
        due_date = datetime.date.fromisoformat(due_date)
    end_date = due_date + datetime.timedelta(days=1)

    service = get_calendar_service()
    event = {
        "summary":     title,
        "description": description,
        "start":       {"date": due_date.isoformat()},
        "end":         {"date": end_date.isoformat()},
    }
    if colour_id:
        event["colorId"] = str(colour_id)

    result = service.events().insert(calendarId="primary", body=event).execute()
    return {"id": result.get("id", ""), "htmlLink": result.get("htmlLink", "")}


def update_all_day_event(event_id, title, due_date, description="", colour_id=None):
    """Patches an existing all-day event. Returns {"id", "htmlLink"}."""
    if isinstance(due_date, str):
        due_date = datetime.date.fromisoformat(due_date)
    end_date = due_date + datetime.timedelta(days=1)

    service = get_calendar_service()
    body = {
        "summary":     title,
        "description": description,
        "start":       {"date": due_date.isoformat()},
        "end":         {"date": end_date.isoformat()},
    }
    if colour_id:
        body["colorId"] = str(colour_id)

    result = service.events().patch(
        calendarId="primary", eventId=event_id, body=body
    ).execute()
    return {"id": result.get("id", event_id), "htmlLink": result.get("htmlLink", "")}


def upsert_tagged_all_day_event(sync_key, title, due_date, description="", colour_id=11):
    """
    Create or update a [Jarvis]-tagged all-day event identified by sync_key.
    Stores/returns the Google event id via data/calendar_sync.json.
    """
    if not title.startswith(TITLE_PREFIX):
        title = f"{TITLE_PREFIX} {title}"

    store = _load_sync_store()
    events = store.setdefault("events", {})
    existing_id = (events.get(sync_key) or {}).get("event_id")

    try:
        if existing_id:
            result = update_all_day_event(
                existing_id, title, due_date, description=description, colour_id=colour_id
            )
        else:
            result = create_all_day_event(
                title, due_date, description=description, colour_id=colour_id
            )
    except Exception:
        # Stale id (deleted in Calendar UI) — recreate.
        result = create_all_day_event(
            title, due_date, description=description, colour_id=colour_id
        )

    events[sync_key] = {
        "event_id": result["id"],
        "htmlLink": result.get("htmlLink", ""),
        "title": title,
        "due": due_date.isoformat() if hasattr(due_date, "isoformat") else str(due_date),
        "updated": datetime.datetime.now(TIMEZONE).isoformat(),
    }
    _save_sync_store(store)
    return result


def remove_synced_event(sync_key):
    """Deletes a previously synced calendar event (if any) and drops its store entry."""
    store = _load_sync_store()
    events = store.setdefault("events", {})
    entry = events.pop(sync_key, None)
    if entry and entry.get("event_id"):
        try:
            delete_event(entry["event_id"])
        except Exception:
            pass
    _save_sync_store(store)


def _collect_term_deadlines(ctx: dict) -> list:
    """
    Returns [{"sync_key", "title", "due", "description", "colour_id"}, ...]
    from assessments + any fee/census/admin deadlines already present in term_context.
    Does not invent dates.
    """
    items = []

    for subject in ctx.get("subjects", []):
        code = subject.get("code", "")
        for a in subject.get("assessments", []):
            if a.get("status") == "submitted":
                # Ensure submitted ones are cleaned up by sync (via remove pass).
                continue
            due = a.get("due")
            if not due:
                continue
            try:
                datetime.date.fromisoformat(due)
            except Exception:
                continue
            name = a.get("name", "Assessment")
            weight = a.get("weight")
            weight_bit = f" ({weight}%)" if weight is not None else ""
            items.append({
                "sync_key": f"assessment:{code}:{name}",
                "title": f"{code} — {name}{weight_bit}",
                "due": due,
                "description": f"Assessment due date synced by Jarvis from term_context.\n{code} — {name}",
                "colour_id": 11,  # tomato
            })

    # Explicit deadlines list: [{"name", "due", "kind"?}, ...]
    for d in ctx.get("deadlines", []) or []:
        due = d.get("due") or d.get("date")
        name = d.get("name") or d.get("title")
        if not due or not name:
            continue
        try:
            datetime.date.fromisoformat(due)
        except Exception:
            continue
        kind = (d.get("kind") or "deadline").lower()
        items.append({
            "sync_key": f"deadline:{kind}:{name}",
            "title": f"{name}",
            "due": due,
            "description": f"Term deadline ({kind}) synced by Jarvis from term_context.",
            "colour_id": 5,  # banana
        })

    # Optional single fields under term: census_date / fee_due / fee_deadline
    term = ctx.get("term") or {}
    for field, label, kind in (
        ("census_date", "Census date", "census"),
        ("fee_due", "Fee due", "fee"),
        ("fee_deadline", "Fee deadline", "fee"),
    ):
        due = term.get(field)
        if not due:
            continue
        try:
            datetime.date.fromisoformat(due)
        except Exception:
            continue
        items.append({
            "sync_key": f"deadline:{kind}:{field}",
            "title": label,
            "due": due,
            "description": f"Term {kind} date from term_context.term.{field}.",
            "colour_id": 5,
        })

    return items


def sync_term_deadlines_to_calendar(force: bool = False) -> dict:
    """
    Upserts Google Calendar all-day events for assessment due dates and any
    fee/census deadlines present in term_context. Idempotent: stores event ids
    in data/calendar_sync.json. When force=False, skips if already run today
    (morning_brief daily gate). Returns a summary dict; never raises.
    """
    summary = {"synced": 0, "removed": 0, "skipped": None, "errors": []}

    try:
        if not force and _already_synced_today():
            summary["skipped"] = "already synced today"
            return summary

        import term_context
        ctx = term_context.load_context()
        wanted = _collect_term_deadlines(ctx)
        wanted_keys = {item["sync_key"] for item in wanted}

        store = _load_sync_store()
        existing = dict(store.get("events") or {})

        # Remove events for submitted / deleted assessments (assessment:* keys only,
        # and deadline keys no longer present).
        for key, entry in list(existing.items()):
            if key.startswith("assessment:") or key.startswith("deadline:"):
                if key not in wanted_keys:
                    try:
                        remove_synced_event(key)
                        summary["removed"] += 1
                    except Exception as e:
                        summary["errors"].append(f"remove {key}: {e}")

        for item in wanted:
            try:
                upsert_tagged_all_day_event(
                    item["sync_key"],
                    item["title"],
                    item["due"],
                    description=item.get("description", ""),
                    colour_id=item.get("colour_id", 11),
                )
                summary["synced"] += 1
            except Exception as e:
                summary["errors"].append(f"{item['sync_key']}: {e}")

        if not force or summary["synced"] or summary["removed"]:
            _mark_synced_today()
    except Exception as e:
        summary["errors"].append(str(e))
        summary["skipped"] = f"sync failed: {e}"

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Calendar & Task Manager")
    parser.add_argument("--plan",       action="store_true", help="Generate today's smart day plan")
    parser.add_argument("--tasks",      action="store_true", help="Show pending tasks")
    parser.add_argument("--add-task",   metavar="TITLE",     help="Add a task")
    parser.add_argument("--complete",   metavar="TASK_ID",   help="Mark task complete by ID")
    parser.add_argument("--events",     action="store_true", help="Show today's events")
    parser.add_argument("--block",      metavar="REQUEST",   help="Create event from natural language")
    parser.add_argument("--sync-deadlines", action="store_true",
                        help="Upsert assessment/fee deadlines from term_context to Google Calendar")
    args = parser.parse_args()

    if args.sync_deadlines:
        result = sync_term_deadlines_to_calendar(force=True)
        if result.get("skipped") and not result["synced"]:
            print(f"⏭  {result['skipped']}")
        else:
            print(f"✅ Synced {result['synced']} deadline(s), removed {result['removed']}")
        for err in result.get("errors") or []:
            print(f"⚠️  {err}")
        raise SystemExit(0)

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

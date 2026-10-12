"""
Jarvis — Daily Commitment Ledger
================================
One file per day, data/ledger/YYYY-MM-DD.json. It holds that day's
commitments (gym, steps; study and area items come later), the evidence
events that arrived for it, and the debt carried in from the day before.

Everything that enforces or reports on the day reads this file: the
Telegram enforcer, the morning brief, and GET /today. Phone evidence
(iOS Shortcuts) arrives through POST /events and lands here via
record_event().

Writes hold json_store.file_lock on the day file for the whole
load -> mutate -> save cycle, the same way term_context does.

See docs/REWORK.md §3.

    python ledger.py           # print today's ledger (creates it if missing)
"""

import datetime
import json
from pathlib import Path

import pytz

import config
from json_store import atomic_write_json, file_lock

try:
    from term_context import get_pplrul_day
    TERM_CONTEXT_AVAILABLE = True
except Exception:
    TERM_CONTEXT_AVAILABLE = False

SCRIPT_DIR = Path(__file__).parent
LEDGER_DIR = SCRIPT_DIR / "data" / "ledger"

TIMEZONE = pytz.timezone(config.TIMEZONE)

DEFAULT_GYM_SLOT = "07:15"
DEFAULT_GYM_MINUTES = 60
STEP_TARGET = 10_000
STEP_DEBT_CAP = 3_000
MAX_STEPS = 100_000
MAX_SLEEP_HOURS = 16

EVENT_TYPES = ("wake", "gym_arrive", "steps", "sleep")
EMPTY_DEBT = {"gym_minutes": 0, "steps": 0}


def now_local():
    """Current time in the configured timezone. Tests replace this."""
    return datetime.datetime.now(TIMEZONE)


def ledger_path(date):
    """Path of the ledger file for a date."""
    return LEDGER_DIR / f"{date.isoformat()}.json"


def load_day(date):
    """Return the stored ledger for date, or None when it does not exist yet."""
    path = ledger_path(date)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _split_label(date):
    """Training split for date, or 'Rest' if term_context is unavailable."""
    if not TERM_CONTEXT_AVAILABLE:
        return "Rest"
    try:
        return get_pplrul_day(date)
    except Exception:
        return "Rest"


def _carried_debt(previous):
    """Debt to carry from the previous day's ledger (zero when there is none)."""
    debt = dict(EMPTY_DEBT)
    debt["makeup"] = None
    if previous:
        stored = previous.get("debt", {})
        for key in EMPTY_DEBT:
            debt[key] = max(0, int(stored.get(key, 0) or 0))
        debt["makeup"] = stored.get("makeup")
    return debt


def apply_carried_debt(day, previous):
    """
    Reset day's debt from previous and re-derive the targets that depend on
    it (gym minutes, step target). Used when a day is built, and when the
    previous day is closed after this day's file already exists.
    """
    debt = _carried_debt(previous)
    day["debt"] = debt
    gym = find_item(day, "gym")
    if gym is not None:
        gym["minutes"] = gym.get("base_minutes", DEFAULT_GYM_MINUTES) + debt["gym_minutes"]
    steps = find_item(day, "steps")
    if steps is not None:
        steps["target"] = STEP_TARGET + min(debt["steps"], STEP_DEBT_CAP)
        if steps["status"] == "done" and steps["count"] < steps["target"]:
            steps["status"] = "pending"
    return day


def build_day(date, previous=None):
    """
    Build a fresh ledger for date. previous is the prior day's ledger, used
    only to carry debt forward. The step target includes step debt, capped
    at STEP_DEBT_CAP.
    """
    items = []

    split = _split_label(date)
    if split != "Rest":
        items.append({
            "id": "gym",
            "kind": "gym",
            "title": split,
            "slot": DEFAULT_GYM_SLOT,
            "base_minutes": DEFAULT_GYM_MINUTES,
            "minutes": DEFAULT_GYM_MINUTES,
            "evidence": "geofence+hevy",
            "status": "pending",
            "excuse": None,
            "arrived_at": None,
        })

    items.append({
        "id": "steps",
        "kind": "steps",
        "title": "Steps",
        "target": STEP_TARGET,
        "count": 0,
        "evidence": "shortcut",
        "status": "pending",
    })

    day = {
        "date": date.isoformat(),
        "split": split,
        "woke_at": None,
        "sleep_hours": None,
        "items": items,
        "debt": None,
        "events": [],
        "closed": False,
    }
    return apply_carried_debt(day, previous)


def _get_or_create_unlocked(date):
    """Load date's ledger, or build and save it. Caller holds the lock."""
    day = load_day(date)
    if day is None:
        previous = load_day(date - datetime.timedelta(days=1))
        day = build_day(date, previous)
        atomic_write_json(ledger_path(date), day)
    return day


def get_or_create_day(date=None):
    """Return the ledger for date (default today), creating it if missing."""
    date = date or now_local().date()
    with file_lock(ledger_path(date)):
        return _get_or_create_unlocked(date)


def update_day(date, mutate):
    """
    Locked read-modify-write of date's ledger (created if missing).
    mutate(day) edits the dict in place and may return a value, which
    update_day returns. The file is saved after mutate runs.
    """
    with file_lock(ledger_path(date)):
        day = _get_or_create_unlocked(date)
        result = mutate(day)
        atomic_write_json(ledger_path(date), day)
    return result


def find_item(day, item_id):
    """Return the item with item_id from a ledger, or None."""
    for item in day.get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def validate_event(body):
    """
    Check a POST /events body and return (type, value).

    Accepted bodies:
        {"type": "wake"} | {"type": "gym_arrive"}
        {"type": "steps", "count": <int 0..100000>}
        {"type": "sleep", "hours": <number 0..16>}
    Raises ValueError on anything else, including unknown fields.
    """
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    event_type = body.get("type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"type must be one of: {', '.join(EVENT_TYPES)}")

    allowed = {"type", "count"} if event_type == "steps" else \
              {"type", "hours"} if event_type == "sleep" else {"type"}
    extra = sorted(set(body) - allowed)
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")

    if event_type == "steps":
        count = body.get("count")
        # Shortcuts sends Health quantities as numbers that may carry a fraction.
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise ValueError("count must be a number")
        if not 0 <= count <= MAX_STEPS:
            raise ValueError(f"count must be between 0 and {MAX_STEPS}")
        return event_type, int(count)
    if event_type == "sleep":
        hours = body.get("hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)):
            raise ValueError("hours must be a number")
        if not 0 <= hours <= MAX_SLEEP_HOURS:
            raise ValueError(f"hours must be between 0 and {MAX_SLEEP_HOURS}")
        return event_type, round(float(hours), 2)
    return event_type, None


def _apply_event(day, event_type, value, ts):
    """
    Apply one event's effect to a ledger in place. Repeats are harmless:
    wake and gym arrival keep the first time, steps keep the highest count,
    sleep keeps the latest report.
    """
    if event_type == "wake":
        if day.get("woke_at") is None:
            day["woke_at"] = ts
    elif event_type == "gym_arrive":
        gym = find_item(day, "gym")
        if gym is not None and gym.get("arrived_at") is None:
            gym["arrived_at"] = ts
    elif event_type == "steps":
        steps = find_item(day, "steps")
        if steps is not None:
            steps["count"] = max(int(steps.get("count", 0)), value)
            if steps["count"] >= steps["target"]:
                steps["status"] = "done"
    elif event_type == "sleep":
        day["sleep_hours"] = value


def record_event(body, now=None):
    """
    Validate an event body, append it to today's ledger and apply its effect.
    Returns the updated ledger. Raises ValueError for a bad body.
    """
    event_type, value = validate_event(body)
    now = now or now_local()
    ts = now.isoformat(timespec="seconds")

    def _mutate(day):
        event = {"ts": ts, "type": event_type}
        if value is not None:
            event["value"] = value
        day["events"].append(event)
        _apply_event(day, event_type, value, ts)
        return day

    return update_day(now.date(), _mutate)


def today_payload(now=None):
    """GET /today body: today's ledger without the raw event log."""
    now = now or now_local()
    day = get_or_create_day(now.date())
    payload = {key: value for key, value in day.items() if key != "events"}
    payload["event_count"] = len(day.get("events", []))
    return payload


if __name__ == "__main__":
    print(json.dumps(get_or_create_day(), indent=2))

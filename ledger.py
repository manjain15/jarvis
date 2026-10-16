"""
Jarvis — Daily Commitment Ledger
================================
One file per day, data/ledger/YYYY-MM-DD.json. It holds that day's
commitments (gym, steps; study and area items come later), the evidence
events that arrived for it, and the debt carried in from the day before.

A new gym day targets DEFAULT_GYM_MINUTES (45). Carried gym debt is added
on top of that base. A day already stored keeps its own base_minutes.

Everything that enforces or reports on the day reads this file: the
Telegram enforcer, the morning brief, and GET /today. Phone evidence
(iOS Shortcuts) arrives through POST /events and lands here via
record_event(). The step count kept is the highest of those shortcut
readings and Google Health (Fitbit). A failed Health fetch leaves the
shortcut count as it was.

    python ledger.py                              # print today's ledger
    python ledger.py --regrade-steps 2026-10-16   # re-grade one closed day

Writes hold json_store.file_lock on the day file for the whole
load -> mutate -> save cycle, the same way term_context does.

See docs/REWORK.md §3.
"""

import datetime
import json
import os
import time
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

DEFAULT_GYM_SLOT = os.environ.get("JARVIS_GYM_SLOT", "06:30")
DEFAULT_GYM_MINUTES = 45
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
        _sync_steps_status(steps)
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


def _sync_steps_status(steps):
    """Mark the steps item done once its count reaches the target."""
    count = int(steps.get("count") or 0)
    target = int(steps.get("target") or 0)
    if target and count >= target:
        steps["status"] = "done"
    elif steps.get("status") == "done":
        steps["status"] = "pending"


def _add_step_source(steps, source):
    """Record source on the steps item once, shortcut first."""
    raw = steps.get("evidence") or "shortcut"
    parts = [p for p in raw.split("+") if p]
    if source not in parts:
        parts.append(source)
    order = ("shortcut", "google_health")
    known = [p for p in order if p in parts]
    rest = [p for p in parts if p not in order]
    steps["evidence"] = "+".join(known + rest)


def has_step_reading(day):
    """True when a positive shortcut or Google Health total is on the day."""
    for event in day.get("events", []):
        kind = event.get("type")
        if kind in ("steps", "steps.health") and (event.get("value") or 0) > 0:
            return True
    steps = find_item(day, "steps")
    if steps is None:
        return False
    sources = (steps.get("evidence") or "").split("+")
    return "google_health" in sources and int(steps.get("count") or 0) > 0


def apply_health_steps(day, count, now=None):
    """
    Fold a Google Health daily total into the steps item.

    The stored count becomes the higher of the shortcut reading and count.
    Evidence gains google_health (shortcut+google_health when the shortcut
    source is already there). A missing or non-positive count changes nothing.
    Returns the steps item, or None when the day has none.
    """
    steps = find_item(day, "steps")
    if steps is None or count is None or isinstance(count, bool):
        return steps
    try:
        count = int(count)
    except (TypeError, ValueError):
        return steps
    if count <= 0:
        return steps
    count = min(count, MAX_STEPS)
    current = int(steps.get("count") or 0)
    fresh = "google_health" not in (steps.get("evidence") or "").split("+")
    raised = count > current
    steps["count"] = max(current, count)
    _add_step_source(steps, "google_health")
    # "shortcut" is the default evidence before any reading arrives. Drop it
    # when the only total we have is from Health.
    if not any(e.get("type") == "steps" and (e.get("value") or 0) > 0
               for e in day.get("events", [])):
        parts = [p for p in steps["evidence"].split("+") if p != "shortcut"]
        steps["evidence"] = "+".join(parts) or "google_health"
    _sync_steps_status(steps)
    if fresh or raised:
        stamp = (now or now_local()).isoformat(timespec="seconds")
        day.setdefault("events", []).append(
            {"ts": stamp, "type": "steps.health", "value": count})
    return steps


def write_step_debt(day):
    """
    Set outgoing step debt from the steps item.

    No usable reading carries nothing. Returns the Telegram line when the
    day is short, otherwise an empty list.
    """
    debt = day["debt"] = day.get("debt") or {"gym_minutes": 0, "steps": 0, "makeup": None}
    debt.setdefault("makeup", None)
    steps = find_item(day, "steps")
    if steps is None or not has_step_reading(day):
        debt["steps"] = 0
        return []
    short = max(0, int(steps["target"]) - int(steps.get("count") or 0))
    debt["steps"] = min(short, STEP_DEBT_CAP)
    if not short:
        return []
    return [f"Steps {int(steps['count']):,} / {int(steps['target']):,}. "
            f"+{debt['steps']:,} tomorrow."]


_health_cache = {}
_health_warned = set()


def fetch_health_steps(date):
    """
    Google Health's step total for date.

    Returns None when the API has no positive total. Raises when the token
    or the request fails; pull_health_steps turns that into None.
    """
    from google_health import fetch_steps, get_access_token
    data = fetch_steps(get_access_token(), date)
    if not data:
        return None
    raw = data.get("steps")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    count = int(raw)
    if count <= 0:
        return None
    return min(count, MAX_STEPS)


def _warn_health(key, message):
    """Log a Health warning once per date so a dead API does not spam the log."""
    if key in _health_warned:
        return
    _health_warned.add(key)
    print(message)


def pull_health_steps(date, max_age_s=0):
    """
    Google Health step total for date, or None.

    A failed fetch or an empty rollup logs one warning and returns None.
    max_age_s reuses a recent success so a shortcut post and the pace check
    that follows it share one request. Close and regrade pass 0 and always
    ask again.
    """
    key = date.isoformat()
    now_m = time.monotonic()
    if max_age_s:
        hit = _health_cache.get(key)
        if hit and now_m - hit[0] < max_age_s:
            return hit[1]
    try:
        count = fetch_health_steps(date)
    except Exception as e:
        _warn_health(key, f"⚠️  Google Health steps unavailable for {date}: {e}")
        return None
    if not count:
        _warn_health(key, f"⚠️  Google Health steps: no data for {date}")
        return None
    _health_cache[key] = (now_m, count)
    return count


def _retarget_closed_steps(day, previous):
    """
    Point a closed day's step target at the debt previous actually settled,
    then recompute this day's own outgoing step debt. Gym debt is left as it
    was settled.
    """
    steps = find_item(day, "steps")
    if steps is None:
        return
    carried = 0
    if previous:
        carried = max(0, int((previous.get("debt") or {}).get("steps") or 0))
    steps["target"] = STEP_TARGET + min(carried, STEP_DEBT_CAP)
    _sync_steps_status(steps)
    write_step_debt(day)


def _push_step_debt(date):
    """Carry date's settled step debt onto the following day, when that file exists."""
    following = date + datetime.timedelta(days=1)
    if load_day(following) is None:
        return

    def _mutate(day):
        prev = load_day(date)
        if day.get("closed"):
            _retarget_closed_steps(day, prev)
        else:
            apply_carried_debt(day, prev)

    update_day(following, _mutate)
    if (load_day(following) or {}).get("closed"):
        _push_step_debt(following)


def reconcile_steps(date, health_count=None, fetch=True):
    """
    Fold Google Health's step total into date's ledger.

    The count becomes max(shortcut, Health). When the day is already closed,
    step debt is recomputed from that count and carried onto later days.
    No ledger file, or no Health total: the files are left untouched.
    A failed fetch is a warning, not an exception. Returns the day, or None
    when there is no ledger.
    """
    stored = load_day(date)
    if stored is None:
        return None
    if health_count is None and fetch:
        health_count = pull_health_steps(date, max_age_s=0)
    if not health_count:
        return stored

    def _mutate(day):
        apply_health_steps(day, health_count)
        if day.get("closed"):
            write_step_debt(day)
        return day

    day = update_day(date, _mutate)
    if day.get("closed"):
        _push_step_debt(date)
        return load_day(date)
    return day


def regrade_steps(date, health_count=None):
    """
    Reconcile one day's steps with Google Health and recarry the debt.

    Idempotent. Returns a summary dict. ok is False when the ledger is
    missing or Health has no total, and the files are unchanged in that case.
    """
    before = load_day(date)
    if before is None:
        return {"ok": False, "date": date.isoformat(), "reason": "no ledger"}
    next_date = date + datetime.timedelta(days=1)
    next_before = load_day(next_date)
    steps_before = find_item(before, "steps") or {}
    if health_count is None:
        health_count = pull_health_steps(date, max_age_s=0)
    if not health_count:
        return {"ok": False, "date": date.isoformat(), "reason": "no health data",
                "health": None}
    after = reconcile_steps(date, health_count=health_count, fetch=False)
    next_after = load_day(next_date)
    steps_after = find_item(after, "steps") or {}
    return {
        "ok": True,
        "date": date.isoformat(),
        "health": int(health_count),
        "count_before": int(steps_before.get("count") or 0),
        "count_after": int(steps_after.get("count") or 0),
        "evidence": steps_after.get("evidence"),
        "debt_before": int((before.get("debt") or {}).get("steps") or 0),
        "debt_after": int((after.get("debt") or {}).get("steps") or 0),
        "next_date": next_date.isoformat() if next_before or next_after else None,
        "next_target_before": int((find_item(next_before, "steps") or {}).get("target") or 0) if next_before else None,
        "next_target_after": int((find_item(next_after, "steps") or {}).get("target") or 0) if next_after else None,
    }


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
            if value:
                _add_step_source(steps, "shortcut")
            _sync_steps_status(steps)
    elif event_type == "sleep":
        day["sleep_hours"] = value


def record_event(body, now=None):
    """
    Validate an event body, append it to today's ledger and apply its effect.
    A positive steps reading also folds in Google Health when it is available,
    keeping the higher count. Returns the updated ledger. Raises ValueError
    for a bad body. A Health failure leaves the shortcut count unchanged.
    """
    event_type, value = validate_event(body)
    now = now or now_local()
    ts = now.isoformat(timespec="seconds")
    health_steps = None
    if event_type == "steps" and value:
        try:
            health_steps = pull_health_steps(now.date(), max_age_s=15 * 60)
        except Exception as e:
            print(f"⚠️  Google Health steps unavailable for {now.date()}: {e}")

    def _mutate(day):
        event = {"ts": ts, "type": event_type}
        if value is not None:
            event["value"] = value
        day["events"].append(event)
        _apply_event(day, event_type, value, ts)
        if health_steps:
            apply_health_steps(day, health_steps, now)
        return day

    return update_day(now.date(), _mutate)


def today_payload(now=None):
    """GET /today body: today's ledger without the raw event log."""
    now = now or now_local()
    day = get_or_create_day(now.date())
    payload = {key: value for key, value in day.items() if key != "events"}
    payload["event_count"] = len(day.get("events", []))
    return payload


def _fmt_count(before, after, noun):
    """'7,140 → 13,000' or '13,000 unchanged'."""
    if before == after:
        return f"{noun} {after:,} unchanged"
    return f"{noun} {before:,} → {after:,}"


def regrade_steps_main(date_str):
    """
    CLI for --regrade-steps. Prints the before/after figures.
    Returns 0 when Health was applied, 1 when the ledger or Health total
    is missing, 2 when the date is not YYYY-MM-DD.
    """
    try:
        date = datetime.date.fromisoformat(date_str)
    except ValueError:
        print(f"Invalid date {date_str}. Use YYYY-MM-DD.")
        return 2
    try:
        result = regrade_steps(date)
    except Exception as e:
        print(f"⚠️  Regrade failed for {date_str}: {e}")
        return 1
    if not result.get("ok"):
        if result.get("reason") == "no ledger":
            print(f"No ledger for {date}. Nothing to regrade.")
        else:
            print(f"Google Health has no step total for {date}. Ledger unchanged.")
        return 1
    print(f"{result['date']} {_fmt_count(result['count_before'], result['count_after'], 'steps')}"
          f" ({result['evidence']})")
    print(f"{result['date']} {_fmt_count(result['debt_before'], result['debt_after'], 'step debt')}")
    if result.get("next_date"):
        print(f"{result['next_date']} "
              f"{_fmt_count(result['next_target_before'], result['next_target_after'], 'step target')}")
    stored = load_day(date)
    if stored is not None and not stored.get("closed"):
        print(f"{date} is still open. The count is updated; step debt is graded at 23:00.")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Jarvis daily ledger")
    parser.add_argument("--regrade-steps", metavar="YYYY-MM-DD",
                        help="re-grade a day's steps from Google Health and recarry the debt")
    args = parser.parse_args()
    if args.regrade_steps:
        sys.exit(regrade_steps_main(args.regrade_steps))
    print(json.dumps(get_or_create_day(), indent=2))

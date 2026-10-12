"""
Jarvis — Day Plan
=================
Builds the day at 05:30 (called from the Telegram daemon's loop): moves the
gym slot if the calendar clashes, sizes today's study and lecture time from
the weekly targets, places the blocks in free time, writes them to Google
Calendar as Jarvis-tagged events, and asks at the end of each block whether
it got done. See docs/REWORK.md §4.

CONFIG (term_context.json → "study_plan", see term_context.get_study_plan):
  targets           hours per ISO week per course, study and lectures
  routine           weekly commitments the calendar doesn't know (work, travel)
  daily_cap_hours   most study a given weekday can take
  ignore_busy       title words that don't block time (["Lecture"]: recordings)

SIZING:
  remaining = weekly target + last week's lecture backlog − done this week
  today     = remaining × today's cap / (today's cap + caps of the days left)
              lectures × 1.5 (watch before the problems that depend on them)
              study × 1.5 inside an assessment's lead window, × 2 within 3 days
  capped at today's cap; lectures keep priority when it binds.
  done counts in full, partial as half.

BLOCKS: 60 or 90 min, 08:00–21:00, 15 min clear of anything busy. One
Claude call writes the task for every block (fallback: plain text from the
course schedule). Calendar events are tagged jarvis=study; a re-plan only
deletes those.

CHECKS: at a block's end, "MATH2931 · Assignment 2 Q1–3. Done? done / partial
/ no". One question at a time. No reply in 60 min: no_reply. Anything not
done rolls into tomorrow's tasks for that course.

    python day_plan.py --dry-run     # print today's plan, write nothing
"""

import argparse
import datetime
import json
import math

import ledger

try:
    import anthropic
    import config
    ANTHROPIC_AVAILABLE = True
except Exception:
    ANTHROPIC_AVAILABLE = False

PLAN_AT          = datetime.time(5, 30)
PLAN_UNTIL       = datetime.time(12, 0)
RETRY_MINUTES    = 30
DAY_START        = datetime.time(8, 0)
DAY_END          = datetime.time(21, 0)
BUFFER_MIN       = 15
GYM_AFTER_MIN    = 30      # shower, food
GYM_LATEST       = datetime.time(20, 0)
LECTURE_WEIGHT   = 1.5
BOOST_LEAD       = 1.5
BOOST_CLOSE      = 2.0
CLOSE_DAYS       = 3
CHECK_WAIT_MIN   = 60
CHECK_STALE_HRS  = 3
MODEL            = "claude-sonnet-4-6"
WEEKDAYS         = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DONE_WORDS       = {"done", "d", "yes", "y", "watched"}
PARTIAL_WORDS    = {"partial", "p", "half"}
NO_WORDS         = {"no", "n", "nope"}


def _at(date, t):
    """A tz-aware datetime on date at time t in the configured timezone."""
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, t))


def _hm(text):
    hours, minutes = text.split(":")
    return datetime.time(int(hours), int(minutes))


# ── Inputs (replaced in tests) ───────────────────────────────────────────────

def load_study_plan():
    """The study plan from term_context, or the empty default."""
    try:
        from term_context import get_study_plan
        return get_study_plan()
    except Exception:
        return {"targets": {}, "routine": {}, "daily_cap_hours": {}}


def calendar_busy():
    """
    Today's busy periods, without Jarvis's own blocks or anything matching
    the plan's ignore_busy words. None when unreadable.
    """
    try:
        from jarvis_calendar import get_busy_periods
        return get_busy_periods(include_jarvis=False,
                                ignore_words=load_study_plan().get("ignore_busy", []))
    except Exception as e:
        print(f"⚠️  day_plan: calendar unreadable: {e}")
        return None


def assessment_boosts(date, courses):
    """{course: multiplier} for courses with an assessment inside its lead window."""
    try:
        from study_tracker import _lead_days, _load_assessments
        upcoming = _load_assessments(date)
    except Exception:
        return {}
    boosts = {}
    for a in upcoming:
        if a["subject"] not in courses or a["days_left"] < 0:
            continue
        if a["days_left"] <= CLOSE_DAYS:
            boosts[a["subject"]] = max(boosts.get(a["subject"], 1), BOOST_CLOSE)
        elif a["days_left"] <= _lead_days(a["weight"]):
            boosts[a["subject"]] = max(boosts.get(a["subject"], 1), BOOST_LEAD)
    return boosts


def course_context(date, courses):
    """Per-course text for the task prompt: week topics and upcoming assessments."""
    out = {}
    try:
        import course_schedule
        week = course_schedule._current_week()
    except Exception:
        week = None
    try:
        from study_tracker import _load_assessments
        upcoming = _load_assessments(date)
    except Exception:
        upcoming = []
    for code in courses:
        lines = []
        if week:
            try:
                for c in course_schedule.get_week_topics(week, code):
                    if c.get("topics"):
                        lines.append(f"Week {week} topics: {', '.join(c['topics'])}")
            except Exception:
                pass
        for a in upcoming:
            if a["subject"] == code and 0 <= a["days_left"] <= 28:
                lines.append(f"{a['name']} due {a['due']:%a %d %b} ({a['days_left']}d, {a.get('weight')}%)")
        out[code] = {"week": week, "notes": lines}
    return out


# ── Sizing ───────────────────────────────────────────────────────────────────

def week_start(date):
    """Monday of date's ISO week."""
    return date - datetime.timedelta(days=date.weekday())


def _credit(item):
    """Minutes an item counts for: done in full, partial as half."""
    if item.get("status") == "done":
        return item["minutes"]
    if item.get("status") == "partial":
        return item["minutes"] // 2
    return 0


def minutes_done(start, end):
    """{(course, type): minutes} from study items on ledgers in [start, end)."""
    done = {}
    day = start
    while day < end:
        stored = ledger.load_day(day)
        for item in (stored or {}).get("items", []):
            if item.get("kind") == "study":
                key = (item["course"], item["type"])
                done[key] = done.get(key, 0) + _credit(item)
        day += datetime.timedelta(days=1)
    return done


def lecture_backlog(date, targets):
    """
    Last week's unwatched lecture minutes per course, scaled to the days that
    week that were actually planned (so the week before go-live owes nothing).
    """
    start = week_start(date) - datetime.timedelta(days=7)
    end = week_start(date)
    planned = sum(1 for i in range(7)
                  if (ledger.load_day(start + datetime.timedelta(days=i)) or {}).get("planned"))
    if not planned:
        return {}
    done = minutes_done(start, end)
    backlog = {}
    for course, t in targets.items():
        owed = t.get("lectures", 0) * 60 * planned / 7 - done.get((course, "lecture"), 0)
        if owed > 0:
            backlog[course] = int(owed)
    return backlog


def remaining_minutes(date, targets):
    """{(course, type): minutes still to do this week}, including lecture backlog."""
    done = minutes_done(week_start(date), date)
    backlog = lecture_backlog(date, targets)
    out = {}
    for course, t in targets.items():
        for kind, key in (("study", "study"), ("lecture", "lectures")):
            target = t.get(key, 0) * 60 + (backlog.get(course, 0) if kind == "lecture" else 0)
            left = target - done.get((course, kind), 0)
            if left > 0:
                out[(course, kind)] = left
    return out


def allocate(remaining, date, caps, boosts):
    """
    Today's minutes per (course, type). caps is weekday name → hours.
    Lectures are front-loaded; study is boosted near assessments.
    """
    today_cap = caps.get(WEEKDAYS[date.weekday()], 0) * 60
    if today_cap <= 0 or not remaining:
        return {}
    later = sum(caps.get(WEEKDAYS[d], 0) * 60 for d in range(date.weekday() + 1, 7))
    share = today_cap / (today_cap + later)

    lectures, study = {}, {}
    for (course, kind), left in remaining.items():
        if kind == "lecture":
            lectures[(course, kind)] = min(left, left * share * LECTURE_WEIGHT)
        else:
            study[(course, kind)] = min(left, left * share * boosts.get(course, 1))

    lecture_total = sum(lectures.values())
    if lecture_total > today_cap:
        scale = today_cap / lecture_total
        lectures = {k: v * scale for k, v in lectures.items()}
        lecture_total = today_cap
    room = today_cap - lecture_total
    study_total = sum(study.values())
    if study_total > room:
        scale = room / study_total if study_total else 0
        study = {k: v * scale for k, v in study.items()}
    return {k: int(v) for k, v in {**lectures, **study}.items() if v > 0}


def to_blocks(minutes):
    """Split minutes into 60/90-min blocks. Under 40 min is not worth a block."""
    blocks = []
    while minutes >= 105:
        blocks.append(90)
        minutes -= 90
    if minutes >= 75:
        blocks.append(90)
    elif minutes >= 40:
        blocks.append(60)
    return blocks


# ── Placement ────────────────────────────────────────────────────────────────

def routine_busy(plan, date):
    """The routine's fixed commitments on date as (start, end) periods."""
    out = []
    for entry in plan.get("routine", {}).get(WEEKDAYS[date.weekday()], []):
        out.append((_at(date, _hm(entry["start"])), _at(date, _hm(entry["end"]))))
    return out


def gym_period(gym, date, travel):
    """The time a gym session takes out of the day, travel both ways included."""
    start = _at(date, _hm(gym["slot"])) - datetime.timedelta(minutes=travel)
    end = _at(date, _hm(gym["slot"])) + datetime.timedelta(minutes=gym["minutes"] + travel + GYM_AFTER_MIN)
    return start, end


def _overlaps(period, busy):
    start, end = period
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def choose_gym_slot(gym, date, busy, travel):
    """
    The gym slot: kept when its period is clear, else the first clear
    15-minute step after it, up to GYM_LATEST. Returns 'HH:MM'.
    """
    if not _overlaps(gym_period(gym, date, travel), busy):
        return gym["slot"]
    candidate = _at(date, _hm(gym["slot"]))
    latest = _at(date, GYM_LATEST)
    while candidate <= latest:
        trial = dict(gym, slot=candidate.strftime("%H:%M"))
        if not _overlaps(gym_period(trial, date, travel), busy):
            return trial["slot"]
        candidate += datetime.timedelta(minutes=15)
    return gym["slot"]


def free_gaps(busy, date, not_before):
    """Free (start, end) gaps between DAY_START and DAY_END, BUFFER_MIN clear of busy."""
    buf = datetime.timedelta(minutes=BUFFER_MIN)
    padded = sorted((s - buf, e + buf) for s, e in busy)
    cursor = max(_at(date, DAY_START), not_before)
    end = _at(date, DAY_END)
    gaps = []
    for b_start, b_end in padded:
        if b_end <= cursor:
            continue
        if b_start > cursor:
            gaps.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(s, e) for s, e in gaps if (e - s).total_seconds() >= 40 * 60]


def requested_blocks(alloc):
    """
    Blocks to place, as (course, type, minutes): lectures first, then courses
    interleaved so one course doesn't take the whole day.
    """
    queues = {}
    for (course, kind), minutes in sorted(alloc.items()):
        queues[(course, kind)] = [(course, kind, m) for m in to_blocks(minutes)]
    ordered = []
    for kind in ("lecture", "study"):
        keys = [k for k in queues if k[1] == kind]
        while any(queues[k] for k in keys):
            for k in keys:
                if queues[k]:
                    ordered.append(queues[k].pop(0))
    return ordered


def fit_cap(blocks, cap_minutes):
    """
    Keep blocks in order while they fit under the day's cap. A 90 that
    doesn't fit becomes a 60 if that does. Rounding up to whole blocks can
    otherwise push a day past its cap.
    """
    kept, total = [], 0
    for course, kind, minutes in blocks:
        for length in (minutes, 60):
            if length <= minutes and total + length <= cap_minutes:
                kept.append((course, kind, length))
                total += length
                break
    return kept


def place(blocks, gaps):
    """
    First-fit each block into the gaps, with a BUFFER_MIN break between
    blocks. Returns [(course, type, start, end)]; blocks that don't fit drop.
    """
    gaps = [list(g) for g in gaps]
    placed = []
    for course, kind, minutes in blocks:
        length = datetime.timedelta(minutes=minutes)
        for gap in gaps:
            if gap[1] - gap[0] >= length:
                start = gap[0]
                placed.append((course, kind, start, start + length))
                gap[0] = start + length + datetime.timedelta(minutes=BUFFER_MIN)
                break
    return sorted(placed, key=lambda b: b[2])


# ── Tasks ────────────────────────────────────────────────────────────────────

def leftovers(date):
    """Yesterday's unfinished study tasks as [(course, type, task, status)]."""
    stored = ledger.load_day(date - datetime.timedelta(days=1)) or {}
    return [(i["course"], i["type"], i["task"], i["status"])
            for i in stored.get("items", [])
            if i.get("kind") == "study" and i.get("status") in ("partial", "no", "no_reply", "pending")]


def fallback_task(course, kind, context, carried):
    """Plain task text when Claude isn't available."""
    if carried:
        return f"Finish: {carried.pop(0)}"
    week = (context.get(course) or {}).get("week")
    label = f"W{week}" if week else "this week's"
    if kind == "lecture":
        return f"{label} lectures"
    notes = (context.get(course) or {}).get("notes") or []
    due = [n for n in notes if " due " in n]
    return due[0].split(" due ")[0] if due else f"{label} problems"


def write_tasks(placed, context, carry):
    """
    One task per placed block. One Claude call; plain text if it fails.
    carry is leftovers(date). Returns a list of strings, same order as placed.
    """
    carried = {}
    for course, kind, task, _status in carry:
        carried.setdefault((course, kind), []).append(task)
    fallback = [fallback_task(c, k, context, carried.setdefault((c, k), []))
                for c, k, _s, _e in placed]
    if not placed or not ANTHROPIC_AVAILABLE:
        return fallback

    lines = [f"{i + 1}. {c} {'lecture' if k == 'lecture' else 'study'} "
             f"{s:%H:%M}–{e:%H:%M} ({int((e - s).total_seconds() // 60)} min)"
             for i, (c, k, s, e) in enumerate(placed)]
    prompt = f"""Write one concrete task for each study block below. Manav is a UNSW student.

Rules:
- Each task fits its block's minutes. Specific and checkable: "Assignment 2 Q1-3", "W5 lecture 2 + notes", not "study".
- Unfinished tasks carried from yesterday go first, in the first block for that course.
- Lecture blocks are for watching recorded lectures for the current week or catching up.
- No emoji. Under 60 characters each.

BLOCKS:
{chr(10).join(lines)}

COURSE CONTEXT:
{json.dumps(context, default=str)}

CARRIED FROM YESTERDAY (course, type, task, status):
{json.dumps(carry)}

Return only a JSON array of {len(placed)} strings, in block order."""
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        tasks = json.loads(text[text.index("["):text.rindex("]") + 1])
        if len(tasks) == len(placed) and all(isinstance(t, str) and t.strip() for t in tasks):
            return [t.strip()[:80] for t in tasks]
    except Exception as e:
        print(f"⚠️  day_plan: task writing failed, using plain tasks: {e}")
    return fallback


# ── Build ────────────────────────────────────────────────────────────────────

def _title(course, kind):
    return f"Lecture · {course}" if kind == "lecture" else f"Study · {course}"


def build_plan(now=None, dry_run=False, busy_fn=None, tasks_fn=None, calendar=None):
    """
    Plan today. Returns the study items. dry_run computes and returns them
    without touching the calendar or the ledger.
    calendar: object with create(title, start, end, description, date_str) -> id,
    delete_all(date_str). Defaults to Google Calendar.
    """
    import enforcer
    now = now or ledger.now_local()
    date = now.date()
    plan = load_study_plan()
    targets = {c: t for c, t in plan.get("targets", {}).items()
               if t.get("study", 0) or t.get("lectures", 0)}
    calendar = calendar or _GoogleCalendar()
    busy_fn = busy_fn or calendar_busy
    tasks_fn = tasks_fn or write_tasks

    if dry_run:
        day = ledger.load_day(date) or ledger.build_day(
            date, ledger.load_day(date - datetime.timedelta(days=1)))
    else:
        day = ledger.get_or_create_day(date)
    busy_cal = busy_fn()
    if busy_cal is None:
        raise RuntimeError("calendar unreadable")
    busy = busy_cal + routine_busy(plan, date)

    gym = ledger.find_item(day, "gym")
    new_slot = None
    if gym is not None and gym.get("status") == "pending" and not (gym.get("seq") or {}).get("started"):
        new_slot = choose_gym_slot(gym, date, busy, enforcer.TRAVEL_MINUTES)
        gym = dict(gym, slot=new_slot)
    if gym is not None and gym.get("status") in ("pending", "done", "partial"):
        busy = busy + [gym_period(gym, date, enforcer.TRAVEL_MINUTES)]

    alloc = allocate(remaining_minutes(date, targets), date,
                     plan.get("daily_cap_hours", {}), assessment_boosts(date, targets))
    gaps = free_gaps(busy, date, now + datetime.timedelta(minutes=BUFFER_MIN))
    cap = plan.get("daily_cap_hours", {}).get(WEEKDAYS[date.weekday()], 0) * 60
    placed = place(fit_cap(requested_blocks(alloc), cap), gaps)
    tasks = tasks_fn(placed, course_context(date, targets), leftovers(date)) if placed else []

    items = []
    for n, ((course, kind, start, end), task) in enumerate(zip(placed, tasks), 1):
        items.append({
            "id": f"study-{n}", "kind": "study", "type": kind, "course": course,
            "title": _title(course, kind), "task": task,
            "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
            "minutes": int((end - start).total_seconds() // 60),
            "status": "pending", "event_id": None, "asked_at": None,
        })
    if dry_run:
        return {"gym_slot": new_slot, "allocation": alloc, "items": items}

    date_str = date.isoformat()
    calendar.delete_all(date_str)
    for item in items:
        item["event_id"] = calendar.create(
            f"{item['title']}", datetime.datetime.fromisoformat(item["start"]),
            datetime.datetime.fromisoformat(item["end"]), item["task"], date_str)

    def _save(d):
        d["items"] = [i for i in d["items"] if i.get("kind") != "study"] + items
        g = ledger.find_item(d, "gym")
        if g is not None and new_slot and g.get("status") == "pending" \
                and not (g.get("seq") or {}).get("started"):
            if g["slot"] != new_slot:
                g["moved_from"] = g["slot"]
            g["slot"] = new_slot
        d["planned"] = now.isoformat(timespec="seconds")
        d["events"].append({"ts": d["planned"], "type": "plan.built",
                            "blocks": len(items), "gym_slot": new_slot})
    ledger.update_day(date, _save)
    return items


class _GoogleCalendar:
    """Writes Jarvis-tagged study events to Google Calendar."""

    def create(self, title, start, end, description, date_str):
        from jarvis_calendar import create_tagged_event
        return create_tagged_event(title, start, end, description, "study", date_str)

    def delete_all(self, date_str):
        from jarvis_calendar import delete_event, list_tagged_events
        for event_id in list_tagged_events("study", date_str):
            delete_event(event_id)


_last_attempt = {}


def tick(now=None, build=None):
    """Build today's plan once, from 05:30 until noon, retrying every 30 min. Silent."""
    now = now or ledger.now_local()
    build = build or build_plan
    if not PLAN_AT <= now.time() < PLAN_UNTIL:
        return []
    stored = ledger.load_day(now.date())
    if stored and stored.get("planned"):
        return []
    last = _last_attempt.get(now.date())
    if last and now - last < datetime.timedelta(minutes=RETRY_MINUTES):
        return []
    _last_attempt[now.date()] = now
    try:
        build(now=now)
    except Exception as e:
        print(f"⚠️  day_plan: build failed, retrying in {RETRY_MINUTES} min: {e}")
    return []


# ── Block checks ─────────────────────────────────────────────────────────────

def _question(item):
    verb = "Watched" if item["type"] == "lecture" else "Done"
    return f"{item['course']} · {item['task']}. {verb}? done / partial / no"


def check_tick(now=None):
    """Ask about the earliest finished, unasked block. One question at a time."""
    now = now or ledger.now_local()

    def _check(day):
        blocks = [i for i in day.get("items", []) if i.get("kind") == "study"]
        for item in blocks:
            if item["status"] == "pending" and item.get("asked_at"):
                asked = datetime.datetime.fromisoformat(item["asked_at"])
                if now - asked < datetime.timedelta(minutes=CHECK_WAIT_MIN):
                    return None  # Waiting on an answer.
                item["status"] = "no_reply"
        for item in sorted(blocks, key=lambda i: i["end"]):
            end = datetime.datetime.fromisoformat(item["end"])
            if item["status"] != "pending" or item.get("asked_at") or now < end:
                continue
            if now - end > datetime.timedelta(hours=CHECK_STALE_HRS):
                item["status"] = "no_reply"  # Daemon was down; don't ask hours late.
                continue
            item["asked_at"] = now.isoformat(timespec="seconds")
            return _question(item)
        return None

    question = ledger.update_day(now.date(), _check)
    return [question] if question else []


def handle_reply(text, now=None):
    """Answer the outstanding block question. Returns the reply, or None if there isn't one."""
    now = now or ledger.now_local()
    word = text.strip().lower().rstrip(".!")
    if word in DONE_WORDS:
        status = "done"
    elif word in PARTIAL_WORDS:
        status = "partial"
    elif word in NO_WORDS:
        status = "no"
    else:
        return None

    def _answer(day):
        for item in day.get("items", []):
            if item.get("kind") == "study" and item["status"] == "pending" and item.get("asked_at"):
                item["status"] = status
                item["answered_at"] = now.isoformat(timespec="seconds")
                if status == "done":
                    return "Logged."
                if status == "partial":
                    return "Partial. The rest rolls to tomorrow."
                return "Rolls to tomorrow."
        return None

    return ledger.update_day(now.date(), _answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis day plan")
    parser.add_argument("--dry-run", action="store_true", help="print today's plan, write nothing")
    args = parser.parse_args()
    # A dry run uses plain tasks, so it costs nothing.
    plain = lambda placed, context, carry: [fallback_task(c, k, context, []) for c, k, _s, _e in placed]
    result = build_plan(dry_run=args.dry_run, tasks_fn=plain if args.dry_run else None)
    print(json.dumps(result, indent=2, default=str))

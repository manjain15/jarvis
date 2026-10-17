"""
Jarvis — Day Plan
=================
Builds the day at 05:30 (called from the Telegram daemon's loop): moves the
gym slot if the calendar clashes, sizes today's study and lecture time from
the weekly targets, places the blocks in free time, writes them to Google
Calendar as Jarvis-tagged events, and asks at the end of each block whether
it got done. See docs/REWORK.md §4.

CONFIG (term_context.json → "study_plan", see term_context.get_study_plan):
  targets           hours per training week per course, study and lectures
  routine           weekly commitments the calendar doesn't know (work, travel)
  daily_cap_hours   most study a given weekday can take
  ignore_busy       title words that don't block time (["Lecture"]: recordings)
  study_styles      study-block wording (term_context.DEFAULT_STUDY_STYLES).
                    COMP defaults to practice problems; other courses review notes.
                    Lecture blocks ignore this.

SIZING:
  remaining = weekly target + last week's lecture backlog − done this week
              (the week starts on the split's first day: Sunday by default)
  today     = remaining × today's cap / (today's cap + caps of the days left)
              lectures × 1.5 (watch before the problems that depend on them)
              study × 1.5 inside an assessment's lead window, × 2 within 3 days
  capped at today's cap; lectures keep priority when it binds.
  done counts in full, partial as half.

AREA BLOCK: one 45-min block for the most neglected unpaused area (areas.py),
after the study blocks and outside the study cap, with its next action as the task.

BLOCKS: 60 or 90 min, 08:00–21:00, 15 min clear of anything busy. One
Claude call writes the task for every block (fallback: plain text from the
course schedule). Calendar events are tagged jarvis=study; a re-plan only
deletes those.

WEEKLY CHECKS: study_plan.weekly_checks adds small recurring yes/no tasks
(SCIF1000 pre/post-class activities) with a follow-up if the answer is no.

CHECKS: at a block's end, "MATH2931 · Assignment 2 Q1–3. Done? done / partial
/ no". One question at a time. No reply in 60 min: no_reply. Replying to that
Telegram message still logs the block after it expires. A late plain "done"
with nothing open asks which question to reply to, and does not guess.
Anything not done rolls into tomorrow's tasks for that course.

    python day_plan.py --dry-run     # print today's plan, write nothing
    python day_plan.py --answer 2026-10-17 MATH2931 done --asked 13:00
"""

import argparse
import datetime
import json
import math
import re

import ledger
from json_store import atomic_write_json, file_lock

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
MIN_LECTURE_BLOCK = 60     # a course with watchable lectures gets at least this much
BOOST_LEAD       = 1.5
BOOST_CLOSE      = 2.0
CLOSE_DAYS       = 3
CHECK_WAIT_MIN   = 60
CHECK_STALE_HRS  = 3
ASK_LOOKBACK_DAYS = 2     # reply-to still hits today and the two days before
LATE_HINT = "Nothing open. Reply to the question you're answering."
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
    """Per-course text for the task prompt: week topics, assessments, study style."""
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
    try:
        from term_context import get_study_plan, study_style
        plan = get_study_plan()
        styles = {code: study_style(code, plan) for code in courses}
    except Exception:
        styles = {}
    for code in courses:
        lines = []
        if week:
            try:
                for c in course_schedule.get_week_topics(week, code):
                    if c.get("topics"):
                        lines.append(f"Week {week} topics: {'; '.join(c['topics'])}")
                    if c.get("activities"):
                        lines.append(f"Week {week} classes and tasks: {'; '.join(c['activities'])}")
            except Exception:
                pass
        for a in upcoming:
            if a["subject"] == code and 0 <= a["days_left"] <= 28:
                lines.append(f"{a['name']} due {a['due']:%a %d %b} ({a['days_left']}d, {a.get('weight')}%)")
        out[code] = {"week": week, "notes": lines, "study_style": styles.get(code, "notes")}
    return out


# ── Sizing ───────────────────────────────────────────────────────────────────

def week_start(date):
    """First day of date's training week (term_context.week_start; Sunday by default)."""
    try:
        from term_context import week_start as _week_start
        return _week_start(date)
    except Exception:
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
    days_left = 6 - (date - week_start(date)).days
    later = sum(caps.get(WEEKDAYS[(date + datetime.timedelta(days=i)).weekday()], 0) * 60
                for i in range(1, days_left + 1))
    share = today_cap / (today_cap + later)

    lectures, study = {}, {}
    for (course, kind), left in remaining.items():
        if kind == "lecture":
            # Recorded lectures that are out get at least one block: watch them soon.
            floor = MIN_LECTURE_BLOCK if left >= 40 else 0
            lectures[(course, kind)] = min(left, max(floor, left * share * LECTURE_WEIGHT))
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

# ── One-off schedule changes ─────────────────────────────────────────────────
# data/schedule_overrides.json: {"2026-10-13": {"add": [{"start", "end", "label"}],
# "remove": ["Automation work"]}}. "remove" drops routine entries whose label
# contains the text; "add" blocks extra time. Written by POST /schedule and by
# the evening check-in ("automation work moved to tomorrow").

def _overrides_file():
    return ledger.LEDGER_DIR.parent / "schedule_overrides.json"


def load_overrides():
    """All stored schedule changes, by ISO date."""
    try:
        return json.loads(_overrides_file().read_text())
    except Exception:
        return {}


def clean_change(change):
    """Validate one {"add": [...], "remove": [...]} change. Raises ValueError."""
    if not isinstance(change, dict):
        raise ValueError("change must be an object")
    extra = sorted(set(change) - {"add", "remove"})
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")
    add, remove = change.get("add") or [], change.get("remove") or []
    if not isinstance(add, list) or not isinstance(remove, list):
        raise ValueError("add and remove must be lists")
    cleaned_add = []
    for entry in add:
        if not isinstance(entry, dict) or set(entry) - {"start", "end", "label"}:
            raise ValueError('add entries are {"start": "HH:MM", "end": "HH:MM", "label": "..."}')
        try:
            start, end = _hm(entry["start"]), _hm(entry["end"])
        except Exception:
            raise ValueError("start and end must be HH:MM")
        if end <= start:
            raise ValueError("end must be after start")
        label = str(entry.get("label") or "Busy")[:80]
        cleaned_add.append({"start": entry["start"], "end": entry["end"], "label": label})
    cleaned_remove = []
    for label in remove:
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("remove entries are routine labels (text)")
        cleaned_remove.append(label.strip())
    if not cleaned_add and not cleaned_remove:
        raise ValueError("a change needs add or remove")
    return {"add": cleaned_add, "remove": cleaned_remove}


def set_override(date, change):
    """
    Merge a validated change into date's overrides. Adds append; removes are
    unioned. Returns date's overrides after the merge.
    """
    change = clean_change(change)
    path = _overrides_file()
    with file_lock(path):
        data = load_overrides()
        today = ledger.now_local().date()
        # Keep the file small: drop changes more than 14 days old.
        data = {k: v for k, v in data.items() if k >= (today - datetime.timedelta(days=14)).isoformat()}
        current = data.setdefault(date.isoformat(), {"add": [], "remove": []})
        current["add"] += [a for a in change["add"] if a not in current["add"]]
        current["remove"] = sorted(set(current["remove"]) | set(change["remove"]))
        atomic_write_json(path, data)
    return current


def routine_busy(plan, date, overrides=None):
    """
    The routine's fixed commitments on date as (start, end) periods, with
    that date's one-off changes applied (routine entries removed, extra time added).
    """
    change = (load_overrides() if overrides is None else overrides).get(date.isoformat(), {})
    removed = [r.lower() for r in change.get("remove", [])]
    out = []
    for entry in plan.get("routine", {}).get(WEEKDAYS[date.weekday()], []):
        if any(r in entry.get("label", "").lower() for r in removed):
            continue
        out.append((_at(date, _hm(entry["start"])), _at(date, _hm(entry["end"]))))
    for entry in change.get("add", []):
        out.append((_at(date, _hm(entry["start"])), _at(date, _hm(entry["end"]))))
    return out


def gym_period(gym, date, travel):
    """
    The time a gym session takes out of the day, travel both ways included.
    Length is the ledger target (the 45-minute base plus any gym debt) plus
    travel and GYM_AFTER_MIN.
    """
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


# ── Lecture recordings ───────────────────────────────────────────────────────
# A recording can only be watched after the lecture has happened. Supply per
# course = last week's unwatched lecture time (watchable now) + this week's
# timetable lectures, each watchable from its end. Lecture minutes already
# watched this week use up the supply oldest-first.

LECTURE_SLACK_MIN = 15


def timetable_lectures(code, start, end):
    """[(start, end)] of code's lectures in the timetable from date start to date end inclusive."""
    import uni_timetable
    out, day = [], start
    while day <= end:
        for c in uni_timetable.get_events_for(day):
            title = (c.get("title") or "").lower()
            if c.get("time") != "All day" and code.lower() in title and "lecture" in title:
                out.append((c["start"], c["end"]))
        day += datetime.timedelta(days=1)
    return sorted(out)


def lecture_supply(date, targets, lectures_fn=None):
    """
    {course: [(available_from or None, minutes, label)]} of unwatched lecture
    time up to the end of date. None when the timetable can't be read.
    """
    lectures_fn = lectures_fn or timetable_lectures
    start = week_start(date)
    done = minutes_done(start, date)
    backlog = lecture_backlog(date, targets)
    try:
        import course_schedule
        week = course_schedule._current_week()
    except Exception:
        week = None
    supply = {}
    for code, t in targets.items():
        if not t.get("lectures"):
            continue
        try:
            lectures = lectures_fn(code, start, date)
        except Exception as e:
            print(f"⚠️  day_plan: timetable unreadable for {code}: {e}")
            return None
        units = []
        if backlog.get(code):
            units.append([None, backlog[code], "last week's lectures"])
        for n, (l_start, l_end) in enumerate(lectures, 1):
            label = f"W{week} lecture {n} ({l_start:%a %H:%M})" if week else f"lecture {n} ({l_start:%a %H:%M})"
            units.append([l_end, int((l_end - l_start).total_seconds() // 60), label])
        watched = done.get((code, "lecture"), 0)
        for unit in units:
            used = min(unit[1], watched)
            unit[1] -= used
            watched -= used
        supply[code] = [tuple(u) for u in units if u[1] > 0]
    return supply


def lecture_ready(units, needed):
    """When `needed` minutes of a course's unwatched lectures exist (None = now), or False if never today."""
    have = 0
    for available_from, minutes, _label in units:
        have += minutes
        if have >= needed - LECTURE_SLACK_MIN:
            return available_from
    return False


def lecture_label(units, already):
    """The label of the lecture a block starts in, after `already` minutes are planned."""
    seen = 0
    for _available_from, minutes, label in units:
        seen += minutes
        if seen > already:
            return label
    return units[-1][2] if units else None


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


def place(blocks, gaps, supply=None):
    """
    First-fit each block into the gaps, with a BUFFER_MIN break between
    blocks. Returns [(course, type, start, end)]; blocks that don't fit drop.

    supply (lecture_supply) holds lecture blocks back until enough of that
    course's lectures have happened: a block can't start before the lecture
    it covers ends. Placing a block splits its gap, so time before a late
    lecture block stays free for study blocks.
    """
    gaps = [list(g) for g in gaps]
    buf = datetime.timedelta(minutes=BUFFER_MIN)
    planned = {}
    placed = []
    for course, kind, minutes in blocks:
        length = datetime.timedelta(minutes=minutes)
        earliest = None
        if kind == "lecture" and supply is not None:
            ready = lecture_ready(supply.get(course, []), planned.get(course, 0) + minutes)
            if ready is False:
                continue  # Not enough of this course's lectures have happened yet.
            earliest = ready
        for i, gap in enumerate(gaps):
            start = max(gap[0], earliest) if earliest else gap[0]
            if gap[1] - start >= length:
                placed.append((course, kind, start, start + length))
                after = [start + length + buf, gap[1]]
                gaps[i:i + 1] = [g for g in ([gap[0], start - buf], after) if g[1] > g[0]]
                if kind == "lecture":
                    planned[course] = planned.get(course, 0) + minutes
                break
    return sorted(placed, key=lambda b: b[2])


# ── Tasks ────────────────────────────────────────────────────────────────────

def leftovers(date):
    """
    Yesterday's unfinished study tasks as [(course, type, task, status)]. When
    the check-in said what's left, that replaces the full task.
    """
    stored = ledger.load_day(date - datetime.timedelta(days=1)) or {}
    return [(i["course"], i["type"], i.get("left") or i["task"], i["status"])
            for i in stored.get("items", [])
            if i.get("kind") == "study" and i.get("status") in ("partial", "no", "no_reply", "pending")]


# ── Area block ───────────────────────────────────────────────────────────────

AREA_BLOCK_MIN = 45


def area_pick(date):
    """The most neglected unpaused, blockable area (areas.py), or None."""
    try:
        import areas
        return next(iter(areas.most_neglected(areas.refresh_evidence(), date, blockable_only=True)), None)
    except Exception as e:
        print(f"⚠️  day_plan: areas unavailable: {e}")
        return None


def place_area_block(area, gaps, placed):
    """
    (start, end) for one AREA_BLOCK_MIN block after the study blocks, in
    the first gap with room, or None. Not counted against the study cap.
    """
    taken = [(s, e) for _c, _k, s, e in placed]
    buf = datetime.timedelta(minutes=BUFFER_MIN)
    length = datetime.timedelta(minutes=AREA_BLOCK_MIN)
    for g_start, g_end in gaps:
        cursor = g_start
        for s_, e_ in sorted(taken):
            if s_ < g_end and e_ > g_start:
                if s_ - cursor >= length:
                    return cursor, cursor + length
                cursor = max(cursor, e_ + buf)
        if g_end - cursor >= length:
            return cursor, cursor + length
    return None


def _styles_for(courses):
    """Map each course to its study-block style. 'notes' if config can't be read."""
    try:
        from term_context import study_style
        plan = load_study_plan()
    except Exception:
        return {c: "notes" for c in courses}
    return {c: study_style(c, plan) for c in dict.fromkeys(courses)}


_TOPIC_LINE = re.compile(r"topics:\s*(.+)", re.I)
_PAREN = re.compile(r"\s*\([^)]*\)")
_NUMERAL = re.compile(r"\s+(?:[IVX]+|\d+)(?:\s+and\s+(?:[IVX]+|\d+))?$", re.I)


def _clean_topic(text):
    """Drop parentheticals and a trailing part number from one topic string."""
    text = _PAREN.sub(" ", text or "")
    text = _NUMERAL.sub("", text.strip())
    return re.sub(r"\s+", " ", text).strip(" .:-")


def _topic_label(notes):
    """Short topic for a practice task, from the first 'topics:' context line.

    'Dynamic Programming I (…); Dynamic Programming II (…)' becomes
    'Dynamic Programming'.
    """
    topics = []
    for line in notes or []:
        match = _TOPIC_LINE.search(line or "")
        if not match:
            continue
        for part in match.group(1).split(";"):
            cleaned = _clean_topic(part)
            if cleaned:
                topics.append(cleaned)
        break
    if not topics:
        return ""
    stem = topics[0]
    for other in topics[1:]:
        while stem and not other.startswith(stem):
            stem = stem.rsplit(" ", 1)[0]
    stem = stem.strip()
    return stem if len(stem) >= 4 else topics[0]


def practice_task(course, context, minutes=None):
    """Practice-problem task for one study block. Lecture blocks do not use this."""
    info = context.get(course) or {}
    week = info.get("week")
    topic = _topic_label(info.get("notes") or [])
    bits = [f"{course}:"]
    if week:
        bits.append(f"W{week}")
    if topic:
        bits.append(topic)
    bits.append("practice problems")
    text = " ".join(bits)
    suffix = f" ({int(minutes)} min)" if minutes else ""
    if topic and len(text) + len(suffix) > 80:
        text = " ".join(bit for bit in bits if bit != topic)
    return (text + suffix)[:80]


def fallback_task(course, kind, context, carried, minutes=None, style=None):
    """Plain task text when Claude isn't available.

    Practice-style courses (COMP by default) get practice problems on the
    week's topic. Note-review leftovers are dropped for those courses.
    Every other course keeps the previous wording. A due assessment still
    wins over a generic problem set.
    """
    if style is None:
        style = _styles_for([course]).get(course, "notes")
    practice = kind == "study" and style == "practice"
    if practice:
        while carried and is_note_review(carried[0]):
            carried.pop(0)
    if carried:
        return f"Finish: {carried.pop(0)}"
    week = (context.get(course) or {}).get("week")
    label = f"W{week}" if week else "this week's"
    if kind == "lecture":
        return f"{label} lectures"
    notes = (context.get(course) or {}).get("notes") or []
    due = [n for n in notes if " due " in n]
    if due:
        task = due[0].split(" due ")[0]
        if not (practice and is_note_review(task)):
            return task
    if practice:
        return practice_task(course, context, minutes)
    return f"{label} problems"


_LECTURE_WORDS = re.compile(r"\b(watch|lecture|lectures|recording|recordings)\b", re.I)
# Reading or reviewing notes. "Read the problem sheet" does not match.
_NOTE_REVIEW = re.compile(
    r"\b(?:re-?read|reading|read|review(?:ing)?)\b(?:\W+\w+){0,12}\W+\bnotes?\b"
    r"|\bnotes?\b(?:\W+\w+){0,8}\W+\b(?:re-?read|reading|read|review(?:ing)?)\b"
    r"|\bnotes?[- ](?:reviews?|reading|read)\b",
    re.I,
)


def is_note_review(task):
    """True when task text is reading or reviewing notes."""
    return bool(_NOTE_REVIEW.search(task or ""))


def _guard_task(kind, task, fallback, style):
    """Replace a bad study task with its plain fallback. Lecture text is kept.

    Study blocks are never lecture-watching. Practice-style courses also
    reject note-review wording; that fallback is a practice-problem task.
    Model text we keep is capped at 80 characters. A fallback is not recut.
    """
    text = (task or "").strip()
    if kind == "study" and (
        _LECTURE_WORDS.search(text)
        or (style == "practice" and is_note_review(text))
    ):
        return (fallback or "").strip()
    return text[:80]


def _task_prompt(placed, context, carry, labels, styles):
    """The one Claude prompt that writes every block's task."""
    lines = []
    for i, (c, k, s, e) in enumerate(placed):
        minutes = int((e - s).total_seconds() // 60)
        kind = "lecture" if k == "lecture" else "study"
        line = f"{i + 1}. {c} {kind} {s:%H:%M}–{e:%H:%M} ({minutes} min)"
        if labels.get(i):
            line += f", covers {labels[i]}"
        if kind == "study":
            line += f", study_style={styles.get(c, 'notes')}"
        lines.append(line)
    shown = {code: dict(info or {}) for code, info in (context or {}).items()}
    for code, info in shown.items():
        if code in styles:
            info["study_style"] = styles[code]
    for c, _k, _s, _e in placed:
        shown.setdefault(c, {"study_style": styles.get(c, "notes")})
    practice = sorted({c for c, k, _s, _e in placed
                       if k == "study" and styles.get(c) == "practice"})
    other = sorted({c for c, k, _s, _e in placed
                    if k == "study" and styles.get(c) != "practice"})
    rules = [
        '- Each task fits its block\'s minutes. Specific and checkable: "Assignment 2 Q1-3", not "study".',
        "- Unfinished tasks carried from yesterday go first, in the first block for that course.",
        "- A due assessment comes before a generic problem set or note review.",
        '- Lecture blocks are for watching the recorded lecture named in "covers" (it has already',
        '  happened). "W5 lecture 2 + notes" is fine on a lecture block. Never a lecture that hasn\'t happened yet.',
        "- A study block is never lecture-watching.",
    ]
    if practice:
        sample = practice[0]
        rules.append(
            f"- Study blocks whose study_style is practice ({', '.join(practice)}) are practice "
            f"questions or problems on that week's topic. "
            f'Shape: "{sample}: W5 <topic> practice problems (60 min)". '
            f'Do not write "review notes", "read notes", "reading notes", or any note-review task '
            f"for these courses. If a carried task is note review, replace it with practice problems."
        )
    if other:
        rules.append(
            f"- Study blocks whose study_style is notes ({', '.join(other)}) review or read that "
            f"week's notes, or do the set work (tutorial questions, an assignment)."
        )
    rules.append("- No emoji. Under 60 characters each.")
    from voice import VOICE_RULES
    return f"""{VOICE_RULES}

Write one concrete task for each study block below.

Rules:
{chr(10).join(rules)}

BLOCKS:
{chr(10).join(lines)}

COURSE CONTEXT:
{json.dumps(shown, default=str)}

CARRIED FROM YESTERDAY (course, type, task, status):
{json.dumps(carry)}

Return only a JSON array of {len(placed)} strings, in block order."""


def write_tasks(placed, context, carry, labels=None):
    """
    One task per placed block. One Claude call; plain text if it fails.
    carry is leftovers(date). labels maps a block index to the lecture it
    covers ("W5 lecture 1 (Tue 11:00)"). Returns a list of strings, same
    order as placed. A study task that turns out to be lecture-watching is
    replaced with a plain study task. A practice-style study task (COMP by
    default) that reviews or reads notes is replaced with practice problems.
    """
    labels = labels or {}
    styles = _styles_for([c for c, _k, _s, _e in placed])
    carried = {}
    for course, kind, task, _status in carry:
        carried.setdefault((course, kind), []).append(task)
    fallback = []
    for i, (c, k, s, e) in enumerate(placed):
        minutes = int((e - s).total_seconds() // 60)
        if k == "lecture" and labels.get(i) and not carried.get((c, k)):
            fallback.append(f"Watch {labels[i]}")
        else:
            fallback.append(fallback_task(
                c, k, context, carried.setdefault((c, k), []),
                minutes=minutes, style=styles.get(c, "notes")))
    if not placed or not ANTHROPIC_AVAILABLE:
        return fallback

    prompt = _task_prompt(placed, context, carry, labels, styles)
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        tasks = json.loads(text[text.index("["):text.rindex("]") + 1])
        if len(tasks) == len(placed) and all(isinstance(t, str) and t.strip() for t in tasks):
            return [_guard_task(k, task, fallback[i], styles.get(c, "notes"))
                    for i, ((c, k, _s, _e), task) in enumerate(zip(placed, tasks))]
    except Exception as e:
        print(f"⚠️  day_plan: task writing failed, using plain tasks: {e}")
    return fallback


# ── Build ────────────────────────────────────────────────────────────────────

def _title(course, kind):
    return f"Lecture · {course}" if kind == "lecture" else f"Study · {course}"


def _kept(item, now):
    """
    In a re-plan, keep blocks that have been answered or asked about. A block
    still pending and never asked is replaced, even if its time has started:
    the schedule change that triggered the re-plan may be why it can't happen.
    """
    if item.get("kind") not in ("study", "area"):
        return False
    return item.get("status") != "pending" or bool(item.get("asked_at"))


def _reconcile_yesterday_steps(date):
    """
    Fold Google Health into yesterday before today is built, so today's step
    target carries the reconciled debt. A Health failure leaves yesterday as
    it was and never blocks the plan.
    """
    try:
        ledger.reconcile_steps(date - datetime.timedelta(days=1))
    except Exception as e:
        print(f"⚠️  day_plan: couldn't reconcile yesterday's steps: {e}")


def build_plan(now=None, dry_run=False, busy_fn=None, tasks_fn=None, calendar=None, class_fn=None,
               area_fn=None, lectures_fn=None, replan=False):
    """
    Plan today. Returns the study items. dry_run computes and returns them
    without touching the calendar or the ledger.
    calendar: object with create(title, start, end, description, date_str) -> id,
    delete(event_id) and delete_all(date_str). Defaults to Google Calendar.
    class_fn(date, code) says whether the timetable has that class (weekly checks).
    area_fn(date) picks the area for the area block (default: areas.most_neglected).
    lectures_fn(code, start, end) lists timetable lectures (default: uni_timetable).
    replan=True rebuilds only the rest of today: answered or asked blocks
    stay; everything else, and its calendar event, is replaced.
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
        _reconcile_yesterday_steps(date)
        day = ledger.get_or_create_day(date)
    replan = replan and bool(day.get("planned"))
    kept = [i for i in day.get("items", []) if replan and _kept(i, now)]
    dropped = [i for i in day.get("items", []) if replan and i.get("kind") in ("study", "area")
               and not _kept(i, now)]

    busy_cal = busy_fn()
    if busy_cal is None:
        raise RuntimeError("calendar unreadable")
    busy = busy_cal + routine_busy(plan, date)
    busy += [(datetime.datetime.fromisoformat(i["start"]), datetime.datetime.fromisoformat(i["end"]))
             for i in kept]

    gym = ledger.find_item(day, "gym")
    new_slot = None
    if gym is not None and gym.get("status") == "pending" and not (gym.get("seq") or {}).get("started"):
        new_slot = choose_gym_slot(gym, date, busy, enforcer.TRAVEL_MINUTES)
        gym = dict(gym, slot=new_slot)
    if gym is not None and gym.get("status") in ("pending", "done", "partial"):
        busy = busy + [gym_period(gym, date, enforcer.TRAVEL_MINUTES)]

    # Today's kept blocks count as done (credit) and take room under the cap.
    remaining = remaining_minutes(date, targets)
    for item in kept:
        if item.get("kind") == "study":
            key = (item["course"], item["type"])
            if key in remaining:
                remaining[key] = max(0, remaining[key] - _credit(item))
    supply = lecture_supply(date, targets, lectures_fn=lectures_fn)
    if supply is not None:
        for item in kept:
            if item.get("kind") == "study" and item["type"] == "lecture" and item["course"] in supply:
                watched = _credit(item)
                units = [list(u) for u in supply[item["course"]]]
                for unit in units:
                    used = min(unit[1], watched)
                    unit[1] -= used
                    watched -= used
                supply[item["course"]] = [tuple(u) for u in units if u[1] > 0]
        for code, units in supply.items():
            if (code, "lecture") in remaining:
                remaining[(code, "lecture")] = min(remaining[(code, "lecture")], sum(u[1] for u in units))

    alloc = allocate({k: v for k, v in remaining.items() if v > 0}, date,
                     plan.get("daily_cap_hours", {}), assessment_boosts(date, targets))
    gaps = free_gaps(busy, date, now + datetime.timedelta(minutes=BUFFER_MIN))
    cap = plan.get("daily_cap_hours", {}).get(WEEKDAYS[date.weekday()], 0) * 60
    cap -= sum(i["minutes"] for i in kept if i.get("kind") == "study")
    placed = place(fit_cap(requested_blocks(alloc), max(0, cap)), gaps, supply=supply)

    labels, planned = {}, {}
    if supply is not None:
        for i, (course, kind, start, end) in enumerate(placed):
            if kind == "lecture":
                labels[i] = lecture_label(supply.get(course, []), planned.get(course, 0))
                planned[course] = planned.get(course, 0) + int((end - start).total_seconds() // 60)
    tasks = tasks_fn(placed, course_context(date, targets), leftovers(date), labels) if placed else []

    has_area = any(i.get("kind") == "area" for i in kept)
    area = None if has_area else (area_pick(date) if area_fn is None else area_fn(date))
    area_slot = place_area_block(area, gaps, placed) if area else None

    taken = {i["id"] for i in kept}
    items, n = [], 0
    for (course, kind, start, end), task in zip(placed, tasks):
        n += 1
        while f"study-{n}" in taken:
            n += 1
        items.append({
            "id": f"study-{n}", "kind": "study", "type": kind, "course": course,
            "title": _title(course, kind), "task": task,
            "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
            "minutes": int((end - start).total_seconds() // 60),
            "status": "pending", "event_id": None, "asked_at": None,
        })
    if area_slot:
        start, end = area_slot
        items.append({
            "id": "area-1", "kind": "area", "area": area["name"], "title": area["label"],
            "task": area.get("next_action") or "Pick the next step",
            "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
            "minutes": AREA_BLOCK_MIN, "status": "pending", "event_id": None, "asked_at": None,
        })
    if replan:
        checks = [i for i in day.get("items", []) if i.get("kind") == "check"]
    else:
        checks = weekly_check_items(plan, date, class_fn=class_fn)
    if dry_run:
        return {"gym_slot": new_slot, "allocation": alloc, "items": items, "checks": checks}

    date_str = date.isoformat()
    if replan:
        for item in dropped:
            if item.get("event_id"):
                try:
                    calendar.delete(item["event_id"])
                except Exception as e:
                    print(f"⚠️  day_plan: couldn't delete event {item['event_id']}: {e}")
    else:
        calendar.delete_all(date_str)
    for item in items:
        item["event_id"] = calendar.create(
            f"{item['title']}", datetime.datetime.fromisoformat(item["start"]),
            datetime.datetime.fromisoformat(item["end"]), item["task"], date_str)

    def _save(d):
        d["items"] = [i for i in d["items"] if i.get("kind") not in ("study", "check", "area")] \
            + kept + items + checks
        g = ledger.find_item(d, "gym")
        if g is not None and new_slot and g.get("status") == "pending" \
                and not (g.get("seq") or {}).get("started"):
            if g["slot"] != new_slot:
                g["moved_from"] = g["slot"]
            g["slot"] = new_slot
        stamp = now.isoformat(timespec="seconds")
        if not d.get("planned"):
            d["planned"] = stamp
        d["events"].append({"ts": stamp, "type": "plan.replanned" if replan else "plan.built",
                            "blocks": len(items), "kept": len(kept), "gym_slot": new_slot})
    ledger.update_day(date, _save)
    return items


def replan_today(now=None, **kwargs):
    """Rebuild the rest of today after a schedule change. Returns the new items."""
    return build_plan(now=now, replan=True, **kwargs)


class _GoogleCalendar:
    """Writes Jarvis-tagged study events to Google Calendar."""

    def create(self, title, start, end, description, date_str):
        from jarvis_calendar import create_tagged_event
        return create_tagged_event(title, start, end, description, "study", date_str)

    def delete(self, event_id):
        from jarvis_calendar import delete_event
        delete_event(event_id)

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


# ── Weekly checks ────────────────────────────────────────────────────────────
# Small recurring tasks that don't need a study block, e.g. SCIF1000 pre- and
# post-class activities. Config: study_plan.weekly_checks, a list of
#   {"id", "label", "day": "Thursday", "ask": "11:00", "followup": "12:45",
#    "requires_class": "SCIF1000"}
# requires_class skips the check on days the timetable has no such class
# (flexibility week). If the timetable can't be read, the check still runs.

def has_class(date, code):
    """True when the timetable has a class for code on date (True if unreadable)."""
    try:
        import uni_timetable
        return any(code.upper() in (c.get("title") or "").upper()
                   for c in uni_timetable.get_events_for(date))
    except Exception:
        return True


def weekly_check_items(plan, date, class_fn=None):
    """Ledger items for the weekly checks that fall on date."""
    class_fn = class_fn or has_class
    items = []
    for cfg in plan.get("weekly_checks", []):
        if cfg.get("day") != WEEKDAYS[date.weekday()]:
            continue
        if cfg.get("requires_class") and not class_fn(date, cfg["requires_class"]):
            continue
        items.append({
            "id": cfg["id"], "kind": "check", "label": cfg["label"],
            "ask_at": _at(date, _hm(cfg["ask"])).isoformat(timespec="minutes"),
            "followup_at": _at(date, _hm(cfg["followup"])).isoformat(timespec="minutes")
            if cfg.get("followup") else None,
            "status": "pending", "asked_at": None, "followup_sent": False,
        })
    return items


# ── Block and check questions ────────────────────────────────────────────────

def _question(item):
    if item["kind"] == "area":
        return f"{item['title']} · {item['task']}. Done? done / partial / no"
    if item["kind"] == "check":
        lead = "Still open: " if item.get("followup_sent") else ""
        return f"{lead}{item['label']}. Done? done / no"
    verb = "Watched" if item["type"] == "lecture" else "Done"
    return f"{item['course']} · {item['task']}. {verb}? done / partial / no"


def _due_at(item):
    """When an item's next question is due, or None when it has nothing to ask."""
    if item["kind"] in ("study", "area"):
        if item["status"] == "pending" and not item.get("asked_at"):
            return datetime.datetime.fromisoformat(item["end"])
        return None
    if item["status"] == "pending" and not item.get("asked_at"):
        return datetime.datetime.fromisoformat(item["ask_at"])
    if item["status"] in ("no", "no_reply") and item.get("followup_at") and not item.get("followup_sent"):
        return datetime.datetime.fromisoformat(item["followup_at"])
    return None


def check_tick(now=None):
    """Ask the next due block or weekly-check question. One question at a time.

    The caller sends the returned text and stores Telegram's message id with
    record_ask_message, so a later reply to that message can still log the item.
    """
    now = now or ledger.now_local()

    def _check(day):
        askable = [i for i in day.get("items", []) if i.get("kind") in ("study", "check", "area")]
        for item in askable:
            if item["status"] == "pending" and item.get("asked_at"):
                asked = datetime.datetime.fromisoformat(item["asked_at"])
                if now - asked < datetime.timedelta(minutes=CHECK_WAIT_MIN):
                    return None  # Waiting on an answer.
                item["status"] = "no_reply"
        due = [(t, i) for i in askable for t in [_due_at(i)] if t is not None and t <= now]
        for t, item in sorted(due, key=lambda pair: pair[0]):
            if now - t > datetime.timedelta(hours=CHECK_STALE_HRS):
                # The daemon was down; don't ask hours late.
                if item["status"] == "pending":
                    item["status"] = "no_reply"
                item["followup_sent"] = True
                continue
            if item["kind"] == "check" and item["status"] in ("no", "no_reply"):
                item["followup_sent"] = True
                item["status"] = "pending"
            item["asked_at"] = now.isoformat(timespec="seconds")
            return _question(item)
        return None

    question = ledger.update_day(now.date(), _check)
    return [question] if question else []


def _parse_status(text):
    """Map a reply to done, partial or no. None when it isn't one of those words."""
    word = (text or "").strip().lower().rstrip(".!")
    if word in DONE_WORDS:
        return "done"
    if word in PARTIAL_WORDS:
        return "partial"
    if word in NO_WORDS:
        return "no"
    return None


def _item_label(item):
    """Short name for a check-in reply: the course, the check label, or the area title."""
    if item.get("kind") == "area":
        return item.get("title") or item.get("area") or "that block"
    if item.get("kind") == "check":
        return item.get("label") or "that check"
    return item.get("course") or "that block"


def _message_id(value):
    """A Telegram message id as an int, or None when value isn't one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ask_ids(item):
    """Telegram message ids that asked this item, oldest first."""
    raw = item.get("ask_message_ids") or []
    if not isinstance(raw, list):
        raw = [raw]
    values = list(raw)
    if item.get("ask_message_id") is not None:
        values.append(item.get("ask_message_id"))
    ids = []
    for value in values:
        number = _message_id(value)
        if number is not None and number not in ids:
            ids.append(number)
    return ids


def _ask_dates(now):
    """Today and the previous ASK_LOOKBACK_DAYS dates, newest first."""
    return [now.date() - datetime.timedelta(days=i) for i in range(ASK_LOOKBACK_DAYS + 1)]


def _clock(iso):
    """HH:MM from an ISO timestamp, or None."""
    if not iso or len(iso) < 16 or iso[10:11] != "T":
        return None
    return iso[11:16]


def _clock_matches(item, hm):
    """True when the item was asked at hm, or its block ends / check is due then."""
    return any(_clock(item.get(field)) == hm for field in ("asked_at", "end", "ask_at"))


def _resolved_status(item, status):
    """Status to store. Weekly checks have no partial."""
    if item.get("kind") == "check" and status == "partial":
        return "no"
    return status


def _apply_status(item, status, now):
    """Write the answer onto item. Returns (stored status, area name or None)."""
    answered = _resolved_status(item, status)
    item["status"] = answered
    item["answered_at"] = now.isoformat(timespec="seconds")
    area = item.get("area") if item.get("kind") == "area" and answered in ("done", "partial") else None
    return answered, area


def _touch_areas(names, now):
    """Mark areas worked by a done or partial block. A failure is logged, not raised."""
    for name in names:
        try:
            import areas
            areas.mutate(lambda a, n=name: areas.touch(a, n, now.date(), "block"))
        except Exception as e:
            print(f"⚠️  day_plan: couldn't mark {name} touched: {e}")


def _has_no_reply(day):
    """True when a check-in today went unanswered."""
    return any(
        i.get("kind") in ("study", "check", "area") and i.get("status") == "no_reply"
        for i in day.get("items", [])
    )


def _pending_reply(item, answered):
    """Reply for the one question that is still open."""
    if item["kind"] == "area" and answered != "done":
        return "Logged. It stays the push until it moves."
    if answered == "done":
        return "Logged."
    if item["kind"] == "check":
        if item.get("followup_at") and not item.get("followup_sent"):
            return f"Asking again at {item['followup_at'][11:16]}."
        return "Logged as not done."
    if answered == "partial":
        return "Partial. The rest rolls to tomorrow."
    return "Rolls to tomorrow."


def _named_reply(item, answered, previous):
    """Reply that names the item a Telegram reply-to just answered."""
    name = _item_label(item)
    if item["kind"] == "area" and answered != "done":
        text = f"Logged {name}. It stays the push until it moves."
    elif answered == "done":
        text = f"Logged {name}."
    elif item["kind"] == "check":
        if item.get("followup_at") and not item.get("followup_sent"):
            text = f"Logged {name}. Asking again at {item['followup_at'][11:16]}."
        else:
            text = f"Logged {name}. Not done."
    elif answered == "partial":
        text = f"Logged {name}. The rest rolls to tomorrow."
    else:
        text = f"Logged {name}. Rolls to tomorrow."
    if previous in ("done", "partial", "no") and previous != answered:
        text += f" Was {previous}."
    return text


def record_ask_message(message_id, now=None):
    """Store message_id on the check-in asked most recently today.

    Follow-up asks keep every id in ask_message_ids and the latest in
    ask_message_id. Returns the id stored, or None when it isn't a message id
    or nothing has been asked.
    """
    message_id = _message_id(message_id)
    if message_id is None:
        return None
    now = now or ledger.now_local()

    def _stamp(day):
        candidates = [
            i for i in day.get("items", [])
            if i.get("kind") in ("study", "check", "area") and i.get("asked_at")
        ]
        if not candidates:
            print("⚠️  day_plan: no asked item to attach the Telegram message id to")
            return None
        item = max(candidates, key=lambda i: i["asked_at"])
        ids = _ask_ids(item)
        if message_id not in ids:
            ids.append(message_id)
        item["ask_message_ids"] = ids
        item["ask_message_id"] = message_id
        return message_id

    return ledger.update_day(now.date(), _stamp)


def handle_reply(text, now=None, *, late_hint=True):
    """Answer the outstanding question. Returns the reply, or None if there isn't one.

    A done/partial/no word with nothing open, when something went no_reply
    today, returns a hint instead of guessing which block it was. Pass
    late_hint False when the text was a reply to some other message.
    """
    now = now or ledger.now_local()
    status = _parse_status(text)
    if status is None:
        return None

    touched = []

    def _answer(day):
        for item in day.get("items", []):
            if item.get("kind") not in ("study", "check", "area") or item.get("status") != "pending" \
                    or not item.get("asked_at"):
                continue
            answered, area = _apply_status(item, status, now)
            if area:
                touched.append(area)
            return _pending_reply(item, answered)
        if late_hint and _has_no_reply(day):
            return LATE_HINT
        return None

    reply = ledger.update_day(now.date(), _answer)
    _touch_areas(touched, now)
    return reply


def handle_reply_to(message_id, text, now=None):
    """Answer the check-in message_id asked, even after it expired or was answered.

    The last answer wins. Returns None when message_id isn't a check-in from
    the last few days, or text isn't done/partial/no, so the caller falls through.
    """
    now = now or ledger.now_local()
    status = _parse_status(text)
    message_id = _message_id(message_id)
    if status is None or message_id is None:
        return None

    found = None
    for date in _ask_dates(now):
        day = ledger.load_day(date)
        if not day:
            continue
        for item in day.get("items", []):
            if item.get("kind") in ("study", "check", "area") and message_id in _ask_ids(item):
                found = (date, item.get("id"))
                break
        if found:
            break
    if not found:
        return None

    date, item_id = found
    touched = []

    def _answer(day):
        item = None
        for candidate in day.get("items", []):
            if message_id in _ask_ids(candidate) and (item_id is None or candidate.get("id") == item_id):
                item = candidate
                break
        if item is None:
            return None
        previous = item.get("status")
        answered, area = _apply_status(item, status, now)
        if area:
            touched.append(area)
        return _named_reply(item, answered, previous)

    reply = ledger.update_day(date, _answer)
    _touch_areas(touched, now)
    return reply


def _match_key(item, key):
    """True when key is this item's id, course, label, title or area name."""
    wanted = key.strip().lower()
    fields = (item.get("id"), item.get("course"), item.get("label"), item.get("title"), item.get("area"))
    return wanted in {str(field).strip().lower() for field in fields if field}


def _select_matches(day, key, asked_hm):
    """Items key could mean. One open asked item wins when several share a course."""
    items = [i for i in day.get("items", []) if i.get("kind") in ("study", "check", "area")]
    by_id = [i for i in items if str(i.get("id", "")).strip().lower() == key.strip().lower()]
    matches = by_id or [i for i in items if _match_key(i, key)]
    if asked_hm:
        return [i for i in matches if _clock_matches(i, asked_hm)]
    if len(matches) <= 1:
        return matches
    open_ones = [
        i for i in matches
        if i.get("status") in ("no_reply", "pending") and i.get("asked_at")
    ]
    if len(open_ones) == 1:
        return open_ones
    return matches


def answer_item(date, key, status, now=None, asked_hm=None):
    """Mark one study, check or area item. Idempotent when it already has that status.

    key is an item id or a course code (also a check label or area title).
    asked_hm (HH:MM) limits the match to an item asked or ending at that time.
    Raises ValueError when the ledger, the word, or the item can't be resolved.
    """
    now = now or ledger.now_local()
    parsed = _parse_status(status)
    if parsed is None:
        raise ValueError("Status must be done, partial or no.")
    if asked_hm:
        asked_hm = asked_hm.strip()
        if _clock(f"2000-01-01T{asked_hm}") != asked_hm:
            raise ValueError("Asked time must look like 13:00.")
    day = ledger.load_day(date)
    if day is None:
        raise ValueError(f"No ledger for {date.isoformat()}.")
    matches = _select_matches(day, key, asked_hm)
    if not matches:
        raise ValueError(f"No item matching {key!r} on {date.isoformat()}.")
    if len(matches) > 1:
        lines = [
            f"{i.get('id')} {_item_label(i)} status={i.get('status')} asked={i.get('asked_at')} end={i.get('end')}"
            for i in matches
        ]
        raise ValueError(
            "Several items match. Re-run with the id or --asked HH:MM:\n" + "\n".join(lines)
        )
    item_id = matches[0].get("id")
    touched = []

    def _set(day):
        item = None
        for candidate in day.get("items", []):
            if candidate.get("id") == item_id:
                item = candidate
                break
        if item is None:
            return None
        target = _resolved_status(item, parsed)
        if item.get("status") == target:
            return f"Already {target}: {item_id} {_item_label(item)}."
        previous = item.get("status")
        answered, area = _apply_status(item, parsed, now)
        if area:
            touched.append(area)
        return f"Set {item_id} {_item_label(item)} to {answered} (was {previous})."

    reply = ledger.update_day(date, _set)
    if reply is None:
        raise ValueError(f"No item matching {key!r} on {date.isoformat()}.")
    _touch_areas(touched, now)
    return reply


def main(argv=None):
    """Print a dry-run plan, or mark one block's answer. A bad answer exits 1."""
    try:
        _main(argv)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _main(argv=None):
    """CLI body. Raises ValueError for a bad date or an item the answer can't resolve."""
    parser = argparse.ArgumentParser(description="Jarvis day plan")
    parser.add_argument("--dry-run", action="store_true", help="print today's plan, write nothing")
    parser.add_argument("--answer", nargs=3, metavar=("DATE", "ITEM", "STATUS"),
                        help="mark a block: --answer 2026-10-17 MATH2931 done")
    parser.add_argument("--asked", metavar="HH:MM",
                        help="pick the item asked or ending at this time, e.g. 13:00")
    args = parser.parse_args(argv)
    if args.answer:
        date_s, key, status = args.answer
        try:
            date = datetime.date.fromisoformat(date_s)
        except ValueError:
            raise ValueError(f"Bad date {date_s!r}. Use YYYY-MM-DD.") from None
        print(answer_item(date, key, status, asked_hm=args.asked))
        return
    # A dry run uses plain tasks, so it costs nothing.
    def plain(placed, context, carry, labels=None):
        """Plain tasks for a dry run, so it makes no Claude call."""
        global ANTHROPIC_AVAILABLE
        saved, ANTHROPIC_AVAILABLE = ANTHROPIC_AVAILABLE, False
        try:
            return write_tasks(placed, context, carry, labels)
        finally:
            ANTHROPIC_AVAILABLE = saved
    result = build_plan(dry_run=args.dry_run, tasks_fn=plain if args.dry_run else None)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

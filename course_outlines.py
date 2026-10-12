"""
Jarvis — UNSW Course Outlines
=============================
Pulls each current subject's public course outline from UNSW and saves its
week-by-week schedule to data/outlines/<code>.json. course_schedule.py reads
those, with anything filled in by hand in courses/<code>.json taking
precedence, so the day plan and the morning brief know what each week covers
without manual entry.

SOURCE: the JSON API behind www.unsw.edu.au/course-outlines (public, no login):
  https://courseoutlines.unsw.edu.au/v1/publicsitecourseoutlines/detail
  ?year=2026&term=Term 3&teachingPeriod=T3&courseCode=MATH2931
  &deliveryMode=In Person&deliveryFormat=Standard&deliveryLocation=Kensington
  &activityGroupId=1
Courses delivered differently can override any parameter in
courses/<code>.json under "outline_params".

Outlines publish the schedule two ways, and both are parsed:
  - structured rows (integrat_CO_Schedule): one row per week, e.g. MATH2931
  - free text (integrat_scheduleinfo): "<strong>Week 5</strong><br>Lecture 9: ...", e.g. COMP3121

WHEN: Sundays from the Telegram daemon (refresh_tick), and on /outlines.
A course is rewritten only when its "last published" date changes. A failed
fetch keeps the last saved copy. No Anthropic calls; one HTTP request per
subject per run.

    python course_outlines.py           # fetch and print what changed
    python course_outlines.py --force   # rewrite even if unchanged
"""

import argparse
import datetime
import html
import json
import re
from pathlib import Path

import requests

from json_store import atomic_write_json

SCRIPT_DIR   = Path(__file__).parent
OUTLINES_DIR = SCRIPT_DIR / "data" / "outlines"
COURSES_DIR  = SCRIPT_DIR / "courses"
STATE_FILE   = OUTLINES_DIR / "_state.json"

API_URL = "https://courseoutlines.unsw.edu.au/v1/publicsitecourseoutlines/detail"
DEFAULT_PARAMS = {
    "deliveryMode": "In Person",
    "deliveryFormat": "Standard",
    "deliveryLocation": "Kensington",
    "activityGroupId": "1",
}
TIMEOUT = 20
REFRESH_WEEKDAY = 6          # Sunday
REFRESH_AFTER = datetime.time(4, 0)

_WEEK_HEAD = re.compile(r"<strong>\s*Week\s+(\d+)\s*</strong>", re.I)
_WEEK_PLAIN = re.compile(r"\bWeek\s+(\d+)\b", re.I)
_LINE_BREAK = re.compile(r"<br\s*/?>|</p>|</li>|</div>", re.I)
_TAG = re.compile(r"<[^>]+>")
_LECTURE = re.compile(r"^Lecture\s*\d*\s*:\s*", re.I)


# ── Parsing ──────────────────────────────────────────────────────────────────

def _text(raw):
    """HTML fragment → plain text with collapsed whitespace."""
    text = html.unescape(_TAG.sub(" ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


_EMPTY_ENTRY = re.compile(r"^(?:[^:]*:\s*)?-$")  # "-" or "Formatif: -" (nothing that week)


def _lines(raw):
    """HTML fragment → non-empty plain-text lines."""
    return [t for t in (_text(part) for part in _LINE_BREAK.split(raw or ""))
            if t and not _EMPTY_ENTRY.match(t)]


def _sort_line(line, entry):
    """File one schedule line under topics (lectures, flex week) or activities."""
    if _LECTURE.match(line):
        topic = _LECTURE.sub("", line).strip()
        if topic and "cancelled" not in topic.lower():
            entry["topics"].append(topic)
    elif "flexibility" in line.lower() or "no classes" in line.lower():
        entry["topics"].append("Flexibility week")
    else:
        entry["activities"].append(line)


def parse_schedule(outline):
    """{week: {"topics": [...], "activities": [...]}} from either schedule format."""
    weeks = {}

    def entry(week):
        return weeks.setdefault(week, {"topics": [], "activities": []})

    for row in outline.get("integrat_CO_Schedule") or []:
        match = _WEEK_PLAIN.search(row.get("integrat_teachingweek") or "")
        if not match:
            continue
        e = entry(int(match.group(1)))
        content = _text(row.get("integrat_content"))
        if not content:
            continue
        kind = (row.get("integrat_type") or "").strip()
        if "flexibility" in content.lower():
            e["topics"].append("Flexibility week")
        elif kind.lower() in ("lecture", ""):
            e["topics"].append(content.rstrip(".;"))
        else:
            e["activities"].append(f"{kind}: {content}")

    if not weeks:
        raw = outline.get("integrat_scheduleinfo") or ""
        parts = _WEEK_HEAD.split(raw)
        if len(parts) < 3:
            parts = _WEEK_PLAIN.split(_text(raw))
        # parts = [preamble, week, body, week, body, ...]
        for i in range(1, len(parts) - 1, 2):
            e = entry(int(parts[i]))
            for line in _lines(parts[i + 1]):
                _sort_line(line, e)
    return weeks


def parse_assessments(outline):
    """[{"name", "week" (int or None), "weight", "due"}] from the outline."""
    out = []
    for a in outline.get("integrat_CO_Assessment") or []:
        name = _text(a.get("integrat_title")) or "Assessment"
        due = _text(a.get("integrat_duedate"))
        match = _WEEK_PLAIN.search(due)
        out.append({
            "name": name,
            "week": int(match.group(1)) if match else None,
            "weight": _text(a.get("integrat_weight")),
            "due": due,
        })
    return out


def build_course(code, outline, term_name):
    """The data/outlines/<code>.json record for one outline."""
    weeks = parse_schedule(outline)
    assessments = parse_assessments(outline)
    last = max(list(weeks) + [a["week"] for a in assessments if a["week"]] + [10])
    schedule = []
    for week in range(1, last + 1):
        e = weeks.get(week, {"topics": [], "activities": []})
        labels = [f"{a['name']} ({a['weight']})" if a["weight"] else a["name"]
                  for a in assessments if a["week"] == week]
        schedule.append({"week": week, "topics": e["topics"],
                         "activities": e["activities"], "assessments": labels})
    return {
        "code": code,
        "name": _text(outline.get("integrat_coursename")),
        "term": term_name,
        "published": _text(outline.get("integrat_lastpublishedon")),
        "source": "UNSW course outline (courseoutlines.unsw.edu.au)",
        "fetched": datetime.date.today().isoformat(),
        "assessments": assessments,
        "schedule": schedule,
    }


# ── Fetching ─────────────────────────────────────────────────────────────────

def term_params(term_name):
    """'T3 2026' → {"year": "2026", "term": "Term 3", "teachingPeriod": "T3"}."""
    match = re.match(r"^\s*T(\d)\s+(\d{4})\s*$", term_name or "")
    if not match:
        raise ValueError(f"term name {term_name!r} is not like 'T3 2026'")
    n, year = match.groups()
    return {"year": year, "term": f"Term {n}", "teachingPeriod": f"T{n}"}


def _overrides(code):
    """Per-course API parameter overrides from courses/<code>.json, if any."""
    path = COURSES_DIR / f"{code.lower()}.json"
    try:
        return json.loads(path.read_text()).get("outline_params") or {}
    except Exception:
        return {}


def fetch_outline(code, term_name, get=None):
    """The outline JSON for code, or None when it can't be fetched or has no data."""
    get = get or requests.get
    params = {**DEFAULT_PARAMS, **term_params(term_name), "courseCode": code, **_overrides(code)}
    try:
        response = get(API_URL, params=params, timeout=TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️  outline {code}: HTTP {response.status_code}")
            return None
        data = response.json()
    except Exception as e:
        print(f"⚠️  outline {code}: {e}")
        return None
    if not isinstance(data, dict) or data.get("type") in ("ValidationError", "No Results"):
        print(f"⚠️  outline {code}: no outline for these parameters")
        return None
    return data


def _current_subjects():
    """(term name, [subject codes]) from term_context."""
    import term_context
    ctx = term_context.load_context()
    return ctx.get("term", {}).get("name", ""), [s["code"] for s in ctx.get("subjects", [])]


def refresh(force=False, get=None, subjects=None):
    """
    Fetch every current subject's outline. Returns one line per subject.
    A course file is rewritten only when the outline's published date
    changed (or force). A failed fetch leaves the saved copy alone.
    """
    term_name, codes = subjects or _current_subjects()
    lines = []
    for code in codes:
        outline = fetch_outline(code, term_name, get=get)
        if outline is None:
            lines.append(f"{code}: couldn't fetch, kept the saved copy")
            continue
        path = OUTLINES_DIR / f"{code.lower()}.json"
        record = build_course(code, outline, term_name)
        try:
            saved = json.loads(path.read_text())
        except Exception:
            saved = None
        if saved and not force and saved.get("published") == record["published"] \
                and saved.get("term") == term_name:
            lines.append(f"{code}: unchanged (published {record['published']})")
            continue
        atomic_write_json(path, record)
        filled = sum(1 for w in record["schedule"] if w["topics"])
        lines.append(f"{code}: updated, {filled}/{len(record['schedule'])} weeks with topics "
                     f"(published {record['published']})")
    atomic_write_json(STATE_FILE, {"last_run": datetime.date.today().isoformat()})
    return lines


def refresh_tick(now):
    """Run refresh once each Sunday from 04:00. Silent; returns no messages."""
    if now.weekday() != REFRESH_WEEKDAY or now.time() < REFRESH_AFTER:
        return []
    try:
        last = json.loads(STATE_FILE.read_text()).get("last_run")
    except Exception:
        last = None
    if last == now.date().isoformat():
        return []
    try:
        for line in refresh():
            print(f"📚  {line}")
    except Exception as e:
        print(f"⚠️  outline refresh failed: {e}")
        atomic_write_json(STATE_FILE, {"last_run": now.date().isoformat(), "error": str(e)})
    return []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch UNSW course outlines")
    parser.add_argument("--force", action="store_true", help="rewrite even if unchanged")
    args = parser.parse_args()
    for line in refresh(force=args.force):
        print(line)

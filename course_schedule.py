"""
Jarvis — Course Schedule (weekly topics from course outlines)
=============================================================
Course outlines and weekly schedules live behind UNSW SSO (Moodle / course
sites), so they can't be auto-fetched like the timetable. They are also fixed
for the whole term, so they're entered once per term as JSON in courses/.

This module loads those per-course schedules and surfaces "this week's topics"
into the morning brief, using the current term week from term_context. Drop a
new courses/<code>.json (same shape as comp2511.json) and it's picked up
automatically — no code change needed.

Optional + defensive: every accessor returns empty on any error, so a missing
or malformed file can never break the brief.
"""

import json
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
COURSES_DIR = SCRIPT_DIR / "courses"


def _current_week():
    """Current term week from term_context; None if unavailable."""
    try:
        import term_context
        return term_context.current_term_week(term_context.load_context())
    except Exception:
        return None


def load_courses():
    """Load every courses/*.json. Returns a list of course dicts ([] on error)."""
    if not COURSES_DIR.exists():
        return []
    courses = []
    for path in sorted(COURSES_DIR.glob("*.json")):
        try:
            courses.append(json.loads(path.read_text()))
        except Exception:
            continue
    return courses


def get_week_topics(week, course=None):
    """
    Return [{code, name, topics, assessments}] for the given term week.
    If `course` (a course code) is given, restrict to that course.
    """
    out = []
    for c in load_courses():
        if course and c.get("code", "").upper() != course.upper():
            continue
        for entry in c.get("schedule", []):
            if entry.get("week") == week:
                out.append({
                    "code":        c.get("code", "?"),
                    "name":        c.get("name", ""),
                    "topics":      entry.get("topics", []),
                    "assessments": entry.get("assessments", []),
                })
    return out


def get_current_week_summary():
    """
    Text block of this week's topics across all courses, for the morning brief.
    Empty string if the term week or schedules are unavailable.
    """
    week = _current_week()
    if not week:
        return ""
    rows = get_week_topics(week)
    if not rows:
        return ""
    lines = [f"COURSE TOPICS — Week {week}:"]
    for r in rows:
        topics = "; ".join(r["topics"]) if r["topics"] else "—"
        lines.append(f"  {r['code']}: {topics}")
        if r["assessments"]:
            lines.append(f"     ⚑ {' | '.join(r['assessments'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    week = _current_week()
    print(f"Current term week: {week}")
    courses = load_courses()
    print(f"Loaded {len(courses)} course schedule(s): "
          + ", ".join(c.get("code", "?") for c in courses))
    print()
    print(get_current_week_summary() or "(no topics for the current week)")

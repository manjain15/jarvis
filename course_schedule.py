"""
Jarvis — Course Schedule (weekly topics from course outlines)
=============================================================
Weekly schedules come from two places:
  data/outlines/<code>.json  fetched from the public UNSW course outline API
                             by course_outlines.py (weekly, automatic)
  courses/<code>.json        filled in by hand (same shape as comp2511.json)

They are merged per week: hand-entered topics replace the outline's for that
week, and hand-entered assessments are added to the outline's. A course with
only one of the two still works. This module surfaces "this week's topics"
into the morning brief and the day plan, using the current term week from
term_context.

Optional + defensive: every accessor returns empty on any error, so a missing
or malformed file can never break the brief.
"""

import json
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
COURSES_DIR  = SCRIPT_DIR / "courses"
OUTLINES_DIR = SCRIPT_DIR / "data" / "outlines"


def _current_week():
    """Current term week from term_context; None if unavailable."""
    try:
        import term_context
        return term_context.current_term_week(term_context.load_context())
    except Exception:
        return None


def _load_dir(directory):
    """{CODE: course dict} for every readable <code>.json in directory."""
    found = {}
    if not directory.exists():
        return found
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            course = json.loads(path.read_text())
            found[course.get("code", path.stem).upper()] = course
        except Exception:
            continue
    return found


def _merge(outline, manual):
    """One course: the outline's weeks, with hand-entered weeks layered on top."""
    if not outline:
        return manual
    if not manual:
        return outline
    merged = {**outline, **{k: v for k, v in manual.items() if k != "schedule" and v}}
    weeks = {w["week"]: dict(w) for w in outline.get("schedule", [])}
    for w in manual.get("schedule", []):
        base = weeks.setdefault(w["week"], {"week": w["week"], "topics": [], "assessments": []})
        if w.get("topics"):
            base["topics"] = w["topics"]
        extra = [a for a in w.get("assessments", []) if a not in base.get("assessments", [])]
        base["assessments"] = base.get("assessments", []) + extra
    merged["schedule"] = [weeks[k] for k in sorted(weeks)]
    return merged


def load_courses():
    """
    Every course with a schedule, outline and hand-entered data merged.
    Returns a list of course dicts ([] on error).
    """
    outlines = _load_dir(OUTLINES_DIR)
    manual = _load_dir(COURSES_DIR)
    return [_merge(outlines.get(code), manual.get(code))
            for code in sorted(set(outlines) | set(manual))]


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
                    "activities":  entry.get("activities", []),
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

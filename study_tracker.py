"""
Jarvis — Study Tracker (academic intelligence)
==============================================
Turns the loaded course data (assessments in term_context.json + weekly topic
schedules in courses/*.json) into proactive *alerts* that keep Manav ahead of
the term, rather than just informational lists:

  1. Ramp-up warnings — for each assessment with a due date, an escalating nudge
     whose lead time scales with the assessment's weight (a 30% midterm starts
     warning earlier than a 5% class test). Tells him to START, not just that
     it's due.

  2. Revision triggers — for test-type assessments that declare `covers_weeks`,
     surfaces (a few days out) exactly which weeks/topics the test covers, pulled
     from the course schedule, so revision is targeted.

Pure-Python and read-only — no API cost. Defensive throughout: any missing data
yields no alerts rather than an error, so the morning brief never breaks.

Public API:
  get_assessment_alerts(today=None) -> list[str]
  get_revision_alerts(today=None)   -> list[str]
  get_academic_alerts(today=None)   -> list[str]   (both, ramp-up first)
  get_academic_alerts_block(today=None) -> str      (text block for the brief)
"""

import datetime


# Lead time (days before due) at which an assessment starts generating ramp-up
# alerts, by weight. Heavier assessments surface earlier.
def _lead_days(weight):
    """Days-before-due to begin warning, scaled by assessment weight."""
    if weight is None:
        return 10
    if weight >= 25:
        return 21
    if weight >= 10:
        return 14
    return 7


# Begin revision prompts this many days before a test (capped by its lead time).
_REVISION_LEAD = 10


def _today(today):
    return today or datetime.date.today()


def _load_assessments(today):
    """
    All non-submitted assessments with a parseable due date, each annotated with
    days_left. Returns [] on any failure. Mirrors term_context's own parsing.
    """
    try:
        import term_context
        ctx = term_context.load_context()
    except Exception:
        return []

    out = []
    for subject in ctx.get("subjects", []):
        for a in subject.get("assessments", []):
            if a.get("status") == "submitted":
                continue
            due_raw = a.get("due")
            if not due_raw:
                continue
            try:
                due = datetime.date.fromisoformat(due_raw)
            except Exception:
                continue
            out.append({
                "subject":      subject["code"],
                "name":         a["name"],
                "due":          due,
                "days_left":    (due - today).days,
                "weight":       a.get("weight"),
                "covers_weeks": a.get("covers_weeks") or [],
            })
    return out


def get_assessment_alerts(today=None):
    """Escalating ramp-up nudges for assessments inside their (weight-scaled) lead window."""
    today = _today(today)
    alerts = []
    for a in sorted(_load_assessments(today), key=lambda x: x["days_left"]):
        d = a["days_left"]
        if d < 0:
            continue
        lead = _lead_days(a["weight"])
        if d > lead:
            continue  # too far out to nag yet
        weight = f" [{a['weight']}%]" if a.get("weight") else ""
        tag = f"{a['subject']} {a['name']}{weight}"
        if d <= 2:
            alerts.append(f"🔴 {tag} — DUE in {d}d. Finalise and submit.")
        elif d <= 7:
            alerts.append(f"🟠 {tag} — {d}d out. Should be well underway.")
        else:
            alerts.append(f"🟡 {tag} — {d}d out. Start scaffolding now (high weight).")
    return alerts


def _topics_for_weeks(course_code, weeks):
    """Flat list of topic strings for a course across the given weeks ([] on failure)."""
    try:
        import course_schedule
    except Exception:
        return []
    topics = []
    for w in weeks:
        for row in course_schedule.get_week_topics(w, course=course_code):
            topics.extend(row.get("topics", []))
    return topics


def get_revision_alerts(today=None):
    """For tests declaring covers_weeks, surface coverage + topics a few days out."""
    today = _today(today)
    alerts = []
    for a in sorted(_load_assessments(today), key=lambda x: x["days_left"]):
        d = a["days_left"]
        weeks = a.get("covers_weeks")
        if not weeks or d < 0:
            continue
        lead = min(_REVISION_LEAD, _lead_days(a["weight"]))
        if d > lead:
            continue
        wk_lo, wk_hi = min(weeks), max(weeks)
        span = f"Week {wk_lo}" if wk_lo == wk_hi else f"Weeks {wk_lo}-{wk_hi}"
        topics = _topics_for_weeks(a["subject"], weeks)
        topic_str = ("; ".join(topics)) if topics else "see course schedule"
        alerts.append(
            f"📖 {a['subject']} {a['name']} in {d}d — revise {span}: {topic_str}"
        )
    return alerts


def get_drift_alerts(today=None, max_age_days=8):
    """
    Flag courses Manav reported falling behind in, from the most recent evening
    check-in (keepup_<CODE> == False, asked Sundays). Only honours a check-in
    within the last `max_age_days` so a stale 'no' doesn't nag indefinitely.
    """
    today = _today(today)
    try:
        from pathlib import Path
        import json
        data_dir = Path(__file__).parent / "data"
        files = sorted(data_dir.glob("checkin_*.json"))
        if not files:
            return []
        latest = files[-1]
        # Date is encoded in the filename: checkin_YYYY-MM-DD.json
        stamp = datetime.date.fromisoformat(latest.stem.replace("checkin_", ""))
        if (today - stamp).days > max_age_days:
            return []
        data = json.loads(latest.read_text())
    except Exception:
        return []

    alerts = []
    for key, val in data.items():
        if key.startswith("keepup_") and val is False:
            code = key[len("keepup_"):]
            detail = data.get(f"behind_{code}")
            tail = f" — {detail}" if detail else ""
            alerts.append(f"⚠️ Behind in {code}{tail}. Catch up before it compounds.")
    return alerts


def get_academic_alerts(today=None):
    """Drift flags, then ramp-up warnings, then revision triggers."""
    today = _today(today)
    return get_drift_alerts(today) + get_assessment_alerts(today) + get_revision_alerts(today)


def get_academic_alerts_block(today=None):
    """Text block for the morning brief. Empty string if nothing is live."""
    alerts = get_academic_alerts(today)
    if not alerts:
        return ""
    return "ACADEMIC ALERTS:\n" + "\n".join(f"  {a}" for a in alerts)


if __name__ == "__main__":
    import sys
    # Optional: pass an ISO date to simulate (e.g. python study_tracker.py 2026-06-24)
    sim = None
    if len(sys.argv) > 1:
        try:
            sim = datetime.date.fromisoformat(sys.argv[1])
        except Exception:
            pass
    day = sim or datetime.date.today()
    print(f"Academic alerts for {day:%A %d %b %Y}:\n")
    block = get_academic_alerts_block(day)
    print(block or "  (no alerts — nothing inside its lead window)")

"""
Jarvis — Term Context Module
==============================
Reads term_context.json and produces structured summaries
for injection into the morning brief, weekly review, and
weekly intelligence report.

Tracks:
  - Academic: subjects, upcoming assessments, week of term
  - Internships: pipeline status, pending actions, stale applications
  - Mentor: last contact, awaiting response, follow-up nudges
  - Extracurriculars / portfolio: active projects, goals
  - US Exchange: savings progress reminder

USAGE:
  from term_context import get_term_summary, get_flags

  summary = get_term_summary()   # dict — for injecting into briefs
  flags   = get_flags()          # list of urgent nudges for today

UPDATE WORKFLOW:
  - Add assessment due dates as soon as they appear on Moodle
  - Update internship statuses after each touchpoint
  - Update mentor.last_contact after every conversation
  - Bump term.week each Monday (or automate via start_date)
"""

import json
import datetime
from pathlib import Path

from json_store import file_lock, atomic_write_json

SCRIPT_DIR   = Path(__file__).parent
CONTEXT_FILE = SCRIPT_DIR / "term_context.json"


# ── Loader ────────────────────────────────────────────────────────────────────

def load_context() -> dict:
    try:
        return json.loads(CONTEXT_FILE.read_text())
    except Exception:
        return {}


def mutate_context(mutate_fn):
    """
    Race-safe read-modify-write for term_context.json: holds a lock across
    the full load -> mutate -> save cycle so a concurrent cron job or
    Telegram command can't clobber this write. `mutate_fn(ctx)` mutates
    the loaded dict in place.
    """
    with file_lock(CONTEXT_FILE):
        ctx = load_context()
        mutate_fn(ctx)
        atomic_write_json(CONTEXT_FILE, ctx)
        return ctx


# ── Week calculator ───────────────────────────────────────────────────────────

def current_term_week(ctx: dict) -> int:
    """Calculates current week of term from start_date."""
    try:
        start = datetime.date.fromisoformat(ctx["term"]["start_date"])
        today = datetime.date.today()
        if today < start:
            return 0
        return ((today - start).days // 7) + 1
    except Exception:
        return ctx.get("term", {}).get("week", 1)


# ── Upcoming assessments ──────────────────────────────────────────────────────

def get_upcoming_assessments(ctx: dict, days_ahead: int = 21) -> list:
    """Returns assessments due within the next N days, sorted by due date."""
    today     = datetime.date.today()
    cutoff    = today + datetime.timedelta(days=days_ahead)
    upcoming  = []

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
            if today <= due <= cutoff:
                days_left = (due - today).days
                upcoming.append({
                    "subject":   subject["code"],
                    "name":      a["name"],
                    "due":       due_raw,
                    "days_left": days_left,
                    "weight":    a.get("weight"),
                    "status":    a.get("status", "pending"),
                })

    return sorted(upcoming, key=lambda x: x["days_left"])


# ── Internship flags ──────────────────────────────────────────────────────────

def get_internship_flags(ctx: dict) -> list:
    """
    Returns nudges for stale applications or pending actions.
    Stale = no update in 14+ days and status is applied/OA_completed.
    """
    today = datetime.date.today()
    flags = []

    for app in ctx.get("internships", []):
        status = app.get("status", "")
        if status in ("offer", "rejected", "withdrawn"):
            continue

        last_raw = app.get("last_update")
        if last_raw:
            try:
                last = datetime.date.fromisoformat(last_raw)
                days_since = (today - last).days
                if days_since >= 14:
                    flags.append(
                        f"{app['company']} ({app['role']}) — no update in {days_since} days. "
                        f"Next action: {app.get('next_action', 'check status')}"
                    )
            except Exception:
                pass

        # Always surface OA completed waiting for interview
        if status == "OA_completed":
            flags.append(
                f"{app['company']} — OA completed, awaiting interview invite. "
                f"{app.get('next_action', '')}"
            )

    return flags


# ── Mentor flags ──────────────────────────────────────────────────────────────

def get_mentor_flags(ctx: dict) -> list:
    """Nudges for mentor follow-up."""
    today  = datetime.date.today()
    mentor = ctx.get("mentor", {})
    flags  = []

    if mentor.get("awaiting_response"):
        last_raw = mentor.get("last_contact")
        if last_raw:
            try:
                last      = datetime.date.fromisoformat(last_raw)
                days_since = (today - last).days
                if days_since >= 7:
                    flags.append(
                        f"Google Mentor — sent message {days_since} days ago (no reply yet). "
                        f"Consider following up. Last topic: {mentor.get('last_topic', '')}"
                    )
            except Exception:
                pass

    return flags


# ── Main summary ──────────────────────────────────────────────────────────────

def get_term_summary() -> dict:
    """
    Returns a structured dict for injection into morning brief / weekly review.
    """
    ctx  = load_context()
    week = current_term_week(ctx)

    return {
        "term_name":     ctx.get("term", {}).get("name", ""),
        "term_week":     week,
        "subjects":      [s["code"] for s in ctx.get("subjects", [])],
        "assessments":   get_upcoming_assessments(ctx, days_ahead=21),
        "internships":   ctx.get("internships", []),
        "mentor":        ctx.get("mentor", {}),
        "extracurriculars": ctx.get("extracurriculars", []),
        "portfolio_targets": ctx.get("portfolio_targets", []),
        "exchange_target": ctx.get("us_exchange", {}),
    }


def get_flags() -> list:
    """
    Returns all urgent nudges for today's brief.
    These get injected as action items into the morning brief.
    """
    ctx   = load_context()
    flags = []
    flags += get_internship_flags(ctx)
    flags += get_mentor_flags(ctx)

    # Assessment warnings (7 days)
    for a in get_upcoming_assessments(ctx, days_ahead=7):
        flags.append(
            f"{a['subject']} — {a['name']} due in {a['days_left']} day(s)"
            + (f" ({a['weight']}%)" if a.get('weight') else "")
        )

    return flags


# ── Updaters ──────────────────────────────────────────────────────────────────

def update_internship(company: str, **kwargs):
    """
    Update an internship entry by company name.
    Usage: update_internship("Canva", status="interview", next_action="Prep system design")
    """
    today = datetime.date.today().isoformat()

    def _mutate(ctx):
        for app in ctx.get("internships", []):
            if app["company"].lower() == company.lower():
                app.update(kwargs)
                app["last_update"] = today
                break

    mutate_context(_mutate)
    print(f"✅ Updated {company}")


def update_mentor(last_topic: str, awaiting: bool = True):
    """
    Log a mentor touchpoint.
    Usage: update_mentor("Discussed startup ideas", awaiting=True)
    """
    today = datetime.date.today().isoformat()

    def _mutate(ctx):
        ctx["mentor"]["last_contact"]      = today
        ctx["mentor"]["last_topic"]        = last_topic
        ctx["mentor"]["awaiting_response"] = awaiting

    mutate_context(_mutate)
    print(f"✅ Mentor updated — {last_topic}")


def mark_assessment_done(subject_code: str, assessment_name: str):
    """
    Mark an assessment as submitted.
    Usage: mark_assessment_done("COMP2511", "Assignment 1")
    """
    matched = {}

    def _mutate(ctx):
        for subject in ctx.get("subjects", []):
            if subject["code"].upper() == subject_code.upper():
                for a in subject["assessments"]:
                    if assessment_name.lower() in a["name"].lower():
                        a["status"] = "submitted"
                        matched["name"] = a["name"]
                        return

    mutate_context(_mutate)
    if matched:
        print(f"✅ Marked {subject_code} — {matched['name']} as submitted")
    else:
        print("❌ Assessment not found")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "--flags":
            flags = get_flags()
            if flags:
                print("🚩 Today's flags:")
                for f in flags:
                    print(f"   • {f}")
            else:
                print("✅ No flags today")

        elif cmd == "--summary":
            import pprint
            pprint.pprint(get_term_summary())

        elif cmd == "--update-internship":
            # python term_context.py --update-internship Canva status=interview
            company = sys.argv[2]
            kwargs  = {}
            for arg in sys.argv[3:]:
                k, v = arg.split("=", 1)
                kwargs[k] = v
            update_internship(company, **kwargs)

        elif cmd == "--update-mentor":
            topic   = sys.argv[2]
            waiting = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else True
            update_mentor(topic, waiting)

        elif cmd == "--done":
            subject = sys.argv[2]
            name    = sys.argv[3]
            mark_assessment_done(subject, name)

    else:
        # Default: show flags + upcoming assessments
        ctx  = load_context()
        week = current_term_week(ctx)
        print(f"\n📚 {ctx.get('term', {}).get('name', 'Term')} — Week {week}")

        upcoming = get_upcoming_assessments(ctx, days_ahead=21)
        if upcoming:
            print("\n📅 Upcoming assessments (21 days):")
            for a in upcoming:
                weight = f" [{a['weight']}%]" if a.get("weight") else ""
                print(f"   • {a['subject']} — {a['name']}{weight} → {a['due']} ({a['days_left']}d)")
        else:
            print("\n📅 No assessments due in the next 21 days (add dates to term_context.json)")

        flags = get_flags()
        if flags:
            print("\n🚩 Flags:")
            for f in flags:
                print(f"   • {f}")

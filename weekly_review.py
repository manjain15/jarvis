"""
Jarvis Weekly Review
=====================
Runs every Sunday morning at 7am (replaces the standard morning brief on Sundays).
Produces a full deep-dive review of the past week across all life areas,
plus a forward-looking focus for the week ahead.

SCHEDULE (already handled if you add to crontab):
  The weekly review runs AT 7am on Sundays via the existing morning brief cron.
  morning_brief.py detects Sunday and calls this instead.

OR run standalone:
  python weekly_review.py
  python weekly_review.py --preview /tmp/review.html   # render, don't send

The email uses the same dark design as the morning brief (email_template.py).
Python builds every section from data; one Claude call writes the read on the
week and next week's three priorities (JSON).
"""
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    from weekly_intelligence import get_intelligence_html
    INTELLIGENCE_AVAILABLE = True
except Exception:
    INTELLIGENCE_AVAILABLE = False

try:
    from term_context import get_term_summary, get_flags
    TERM_CONTEXT_AVAILABLE = True
except Exception:
    TERM_CONTEXT_AVAILABLE = False


import json
import datetime
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import anthropic
import config

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
DATA_DIR     = SCRIPT_DIR / "data"
MEMORY_DIR   = SCRIPT_DIR / "memory"
PROFILE_FILE = SCRIPT_DIR / "profile.md"
TOKEN_FILE   = SCRIPT_DIR / "token.json"

TIMEZONE = pytz.timezone(config.TIMEZONE)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_google_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tmp_file = TOKEN_FILE.parent / (TOKEN_FILE.name + ".tmp")
            tmp_file.write_text(creds.to_json())
            tmp_file.replace(TOKEN_FILE)
    return creds


# ── Data aggregation ──────────────────────────────────────────────────────────

def get_week_dates():
    """Returns the start and end of the current week (Mon-Sun)."""
    tz    = TIMEZONE
    today = datetime.datetime.now(tz).date()
    # This Monday
    days_since_monday = today.weekday()
    week_start = today - datetime.timedelta(days=days_since_monday)
    week_end   = week_start + datetime.timedelta(days=6)
    return week_start, week_end


def collect_health_data(week_start, week_end):
    """Collects 7 days of sleep, HR, and steps."""
    lines = ["HEALTH DATA (past 7 nights):"]
    try:
        from google_health import fetch_sleep, fetch_resting_hr, fetch_steps, get_access_token
        token = get_access_token()

        sleep_total   = 0
        sleep_nights  = 0
        below_target  = 0
        steps_total   = 0
        steps_days    = 0
        hr_readings   = []

        for i in range(7):
            date = week_start + datetime.timedelta(days=i)
            # Sleep ends the morning after
            sleep = fetch_sleep(token, date)
            hr    = fetch_resting_hr(token, date)
            steps = fetch_steps(token, date)

            day_label = date.strftime("%a %d %b")

            if sleep:
                sleep_total  += sleep["total_minutes"]
                sleep_nights += 1
                if sleep["vs_7hr"] < 0:
                    below_target += 1
                lines.append(
                    f"  {day_label}: {sleep['duration_str']}"
                    + (f" ({abs(sleep['vs_7hr'])}m below target)" if sleep["vs_7hr"] < 0 else " ✓")
                )
            else:
                lines.append(f"  {day_label}: no data")

            if hr and hr.get("resting_hr"):
                hr_readings.append(hr["resting_hr"])

            if steps and steps.get("steps"):
                steps_total += steps["steps"]
                steps_days  += 1

        # Summary
        if sleep_nights > 0:
            avg_sleep = sleep_total / sleep_nights
            lines.append(f"\n  Average sleep: {int(avg_sleep//60)}h {int(avg_sleep%60)}m over {sleep_nights} nights")
            lines.append(f"  Nights below 7hr target: {below_target}/{sleep_nights}")

        if hr_readings:
            avg_hr = sum(hr_readings) / len(hr_readings)
            lines.append(f"  Average resting HR: {avg_hr:.0f} bpm")

        if steps_days > 0:
            avg_steps = steps_total / steps_days
            lines.append(f"  Average daily steps: {avg_steps:,.0f}")

    except Exception as e:
        lines.append(f"  Health data unavailable: {e}")

    return "\n".join(lines)


def collect_workout_data(week_start, week_end):
    """Collects workout sessions for the week."""
    lines = ["WORKOUT DATA (past week):"]
    try:
        from hevy import fetch_recent_workouts, parse_workout_date, get_pplrul_day, find_pbs, PPLRUL

        workouts = fetch_recent_workouts(page_size=10)
        week_workouts = [
            w for w in workouts
            if parse_workout_date(w) and week_start <= parse_workout_date(w).date() <= week_end
        ]

        # Expected sessions this week
        expected_days = []
        for i in range(7):
            d     = week_start + datetime.timedelta(days=i)
            split = get_pplrul_day(d)
            if split != "Rest":
                expected_days.append((d, split))

        trained_dates = {parse_workout_date(w).date() for w in week_workouts}
        sessions_done = len(week_workouts)

        lines.append(f"  Sessions: {sessions_done}/{len(expected_days)} completed")

        # Day by day
        for d, split in expected_days:
            trained = d in trained_dates
            lines.append(f"  {d.strftime('%a %d %b')} ({split}): {'✓' if trained else '✗ missed'}")

        # PBs this week
        all_pbs = []
        for w in week_workouts:
            pbs = find_pbs(w)
            for ex, kg, reps in pbs:
                all_pbs.append(f"{ex} — {kg}kg × {reps}")

        if all_pbs:
            lines.append(f"\n  Personal bests this week:")
            for pb in all_pbs:
                lines.append(f"    🏆 {pb}")

    except Exception as e:
        lines.append(f"  Workout data unavailable: {e}")

    return "\n".join(lines)


def collect_finance_data(week_start, week_end):
    """Collects spending and savings data for the week."""
    lines = ["FINANCE DATA (past week):"]
    try:
        from finance_tracker import parse_stgeorge_csv, analyse_spending, analyse_savings, EVERYDAY_CSV

        if EVERYDAY_CSV.exists():
            from term_context import get_finance_goals
            goals    = get_finance_goals()
            txns    = parse_stgeorge_csv(EVERYDAY_CSV)
            spending = analyse_spending(txns, days=7)
            savings  = analyse_savings()

            lines.append(f"  Total spend: ${spending['total_spend']:.2f} (budget: ~${goals['weekly_budget']:.0f}/week)")

            for cat, amt in sorted(spending["category_totals"].items(), key=lambda x: -x[1]):
                if amt > 0:
                    lines.append(f"  {cat}: ${amt:.2f}")

            lines.append(f"\n  Savings: ${savings['total']:,.2f} of ${savings['goal']:,.0f} ({savings['pct']:.1f}%)")
            deadline_str = savings['deadline'].strftime('%B %Y')
            lines.append(
                f"  Projection: {savings['projected_date'].strftime('%B %Y')} "
                f"({'on track' if savings['on_track'] else f'behind — {deadline_str} deadline'})"
            )
        else:
            lines.append("  No finance CSV — export St. George CSV today.")

    except Exception as e:
        lines.append(f"  Finance data unavailable: {e}")

    return "\n".join(lines)


def collect_term_context():
    """Formats term/uni/internship/mentor context for the weekly review prompt."""
    if not TERM_CONTEXT_AVAILABLE:
        return "TERM CONTEXT: module not available."

    try:
        term  = get_term_summary()
        flags = get_flags()
    except Exception as e:
        return f"TERM CONTEXT: unavailable ({e})."

    lines = ["TERM / UNI / INTERNSHIP CONTEXT:"]

    if term.get("term_name"):
        lines.append(f"  Term: {term['term_name']} — Week {term.get('term_week','?')}")
    if term.get("subjects"):
        lines.append(f"  Subjects: {', '.join(term['subjects'])}")

    assessments = term.get("assessments") or []
    if assessments:
        lines.append("  Upcoming assessments (next 21 days):")
        for a in assessments:
            weight = f" [{a['weight']}%]" if a.get("weight") else ""
            lines.append(f"    • {a['subject']} — {a['name']}{weight} → due {a['due']} ({a['days_left']}d)")
    else:
        lines.append("  No assessment due dates filled in term_context.json yet.")

    internships = term.get("internships") or []
    if internships:
        lines.append("  Internship pipeline:")
        for app in internships:
            lines.append(
                f"    • {app.get('company')} ({app.get('role','')}) — {app.get('status','')} "
                f"| last update {app.get('last_update','?')} | next: {app.get('next_action','')}"
            )

    mentor = term.get("mentor") or {}
    if mentor:
        lines.append(
            f"  Mentor: {mentor.get('name','')} — last contact {mentor.get('last_contact','?')}, "
            f"topic: {mentor.get('last_topic','')}"
            + (" [awaiting reply]" if mentor.get("awaiting_response") else "")
        )

    portfolio_targets = term.get("portfolio_targets") or []
    if portfolio_targets:
        lines.append("  Portfolio targets this term:")
        for p in portfolio_targets:
            lines.append(f"    • {p}")

    exch = term.get("exchange_target") or {}
    if exch.get("savings_goal"):
        lines.append(
            f"  US Exchange: {exch['savings_deadline']} — "
            f"savings goal ${exch['savings_goal']:,.0f}"
        )

    if flags:
        lines.append("  Flags:")
        for f in flags:
            lines.append(f"    • {f}")

    return "\n".join(lines)


def collect_episodic_memory(week_start, week_end):
    """Pulls episodic memory entries from the past week."""
    episodic_path = MEMORY_DIR / "episodic.md"
    if not episodic_path.exists():
        return "MEMORY: No episodic memory yet."

    content = episodic_path.read_text()
    lines   = content.split("\n")

    relevant = []
    include  = False
    for line in lines:
        if line.startswith("### "):
            try:
                date_str   = line.replace("### ", "").strip()
                entry_date = datetime.datetime.strptime(date_str, "%A %d %b %Y").date()
                include    = week_start <= entry_date <= week_end
            except Exception:
                include = False
        if include:
            relevant.append(line)

    if relevant:
        return "EPISODIC MEMORY (this week):\n" + "\n".join(relevant)
    return "EPISODIC MEMORY: No entries for this week yet."


# ── Study plan ────────────────────────────────────────────────────────────────

def collect_study_plan():
    """
    Builds next week's study picture for the review: the coming week's per-course
    topics + the live assessment/revision alerts (reused from the morning brief).
    Empty string on any failure.
    """
    parts = []

    # Next week's topics, by current term week + 1.
    try:
        import term_context, course_schedule
        week = term_context.current_term_week(term_context.load_context())
        if week is not None and week >= 0:
            next_week = week + 1
            rows = course_schedule.get_week_topics(next_week)
            if rows:
                lines = [f"Next week (Week {next_week}) topics:"]
                for r in rows:
                    topics = "; ".join(r["topics"]) if r["topics"] else "—"
                    lines.append(f"  {r['code']}: {topics}")
                    if r["assessments"]:
                        lines.append(f"     ⚑ {' | '.join(r['assessments'])}")
                parts.append("\n".join(lines))
    except Exception:
        pass

    # Assessment ramp-up + revision + drift alerts (weight-scaled).
    try:
        import study_tracker
        block = study_tracker.get_academic_alerts_block()
        if block:
            parts.append(block)
    except Exception:
        pass

    return "\n\n".join(parts)


# ── Sections from data ────────────────────────────────────────────────────────
# Every number in the email comes from here, not from Claude. Each builder is
# a pure function over plain data so it can be tested without Google.

GYM_WORDS = {"done": "Done", "partial": "Partial", "missed": "Missed",
             "excused": "Excused", "unenforced": "Not tracked", "pending": "Open"}


def week_days(week_start, until):
    """[(date, ledger day or None)] from week_start up to and excluding until."""
    import ledger
    out, day = [], week_start
    while day < until:
        out.append((day, ledger.load_day(day)))
        day += datetime.timedelta(days=1)
    return out


def _find(day, item_id):
    for item in (day or {}).get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def gym_section(days):
    """Per-day gym rows plus the trial counts (planned, done, unexcused misses)."""
    rows, counts = [], {}
    for date, day in days:
        gym = _find(day, "gym")
        if gym is None:
            continue
        status = gym.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
        minutes = gym.get("logged_minutes")
        right = GYM_WORDS.get(status, status) + (f" · {minutes} min" if minutes else "")
        sub = f"skip: {gym['excuse']}" if gym.get("excuse") else None
        rows.append({"text": f"{date:%a} · {gym['title']}", "right": right, "sub": sub,
                     "miss": status == "missed"})
    planned = sum(n for s, n in counts.items() if s != "unenforced")
    done = counts.get("done", 0) + counts.get("partial", 0)
    if planned:
        rows.append({"text": "Sessions", "right": f"{done} / {planned}", "miss": done < planned})
        rows.append({"text": "Unexcused misses", "right": str(counts.get("missed", 0)),
                     "miss": counts.get("missed", 0) > 0})
    return rows


def steps_stats(days):
    """Days at target, average, and lowest day, from days with phone readings."""
    counted = []
    for _date, day in days:
        steps = _find(day, "steps")
        if steps and steps.get("count"):
            counted.append((steps["count"], steps.get("target", 10_000)))
    if not counted:
        return []
    at_target = sum(1 for c, t in counted if c >= t)
    average = sum(c for c, _t in counted) // len(counted)
    return [
        {"value": f"{at_target}/{len(counted)}", "label": "Days at target", "miss": at_target < len(counted)},
        {"value": f"{average:,}", "label": "Average", "miss": average < 10_000},
        {"value": f"{min(c for c, _t in counted):,}", "label": "Lowest", "miss": False},
    ]


def study_rows(done, targets, days_planned):
    """
    Hours done against the week's target per course and type. done is
    day_plan.minutes_done's {(course, type): minutes}. Under 75% is red.
    """
    rows = []
    for course, t in sorted(targets.items()):
        for kind, key, label in (("lecture", "lectures", "lectures"), ("study", "study", "study")):
            target = t.get(key, 0)
            if not target:
                continue
            hours = done.get((course, kind), 0) / 60
            rows.append({"text": f"{course} {label}", "right": f"{hours:g} / {target}h",
                         "miss": days_planned and hours < target * 0.75})
    return rows


def money_rows(spending, savings):
    """This week's spend against the budget, and savings progress."""
    rows = []
    if spending and spending.get("available"):
        total, budget = spending.get("total_spend", 0), spending.get("weekly_budget", 75)
        rows.append({"text": "Spent this week", "right": f"${total:,.0f} / ${budget:,.0f}",
                     "miss": total > budget})
    if savings and savings.get("available"):
        goal = savings.get("exchange_target", {})
        rows.append({"text": "Savings", "sub": "on track" if savings.get("on_track") else "behind",
                     "right": f"${savings['balance']:,.0f} / ${goal.get('savings_goal', 0):,.0f}",
                     "miss": not savings.get("on_track")})
    return rows


def next_week_rows(topics):
    """Next week's topics per course, from course_schedule.get_week_topics."""
    rows = []
    for r in topics:
        if r.get("topics"):
            rows.append({"text": r["code"], "sub": "; ".join(r["topics"])})
    return rows


UPKEEP_ROWS = [
    {"text": "Export the St. George CSVs", "sub": "everyday, savings, investing → finance/, then deploy/sync-finance-up.sh"},
    {"text": "Check term_context.json", "sub": "new due dates, internship status changes, mentor contact"},
]


# ── Claude: the read on the week ──────────────────────────────────────────────

def weekly_prompt(context, week_label):
    """The prompt for the note and next week's three priorities."""
    from voice import VOICE_RULES
    return f"""{VOICE_RULES}

This is the context for Manav's weekly review, week of {week_label}. The email
around your text already shows the numbers: gym day by day, steps, study hours
against target, money, debt and next week's topics. Do not repeat them as lists.

{context}

Write two things.

"note": at most 3 sentences. An honest read of the week: the biggest win, the
biggest miss, and the pattern behind them if the data shows one. Don't soften
bad numbers.

"priorities": exactly 3 short lines for next week, one each for training, study
and career. Concrete and checkable ("Send the Atlassian application by Wed"),
achievable but not easy.

Under 120 words in total. Return only JSON: {{"note": "...", "priorities": ["...", "...", "..."]}}"""


def parse_weekly(text):
    """{"note", "priorities"} from Claude's reply; empty when it can't be parsed."""
    from voice import strip_emoji
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        return {"note": "", "priorities": []}
    priorities = [strip_emoji(str(p))[:160] for p in (data.get("priorities") or []) if str(p).strip()]
    return {"note": strip_emoji(str(data.get("note") or ""))[:600], "priorities": priorities[:3]}


def generate_weekly_note(context, week_label):
    """One Claude call. Any failure returns empty fields so the review still sends."""
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": weekly_prompt(context, week_label)}],
        )
        return parse_weekly(message.content[0].text if message.content else "")
    except Exception as e:
        print(f"    ⚠️  Weekly note failed: {e} — sending the review without it")
        return {"note": "", "priorities": []}


def collect_trends():
    """Four-week trends from weekly_intelligence, as context text. No Claude call."""
    if not INTELLIGENCE_AVAILABLE:
        return ""
    parts = []
    try:
        import weekly_intelligence as wi
        for label, fn in (("Sleep trend", wi.collect_sleep_trend), ("Training trend", wi.collect_training_trend),
                          ("Finance trend", wi.collect_finance_trend),
                          ("Applications", wi.collect_application_history)):
            try:
                parts.append(f"{label}: {json.dumps(fn(), default=str)[:800]}")
            except Exception:
                continue
    except Exception:
        return ""
    return "4-WEEK TRENDS:\n" + "\n".join(parts)


# ── Send the review ───────────────────────────────────────────────────────────

def send_weekly_review(html_body, text_body, subject):
    """Sends the weekly review (HTML + plain text) via Gmail."""
    from google.oauth2.credentials import Credentials as _Creds
    from google.auth.transport.requests import Request as _Req
    _token_file = SCRIPT_DIR / "token.json"
    creds = _Creds.from_authorized_user_file(str(_token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(_Req())
        _tmp_file = _token_file.parent / (_token_file.name + ".tmp")
        _tmp_file.write_text(creds.to_json())
        _tmp_file.replace(_token_file)
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.YOUR_EMAIL
    msg["To"]      = config.YOUR_EMAIL
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅  Weekly review sent to {config.YOUR_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_review_sections(today, note):
    """(title, meta, sections) for the week ending yesterday (the review runs Sunday morning)."""
    week_start = today - datetime.timedelta(days=today.weekday())
    monday_run = today.weekday() == 0
    if monday_run:
        week_start -= datetime.timedelta(days=7)  # run on a Monday: review the week just gone
    days = week_days(week_start, today)

    import term_context
    ctx = term_context.load_context()
    week = term_context.current_term_week(ctx)
    if week and monday_run:
        week -= 1  # current_term_week counts today; the reviewed week is the one before
    plan = term_context.get_study_plan()
    targets = {c: t for c, t in plan.get("targets", {}).items() if t.get("study") or t.get("lectures")}

    try:
        import day_plan
        done = day_plan.minutes_done(week_start, today)
    except Exception:
        done = {}
    planned_days = sum(1 for _d, day in days if (day or {}).get("planned"))

    try:
        from agent_api import get_spending_payload, get_savings_payload
        spending = get_spending_payload(week_start, today - datetime.timedelta(days=1))
        savings = get_savings_payload()
    except Exception as e:
        print(f"⚠️   Finance unavailable for the review: {e}")
        spending = savings = None

    try:
        import course_schedule
        topics = course_schedule.get_week_topics((week or 0) + 1)
    except Exception:
        topics = []

    try:
        from morning_brief import due_rows, owed_rows
        latest = days[-1][1] if days else None
        owed = [{"text": o["label"], "right": o["value"], "miss": o.get("miss")} for o in owed_rows(latest, today)]
        due = [{"text": d["label"], "right": d["value"], "miss": d.get("miss")}
               for d in due_rows(term_context.get_upcoming_assessments(ctx, days_ahead=14))]
    except Exception as e:
        print(f"⚠️   Debt or deadlines unavailable for the review: {e}")
        owed, due = [], []

    sections = [
        {"label": "Gym", "rows": gym_section(days)},
        {"label": "Steps", "stats": steps_stats(days)},
        {"label": "Study", "rows": study_rows(done, targets, planned_days)},
        {"label": "Money", "rows": money_rows(spending, savings)},
        {"label": "Owed", "rows": owed},
        {"label": f"Week {(week or 0) + 1}", "rows": next_week_rows(topics)},
        {"label": "Due soon", "rows": due},
        {"label": "Next week", "rows": [{"text": p} for p in note.get("priorities", [])]},
        {"label": "Upkeep", "rows": UPKEEP_ROWS},
    ]
    last = today - datetime.timedelta(days=1)
    title = f"Week {week} review" if week else "Weekly review"
    meta = f"{week_start:%-d %b} – {last:%-d %b} · {ctx.get('term', {}).get('name', '')}".strip(" ·")
    return title, meta, sections


def run_weekly_review(preview=None):
    """Collect the week, write the note with Claude, render and send. preview writes to a file instead."""
    from email_template import render_sections
    tz = TIMEZONE
    now = datetime.datetime.now(tz)
    week_start, week_end = get_week_dates()
    week_label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"

    print(f"\n📋  Jarvis weekly review — {week_label}")
    print("    ─────────────────────────────────────")

    # Context for Claude's read on the week. The numbers in the email come from
    # build_review_sections, not from this text.
    print("💪  Collecting context...")
    try:
        from job_search import get_links_for_weekly_review
        job_links = get_links_for_weekly_review()
    except Exception:
        job_links = ""
    profile_text = PROFILE_FILE.read_text()[:2000] if PROFILE_FILE.exists() else ""
    context = "\n\n".join(x for x in (
        f"PROFILE:\n{profile_text}",
        collect_health_data(week_start, week_end),
        collect_workout_data(week_start, week_end),
        collect_finance_data(week_start, week_end),
        collect_episodic_memory(week_start, week_end),
        collect_term_context(),
        collect_study_plan(),
        collect_trends(),
        job_links,
    ) if x)

    print("✍️   Writing the note with Claude...")
    note = generate_weekly_note(context[:14000], week_label)

    title, meta, sections = build_review_sections(now.date(), note)
    html_body, text_body = render_sections(title, meta, note.get("note"), sections, f"jarvis · {now:%H:%M}")

    if preview:
        Path(preview).write_text(html_body)
        print(f"\n👀  Preview written to {preview} (not sent, not saved).\n")
        return

    try:
        from agent_api import save_latest_brief
        save_latest_brief(html_body, kind="weekly", label=week_label)
        print("💾  Review saved for the agent API")
    except Exception as e:
        print(f"⚠️   Could not save review for the agent API: {e}")

    print("📤  Sending review...")
    send_weekly_review(html_body, text_body, f"{title} · jarvis")
    print("\n✅  Weekly review sent. Check your inbox.\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis weekly review")
    parser.add_argument("--preview", metavar="FILE", help="Write the email to FILE instead of sending it")
    run_weekly_review(preview=parser.parse_args().preview)

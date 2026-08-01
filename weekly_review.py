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
            txns    = parse_stgeorge_csv(EVERYDAY_CSV)
            spending = analyse_spending(txns, days=7)
            savings  = analyse_savings()

            lines.append(f"  Total spend: ${spending['total_spend']:.2f} (budget: ~$75/week)")

            for cat, amt in sorted(spending["category_totals"].items(), key=lambda x: -x[1]):
                if amt > 0:
                    lines.append(f"  {cat}: ${amt:.2f}")

            lines.append(f"\n  Savings: ${savings['total']:,.2f} of $35,000 ({savings['pct']:.1f}%)")
            lines.append(
                f"  Projection: {savings['projected_date'].strftime('%B %Y')} "
                f"({'on track' if savings['on_track'] else 'behind — Jan 2027 deadline'})"
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
    if exch:
        lines.append(
            f"  US Exchange: {exch.get('target_date','?')} — "
            f"savings goal ${exch.get('savings_target','?')}"
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


# ── Generate the review ───────────────────────────────────────────────────────

def generate_weekly_review(health, workouts, finance, memory, job_links='', term='', study_plan=''):
    """Sends all week data to Claude and generates the full review."""
    tz         = TIMEZONE
    week_start, week_end = get_week_dates()
    week_label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
    next_week  = f"{(week_end + datetime.timedelta(days=1)).strftime('%d %b')} – {(week_end + datetime.timedelta(days=7)).strftime('%d %b %Y')}"

    profile_text  = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else ""
    semantic_path = MEMORY_DIR / "semantic.md"
    semantic_text = semantic_path.read_text() if semantic_path.exists() else ""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are Jarvis — Manav's personal AI assistant. Write his weekly review.
This is the most important brief of the week. Be thorough, direct, and genuinely useful.
You know him well — don't be generic, be specific to his life and goals.

WEEK: {week_label}

MANAV'S PROFILE:
{profile_text[:2000]}

SEMANTIC MEMORY:
{semantic_text[:1000]}

THIS WEEK'S DATA:

{health}

{job_links}

{workouts}

{finance}

{memory}

{term}

STUDY PLAN INPUT (next week's topics + live assessment/revision/drift alerts):
{study_plan if study_plan else "  No course schedule / assessment data available."}

Write a full weekly review in clean HTML. Return ONLY raw HTML, no markdown, no code fences.
Structure exactly like this:

<h2>Week of {week_label} — Manav's Review</h2>

<h3>📊 Week in numbers</h3>
[A tight scorecard — 4-6 key metrics from the week with actual numbers.
Sleep average, training sessions completed, spending total, savings progress, applications sent.
Format as short punchy lines. Be honest — don't soften bad numbers.]

<h3>💪 Health & training</h3>
[Full honest assessment of the week's health and training.
- Sleep: pattern, average, best and worst nights
- Training: sessions hit vs missed, any PBs, how the split went
- What the data says about how the body is doing
Be specific with numbers. Flag patterns, not just one-offs.]

<h3>📚 Uni & term progress</h3>
[Use TERM CONTEXT. State current term + week, the subjects he's enrolled in, and the closest
upcoming assessments (with weights + days remaining). If due dates are missing, tell him to update
term_context.json. Comment on whether his portfolio targets for the term are on track. 3-5 lines.]

<h3>📚 Study plan for next week</h3>
[Use STUDY PLAN INPUT. Lay out a concrete week-ahead study plan: which course topics are coming up, which assessments to ADVANCE (start or continue, weighted by importance — a 30% midterm outranks a 5% test), and what to REVISE for upcoming tests. Turn the alerts into specific actions — e.g. "Block two 90-min sessions for the MATH2901 midterm: re-derive the CLT and drill transformations of random variables." If a course was flagged as behind (drift alert), make catching it up the first priority. 4-6 lines.]

<h3>💼 Career & internship</h3>
[Honest assessment of career progress this week. Use the TERM CONTEXT internship pipeline.
- Reference each tracked company by name with its current status (Canva OA, Amazon applied, Dolby applied)
- Applications sent this week (number, companies)
- Google mentor — last_contact date, awaiting_response status, any follow-up needed
- LinkedIn, networking, portfolio work — and Jarvis as a demoable project
- Are they moving fast enough given the term window?
Be direct. If it was a bad week for career, say so clearly.]

<h3>💰 Finance</h3>
[Full financial picture.
- Spending breakdown vs ~$75/week budget
- Any unusual transactions?
- Savings progress toward $35k goal
- Pokemon reselling plan — still undefined?
- One concrete action to improve the financial situation]

<h3>🎯 Biggest win this week</h3>
[The single best thing that happened or that Manav did. Be specific.
If it was a bad week, find the one thing that went right.]

<h3>⚠️ Biggest miss this week</h3>
[The single most important thing that didn't happen or went wrong.
Be direct. Don't soften it. What should have happened that didn't?]

<h3>🗓️ Focus for next week ({next_week})</h3>
[Three specific, actionable priorities for next week — one per life area.
Not vague goals. Actual things: "Send 2 applications by Wednesday", 
"Hit all 5 training sessions", "Define the Pokemon reselling plan by Tuesday".
Make them achievable but not easy.]

<h3>🎯 Internship targets this week</h3>
[From the job links provided, pick the 2-3 most relevant companies to check this week.
List them with their URL. Tell Manav specifically what to do — "Open Canva careers page and
search for software intern roles. If anything is open, apply by Wednesday."
Also remind him to check Prosple and GradConnection. Keep it actionable, not generic.]

<h3>🔧 Jarvis upkeep — answer these</h3>
[Three direct questions for Manav to answer this week so term_context.json stays accurate:
  • Have you updated assessment due dates in term_context.json now that Moodle has them?
  • Any internship status changes this week (new OAs, interviews, offers, rejections)?
  • When did you last speak to your Google mentor — and is a follow-up overdue?
Format as a short bulleted list. Keep it punchy.]

<h3>⚡ One thing</h3>
[If Manav could only do ONE thing differently next week, what would it be?
One sentence. Sharp. Make it land.]

Keep the total under 600 words. Write like you know him — because you do.
No corporate speak. No "Great work!" Start directly with the HTML."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text.strip()


# ── Send the review ───────────────────────────────────────────────────────────

def send_weekly_review(review_html, week_label):
    """Sends the weekly review via Gmail."""
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

    import ssl, certifi
    full_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 15px;
      line-height: 1.7;
      color: #1e293b;
      max-width: 640px;
      margin: 0 auto;
      padding: 24px 20px;
      background: #f8fafc;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 32px 36px;
      border: 1px solid #e2e8f0;
    }}
    h2 {{
      font-size: 22px;
      font-weight: 700;
      color: #0f172a;
      margin: 0 0 24px;
      padding-bottom: 16px;
      border-bottom: 3px solid #1A56DB;
    }}
    h3 {{
      font-size: 16px;
      font-weight: 600;
      color: #1e293b;
      margin: 24px 0 10px;
    }}
    p, li {{ color: #334155; margin: 6px 0; line-height: 1.7; }}
    ul {{ padding-left: 20px; }}
    strong {{ color: #0f172a; }}
    .footer {{
      text-align: center;
      font-size: 12px;
      color: #94a3b8;
      margin-top: 20px;
    }}
  </style>
</head>
<body>
  <div class="card">
    {review_html}
  </div>
  <div class="footer">
    Jarvis Weekly Review · {week_label} · Sydney
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Jarvis Weekly Review — {week_label}"
    msg["From"]    = config.YOUR_EMAIL
    msg["To"]      = config.YOUR_EMAIL
    msg.attach(MIMEText(full_html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅  Weekly review sent to {config.YOUR_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_weekly_review():
    tz         = TIMEZONE
    week_start, week_end = get_week_dates()
    week_label = f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"

    print(f"\n📋  Jarvis weekly review — {week_label}")
    print("    ─────────────────────────────────────")

    print("💪  Collecting health data...")
    health = collect_health_data(week_start, week_end)

    print("🏋️   Collecting workout data...")
    workouts = collect_workout_data(week_start, week_end)

    print("💰  Collecting finance data...")
    finance = collect_finance_data(week_start, week_end)

    print("🧠  Loading episodic memory...")
    memory = collect_episodic_memory(week_start, week_end)

    print("🔍  Loading job links...")
    try:
        from job_search import get_links_for_weekly_review
        job_links = get_links_for_weekly_review()
    except Exception:
        job_links = ""

    print("📚  Loading term context...")
    term = collect_term_context()

    print("📚  Building study plan...")
    study_plan = collect_study_plan()

    print("✍️   Generating review with Claude...")
    review_html = generate_weekly_review(health, workouts, finance, memory, job_links, term, study_plan)

    print("📤  Sending review...")
    # Append intelligence report to the review
    if INTELLIGENCE_AVAILABLE:
        try:
            print("🧠  Generating intelligence report...")
            intelligence_html = get_intelligence_html()
            review_html = review_html + intelligence_html
            print("✅  Intelligence report added")
        except Exception as e:
            print(f"⚠️   Intelligence report failed: {e}")

    send_weekly_review(review_html, week_label)

    print(f"\n✅  Weekly review sent. Check your inbox.\n")


if __name__ == "__main__":
    run_weekly_review()

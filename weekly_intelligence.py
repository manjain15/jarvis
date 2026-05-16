"""
Jarvis — Weekly Intelligence Report
=====================================
Runs every Sunday alongside the weekly review.
Analyses trends across multiple weeks and surfaces patterns,
drifts, and specific recommendations.

Unlike the weekly review (which recaps the past week),
this looks ACROSS time to detect:
  - Multi-week trends (sleep degrading, savings drifting)
  - Anomalies (spending spike, training drop-off)
  - Time-sensitive flags (term starting, deadlines approaching)
  - Cross-domain insights (poor sleep → missed training → low output)

Sent as a separate email section after the weekly review.

CRON: Already covered by morning_brief.py Sunday 7am run.
"""

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import datetime
import json
from pathlib import Path
from collections import defaultdict

import pytz
import anthropic
import config

SCRIPT_DIR = Path(__file__).parent
TIMEZONE   = pytz.timezone(config.TIMEZONE)


# ── Data collectors ───────────────────────────────────────────────────────────

def collect_sleep_trend(weeks=4):
    """Returns weekly average sleep hours for the last N weeks."""
    try:
        from google_health import get_access_token, fetch_sleep
        tz    = TIMEZONE
        today = datetime.datetime.now(tz).date()
        token = get_access_token()

        weekly_avgs = []
        for w in range(weeks):
            week_start = today - datetime.timedelta(days=today.weekday() + 7 * w)
            week_end   = week_start + datetime.timedelta(days=6)
            daily      = []
            for i in range(7):
                d = week_start + datetime.timedelta(days=i)
                if d > today:
                    continue
                s = fetch_sleep(token, d)
                if s:
                    daily.append(s["total_minutes"] / 60)
            if daily:
                weekly_avgs.append({
                    "week":    week_start.strftime("%d %b"),
                    "avg_hrs": round(sum(daily) / len(daily), 1),
                    "days":    len(daily),
                })

        return list(reversed(weekly_avgs))  # oldest first
    except Exception as e:
        return []


def collect_training_trend(weeks=4):
    """Returns weekly training session counts for the last N weeks."""
    try:
        from hevy import fetch_recent_workouts, parse_workout_date
        tz         = TIMEZONE
        today      = datetime.datetime.now(tz).date()
        workouts   = fetch_recent_workouts(page_size=10)

        weekly = defaultdict(int)
        for w in workouts:
            dt = parse_workout_date(w)
            if not dt:
                continue
            d          = dt.date()
            week_start = d - datetime.timedelta(days=d.weekday())
            key        = week_start.strftime("%d %b")
            weekly[key] += 1

        # Build ordered list
        result = []
        for w in range(weeks):
            week_start = today - datetime.timedelta(days=today.weekday() + 7 * w)
            key        = week_start.strftime("%d %b")
            result.append({
                "week":     key,
                "sessions": weekly.get(key, 0),
                "target":   5,  # PPLRUL = 5 training days
            })

        return list(reversed(result))
    except Exception as e:
        return []


def collect_finance_trend(weeks=4):
    """Returns weekly spending totals for the last N weeks."""
    try:
        from finance_tracker import parse_stgeorge_csv, EVERYDAY_CSV
        if not EVERYDAY_CSV.exists():
            return []

        tz    = TIMEZONE
        today = datetime.datetime.now(tz).date()
        txns  = parse_stgeorge_csv(EVERYDAY_CSV)

        # Own account transfers to exclude
        own_accounts = ["0000206850220", "0000436436454", "0000444502124"]

        weekly = []
        for w in range(weeks):
            week_start = today - datetime.timedelta(days=today.weekday() + 7 * w)
            week_end   = week_start + datetime.timedelta(days=6)
            week_txns  = [
                t for t in txns
                if week_start <= t["date"] <= week_end
                and t["debit"] > 0
                and not any(acc in t["description"] for acc in own_accounts)
            ]
            total = sum(t["debit"] for t in week_txns)
            weekly.append({
                "week":    week_start.strftime("%d %b"),
                "spend":   round(total, 2),
                "budget":  75.0,
                "over":    round(total - 75.0, 2),
            })

        return list(reversed(weekly))
    except Exception as e:
        return []


def collect_mem0_insights():
    """Pulls recent memories across key life areas for trend analysis."""
    try:
        from jarvis_mem0 import _get_memory
        from jarvis_mem0 import USER_ID
        m = _get_memory()  # load once, reuse for all queries

        areas = {
            "career":   "internship application job interview",
            "health":   "sleep tired fatigue energy",
            "fitness":  "gym training workout missed",
            "finance":  "spending savings budget money",
            "pokemon":  "pokemon reselling sold inventory",
            "academic": "UNSW uni assignment exam",
        }
        results = {}
        for area, query in areas.items():
            try:
                r    = m.search(query, filters={"user_id": USER_ID}, limit=3)
                mems = [x["memory"] for x in r.get("results", []) if x.get("score", 0) > 0.5]
                if mems:
                    results[area] = " | ".join(mems)
            except Exception:
                pass
        return results
    except Exception as e:
        return {}


def collect_application_history():
    """Checks when applications were last logged in memory."""
    try:
        from jarvis_mem0 import search_memories
        result = search_memories("internship application applied", limit=5)
        return result or ""
    except Exception:
        return ""


# ── Intelligence generation ───────────────────────────────────────────────────

def generate_intelligence_report():
    """
    Generates the weekly intelligence report using Claude Sonnet.
    Returns formatted HTML string.
    """
    tz    = TIMEZONE
    now   = datetime.datetime.now(tz)
    today = now.date()

    print("  📊  Collecting multi-week trend data...")
    sleep_trend    = collect_sleep_trend(weeks=4)
    training_trend = collect_training_trend(weeks=4)
    finance_trend  = collect_finance_trend(weeks=4)
    mem0_insights  = collect_mem0_insights()
    app_history    = collect_application_history()

    # Format trend data
    def fmt_sleep(trend):
        if not trend:
            return "No sleep data available"
        lines = []
        for w in trend:
            bar    = "█" * int(w["avg_hrs"])
            status = "✓" if w["avg_hrs"] >= 7 else "⚠" if w["avg_hrs"] >= 6 else "✗"
            lines.append(f"  {w['week']}: {w['avg_hrs']}h {bar} {status}")
        return "\n".join(lines)

    def fmt_training(trend):
        if not trend:
            return "No training data available"
        lines = []
        for w in trend:
            pct    = int((w["sessions"] / w["target"]) * 100)
            status = "✓" if w["sessions"] >= 4 else "⚠" if w["sessions"] >= 2 else "✗"
            lines.append(f"  {w['week']}: {w['sessions']}/{w['target']} sessions {status}")
        return "\n".join(lines)

    def fmt_finance(trend):
        if not trend:
            return "No finance data available"
        lines = []
        for w in trend:
            status = "✓" if w["spend"] <= 75 else f"⚠ +${w['over']:.0f} over"
            lines.append(f"  {w['week']}: ${w['spend']:.0f} {status}")
        return "\n".join(lines)

    # Calculate trend directions
    def trend_dir(values):
        if len(values) < 2:
            return "stable"
        delta = values[-1] - values[-2]
        if delta > 0.5:
            return "improving"
        elif delta < -0.5:
            return "declining"
        return "stable"

    sleep_dir    = trend_dir([w["avg_hrs"] for w in sleep_trend]) if sleep_trend else "unknown"
    training_dir = trend_dir([w["sessions"] for w in training_trend]) if training_trend else "unknown"
    spend_dir    = trend_dir([-w["spend"] for w in finance_trend]) if finance_trend else "unknown"

    # Build the prompt
    profile_text = (SCRIPT_DIR / "profile.md").read_text() if (SCRIPT_DIR / "profile.md").exists() else ""

    prompt = f"""You are Jarvis, Manav's personal AI assistant. Generate a weekly intelligence report.
This is NOT a recap — it's a pattern analysis and recommendation engine.
Your job is to identify trends across 4 weeks of data and make specific, actionable recommendations.

TODAY: {now.strftime('%A, %d %B %Y')}

MANAV'S PROFILE (abbreviated):
{profile_text[:1000]}

4-WEEK SLEEP TREND ({sleep_dir}):
{fmt_sleep(sleep_trend)}

4-WEEK TRAINING TREND ({training_dir}):
{fmt_training(training_trend)}

4-WEEK SPENDING TREND ({spend_dir}):
{fmt_finance(finance_trend)}

MEMORY PATTERNS:
{json.dumps(mem0_insights, indent=2)[:1500] if mem0_insights else "No patterns loaded"}

APPLICATION HISTORY:
{app_history[:500] if app_history else "No recent application activity detected"}

Generate a report with these EXACT sections (use these headers):

## 🧠 Pattern Analysis
Identify the 2-3 most significant patterns across all data. Look for correlations
(e.g. poor sleep weeks → fewer training sessions). Be specific with numbers.

## ⚠️ Drift Alerts
Flag anything that has been trending in the wrong direction for 2+ weeks.
Only flag genuine, sustained drifts — not one-off bad weeks.

## 🎯 This Week's Priority Recommendations
Exactly 3 specific, actionable recommendations ranked by impact.
Each should reference the actual data. Not generic advice.

## 📅 Horizon Watch
Flag any time-sensitive items in the next 2-4 weeks based on Manav's context:
- UNSW term dates, exam periods
- Internship application deadlines (companies recruiting now)
- Savings milestones
- Any pattern that will compound negatively if not addressed

Be direct, specific, and honest. Reference actual numbers.
Max 400 words total. No fluff."""

    print("  🤖  Generating intelligence report...")
    client  = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text.strip()


def get_intelligence_html():
    """Returns the intelligence report as HTML for embedding in the weekly review email."""
    try:
        report_md = generate_intelligence_report()

        # Convert markdown to basic HTML
        html = report_md
        html = html.replace("## 🧠 Pattern Analysis",
            '<h3 style="color:#00e5ff;margin-top:24px">🧠 Pattern Analysis</h3>')
        html = html.replace("## ⚠️ Drift Alerts",
            '<h3 style="color:#ff9800;margin-top:24px">⚠️ Drift Alerts</h3>')
        html = html.replace("## 🎯 This Week\'s Priority Recommendations",
            '<h3 style="color:#00ff88;margin-top:24px">🎯 This Week\'s Priority Recommendations</h3>')
        html = html.replace("## 📅 Horizon Watch",
            '<h3 style="color:#7b61ff;margin-top:24px">📅 Horizon Watch</h3>')

        # Convert **bold** and bullet points
        import re
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
        html = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', html, flags=re.MULTILINE)
        html = html.replace('\n\n', '</p><p>')
        html = f'<p>{html}</p>'

        return f"""
<div style="margin-top:32px;padding:24px;background:#0a1628;border-radius:8px;border:1px solid #1a2a4a;">
  <h2 style="color:#00e5ff;margin:0 0 8px 0;font-size:18px;">
    🧠 Weekly Intelligence Report
  </h2>
  <p style="color:#8a9bb0;font-size:12px;margin:0 0 20px 0;">
    Pattern analysis across 4 weeks — trends, drifts, and recommendations
  </p>
  {html}
</div>
"""
    except Exception as e:
        return f'<p style="color:orange">Intelligence report failed: {e}</p>'


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🧠  Jarvis Weekly Intelligence Report\n")
    report = generate_intelligence_report()
    print(report)
    print()

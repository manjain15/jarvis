"""
Jarvis — Proactive Alerts
==========================
Sends push notifications to your iPhone via ntfy.sh when something needs attention.
Zero API cost — pure Python logic checking your data files.

ALERTS:
  - Training    — past 6pm, training day, no session logged yet
  - Sleep       — last night under 6 hours
  - Application — no internship applications in last 10 days
  - Savings     — monthly spending on track to exceed $300

SETUP:
  1. Install ntfy app on your iPhone (free, App Store)
  2. Subscribe to your private channel: open ntfy app → + → enter your channel name
  3. Add your channel name to config.py:
       NTFY_CHANNEL = "jarvis-manav-xxxx"  (make it unique and hard to guess)
  4. Test: python alerts.py --test

SCHEDULE (add to crontab):
  Check for alerts multiple times per day:
  0 12 * * * cd /Users/manavjain/jarvis && venv/bin/python alerts.py >> jarvis_alerts.log 2>&1
  0 18 * * * cd /Users/manavjain/jarvis && venv/bin/python alerts.py >> jarvis_alerts.log 2>&1
  0 20 * * * cd /Users/manavjain/jarvis && venv/bin/python alerts.py >> jarvis_alerts.log 2>&1
"""
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


import json
import datetime
import argparse
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote
import ssl
import certifi

import pytz
import config

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
DATA_DIR     = SCRIPT_DIR / "data"
FINANCE_DIR  = SCRIPT_DIR / "finance"
ALERTS_LOG   = DATA_DIR / "alerts_sent.json"  # prevents duplicate alerts

DATA_DIR.mkdir(exist_ok=True)
TIMEZONE = pytz.timezone(config.TIMEZONE)
SSL_CTX  = ssl.create_default_context(cafile=certifi.where())


# ── ntfy.sh notification ──────────────────────────────────────────────────────

def send_notification(title, message, priority="default", tags=""):
    """
    Sends a push notification via ntfy.sh to your iPhone.
    Priority: min, low, default, high, urgent
    Tags: emoji names like "muscle,warning" — shows as emoji on notification
    """
    channel = getattr(config, "NTFY_CHANNEL", "")
    if not channel:
        print(f"⚠️   NTFY_CHANNEL not set in config.py — notification not sent")
        print(f"     Would have sent: [{title}] {message}")
        return False

    url = f"https://ntfy.sh/{quote(channel)}"
    headers = {
        "Title":    title.encode("utf-8"),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags

    req = Request(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST"
    )
    try:
        with urlopen(req, context=SSL_CTX, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"⚠️   ntfy.sh failed: {e}")
        return False


# ── Alert deduplication ───────────────────────────────────────────────────────
# Prevents the same alert firing multiple times on the same day

def load_alerts_sent():
    if not ALERTS_LOG.exists():
        return {}
    try:
        return json.loads(ALERTS_LOG.read_text())
    except Exception:
        return {}


def save_alerts_sent(alerts):
    ALERTS_LOG.write_text(json.dumps(alerts, indent=2))


def already_sent_today(alert_key):
    """Returns True if this alert was already sent today."""
    alerts = load_alerts_sent()
    tz     = TIMEZONE
    today  = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    return alerts.get(alert_key, {}).get("date") == today


def mark_sent(alert_key):
    """Marks an alert as sent today."""
    alerts = load_alerts_sent()
    tz     = TIMEZONE
    today  = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    alerts[alert_key] = {"date": today, "time": datetime.datetime.now(tz).strftime("%H:%M")}
    save_alerts_sent(alerts)


# ── Alert 1: Training ─────────────────────────────────────────────────────────

def check_training_alert():
    """
    Fires if:
    - It's past 6pm
    - Today is a training day (not Rest)
    - No Hevy session logged today yet
    """
    tz    = TIMEZONE
    now   = datetime.datetime.now(tz)
    today = now.date()

    # Only check after 6pm
    if now.hour < 18:
        return False

    # Check if today is a rest day
    try:
        from hevy import get_pplrul_day
        split = get_pplrul_day(today)
        if split == "Rest":
            return False
    except Exception:
        return False

    # Check if already alerted today
    if already_sent_today("training"):
        return False

    # Check if trained today
    try:
        from hevy import fetch_recent_workouts, parse_workout_date
        workouts = fetch_recent_workouts(page_size=5)
        if workouts is None:
            return False  # Hevy API call failed — can't confirm, don't alert
        trained_today = any(
            parse_workout_date(w) and parse_workout_date(w).date() == today
            for w in workouts
        )
        if trained_today:
            return False
    except Exception:
        return False  # Can't check — don't alert

    # Fire the alert
    hour = now.hour
    urgency = "high" if hour >= 20 else "default"
    message = (
        f"It's {now.strftime('%-I:%M %p')} and your {split} session hasn't been logged. "
        f"Gym closes eventually. Get it done before you talk yourself out of it."
    )
    sent = send_notification(
        title=f"🏋️ {split} day — session not logged",
        message=message,
        priority=urgency,
        tags="muscle"
    )
    if sent:
        mark_sent("training")
        print(f"✅  Training alert sent ({split} day, {now.strftime('%-I:%M %p')})")
    return sent


# ── Alert 2: Sleep ────────────────────────────────────────────────────────────

def check_sleep_alert():
    """
    Fires in the morning if last night's sleep was under 6 hours.
    Supplements the morning brief with a direct notification.
    Only fires between 7am and 10am.
    """
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)

    # Only fire 7am–10am
    if not (7 <= now.hour < 10):
        return False

    if already_sent_today("sleep"):
        return False

    try:
        from google_health import fetch_sleep, get_access_token
        token     = get_access_token()
        yesterday = now.date() - datetime.timedelta(days=1)
        sleep     = fetch_sleep(token, yesterday)

        if not sleep:
            return False

        # Only alert if under 6 hours (360 mins) — severe deficit
        if sleep["total_minutes"] >= 360:
            return False

        hours   = sleep["total_minutes"] // 60
        minutes = sleep["total_minutes"] % 60
        deficit = 420 - sleep["total_minutes"]  # vs 7hr target

        message = (
            f"You got {hours}h {minutes}m last night — {deficit} minutes short of your 7hr target. "
            f"That's a real deficit. Protect your focus today and get to bed early tonight."
        )
        sent = send_notification(
            title=f"😴 {hours}h {minutes}m sleep — below threshold",
            message=message,
            priority="high",
            tags="zzz,warning"
        )
        if sent:
            mark_sent("sleep")
            print(f"✅  Sleep alert sent ({hours}h {minutes}m)")
        return sent

    except Exception as e:
        print(f"⚠️   Sleep alert check failed: {e}")
        return False


# ── Alert 3: Application drought ─────────────────────────────────────────────

def check_application_drought():
    """
    Fires if no internship applications have been logged in the last 10 days.
    Checks semantic memory for application status.
    Fires once per week (not daily — would get annoying).
    """
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)

    # Only fire on Monday and Thursday afternoons (twice a week)
    if now.weekday() not in (0, 3):  # Mon=0, Thu=3
        return False

    if now.hour < 12:
        return False

    if already_sent_today("applications"):
        return False

    # Check semantic memory for last application date
    try:
        semantic_path = SCRIPT_DIR / "memory" / "semantic.md"
        if not semantic_path.exists():
            return False

        semantic = semantic_path.read_text().lower()

        # Look for signs of recent applications
        recent_signals = [
            "application sent",
            "applied to",
            "sent application",
            "interview",
            "submitted",
        ]

        # Check if any recent signal is in the last 10 days context
        # Simple heuristic: look for current month mentions with application keywords
        current_month = now.strftime("%B").lower()
        has_recent = any(
            signal in semantic and current_month in semantic
            for signal in recent_signals
        )

        # Also check episodic memory for application mentions this month
        episodic_path = SCRIPT_DIR / "memory" / "episodic.md"
        if episodic_path.exists():
            episodic = episodic_path.read_text().lower()
            # Look for application mentions in entries from last 10 days
            lines       = episodic.split("\n")
            cutoff      = (now.date() - datetime.timedelta(days=10))
            in_window   = False
            found_app   = False
            for line in lines:
                if line.startswith("### "):
                    try:
                        date_str   = line.replace("### ", "").strip()
                        entry_date = datetime.datetime.strptime(date_str, "%A %d %b %Y").date()
                        in_window  = entry_date >= cutoff
                    except Exception:
                        pass
                if in_window and any(s in line.lower() for s in ["appli", "interview", "submitted"]):
                    found_app = True
                    break

            if found_app:
                return False

        if has_recent:
            return False

        # No recent applications found — fire alert
        days_str = "10 days" if now.weekday() == 0 else "this week"
        message = (
            f"No internship applications logged in the last {days_str}. "
            f"Pick one company from your target list and apply today — Canva, Amazon, or Anthropic."
        )
        sent = send_notification(
            title="📋 Application drought — {days_str} without applying",
            message=message,
            priority="high",
            tags="briefcase,warning"
        )
        if sent:
            mark_sent("applications")
            print(f"✅  Application drought alert sent")
        return sent

    except Exception as e:
        print(f"⚠️   Application alert check failed: {e}")
        return False


# ── Alert 4: Savings drift ────────────────────────────────────────────────────

def check_savings_drift():
    """
    Fires mid-month if spending is on track to exceed $300/month.
    Only fires once per month, between the 10th and 20th.
    """
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)

    # Only fire between 10th and 20th of month, at noon
    if not (10 <= now.day <= 20):
        return False

    if now.hour < 12:
        return False

    alert_key = f"savings_drift_{now.strftime('%Y_%m')}"
    if already_sent_today(alert_key):
        return False

    try:
        from finance_tracker import parse_stgeorge_csv, EVERYDAY_CSV
        import datetime as dt

        if not EVERYDAY_CSV.exists():
            return False

        txns = parse_stgeorge_csv(EVERYDAY_CSV)

        # Use last 7 days average to project — avoids one-off weeks skewing it
        today      = now.date()
        week_start = today - datetime.timedelta(days=7)
        week_txns  = [
            t for t in txns
            if t["date"] >= week_start
            and t["debit"] > 0
            and "internet withdrawal" not in t["description"].lower()
            and t["category"] not in ["Entertainment"]  # exclude one-off entertainment
        ]
        spent_7_days = sum(t["debit"] for t in week_txns)
        # Project to monthly based on last 7 days (excluding entertainment blowouts)
        projected = (spent_7_days / 7) * 30

        budget = 300.0

        # Only alert if projected to be 50%+ over budget (not 20%)
        if projected <= budget * 1.5:
            return False

        over_by = projected - budget
        message = (
            f"${spent_so_far:.0f} spent so far this month. "
            f"At this rate you'll spend ${projected:.0f} by month end — "
            f"${over_by:.0f} over your $300 budget. "
            f"Check your spending and cut where you can."
        )
        sent = send_notification(
            title=f"💸 Spending drift — ${projected:.0f} projected this month",
            message=message,
            priority="default",
            tags="money_with_wings,warning"
        )
        if sent:
            mark_sent(alert_key)
            print(f"✅  Savings drift alert sent (projected ${projected:.0f})")
        return sent

    except Exception as e:
        print(f"⚠️   Savings drift check failed: {e}")
        return False


# ── Run all alerts ────────────────────────────────────────────────────────────



def check_pattern_alerts():
    """
    Memory-aware proactive alerts. Uses Mem0 to detect patterns
    across recent history and surfaces actionable nudges.
    Runs once daily (morning check).
    """
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)

    # Only run in morning (7am-11am)
    if not (7 <= now.hour < 11):
        return False

    fired = False

    try:
        from jarvis_mem0 import search_memories
        import anthropic

        # Search for recent patterns across key life areas
        pattern_queries = [
            ("internship applications", "career", 14),
            ("sleep deprivation poor sleep", "health", 7),
            ("training missed skipped", "fitness", 7),
            ("savings spending budget", "finance", 14),
            ("pokemon reselling inventory", "reselling", 21),
        ]

        insights = []
        for query, area, days in pattern_queries:
            result = search_memories(query, limit=3)
            if result:
                insights.append(f"[{area.upper()}] {result}")

        if not insights:
            return False

        # Ask Claude to identify the most important pattern to flag today
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = f"""You are Jarvis, Manav's personal AI assistant.
Review these memory snippets and identify the SINGLE most important pattern
that needs Manav's attention today. Be specific and actionable.

Today: {now.strftime('%A, %d %B %Y')}

MEMORY PATTERNS:
{chr(10).join(insights[:5])}

Rules:
- Only flag something genuinely concerning or time-sensitive
- If everything looks fine, respond with just: CLEAR
- Otherwise respond with a short push notification (max 100 chars)
  Format: ALERT: <title> | <message>
  Example: ALERT: Internship drought | 12 days since last application. Term starts soon."""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()

        if text.startswith("ALERT:"):
            parts   = text.replace("ALERT:", "").strip().split("|")
            title   = parts[0].strip() if parts else "Jarvis"
            message = parts[1].strip() if len(parts) > 1 else text
            send_notification(title, message, priority="default", tags=["brain"])
            print(f"    🧠  Pattern alert: {title} — {message}")
            fired = True

    except Exception as e:
        print(f"    ⚠️   Pattern alert check failed: {e}")

    return fired

def run_all_alerts():
    """Runs all alert checks. Called by cron 3x daily."""
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)
    print(f"\n🔔  Jarvis alerts check — {now.strftime('%A %d %b, %-I:%M %p')}")

    fired = 0

    if check_sleep_alert():
        fired += 1

    if check_training_alert():
        fired += 1

    if check_application_drought():
        fired += 1

    if check_savings_drift():
        fired += 1

    if check_pattern_alerts():
        fired += 1

    if fired == 0:
        print("    All clear — no alerts needed")
    else:
        print(f"    {fired} alert(s) sent")

    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Proactive Alerts")
    parser.add_argument("--test", action="store_true", help="Send a test notification")
    parser.add_argument("--force", action="store_true", help="Run all checks ignoring deduplication")
    args = parser.parse_args()

    if args.test:
        print("\n🔔  Sending test notification...\n")
        sent = send_notification(
            title="✅ Jarvis alerts working",
            message="Your proactive alerts are set up correctly. You'll be notified when something needs attention.",
            priority="default",
            tags="white_check_mark"
        )
        if sent:
            print("✅  Test notification sent — check your phone\n")
        else:
            print("❌  Failed — check NTFY_CHANNEL in config.py\n")

    elif args.force:
        # Clear today's sent log and run everything
        alerts = load_alerts_sent()
        tz     = TIMEZONE
        today  = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        for key in list(alerts.keys()):
            if alerts[key].get("date") == today:
                del alerts[key]
        save_alerts_sent(alerts)
        print("🔄  Cleared today's alerts — running all checks...\n")
        run_all_alerts()

    else:
        run_all_alerts()

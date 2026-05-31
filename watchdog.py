"""
Jarvis — Watchdog (observability)
=================================
The rest of Jarvis is deliberately fail-silent (optional-import everywhere) so a
broken integration can't take down the morning brief. The cost of that is that
breakage is INVISIBLE — a daemon can die or a timer's job can start exiting
non-zero and nothing tells Manav. This watchdog turns silent failure into a
pushed Telegram alert.

SCOPE (be honest about it):
  Catches PROCESS-level failure — the always-on daemon being down, a timer's
  service having failed its last run, a timer no longer scheduled, or the disk
  filling up. It does NOT catch CONTENT bugs (e.g. a feed 403ing but the script
  still exiting 0, or a section rendering empty) — those are what a test suite is
  for. Don't mistake a green watchdog for "everything is correct".

BEHAVIOUR:
  Default run  → checks everything; messages Telegram ONLY if something is wrong
                 (no-news-is-good-news, so alerts stay meaningful and rare).
  --daily      → always sends one message: the all-green heartbeat, or the
                 problems. Run once a day so that silence is trustworthy rather
                 than ambiguous ("did it not run, or was all fine?").
  --print      → print the report to stdout, send nothing (for manual checks).

Runs on its own systemd timer. If the watchdog itself fails, systemd marks
jarvis-watchdog.service failed — visible via /status.
"""

import sys
import shutil
import subprocess

# Units the watchdog checks. Keep in sync with deploy/systemd/.
ALWAYS_ON     = ["jarvis-telegram"]            # .service must be active
TIMER_UNITS   = [
    "jarvis-morningbrief", "jarvis-alerts", "jarvis-gmail",
    "jarvis-memory", "jarvis-jobsearch",
]
DISK_WARN_PCT = 90  # alert if the root filesystem is at/over this % used


def _systemctl(*args):
    """Run a read-only systemctl query; return stripped stdout, '' on error."""
    try:
        out = subprocess.run(["systemctl", *args], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def check_problems():
    """
    Return a list of human-readable problem strings. Empty list = all healthy.
    """
    problems = []

    # Always-on services: the .service itself must be active.
    for unit in ALWAYS_ON:
        state = _systemctl("is-active", f"{unit}.service")
        if state != "active":
            problems.append(f"❌ {unit} service is {state or 'unknown'} (should be active)")

    # Timer-driven units: the timer must be active, and the last service run
    # must not have failed.
    for unit in TIMER_UNITS:
        timer_state = _systemctl("is-active", f"{unit}.timer")
        if timer_state != "active":
            problems.append(f"❌ {unit} timer is {timer_state or 'unknown'} (not scheduled)")
        if _systemctl("is-failed", f"{unit}.service") == "failed":
            problems.append(f"⚠️ {unit} — last run FAILED")

    # Disk: a full disk silently breaks writes (logs, data, mem0).
    try:
        usage = shutil.disk_usage("/")
        pct = round(usage.used / usage.total * 100)
        if pct >= DISK_WARN_PCT:
            problems.append(f"⚠️ Disk at {pct}% (warn ≥{DISK_WARN_PCT}%)")
    except Exception:
        problems.append("⚠️ Could not read disk usage")

    return problems


def _notify(text):
    """Push a message to Telegram. Returns True on success."""
    try:
        from jarvis_telegram import send_message
        return bool(send_message(text))
    except Exception as e:
        # Last resort: surface to stderr so it lands in the journal.
        print(f"watchdog: failed to send Telegram alert: {e}", file=sys.stderr)
        return False


def run(daily=False, print_only=False):
    """Check health; alert per the chosen mode. Returns the problem list."""
    problems = check_problems()

    if problems:
        report = "🚨 Jarvis watchdog — issues detected:\n" + "\n".join(f"  {p}" for p in problems)
    else:
        report = "✅ Jarvis watchdog — all systems healthy."

    if print_only:
        print(report)
    elif problems or daily:
        # Default mode stays quiet when healthy; --daily always reports.
        _notify(report)

    return problems


if __name__ == "__main__":
    daily      = "--daily" in sys.argv
    print_only = "--print" in sys.argv
    found = run(daily=daily, print_only=print_only)
    # Non-zero exit on problems so systemd/journald also reflect the state.
    sys.exit(1 if found else 0)

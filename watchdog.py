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

  jarvis-agent-api is also probed over HTTP (127.0.0.1:5557/health with the
  bearer token). A unit that is "active" but not answering still alerts.

  The same problem set is alerted once. Later runs stay quiet until the set
  changes (or the run is --daily, which always sends). A recovery clears
  that memory so the next failure alerts again.

Runs on its own systemd timer. If the watchdog itself fails, systemd marks
jarvis-watchdog.service failed — visible via /status.
"""

import hashlib
import json
import os
import sys
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# Units the watchdog checks. Keep in sync with deploy/systemd/.
ALWAYS_ON     = ["jarvis-telegram", "jarvis-agent-api"]  # .service must be active
TIMER_UNITS   = [
    "jarvis-morningbrief", "jarvis-alerts", "jarvis-gmail",
    "jarvis-memory", "jarvis-jobsearch",
]
DISK_WARN_PCT = 90  # alert if the root filesystem is at/over this % used
AGENT_API_PORT = 5557
STATE_FILE = Path(__file__).resolve().parent / "data" / "watchdog_state.json"
ENV_FILE = Path(__file__).resolve().parent / ".env"


def _systemctl(*args):
    """Run a read-only systemctl query; return stripped stdout, '' on error."""
    try:
        out = subprocess.run(["systemctl", *args], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def _dotenv_value(key, env_path=None):
    """Return one key from the environment or the repo .env. Empty if absent."""
    value = os.environ.get(key, "").strip().strip('"').strip("'")
    if value:
        return value
    path = Path(env_path) if env_path is not None else ENV_FILE
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if name.strip() != key:
            continue
        return raw_value.strip().strip('"').strip("'")
    return ""


def agent_api_token(env_path=None):
    """
    Bearer token that can call /health.

    Prefers JARVIS_API_TOKEN. Otherwise the first token in JARVIS_API_TOKENS,
    because every bot allowlist includes /health.
    """
    legacy = _dotenv_value("JARVIS_API_TOKEN", env_path)
    if legacy:
        return legacy
    raw = _dotenv_value("JARVIS_API_TOKENS", env_path)
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        token = part.split(":", 1)[1].strip()
        if token:
            return token
    return ""


def agent_api_health_url():
    """Loopback health URL. JARVIS_API_PORT overrides the default port."""
    port = _dotenv_value("JARVIS_API_PORT") or str(AGENT_API_PORT)
    return f"http://127.0.0.1:{port}/health"


def probe_agent_api(opener=None, token=None, url=None):
    """
    GET /health with the bearer token.

    Returns a stable problem string, or None when the body is {"ok": true}.
    Exception text is not included, so a persistent outage stays identical
    across runs and the alert dedupe can recognise it.
    """
    token = agent_api_token() if token is None else token
    url = url or agent_api_health_url()
    if not token:
        return "❌ jarvis-agent-api /health probe has no API token"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=5) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read(4096)
    except urllib.error.HTTPError as exc:
        return f"❌ jarvis-agent-api /health returned HTTP {exc.code}"
    except Exception:
        return "❌ jarvis-agent-api /health probe failed"
    if status != 200:
        return f"❌ jarvis-agent-api /health returned HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return "❌ jarvis-agent-api /health returned invalid JSON"
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return "❌ jarvis-agent-api /health did not report ok"
    return None


def problem_fingerprint(problems):
    """Stable id for one set of problem lines, order preserved."""
    text = "\n".join(problems)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_alert_state(path):
    """Return the last alerted fingerprint, or '' when there is no state."""
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("fingerprint") or "")


def save_alert_state(path, fingerprint):
    """Remember which problem set was last handled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint}) + "\n", encoding="utf-8")
    tmp.replace(path)


def should_notify(problems, daily, previous_fingerprint):
    """
    Decide whether this run should push a Telegram message.

    Returns (notify, fingerprint). Default runs stay quiet when healthy and
    when the same problems were already alerted. --daily always sends.
    """
    fingerprint = problem_fingerprint(problems)
    if daily:
        return True, fingerprint
    if not problems:
        return False, fingerprint
    if fingerprint == previous_fingerprint:
        return False, fingerprint
    return True, fingerprint


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

    # Active is not the same as answering. /health needs the bearer token.
    probe = probe_agent_api()
    if probe:
        problems.append(probe)

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

    notify, fingerprint = should_notify(
        problems, daily, load_alert_state(STATE_FILE),
    )
    if print_only:
        print(report)
    elif notify:
        # Only remember a set we actually delivered, so a failed send retries.
        if _notify(report):
            save_alert_state(STATE_FILE, fingerprint)
    else:
        save_alert_state(STATE_FILE, fingerprint)
        if problems:
            print("watchdog: alert suppressed (unchanged since last alert)")

    return problems


if __name__ == "__main__":
    daily      = "--daily" in sys.argv
    print_only = "--print" in sys.argv
    found = run(daily=daily, print_only=print_only)
    # Non-zero exit on problems so systemd/journald also reflect the state.
    sys.exit(1 if found else 0)

"""
Jarvis — Google Health API Integration
=======================================
Fetches sleep, heart rate, and activity data from the Google Health API.
Uses health_token.json (separate from token.json) due to a Google bug
where mixing health + consumer scopes causes 403 errors.

SETUP: python google_health.py --setup
TEST:  python google_health.py --test
"""

import json
import ssl
import certifi
import datetime
import argparse
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

import config

# ── SSL fix for Mac ───────────────────────────────────────────────────────────
_ssl_context = ssl.create_default_context(cafile=certifi.where())

def _urlopen_ssl(req):
    return urlopen(req, context=_ssl_context)

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR        = Path(__file__).parent
CREDS_FILE        = SCRIPT_DIR / "credentials.json"
HEALTH_TOKEN_FILE = SCRIPT_DIR / "health_token.json"

# ── Constants ─────────────────────────────────────────────────────────────────
API_BASE = "https://health.googleapis.com/v4/users/me"
TIMEZONE = pytz.timezone(config.TIMEZONE)

HEALTH_SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

# ── PPLRUL ────────────────────────────────────────────────────────────────────
PPLRUL      = ["Push", "Pull", "Legs", "Rest", "Upper", "Sharms", "Rest"]
ANCHOR_DATE = datetime.date(2026, 5, 13)
ANCHOR_DAY  = 2  # Legs on May 13 (Wednesday) — Push starts Monday

def get_pplrul_day(date=None):
    if date is None:
        date = datetime.datetime.now(TIMEZONE).date()
    delta = (date - ANCHOR_DATE).days
    return PPLRUL[(ANCHOR_DAY + delta) % len(PPLRUL)]


# ── Auth ──────────────────────────────────────────────────────────────────────

def setup_health_auth():
    """First-time OAuth for health-only scopes. Run once."""
    if not CREDS_FILE.exists():
        raise RuntimeError("credentials.json not found.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, HEALTH_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(HEALTH_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\n✅  Health token saved. Run: python google_health.py --test\n")


def get_access_token():
    """
    Returns a valid access token.
    Priority:
      1. health_token.json file (local Mac)
      2. GOOGLE_HEALTH_TOKEN environment variable (Render/production)
    """
    import os
    import json as _json
    import tempfile

    token_data = None

    if HEALTH_TOKEN_FILE.exists():
        # Local — use the file directly
        creds = Credentials.from_authorized_user_file(HEALTH_TOKEN_FILE, HEALTH_SCOPES)
    else:
        # Production — read from environment variable
        token_json = os.environ.get("GOOGLE_HEALTH_TOKEN", "")
        if not token_json:
            raise RuntimeError(
                "No health token found. Set GOOGLE_HEALTH_TOKEN env var or run --setup."
            )
        # Write to a temp file so Credentials can read it
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(token_json)
            tmp_path = f.name
        creds = Credentials.from_authorized_user_file(tmp_path, HEALTH_SCOPES)
        import os as _os
        _os.unlink(tmp_path)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            # Save refreshed token back if file exists
            if HEALTH_TOKEN_FILE.exists():
                with open(HEALTH_TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
            # Note: on Render, refreshed token isn't persisted — token will
            # refresh again on next request (tokens last 1 hour)
        else:
            raise RuntimeError(
                "Health token expired. Re-run setup locally and update GOOGLE_HEALTH_TOKEN."
            )

    return creds.token


# ── API helpers ───────────────────────────────────────────────────────────────

def _get(endpoint, token):
    url = f"{API_BASE}/{endpoint}"
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with _urlopen_ssl(req) as r:
            return json.loads(r.read())
    except HTTPError as e:
        print(f"\u26a0\ufe0f   Health API error ({endpoint[:60]}): {e.code} — {e.read().decode()[:150]}")
        return None
    except URLError as e:
        print(f"\u26a0\ufe0f   Health API error ({endpoint[:60]}): {e}")
        return None


def _post(endpoint, body, token):
    url  = f"{API_BASE}/{endpoint}"
    data = json.dumps(body).encode()
    req  = Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with _urlopen_ssl(req) as r:
            return json.loads(r.read())
    except HTTPError as e:
        print(f"\u26a0\ufe0f   Health API error ({endpoint[:60]}): {e.code} — {e.read().decode()[:150]}")
        return None
    except URLError as e:
        print(f"\u26a0\ufe0f   Health API error ({endpoint[:60]}): {e}")
        return None


# ── Sleep ─────────────────────────────────────────────────────────────────────

def fetch_sleep(token, date):
    """
    Fetches sleep for a given local date.
    The API returns UTC times with utcOffset — we convert to local
    and match entries whose local end time falls on the target date.
    """
    data = _get("dataTypes/sleep/dataPoints?page_size=10", token)
    if not data or "dataPoints" not in data:
        return None

    tz = TIMEZONE
    main_sleep = None

    for point in data["dataPoints"]:
        sleep    = point.get("sleep", {})
        interval = sleep.get("interval", {})
        end_raw  = interval.get("endTime", "")
        if not end_raw:
            continue
        try:
            end_utc   = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            end_local = end_utc.astimezone(tz)
            # Match on wake date: "last night's sleep" = sleep that you woke from
            # on the morning AFTER the target date.
            # e.g. date=May 12 → find sleep ending on May 13 (woke up May 13 morning)
            wake_date = date + datetime.timedelta(days=1)
            if end_local.date() == wake_date:
                meta = sleep.get("metadata", {})
                if meta.get("main") is True:
                    main_sleep = point
                    break
                elif main_sleep is None:
                    main_sleep = point
        except Exception:
            continue

    if not main_sleep:
        return None

    sleep_data = main_sleep["sleep"]
    stages_raw = sleep_data.get("stages", [])
    summary    = sleep_data.get("summary", {})

    # Tally minutes per stage
    stage_mins = {"DEEP": 0, "REM": 0, "LIGHT": 0, "AWAKE": 0}
    for stage in stages_raw:
        stype = stage.get("type", "").upper()
        if stype not in stage_mins:
            continue
        try:
            s = datetime.datetime.fromisoformat(stage["startTime"].replace("Z", "+00:00"))
            e = datetime.datetime.fromisoformat(stage["endTime"].replace("Z", "+00:00"))
            stage_mins[stype] += int((e - s).total_seconds() / 60)
        except Exception:
            pass

    total_mins = sum(v for k, v in stage_mins.items() if k != "AWAKE")
    if total_mins == 0:
        total_mins = int(summary.get("minutesAsleep", 0))

    h = total_mins // 60
    m = total_mins % 60

    wake_time = ""
    end_raw = sleep_data.get("interval", {}).get("endTime", "")
    if end_raw:
        try:
            wake_time = datetime.datetime.fromisoformat(
                end_raw.replace("Z", "+00:00")
            ).astimezone(tz).strftime("%-I:%M %p")
        except Exception:
            pass

    return {
        "duration_str":  f"{h}h {m}m",
        "total_minutes": total_mins,
        "vs_7hr":        total_mins - 420,
        "deep_minutes":  stage_mins["DEEP"],
        "rem_minutes":   stage_mins["REM"],
        "light_minutes": stage_mins["LIGHT"],
        "wake_minutes":  stage_mins["AWAKE"],
        "wake_time":     wake_time,
    }


# ── Resting heart rate ────────────────────────────────────────────────────────

def fetch_resting_hr(token, date):
    """
    Fetches resting HR for a given date.
    API returns a date object (year/month/day) — match by date directly.
    """
    data = _get("dataTypes/daily-resting-heart-rate/dataPoints?page_size=7", token)
    if not data or "dataPoints" not in data:
        return None

    for point in data["dataPoints"]:
        rhr = point.get("dailyRestingHeartRate", {})
        d   = rhr.get("date", {})
        try:
            point_date = datetime.date(d["year"], d["month"], d["day"])
        except (KeyError, ValueError):
            continue
        if point_date == date:
            bpm = rhr.get("beatsPerMinute")
            return {"resting_hr": int(bpm) if bpm else None}

    return None


# ── Steps ─────────────────────────────────────────────────────────────────────

def fetch_steps(token, date):
    """Steps via dailyRollUp — confirmed working."""
    data = _post(
        "dataTypes/steps/dataPoints:dailyRollUp",
        {"range": {
            "start": {"date": {"year": date.year, "month": date.month, "day": date.day}, "time": {"hours": 0}},
            "end":   {"date": {"year": date.year, "month": date.month, "day": date.day}, "time": {"hours": 23, "minutes": 59, "seconds": 59}}
         }, "windowSizeDays": 1},
        token
    )
    if not data or "rollupDataPoints" not in data:
        return None
    points = data["rollupDataPoints"]
    if not points:
        return None
    return {"steps": int(points[0].get("steps", {}).get("countSum", 0))}


def fetch_active_minutes(token, date):
    """Active minutes via dailyRollUp."""
    data = _post(
        "dataTypes/active-minutes/dataPoints:dailyRollUp",
        {"range": {
            "start": {"date": {"year": date.year, "month": date.month, "day": date.day}, "time": {"hours": 0}},
            "end":   {"date": {"year": date.year, "month": date.month, "day": date.day}, "time": {"hours": 23, "minutes": 59, "seconds": 59}}
         }, "windowSizeDays": 1},
        token
    )
    if not data or "rollupDataPoints" not in data:
        return None
    points = data["rollupDataPoints"]
    if not points:
        return None
    return {"active_minutes": int(points[0].get("activeMinutes", {}).get("minutesSum", 0))}


# ── Main summary ──────────────────────────────────────────────────────────────

def fetch_health_data():
    """Returns a formatted health summary string for the morning brief."""
    try:
        token = get_access_token()
    except RuntimeError as e:
        return f"Google Health not connected: {e}"

    tz        = TIMEZONE
    today     = datetime.datetime.now(tz).date()
    yesterday = today - datetime.timedelta(days=1)

    today_split     = get_pplrul_day(today)
    yesterday_split = get_pplrul_day(yesterday)

    lines = [f"PPLRUL — yesterday: {yesterday_split}  |  today: {today_split}"]
    lines.append("")

    # Sleep
    sleep = fetch_sleep(token, yesterday)
    if sleep:
        vs     = sleep["vs_7hr"]
        vs_str = (f"+{vs}m above target" if vs > 0
                  else f"{abs(vs)}m BELOW 7hr target" if vs < 0
                  else "exactly on 7hr target")
        lines.append("SLEEP (last night):")
        lines.append(f"  Total:      {sleep['duration_str']}  ({vs_str})")
        if sleep["wake_time"]:
            lines.append(f"  Woke up:    {sleep['wake_time']}")
        if sleep["deep_minutes"] or sleep["rem_minutes"]:
            lines.append(
                f"  Stages:     Deep {sleep['deep_minutes']}m  |  "
                f"REM {sleep['rem_minutes']}m  |  "
                f"Light {sleep['light_minutes']}m  |  "
                f"Awake {sleep['wake_minutes']}m"
            )
    else:
        lines.append("SLEEP: No data for last night.")
    lines.append("")

    # Resting HR
    hr = fetch_resting_hr(token, yesterday)
    if hr and hr["resting_hr"]:
        lines.append(f"HEART RATE:  Resting {hr['resting_hr']} bpm (yesterday)")
    else:
        lines.append("HEART RATE:  No data available.")
    lines.append("")

    # Steps + activity
    steps  = fetch_steps(token, yesterday)
    active = fetch_active_minutes(token, yesterday)
    lines.append("ACTIVITY (yesterday):")
    if steps:
        lines.append(f"  Steps:          {steps['steps']:,}")
    if active:
        lines.append(f"  Active minutes: {active['active_minutes']}m")
    if not steps and not active:
        lines.append("  No data available.")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Google Health Integration")
    parser.add_argument("--setup", action="store_true", help="Authenticate with Google Health (run once)")
    parser.add_argument("--test",  action="store_true", help="Print yesterday's health data")
    args = parser.parse_args()

    if args.setup:
        setup_health_auth()
    else:
        print("\n\U0001f3c3  Fetching Google Health data...\n")
        print(fetch_health_data())
        print()

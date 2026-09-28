"""
Jarvis — Gmail Monitor
========================
Periodically checks your inbox and pushes notifications for
important emails that need your attention.

Career and mentor follow-ups are owned by an external agent. Those threads
are marked seen and not classified, so this job does not spend a Haiku call
or send a duplicate alert. Payment, uni, bank, and other keep-signals still
alert. The skip list is JARVIS_GMAIL_SKIP_CATEGORIES (default
"internship,mentor"; set it empty to classify everything again).

The systemd timer is hourly (deploy/systemd/jarvis-gmail.timer). Until that
unit is reloaded on the VPS, JARVIS_GMAIL_MIN_INTERVAL_MINUTES (default 50)
makes a leftover 30-minute timer skip the in-between run. 50 rather than 60
so an hourly timer that fires a little early is not skipped.
JARVIS_GMAIL_HOURS_BACK (default 2) stays wider than that gap so a message
is not missed.

Add to crontab (crontab -e), only if you are not using the systemd timer:
  0 * * * * cd /Users/manavjain/jarvis && /Users/manavjain/jarvis/venv/bin/python gmail_monitor.py >> logs/gmail_monitor.log 2>&1
"""

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os, json, datetime, re
from pathlib import Path
from urllib.parse import quote

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
SEEN_FILE  = SCRIPT_DIR / "data" / "gmail_seen.json"
TIMEZONE   = pytz.timezone(config.TIMEZONE)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]

PRIORITY_SENDERS = [
    "google", "canva", "anthropic", "optiver", "amazon", "atlassian",
    "seek", "linkedin", "workday", "greenhouse", "lever",
    "unsw", "myunsw", "moodle",
    "propwealth", "axis",
    "stgeorge", "westpac", "ato.gov",
]

PRIORITY_SUBJECTS = [
    "application", "interview", "internship", "offer", "role", "position",
    "assessment", "test", "screening", "recruiter", "hiring",
    "mentor", "meeting", "catch up", "feedback",
    "payment", "invoice", "receipt", "salary", "paid",
    "assignment", "submission", "result", "grade", "exam",
    "urgent", "action required", "response needed",
]

IGNORE_SENDERS = [
    "noreply", "no-reply", "donotreply", "notifications@",
    "newsletter", "marketing", "promotions", "deals", "offers",
    "spotify", "netflix", "uber", "deliveroo", "doordash",
]

# Recruiting platforms are career mail even when the subject is generic.
# Company domains (Google, Amazon, Canva) are not in this list: those
# inboxes also carry orders and security alerts, which still go through
# the classifier unless the subject itself is career or mentor.
_RECRUITER_SENDERS = (
    "seek.com", "linkedin", "workday", "greenhouse", "lever.co",
    "smartrecruiters", "myworkday",
)
_CAREER_SUBJECTS = (
    "application", "interview", "internship", "recruiter", "hiring", "screening",
)
_MENTOR_SUBJECTS = ("mentor",)
# A keep-signal wins over the external-agent skip. "offer" alone is too broad
# (promotions); a job offer still matches "hiring" / "interview" / recruiter.
_KEEP_SUBJECTS = (
    "payment", "invoice", "receipt", "salary", "paid", "assignment",
    "submission", "result", "grade", "exam", "urgent", "action required",
    "response needed", "security", "sign-in", "sign in", "password",
    "verification",
)
_KEEP_SENDERS = (
    "unsw", "myunsw", "moodle", "propwealth", "axis",
    "stgeorge", "westpac", "ato.gov",
)

RUN_STAMP = SCRIPT_DIR / "data" / "gmail_monitor_stamp.json"
_DEFAULT_SKIP = "internship,mentor"


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token_file = SCRIPT_DIR / "token.json"
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tmp_file = token_file.parent / (token_file.name + ".tmp")
        tmp_file.write_text(creds.to_json())
        tmp_file.replace(token_file)
    return creds


def load_seen():
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return set(data.get("ids", []))
        except Exception:
            pass
    return set()


def save_seen(seen_ids):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    ids = list(seen_ids)[-500:]
    SEEN_FILE.write_text(json.dumps({"ids": ids}))


def fetch_recent_emails(hours_back=1, max_emails=20):
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=get_credentials())
    query   = f"is:unread newer_than:{hours_back}h -category:promotions -category:social -category:updates"
    result  = service.users().messages().list(userId="me", q=query, maxResults=max_emails).execute()
    emails  = []

    for msg in result.get("messages", []):
        try:
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers      = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
            sender_raw   = headers.get("From", "Unknown")
            sender       = sender_raw.split("<")[0].strip().strip('"')
            sender_email = sender_raw.split("<")[1].rstrip(">").strip() if "<" in sender_raw else ""
            emails.append({
                "id":           msg["id"],
                "sender":       sender,
                "sender_email": sender_email,
                "subject":      headers.get("Subject", "(no subject)"),
                "snippet":      msg_data.get("snippet", "")[:300],
            })
        except Exception as e:
            print(f"  ⚠️  Failed to fetch {msg['id']}: {e}")

    return emails


def is_ignorable(email):
    return any(kw in (email["sender"] + email["sender_email"]).lower() for kw in IGNORE_SENDERS)


def is_priority(email):
    s = (email["sender"] + email["sender_email"]).lower()
    j = email["subject"].lower()
    return any(kw in s for kw in PRIORITY_SENDERS) or any(kw in j for kw in PRIORITY_SUBJECTS)


def _sender_blob(email):
    return f"{email.get('sender', '')} {email.get('sender_email', '')}".lower()


def skip_categories():
    """
    Categories the external career/mentor agent already handles.

    JARVIS_GMAIL_SKIP_CATEGORIES is a comma-separated list. An empty value
    disables the skip and classifies every priority email, as before.
    """
    raw = os.environ.get("JARVIS_GMAIL_SKIP_CATEGORIES", _DEFAULT_SKIP)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def min_interval_minutes():
    """Minimum gap between inbox checks. 0 disables the guard."""
    raw = os.environ.get("JARVIS_GMAIL_MIN_INTERVAL_MINUTES", "50")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 50


def poll_hours_back():
    """How far back to ask Gmail. Keep this at least as wide as the timer gap."""
    raw = os.environ.get("JARVIS_GMAIL_HOURS_BACK", "2")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 2


def has_keep_signal(email):
    """
    True when Jarvis should still alert even if the thread looks career-related.

    Uni, bank, payroll, and explicit action-required mail stay local.
    """
    sender = _sender_blob(email)
    subject = (email.get("subject") or "").lower()
    return (
        any(kw in subject for kw in _KEEP_SUBJECTS)
        or any(kw in sender for kw in _KEEP_SENDERS)
    )


def externally_owned_category(email):
    """
    Return 'internship' or 'mentor' when the external agent owns this mail.

    Recruiter-platform senders count as internship mail on their own. Other
    senders need a career or mentor subject. A keep-signal always returns None
    so payment and academic mail is still classified.
    """
    skipped = skip_categories()
    if not skipped or has_keep_signal(email):
        return None
    sender = _sender_blob(email)
    subject = (email.get("subject") or "").lower()
    if "mentor" in skipped and any(kw in subject for kw in _MENTOR_SUBJECTS):
        return "mentor"
    if "internship" in skipped:
        if any(kw in sender for kw in _RECRUITER_SENDERS):
            return "internship"
        if any(kw in subject for kw in _CAREER_SUBJECTS):
            return "internship"
        if "offer" in subject and any(kw in subject for kw in ("role", "position", "job", "intern", "candidate")):
            return "internship"
    return None


def _ran_recently(now):
    """True when the last successful check is inside the minimum interval."""
    gap = min_interval_minutes()
    if gap <= 0 or not RUN_STAMP.exists():
        return False
    try:
        data = json.loads(RUN_STAMP.read_text())
        last = datetime.datetime.fromisoformat(data["ts"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return False
    if last.tzinfo is None:
        last = TIMEZONE.localize(last)
    return now - last < datetime.timedelta(minutes=gap)


def _stamp_run(now):
    """Remember a successful check so a faster timer does not repeat the work."""
    RUN_STAMP.parent.mkdir(parents=True, exist_ok=True)
    RUN_STAMP.write_text(json.dumps({"ts": now.isoformat(timespec="seconds")}))


def classify_email(email):
    import anthropic as _ant
    client = _ant.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        "Classify this email for Manav Jain, Year 2 UNSW CS student in Sydney. "
        "He applies for internships at Google, Canva, Anthropic, Optiver, Amazon. "
        "He tutors and does automation work for PropWealth.\n\n"
        f"From: {email['sender']} <{email['sender_email']}>\n"
        f"Subject: {email['subject']}\n"
        f"Preview: {email['snippet']}\n\n"
        "Respond ONLY with JSON:\n"
        '{"needs_action": true/false, "category": "internship|mentor|payment|academic|admin|personal", '
        '"priority": "high|medium|low", "summary": "one sentence", "suggested_action": "what to do or null"}\n\n'
        "needs_action=true only if Manav should reply or act within 24 hours. "
        "Do NOT flag automated confirmations or newsletters."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text  = resp.content[0].text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  ⚠️  Classification failed: {e}")
    return {"needs_action": False, "category": "unknown", "priority": "low",
            "summary": email["snippet"][:100], "suggested_action": None}


def send_notification(title, message, priority="default", tags=None):
    import ssl, certifi
    from urllib.request import urlopen, Request as UReq
    channel = getattr(config, "NTFY_CHANNEL", "")
    if not channel:
        return
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    url     = f"https://ntfy.sh/{quote(channel)}"
    headers = {"Title": title, "Priority": priority, "Tags": ",".join(tags or ["email"])}
    req = UReq(url, data=message.encode(), headers=headers, method="POST")
    try:
        with urlopen(req, context=ssl_ctx, timeout=10):
            pass
        print(f"  📱  Notified: {title}")
    except Exception as e:
        print(f"  ⚠️  Notification failed: {e}")


def run_monitor(force=False):
    """
    Check unread mail and notify on anything Jarvis still owns.

    force=True ignores the minimum interval (manual runs). Career and mentor
    mail is recorded as seen so a later run does not classify it again.
    """
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)
    print(f"\n📬  Gmail monitor — {now.strftime('%A %d %b, %-I:%M %p')}")

    if not force and _ran_recently(now):
        print("    Skipping — last check is inside the minimum interval\n")
        return

    seen       = load_seen()
    emails     = fetch_recent_emails(hours_back=poll_hours_back(), max_emails=20)
    new_emails = [e for e in emails if e["id"] not in seen]
    print(f"    {len(new_emails)} new email(s) to check")

    skipped = skip_categories()
    action_emails = []
    for email in new_emails:
        seen.add(email["id"])
        if is_ignorable(email):
            continue
        if not is_priority(email):
            continue
        owned = externally_owned_category(email)
        if owned:
            print(f"  ↪   Left to external agent ({owned}): {email['subject'][:50]}")
            continue
        print(f"  🔍  Classifying: {email['subject'][:50]}...")
        result = classify_email(email)
        category = (result.get("category") or "").lower()
        if result.get("needs_action") and category in skipped and not has_keep_signal(email):
            print(f"  ↪   {category} owned externally — no alert")
            continue
        if result.get("needs_action"):
            action_emails.append({**email, **result})
            print(f"  ✅  Action: {result['summary'][:60]}")
        else:
            print(f"  ✓   No action: {result['summary'][:60]}")

    icons = {"internship": "🎯", "mentor": "👨‍💼", "payment": "💰",
             "academic": "📚", "admin": "📋", "personal": "👤"}
    pmap  = {"high": "urgent", "medium": "default", "low": "low"}

    for email in action_emails:
        icon    = icons.get(email.get("category", ""), "📧")
        title   = f"{icon} {email['sender']}: {email['subject'][:40]}"
        message = email.get("summary", email["snippet"][:100])
        if email.get("suggested_action"):
            message += f"\n→ {email['suggested_action']}"
        send_notification(title, message,
                          priority=pmap.get(email.get("priority", "low"), "default"),
                          tags=["email", email.get("category", "mail")])

    save_seen(seen)
    _stamp_run(now)
    print(f"    {len(action_emails)} action email(s) notified\n" if action_emails else "    All clear\n")


if __name__ == "__main__":
    run_monitor()

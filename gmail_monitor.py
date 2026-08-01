"""
Jarvis — Gmail Monitor
========================
Periodically checks your inbox and pushes notifications for
important emails that need your attention.

Add to crontab (crontab -e):
  */30 * * * * cd /Users/manavjain/jarvis && /Users/manavjain/jarvis/venv/bin/python gmail_monitor.py >> logs/gmail_monitor.log 2>&1
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


def run_monitor():
    tz  = TIMEZONE
    now = datetime.datetime.now(tz)
    print(f"\n📬  Gmail monitor — {now.strftime('%A %d %b, %-I:%M %p')}")

    seen       = load_seen()
    emails     = fetch_recent_emails(hours_back=1, max_emails=20)
    new_emails = [e for e in emails if e["id"] not in seen]
    print(f"    {len(new_emails)} new email(s) to check")

    action_emails = []
    for email in new_emails:
        seen.add(email["id"])
        if is_ignorable(email):
            continue
        if not is_priority(email):
            continue
        print(f"  🔍  Classifying: {email['subject'][:50]}...")
        result = classify_email(email)
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
    print(f"    {len(action_emails)} action email(s) notified\n" if action_emails else "    All clear\n")


if __name__ == "__main__":
    run_monitor()

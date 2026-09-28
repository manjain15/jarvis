"""
Jarvis — Mentor follow-up drafts
================================
When mentor flags say we're awaiting a reply and enough days have passed
silently, create a Gmail *draft* (never send) so Manav can review and send.

Called from morning_brief after term flags are collected. Idempotent: at most
one auto-draft per silent streak (keyed on last_contact + date), tracked in
data/mentor_followup_draft.json.

USAGE:
  from followups import maybe_draft_mentor_followup
  info = maybe_draft_mentor_followup(creds)   # fail-soft dict for the brief
  # or: python followups.py --draft
"""

import base64
import datetime
import json
from email.mime.text import MIMEText
from pathlib import Path

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
FLAG_FILE  = DATA_DIR / "mentor_followup_draft.json"
TIMEZONE   = pytz.timezone(config.TIMEZONE)

# Match term_context.get_mentor_flags threshold (7 days silent while awaiting).
DEFAULT_SILENT_DAYS = 7


def _load_flag() -> dict:
    try:
        return json.loads(FLAG_FILE.read_text())
    except Exception:
        return {}


def _save_flag(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    from json_store import atomic_write_json
    atomic_write_json(FLAG_FILE, data)


def _mentor_silent_days(mentor: dict) -> int | None:
    """Days since last_contact, or None if unparseable / not awaiting."""
    if not mentor.get("awaiting_response"):
        return None
    last_raw = mentor.get("last_contact")
    if not last_raw:
        return None
    try:
        last = datetime.date.fromisoformat(last_raw)
    except Exception:
        return None
    today = datetime.datetime.now(TIMEZONE).date()
    return (today - last).days


def _greeting(mentor: dict) -> str:
    """Warm salutation from mentor name/email — never invent a person name."""
    raw = (mentor.get("name") or "").strip()
    if raw and "mentor" not in raw.lower():
        return f"Hi {raw.split()[0]},"
    email = (mentor.get("email") or "").strip()
    if email and "@" in email:
        local = email.split("@")[0].split(".")[0]
        if local and local.isalpha():
            return f"Hi {local.capitalize()},"
    return "Hi,"


def _draft_body(mentor: dict) -> str:
    topic = (mentor.get("last_topic") or "our last chat").strip()
    # Keep topic short in the body if it's a long note.
    if len(topic) > 120:
        topic = topic[:117].rstrip() + "…"
    return (
        f"{_greeting(mentor)}\n\n"
        f"Hope you're well — just a quick follow-up on my last note about {topic}. "
        f"Happy to jump on a call whenever suits if that's useful.\n\n"
        f"Cheers,\n"
        f"Manav\n"
    )


def _draft_subject(mentor: dict) -> str:
    topic = (mentor.get("last_topic") or "catch-up").strip()
    # Subject stays short.
    short = topic if len(topic) <= 60 else topic[:57].rstrip() + "…"
    return f"Following up — {short}"


def _already_drafted_for(mentor: dict, today: str) -> bool:
    """
    Skip if we already drafted today, or already drafted for this same
    last_contact streak (so re-runs the same day / week don't duplicate).
    """
    flag = _load_flag()
    if not flag:
        return False
    if flag.get("date") == today:
        return True
    if (
        flag.get("last_contact") == mentor.get("last_contact")
        and flag.get("awaiting_response") is True
        and mentor.get("awaiting_response")
    ):
        return True
    return False


def create_mentor_followup_draft(creds, mentor: dict) -> dict:
    """
    Creates a Gmail draft to mentor['email']. Never sends.
    Returns {ok, draft_id, subject, to, error?}.
    Requires gmail.compose (or broader) scope on the token.
    """
    to_addr = (mentor.get("email") or "").strip()
    if not to_addr:
        return {
            "ok": False,
            "error": "mentor.email missing in term_context.json — add it to enable drafts",
        }

    from googleapiclient.discovery import build

    subject = _draft_subject(mentor)
    body    = _draft_body(mentor)

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"]      = to_addr
    msg["From"]    = config.YOUR_EMAIL
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = build("gmail", "v1", credentials=creds)
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()

    draft_id = draft.get("id", "")
    # Gmail draft deep-link (opens drafts folder; specific draft id in URL is unreliable).
    link = "https://mail.google.com/mail/u/0/#drafts"

    return {
        "ok": True,
        "draft_id": draft_id,
        "subject": subject,
        "to": to_addr,
        "link": link,
    }


def maybe_draft_mentor_followup(creds=None, force: bool = False,
                                silent_days: int = DEFAULT_SILENT_DAYS) -> dict:
    """
    If mentor is awaiting a reply and silent_days have passed, create a Gmail
    draft (idempotent). Returns a small dict the morning brief can surface:

      {
        "drafted": bool,
        "skipped": str | None,   # reason when not drafted
        "subject": str | None,
        "link": str | None,
        "to": str | None,
        "brief_note": str | None,  # one line for the prompt
      }
    """
    result = {
        "drafted": False,
        "skipped": None,
        "subject": None,
        "link": None,
        "to": None,
        "brief_note": None,
    }

    try:
        from term_context import load_context
        mentor = load_context().get("mentor") or {}
    except Exception as e:
        result["skipped"] = f"term_context unavailable: {e}"
        return result

    days = _mentor_silent_days(mentor)
    if days is None:
        result["skipped"] = "not awaiting response (or no last_contact)"
        return result
    if days < silent_days and not force:
        result["skipped"] = f"only {days}d silent (threshold {silent_days})"
        return result
    if not (mentor.get("email") or "").strip():
        result["skipped"] = (
            "mentor.email missing in term_context.json — add it to enable drafts"
        )
        return result

    today = datetime.datetime.now(TIMEZONE).date().isoformat()
    if not force and _already_drafted_for(mentor, today):
        flag = _load_flag()
        result["skipped"] = "draft already exists for this streak"
        # Still surface prior draft in the brief so Manav knows it's waiting.
        if flag.get("subject"):
            result["subject"] = flag.get("subject")
            result["link"] = flag.get("link") or "https://mail.google.com/mail/u/0/#drafts"
            result["to"] = flag.get("to")
            result["brief_note"] = (
                f"Gmail draft waiting (from earlier): \"{flag['subject']}\" — "
                f"open Drafts to review/send. {result['link']}"
            )
        return result

    if creds is None:
        try:
            from morning_brief import get_google_credentials
            creds = get_google_credentials()
        except Exception as e:
            result["skipped"] = f"no Google credentials: {e}"
            return result

    created = create_mentor_followup_draft(creds, mentor)
    if not created.get("ok"):
        result["skipped"] = created.get("error", "draft create failed")
        return result

    _save_flag({
        "date": today,
        "draft_id": created["draft_id"],
        "subject": created["subject"],
        "to": created["to"],
        "link": created["link"],
        "last_contact": mentor.get("last_contact"),
        "awaiting_response": True,
        "days_silent": days,
    })

    result.update({
        "drafted": True,
        "subject": created["subject"],
        "link": created["link"],
        "to": created["to"],
        "brief_note": (
            f"Auto-drafted mentor follow-up in Gmail Drafts: \"{created['subject']}\" "
            f"→ {created['to']}. Review before sending. {created['link']}"
        ),
    })
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis mentor follow-up drafts")
    parser.add_argument("--draft", action="store_true",
                        help="Create a mentor follow-up draft if thresholds met")
    parser.add_argument("--force", action="store_true",
                        help="Ignore silent-day / idempotency gates")
    args = parser.parse_args()

    if not args.draft:
        parser.print_help()
        raise SystemExit(0)

    info = maybe_draft_mentor_followup(force=args.force)
    if info["drafted"]:
        print(f"✅ Draft created: {info['subject']} → {info['to']}")
        print(f"   {info['link']}")
    else:
        print(f"⏭  Skipped: {info['skipped']}")
        if info.get("brief_note"):
            print(f"   {info['brief_note']}")

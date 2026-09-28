"""
Jarvis — Agent HTTP API
=======================
A small HTTP API so an external assistant (for example a Grok bot)
can query Jarvis, and correct stale mentor / internship records.

This is a separate process from dashboard.py and live_spend.py:

  - dashboard.py is the local UI. It binds to localhost:5555 with no auth,
    and it also serves the voice /ask shortcut. Putting a bearer-token
    agent API on that process would mix two trust boundaries.
  - live_spend.py is the iPhone Back Tap writer. It uses a different
    token (X-Jarvis-Token from data/.spend_token) on port 5556. The
    shortcut contract stays as it is; this API calls the same log_spend().

Every route, including /health, requires:

    Authorization: Bearer $JARVIS_API_TOKEN

The token is read from the environment (repo .env via config.load_dotenv).
Comparison is constant-time. The server binds 127.0.0.1 only — expose it
with HTTPS (Cloudflare Tunnel, Tailscale Serve, or a reverse proxy).
Do not open the port on the public firewall. See docs/API.md.

COST:
  POST /ask is one Claude call, the same order of cost as a Telegram message.
  GET /memory/search hits Mem0 (OpenAI embeddings). Do not poll either.
  Mentor and internship writes do not call Anthropic. They update
  term_context.json through term_context.mutate_context and append
  data/agent_api_audit.jsonl.
  GET /finance* is read-only. It reads local CSVs and term_context goals.
  Reselling inventory also reads the Google Sheet once per call. No Anthropic.
  A weekly Money check should call GET /finance, not poll it.

SETUP:
  Add JARVIS_API_TOKEN to .env (long random string).
  python agent_api.py --serve
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import pytz

import config
import term_context
from json_store import atomic_write_json, file_lock

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
BRIEF_FILE = DATA_DIR / "latest_brief.json"
AUDIT_FILE = DATA_DIR / "agent_api_audit.jsonl"
USAGE_FILE = DATA_DIR / "agent_api_usage.json"

MIN_TOKEN_LEN = 32
MAX_BODY_BYTES = 16 * 1024
AUTH_FAIL_LIMIT = 10        # failed logins per client before it is blocked
AUTH_FAIL_WINDOW = 300      # seconds
SPEND_DEDUPE_SECONDS = 60

# Per-bot tokens: JARVIS_API_TOKENS="career:<token>,study:<token>,money:<token>".
# The name becomes the audit actor and limits which routes the token can call.
# "admin" (and the legacy single JARVIS_API_TOKEN) may call every route.
_FINANCE_ROUTES = {
    "finance", "finance_spending", "finance_savings",
    "finance_subscriptions", "finance_reselling",
}
BOT_SCOPES = {
    "career": {"health", "flags", "context", "patch_mentor", "patch_internship", "add_internship"},
    "study": {"health", "flags", "context"},
    "money": {"health", "spend"} | _FINANCE_ROUTES,
    "admin": None,
}

# Daily call caps for the routes that cost money. Env override, 0 disables.
_DAILY_CAP_ENV = {"ask": ("JARVIS_API_ASK_DAILY", 30), "memory_search": ("JARVIS_API_MEMORY_DAILY", 100)}

_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_MENTOR_BODY_KEYS = {"actor", "reason", *term_context.MENTOR_WRITABLE_FIELDS}
_INTERNSHIP_PATCH_KEYS = {"actor", "reason", "company", "role", *term_context.INTERNSHIP_WRITABLE_FIELDS}
_INTERNSHIP_ADD_KEYS = {"actor", "reason", "company", "role", "status", "last_update", "next_action", "notes"}

TIMEZONE     = pytz.timezone(config.TIMEZONE)
DEFAULT_PORT = 5557

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<br\s*/?>|</p>|</h[1-6]>|</li>|</div>|</tr>", re.I)
# St. George descriptions embed the owner's own account numbers, card numbers
# and PayID details. Digit runs of 8+ digits are redacted even when broken up by
# single spaces or dashes ("4111 1111 1111 1111"), as are masked card tails
# ("xx1234") and email addresses. ISO dates are left alone, as are dollar amounts
# (they carry commas or a decimal point).
_ACCOUNT_DIGITS = re.compile(
    r"(?<![\d.,])(?:(?P<date>\d{4}-\d{2}-\d{2})(?!\d)|\d(?:[ -]?\d){7,}(?![\d]|[.,]\d))"
)
_MASKED_TAIL = re.compile(r"\b[xX*]{2,}[ -]?\d{2,4}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def api_token():
    """Return the legacy single bearer token, or '' if it is unset."""
    return os.environ.get("JARVIS_API_TOKEN", "").strip()


def load_tokens():
    """
    Return [(token, actor_or_None)] from the environment.

    JARVIS_API_TOKENS holds "name:token" pairs for per-bot tokens; names must be
    keys of BOT_SCOPES and tokens at least MIN_TOKEN_LEN characters. The legacy
    JARVIS_API_TOKEN maps to actor None (full access, actor taken from the
    request body). Raises RuntimeError on a malformed pair.
    """
    tokens = []
    for pair in os.environ.get("JARVIS_API_TOKENS", "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, token = pair.partition(":")
        name, token = name.strip().lower(), token.strip()
        if not sep or name not in BOT_SCOPES:
            raise RuntimeError(f"JARVIS_API_TOKENS entry must be name:token with name in {sorted(BOT_SCOPES)}")
        if len(token) < MIN_TOKEN_LEN:
            raise RuntimeError(f"token for '{name}' must be at least {MIN_TOKEN_LEN} characters")
        tokens.append((token, name))
    legacy = api_token()
    if legacy:
        if len(legacy) < MIN_TOKEN_LEN:
            print(f"⚠️  JARVIS_API_TOKEN is shorter than {MIN_TOKEN_LEN} characters; rotate it")
        tokens.append((legacy, None))
    return tokens


def match_bearer(authorization_header, tokens):
    """
    Return (True, actor) when the header matches any token, else (False, None).

    Every token is compared, with no early exit, so timing does not reveal
    which entry matched.
    """
    hit = (False, None)
    for token, actor in tokens:
        if bearer_matches(authorization_header, token) and not hit[0]:
            hit = (True, actor)
    return hit


class FailureLimiter:
    """Blocks a client after too many failed logins inside a sliding window."""

    def __init__(self, limit=AUTH_FAIL_LIMIT, window=AUTH_FAIL_WINDOW):
        self.limit, self.window = limit, window
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, key, now=None):
        """True when key has used up its failed-login allowance."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, now)) >= self.limit

    def record(self, key, now=None):
        """Note one failed login for key."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(key, now).append(now)


def daily_cap(name):
    """Configured daily call cap for a costly route (0 = unlimited)."""
    env, default = _DAILY_CAP_ENV[name]
    try:
        return max(0, int(os.environ.get(env, default)))
    except ValueError:
        return default


def consume_daily(name):
    """
    Count one call against today's cap for `name`. Returns False when the cap
    is used up. State lives in data/agent_api_usage.json under a lock.
    """
    cap = daily_cap(name)
    if cap == 0:
        return True
    today = datetime.datetime.now(TIMEZONE).date().isoformat()
    with file_lock(USAGE_FILE):
        try:
            data = json.loads(USAGE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today}
        used = int(data.get(name, 0))
        if used >= cap:
            return False
        data[name] = used + 1
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(USAGE_FILE, data)
    return True


def bearer_matches(authorization_header, expected):
    """
    Return True when the Authorization header carries expected as a Bearer token.

    Both sides are hashed to a fixed length before hmac.compare_digest so the
    comparison does not leak the secret through length or early-exit. An empty
    expected token never matches.
    """
    if not expected:
        return False
    header = authorization_header or ""
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    provided = parts[1].strip()
    if not provided:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def html_to_text(html):
    """Flatten brief HTML into plain text for clients that do not render HTML."""
    text = _BREAK_RE.sub("\n", html or "")
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def save_latest_brief(html, kind, label):
    """
    Persist the latest morning brief or weekly review for GET /brief.

    kind is "morning" or "weekly". label is the human date string already
    used as the email subject context. Writes data/latest_brief.json.
    """
    import datetime
    payload = {
        "kind": kind,
        "label": label,
        "generated_at": datetime.datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "html": html,
    }
    BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(BRIEF_FILE, payload)
    return payload


def load_latest_brief():
    """Return the stored brief plus a plain-text rendering, or None if absent."""
    if not BRIEF_FILE.exists():
        return None
    try:
        data = json.loads(BRIEF_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "html" not in data:
        return None
    data = dict(data)
    data["text"] = html_to_text(data.get("html", ""))
    return data


def answer_question(question):
    """
    Answer one free-text question with the Telegram conversational brain.

    Read-only: proposal and remote-work tools are not offered, so this cannot
    queue profile edits or start a coding session. Raises ValueError on a
    blank or oversized question. One Anthropic call per question.
    """
    question = str(question or "").strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > 4000:
        raise ValueError("question must be 4000 characters or fewer")
    from jarvis_telegram import build_static_context, chat_with_claude
    return chat_with_claude(question, build_static_context(), [], tools=[])


def get_flags_payload():
    """
    Current term nudges plus academic alerts.

    Each source fails soft. `unavailable` lists sources that could not be read;
    the exception text stays in the server log.
    """
    flags = []
    academic = []
    unavailable = []
    try:
        from term_context import get_flags
        flags = list(get_flags())
    except Exception as e:
        print(f"⚠️  /flags term context failed: {e}")
        unavailable.append("term_flags")
    try:
        from study_tracker import get_academic_alerts
        academic = list(get_academic_alerts())
    except Exception as e:
        print(f"⚠️  /flags academic alerts failed: {e}")
        unavailable.append("academic_alerts")
    return {
        "flags": flags,
        "academic_alerts": academic,
        "unavailable": unavailable,
    }


def _json_safe(obj):
    """Round-trip through JSON so dates and other objects become strings."""
    return json.loads(json.dumps(obj, default=str))


def upcoming_calendar_events(days_ahead):
    """
    Google Calendar events from now through days_ahead, as plain dicts.

    Raises if Calendar credentials or the API call fail. Callers decide
    whether that should fail the whole response.
    """
    import datetime
    from jarvis_calendar import get_calendar_service

    now = datetime.datetime.now(TIMEZONE)
    end = now + datetime.timedelta(days=days_ahead)
    service = get_calendar_service()
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    events = []
    for event in result.get("items", []):
        start = event.get("start", {})
        end_raw = event.get("end", {})
        events.append({
            "title": event.get("summary", "Untitled"),
            "start": start.get("dateTime", start.get("date", "")),
            "end": end_raw.get("dateTime", end_raw.get("date", "")),
            "location": event.get("location", ""),
        })
    return events


def get_context_payload(days_ahead=21):
    """
    Term summary, assessment deadlines, and upcoming calendar events.

    Calendar failure does not drop the term data; calendar_error is set instead.
    UNSW assessment dates that already sync into Google Calendar show up there.
    """
    from term_context import get_term_summary, get_upcoming_assessments, load_context

    ctx = load_context()
    calendar = []
    calendar_error = None
    try:
        calendar = upcoming_calendar_events(days_ahead)
    except Exception as e:
        print(f"⚠️  /context calendar failed: {e}")
        calendar_error = "calendar unavailable"
    return {
        "days_ahead": days_ahead,
        "term": _json_safe(get_term_summary()),
        "deadlines": _json_safe(get_upcoming_assessments(ctx, days_ahead=days_ahead)),
        "calendar": calendar,
        "calendar_error": calendar_error,
    }


def search_memory_payload(query, limit=5):
    """
    Search Mem0 and return the formatted text search_memories already produces.

    Raises ValueError on a blank or oversized query. One embedding call per search.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("q is required")
    if len(query) > 500:
        raise ValueError("q must be 500 characters or fewer")
    limit = max(1, min(int(limit), 10))
    from jarvis_mem0 import search_memories
    text = search_memories(query, limit=limit) or ""
    return {"query": query, "limit": limit, "text": text}


def _recent_duplicate(amount, category, note):
    """
    Return a live-spend entry identical to this one and logged within
    SPEND_DEDUPE_SECONDS, or None. A bot retrying a timed-out POST must not
    double-count against the weekly budget. Any read problem means no duplicate.
    """
    try:
        from live_spend import load_entries
        amt = round(float(amount), 2)
        cat = str(category or "").strip().lower()
        text = str(note or "").strip()[:200]
        now = datetime.datetime.now(TIMEZONE)
        for entry in load_entries(days=1):
            if (entry["amount"] == amt and entry["category"].lower() == cat
                    and entry.get("note", "") == text):
                age = (now - datetime.datetime.fromisoformat(entry["ts"])).total_seconds()
                if age <= SPEND_DEDUPE_SECONDS:
                    return entry
    except Exception:
        return None
    return None


def log_spend_entry(amount, category, note=""):
    """
    Validate and append one live-spend entry via live_spend.log_spend.

    Raises ValueError on bad input. Returns the same confirmation shape the
    Back Tap endpoint uses, plus the stored entry. An identical entry logged in
    the last SPEND_DEDUPE_SECONDS is returned again with duplicate=True instead
    of being appended twice.
    """
    from live_spend import get_live_summary, log_spend
    duplicate = _recent_duplicate(amount, category, note)
    entry = duplicate or log_spend(amount, category, note)
    week_total = get_live_summary(days=7).get("total", entry["amount"])
    return {
        "ok": True,
        "duplicate": duplicate is not None,
        "logged": f"${entry['amount']:.2f} {entry['category']}",
        "week_total": f"${week_total:.2f}",
        "entry": {
            "ts": entry["ts"],
            "amount": entry["amount"],
            "category": entry["category"],
            "note": entry["note"],
        },
    }


def _json_object(body):
    """Require a JSON object. Flask returns None for a missing or invalid body."""
    if not isinstance(body, dict):
        raise ValueError("JSON object required")


def _reject_nulls(body):
    """JSON null is not a way to clear a field. Omit the key, or send an empty string."""
    for key, value in body.items():
        if value is None:
            raise ValueError(f"{key} must not be null")


def _reject_unknown_fields(body, allowed):
    """Reject keys outside the route's allowlist so stray fields cannot be stored."""
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")


def _actor(body):
    """Return the external agent name from the body (legacy single-token clients only)."""
    if "actor" not in body or body["actor"] is None:
        raise ValueError("actor is required")
    raw = body["actor"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("actor is required")
    actor = raw.strip()
    if not _ACTOR_RE.fullmatch(actor):
        raise ValueError("actor must be 1-40 characters (letters, digits, '.', '_', '-')")
    return actor


def _reason(body):
    """Optional audit-only explanation. Not written into term_context.json."""
    if "reason" not in body:
        return ""
    value = body["reason"]
    if not isinstance(value, str):
        raise ValueError("reason must be a string")
    text = value.strip()
    if len(text) > 300:
        raise ValueError("reason must be 300 characters or fewer")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ValueError("reason contains control characters")
    return text


def append_external_audit(entry):
    """
    Append one JSON line to data/agent_api_audit.jsonl.

    The line records who (actor), when (ts), and what changed (old and new
    values). Locked so two writers cannot interleave a line. This file is
    gitignored with the rest of data/ and is not exposed over HTTP.
    """
    if not isinstance(entry, dict):
        raise ValueError("audit entry must be an object")
    line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(AUDIT_FILE):
        with open(AUDIT_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _audit_entry(actor, action, target, reason, old, new):
    """Build one audit record. old/new are the changed fields, or the new row on add."""
    return {
        "ts": datetime.datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "target": target,
        "reason": reason,
        "old": old,
        "new": new,
    }


def apply_mentor_write(body, actor=None):
    """
    Patch the mentor record and audit the diff.

    Applies immediately through term_context.patch_mentor so GET /flags
    sees the correction on the next read. The term_updates queue is for
    overnight Claude suggestions; leaving this correction there would keep
    flags stale until a manual review, so the write goes straight to
    term_context.json. The audit callback runs inside the context lock,
    before the atomic replace. If it raises, term_context.json stays as it was.
    """
    _json_object(body)
    _reject_unknown_fields(body, _MENTOR_BODY_KEYS)
    _reject_nulls(body)
    actor = actor or _actor(body)
    reason = _reason(body)
    updates = {key: body[key] for key in term_context.MENTOR_WRITABLE_FIELDS if key in body}

    def _audit(old, new, _record):
        append_external_audit(_audit_entry(
            actor, "mentor_update", {"record": "mentor"}, reason, old, new,
        ))

    result = term_context.patch_mentor(updates, audit_fn=_audit)
    return {
        "ok": True,
        "changed": result["changed"],
        "audit_logged": result["changed"],
        "changes": result["changes"],
        "mentor": result["record"],
    }


def apply_internship_write(body, actor=None):
    """
    Patch one internship row and audit the diff.

    `company` selects the row. `role` is required only when that company has
    more than one application. Status, last_update, next_action, and notes
    are the only stored fields this route will change.
    """
    _json_object(body)
    _reject_unknown_fields(body, _INTERNSHIP_PATCH_KEYS)
    _reject_nulls(body)
    actor = actor or _actor(body)
    reason = _reason(body)
    if "company" not in body:
        raise ValueError("company is required")
    updates = {key: body[key] for key in term_context.INTERNSHIP_WRITABLE_FIELDS if key in body}
    role = body["role"] if "role" in body else None

    def _audit(old, new, record):
        target = {
            "company": record.get("company", ""),
            "role": record.get("role", ""),
        }
        append_external_audit(_audit_entry(
            actor, "internship_update", target, reason, old, new,
        ))

    result = term_context.patch_internship(
        body["company"], updates, role=role, audit_fn=_audit,
    )
    return {
        "ok": True,
        "changed": result["changed"],
        "audit_logged": result["changed"],
        "changes": result["changes"],
        "internship": result["record"],
    }


def apply_internship_add(body, actor=None):
    """
    Append one internship application and audit the new row.

    Duplicate company+role (case-insensitive) is rejected. last_update
    defaults to today inside term_context.add_internship when omitted.
    """
    _json_object(body)
    _reject_unknown_fields(body, _INTERNSHIP_ADD_KEYS)
    _reject_nulls(body)
    actor = actor or _actor(body)
    reason = _reason(body)
    for key in ("company", "role", "status"):
        if key not in body:
            raise ValueError(f"{key} is required")

    def _audit(old, new, _record):
        append_external_audit(_audit_entry(
            actor,
            "internship_add",
            {"company": new.get("company"), "role": new.get("role")},
            reason,
            old,
            new,
        ))

    result = term_context.add_internship(
        body["company"],
        body["role"],
        body["status"],
        last_update=body["last_update"] if "last_update" in body else None,
        next_action=body["next_action"] if "next_action" in body else "",
        notes=body["notes"] if "notes" in body else "",
        audit_fn=_audit,
    )
    return {
        "ok": True,
        "changed": True,
        "audit_logged": True,
        "internship": result["record"],
    }


def redact_account_numbers(value):
    """
    Redact account/card numbers, masked tails and emails in a JSON-like structure.

    Finance descriptions quote internal transfer account numbers. Totals,
    dates, and category names do not contain a run that long.
    """
    if isinstance(value, str):
        def _digits(match):
            return match.group(0) if match.group("date") else "[redacted]"
        value = _ACCOUNT_DIGITS.sub(_digits, value)
        value = _MASKED_TAIL.sub("[redacted]", value)
        return _EMAIL.sub("[redacted]", value)
    if isinstance(value, list):
        return [redact_account_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_account_numbers(item) for key, item in value.items()}
    return value


def _parse_iso_date(raw, name):
    """Parse YYYY-MM-DD, or return None when the query param is absent."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return datetime.date.fromisoformat(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD")


def spending_window(start_raw, end_raw, today=None):
    """
    Inclusive spending window. Defaults to the last 7 days ending today.

    start and end must be sent together. The window cannot run past today
    or exceed 366 days.
    """
    today = today or datetime.datetime.now(TIMEZONE).date()
    start = _parse_iso_date(start_raw, "start")
    end = _parse_iso_date(end_raw, "end")
    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    if start is None:
        end = today
        start = today - datetime.timedelta(days=6)
    if end < start:
        raise ValueError("end must be on or after start")
    if end > today:
        raise ValueError("end must not be in the future")
    if (end - start).days + 1 > 366:
        raise ValueError("date range must be 366 days or fewer")
    return start, end


def get_spending_payload(start, end):
    """
    Everyday-account spend for an inclusive range, versus the weekly budget.

    Category totals use finance_tracker.summarise_spending. Live Back Tap
    totals are included when the window reaches today. No raw descriptions
    leave this function without account-number redaction.
    """
    import finance_tracker

    goals = finance_tracker.get_finance_goals()
    weekly_budget = goals.get("weekly_budget", 75.0)
    if not finance_tracker.EVERYDAY_CSV.exists():
        return {
            "available": False,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "weekly_budget": weekly_budget,
            "by_category": {},
            "total_spend": 0.0,
            "reason": "no everyday account export",
        }

    transactions = finance_tracker.parse_stgeorge_csv(finance_tracker.EVERYDAY_CSV)
    summary = finance_tracker.summarise_spending(
        transactions, start, end, weekly_budget, redact=redact_account_numbers,
    )
    summary["available"] = True

    today = datetime.datetime.now(TIMEZONE).date()
    if end >= today:
        try:
            from live_spend import get_live_summary
            days = max((end - start).days + 1, 1)
            live = get_live_summary(days=days, transactions=transactions)
        except Exception as e:
            print(f"⚠️  /finance/spending live spend failed: {e}")
            live = {"available": False}
        if live.get("available"):
            summary["live"] = {
                "total": live["total"],
                "count": live["count"],
                "by_category": {
                    cat: round(amount, 2) for cat, amount in live.get("by_category", {}).items()
                },
                "matched": live.get("matched"),
                "unmatched_count": len(live.get("unmatched") or []),
            }
    return redact_account_numbers(summary)


def get_savings_payload():
    """
    Savings balance against exchange_target (term_context us_exchange goals).

    The goal figures are returned even when the savings CSV is missing.
    Account numbers are not part of this payload.
    """
    import finance_tracker

    goals = finance_tracker.get_finance_goals()
    savings = finance_tracker.analyse_savings()
    projected = savings["projected_date"]
    deadline = savings["deadline"]
    if isinstance(projected, datetime.datetime):
        projected = projected.date()
    if isinstance(deadline, datetime.datetime):
        deadline = deadline.date()
    return redact_account_numbers({
        "available": finance_tracker.SAVINGS1_CSV.exists(),
        "exchange_target": {
            "savings_goal": goals["savings_goal"],
            "savings_deadline": goals["savings_deadline"],
            "weekly_budget": goals["weekly_budget"],
            "monthly_budget": goals["monthly_budget"],
            "monthly_income": goals["monthly_income"],
        },
        "balance": round(savings["total"], 2),
        "remaining": round(savings["remaining"], 2),
        "pct": round(savings["pct"], 1),
        "on_track": bool(savings["on_track"]),
        "projected_date": projected.isoformat(),
        "days_to_deadline": savings["days_to_deadline"],
        "monthly_savings": round(savings["monthly_savings"], 2),
    })


def get_subscriptions_payload(months):
    """Detected recurring charges from the everyday CSV, known vs needs review."""
    from subscription_audit import summarise_subscriptions
    return redact_account_numbers(summarise_subscriptions(months=months))


def load_reselling_inventory():
    """
    All-time reselling P&L from the Google Sheet, as plain numbers.

    Item notes are omitted. Raises if the sheet cannot be read; callers
    treat that as a soft failure so the cashflow summary still returns.
    """
    from reselling_tracker import compute_summary, load_items

    summary = compute_summary(load_items())

    def _flip(item):
        if item is None:
            return None
        margin = item.margin_pct
        return {
            "name": item.name,
            "category": item.category,
            "net": round(item.net_profit or 0, 2),
            "margin_pct": round(margin, 1) if margin is not None else None,
        }

    by_category = {
        cat: {
            "profit": round(data["profit"], 2),
            "revenue": round(data["revenue"], 2),
            "count": data["count"],
        }
        for cat, data in summary["by_category"].items()
    }
    worst = summary["worst_flip"]
    if worst is summary["best_flip"]:
        worst = None
    return {
        "sold_count": summary["sold_count"],
        "stock_count": summary["stock_count"],
        "pending_count": summary["pending_count"],
        "total_revenue": round(summary["total_revenue"], 2),
        "total_cogs": round(summary["total_cogs"], 2),
        "total_fees": round(summary["total_fees"], 2),
        "net_pl": round(summary["net_pl"], 2),
        "avg_margin_pct": round(summary["avg_margin_pct"], 1),
        "capital_stock": round(summary["capital_stock"], 2),
        "capital_pending": round(summary["capital_pending"], 2),
        "deposits_owing": round(summary["deposits_owing"], 2),
        "by_category": by_category,
        "best_flip": _flip(summary["best_flip"]),
        "worst_flip": _flip(worst),
        "stock_names": [item.name for item in summary["stock_items"][:5]],
        "pending_names": [item.name for item in summary["pending_items"][:5]],
    }


def get_reselling_payload(days):
    """
    Reselling cashflow over `days` plus the sheet P&L when the sheet loads.

    Cashflow comes from finance_tracker.analyse_reselling (Revolut CSV,
    otherwise the investing CSV). A sheet failure does not drop the cashflow.
    """
    import finance_tracker

    try:
        cash = finance_tracker.analyse_reselling(days=days)
    except Exception as e:
        print(f"⚠️  /finance/reselling cashflow failed: {e}")
        cash = {
            "available": False,
            "deployed": 0.0,
            "returned": 0.0,
            "net": 0.0,
            "balance": None,
            "txn_count": 0,
            "days": days,
            "source": "none",
        }

    inventory = None
    inventory_error = None
    try:
        inventory = load_reselling_inventory()
    except Exception as e:
        print(f"⚠️  /finance/reselling sheet failed: {e}")
        inventory_error = "reselling sheet unavailable"

    return redact_account_numbers({
        "cashflow": {
            "available": bool(cash.get("available")),
            "days": cash.get("days", days),
            "source": cash.get("source", "none"),
            "deployed": cash.get("deployed", 0.0),
            "returned": cash.get("returned", 0.0),
            "net": cash.get("net", 0.0),
            "balance": cash.get("balance"),
            "transaction_count": cash.get("txn_count", 0),
        },
        "inventory": inventory,
        "inventory_error": inventory_error,
    })


def get_finance_payload(start, end, months, reselling_days):
    """
    One weekly-check payload. Each section fails on its own.

    `unavailable` lists sections that raised. A missing CSV is available=false
    inside the section and is not listed here.
    """
    sections = {
        "spending": lambda: get_spending_payload(start, end),
        "savings": get_savings_payload,
        "subscriptions": lambda: get_subscriptions_payload(months),
        "reselling": lambda: get_reselling_payload(reselling_days),
    }
    payload = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "subscription_months": months,
        "reselling_days": reselling_days,
    }
    unavailable = []
    for name, loader in sections.items():
        try:
            payload[name] = loader()
        except Exception as e:
            print(f"⚠️  /finance {name} failed: {e}")
            payload[name] = {"available": False, "error": f"{name} unavailable"}
            unavailable.append(name)
    payload["unavailable"] = unavailable
    return payload


def _bounded_int(raw, default, lo, hi, name):
    """Parse a query integer, or return default. Raises ValueError when out of range."""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def _body_object(body):
    """Return the parsed JSON body, or raise ValueError when it is not an object."""
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    return body


def _client_key(request):
    """
    Identify the caller for failed-login throttling.

    Behind Tailscale Funnel the socket peer is always the local proxy, so use
    the last X-Forwarded-For hop (the one the proxy appended, which a client
    cannot forge). Falls back to the socket address.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    return hops[-1] if hops else (request.remote_addr or "unknown")


def create_app(token=None):
    """
    Build the Flask app. Every route requires a bearer token.

    token: single-token override for tests (full access). None reads
    JARVIS_API_TOKENS and JARVIS_API_TOKEN. Refuses to build the app when no
    token is configured so a misconfigured service cannot start open.
    """
    from flask import Flask, g, jsonify, request
    from werkzeug.exceptions import HTTPException

    tokens = load_tokens() if token is None else ([(token, None)] if token else [])
    if not tokens:
        raise RuntimeError("JARVIS_API_TOKEN (or JARVIS_API_TOKENS) is not set")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    limiter = FailureLimiter()

    @app.before_request
    def _require_bearer():
        client = _client_key(request)
        if limiter.blocked(client):
            return jsonify({"error": "too many failed attempts"}), 429
        ok, actor = match_bearer(request.headers.get("Authorization", ""), tokens)
        if not ok:
            limiter.record(client)
            return jsonify({"error": "unauthorized"}), 401
        g.actor = actor
        allowed = BOT_SCOPES.get(actor)
        if actor is not None and allowed is not None and request.endpoint not in allowed \
                and request.endpoint is not None:
            return jsonify({"error": "forbidden for this token"}), 403

    @app.after_request
    def _no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def _too_large(_e):
        return jsonify({"error": f"body must be {MAX_BODY_BYTES} bytes or fewer"}), 413

    @app.errorhandler(500)
    def _server_error(_e):
        return jsonify({"error": "internal error"}), 500

    def _limited(name):
        """Return a 429 response when today's cap for `name` is used up, else None."""
        try:
            if consume_daily(name):
                return None
        except Exception as e:
            print(f"⚠️  daily cap check failed: {e}")
            return None
        return jsonify({"error": f"daily limit reached for {name}"}), 429

    @app.get("/health")
    def health():
        """Authenticated liveness probe."""
        return jsonify({"ok": True, "service": "jarvis-agent-api"})

    @app.post("/ask")
    def ask():
        """Free-text question. Same conversational brain as Telegram, read-only."""
        try:
            body = _body_object(request.get_json(silent=True))
            question = body.get("question", "")
            if not isinstance(question, str):
                raise ValueError("question must be a string")
            capped = _limited("ask") if question.strip() else None
            if capped:
                return capped
            answer = answer_question(question)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except HTTPException:
            raise
        except Exception as e:
            print(f"⚠️  /ask failed: {e}")
            return jsonify({"error": "ask failed"}), 500
        return jsonify({"ok": True, "answer": answer})

    @app.get("/flags")
    def flags():
        """Term flags and academic alerts."""
        try:
            return jsonify(get_flags_payload())
        except Exception as e:
            print(f"⚠️  /flags failed: {e}")
            return jsonify({"error": "flags failed"}), 500

    @app.get("/context")
    def context():
        """Term summary, deadlines, and upcoming calendar events. ?days=1..120."""
        try:
            days = _bounded_int(request.args.get("days"), 21, 1, 120, "days")
            return jsonify(get_context_payload(days_ahead=days))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /context failed: {e}")
            return jsonify({"error": "context failed"}), 500

    @app.get("/memory/search")
    def memory_search():
        """Semantic memory search. ?q=...&limit=1..10."""
        try:
            limit = _bounded_int(request.args.get("limit"), 5, 1, 10, "limit")
            query = request.args.get("q", "")
            capped = _limited("memory_search") if query.strip() else None
            if capped:
                return capped
            return jsonify(search_memory_payload(query, limit=limit))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /memory/search failed: {e}")
            return jsonify({"error": "memory search failed"}), 500

    @app.get("/brief")
    def brief():
        """Latest morning brief or Sunday weekly review, if one has been saved."""
        stored = load_latest_brief()
        if not stored:
            return jsonify({"error": "no brief saved yet"}), 404
        return jsonify(stored)

    @app.post("/spend")
    def spend():
        """Log one spend entry. Same validation as the Back Tap endpoint. Audited."""
        try:
            body = _body_object(request.get_json(silent=True))
            result = log_spend_entry(
                body.get("amount"),
                body.get("category"),
                body.get("note", ""),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except HTTPException:
            raise
        except Exception as e:
            print(f"⚠️  /spend failed: {e}")
            return jsonify({"error": "spend failed"}), 500
        if not result.get("duplicate"):
            try:
                append_external_audit(_audit_entry(
                    g.get("actor") or "legacy", "spend", {"record": "live_spend"},
                    "", None, result.get("entry"),
                ))
            except Exception as e:
                print(f"⚠️  /spend audit failed: {e}")
        return jsonify(result)

    def _commit(handler, label, success_status=200):
        """Run a term-context write and map record errors to HTTP status codes."""
        body = request.get_json(silent=True)
        try:
            return jsonify(handler(body, actor=g.get("actor"))), success_status
        except term_context.TermRecordNotFound as e:
            return jsonify({"error": str(e)}), 404
        except term_context.TermRecordConflict as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except HTTPException:
            raise
        except Exception as e:
            print(f"⚠️  {label} failed: {e}")
            return jsonify({"error": f"{label} failed"}), 500

    @app.patch("/mentor")
    def patch_mentor():
        """Correct the mentor record. GET /flags reads the same file."""
        return _commit(apply_mentor_write, "mentor update")

    @app.patch("/internships")
    def patch_internship():
        """Correct one internship row, matched by company and optional role."""
        return _commit(apply_internship_write, "internship update")

    @app.post("/internships")
    def add_internship():
        """Add one internship application."""
        return _commit(apply_internship_add, "internship add", success_status=201)

    def _finance_dates():
        return spending_window(request.args.get("start"), request.args.get("end"))

    @app.get("/finance")
    def finance():
        """Weekly check: spending, savings, subscriptions, and reselling."""
        try:
            start, end = _finance_dates()
            months = _bounded_int(request.args.get("months"), 3, 1, 12, "months")
            days = _bounded_int(request.args.get("days"), 7, 1, 120, "days")
            return jsonify(get_finance_payload(start, end, months, days))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /finance failed: {e}")
            return jsonify({"error": "finance failed"}), 500

    @app.get("/finance/spending")
    def finance_spending():
        """Category totals and spend versus the weekly budget for a date range."""
        try:
            start, end = _finance_dates()
            return jsonify(get_spending_payload(start, end))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /finance/spending failed: {e}")
            return jsonify({"error": "spending failed"}), 500

    @app.get("/finance/savings")
    def finance_savings():
        """Savings progress against the exchange_target goal."""
        try:
            return jsonify(get_savings_payload())
        except Exception as e:
            print(f"⚠️  /finance/savings failed: {e}")
            return jsonify({"error": "savings failed"}), 500

    @app.get("/finance/subscriptions")
    def finance_subscriptions():
        """Recurring charges detected on the everyday account. ?months=1..12."""
        try:
            months = _bounded_int(request.args.get("months"), 3, 1, 12, "months")
            return jsonify(get_subscriptions_payload(months))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /finance/subscriptions failed: {e}")
            return jsonify({"error": "subscriptions failed"}), 500

    @app.get("/finance/reselling")
    def finance_reselling():
        """Reselling cashflow plus sheet P&L. ?days=1..120, default 7."""
        try:
            days = _bounded_int(request.args.get("days"), 7, 1, 120, "days")
            return jsonify(get_reselling_payload(days))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /finance/reselling failed: {e}")
            return jsonify({"error": "reselling failed"}), 500

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis agent HTTP API")
    parser.add_argument("--serve", action="store_true", help="run the HTTP server on 127.0.0.1")
    args = parser.parse_args()

    if args.serve:
        port = int(os.environ.get("JARVIS_API_PORT", DEFAULT_PORT))
        print(f"\n🤖  Jarvis agent API on http://127.0.0.1:{port}\n")
        app = create_app()
        try:
            from waitress import serve
            serve(app, host="127.0.0.1", port=port, threads=4)
        except ImportError:
            # Werkzeug's dev server is the fallback until waitress is installed.
            app.run(host="127.0.0.1", port=port)
    else:
        parser.print_help()

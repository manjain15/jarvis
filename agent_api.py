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
from pathlib import Path

import pytz

import config
import term_context
from json_store import atomic_write_json, file_lock

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
BRIEF_FILE = DATA_DIR / "latest_brief.json"
AUDIT_FILE = DATA_DIR / "agent_api_audit.jsonl"

_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_MENTOR_BODY_KEYS = {"actor", "reason", *term_context.MENTOR_WRITABLE_FIELDS}
_INTERNSHIP_PATCH_KEYS = {"actor", "reason", "company", "role", *term_context.INTERNSHIP_WRITABLE_FIELDS}
_INTERNSHIP_ADD_KEYS = {"actor", "reason", "company", "role", "status", "last_update", "next_action", "notes"}

TIMEZONE     = pytz.timezone(config.TIMEZONE)
DEFAULT_PORT = 5557

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<br\s*/?>|</p>|</h[1-6]>|</li>|</div>|</tr>", re.I)


def api_token():
    """Return the configured bearer token, or '' if it is unset."""
    return os.environ.get("JARVIS_API_TOKEN", "").strip()


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


def log_spend_entry(amount, category, note=""):
    """
    Validate and append one live-spend entry via live_spend.log_spend.

    Raises ValueError on bad input. Returns the same confirmation shape the
    Back Tap endpoint uses, plus the stored entry.
    """
    from live_spend import get_live_summary, log_spend
    entry = log_spend(amount, category, note)
    week_total = get_live_summary(days=7).get("total", entry["amount"])
    return {
        "ok": True,
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
    """Return the external agent name recorded in the audit log."""
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


def apply_mentor_write(body):
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
    actor = _actor(body)
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


def apply_internship_write(body):
    """
    Patch one internship row and audit the diff.

    `company` selects the row. `role` is required only when that company has
    more than one application. Status, last_update, next_action, and notes
    are the only stored fields this route will change.
    """
    _json_object(body)
    _reject_unknown_fields(body, _INTERNSHIP_PATCH_KEYS)
    _reject_nulls(body)
    actor = _actor(body)
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


def apply_internship_add(body):
    """
    Append one internship application and audit the new row.

    Duplicate company+role (case-insensitive) is rejected. last_update
    defaults to today inside term_context.add_internship when omitted.
    """
    _json_object(body)
    _reject_unknown_fields(body, _INTERNSHIP_ADD_KEYS)
    _reject_nulls(body)
    actor = _actor(body)
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


def create_app(token=None):
    """
    Build the Flask app. Every route requires the bearer token.

    token: override for tests. None reads JARVIS_API_TOKEN. Refuses to build
    the app when the token is empty so a misconfigured service cannot start open.
    """
    from flask import Flask, jsonify, request

    expected = api_token() if token is None else token
    if not expected:
        raise RuntimeError("JARVIS_API_TOKEN is not set")

    app = Flask(__name__)

    @app.before_request
    def _require_bearer():
        if not bearer_matches(request.headers.get("Authorization", ""), expected):
            return jsonify({"error": "unauthorized"}), 401

    @app.after_request
    def _no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        """Authenticated liveness probe."""
        return jsonify({"ok": True, "service": "jarvis-agent-api"})

    @app.post("/ask")
    def ask():
        """Free-text question. Same conversational brain as Telegram, read-only."""
        body = request.get_json(silent=True) or {}
        try:
            answer = answer_question(body.get("question", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
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
            return jsonify(search_memory_payload(request.args.get("q", ""), limit=limit))
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
        """Log one spend entry. Same validation as the Back Tap endpoint."""
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(log_spend_entry(
                body.get("amount"),
                body.get("category"),
                body.get("note", ""),
            ))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"⚠️  /spend failed: {e}")
            return jsonify({"error": "spend failed"}), 500

    def _commit(handler, label, success_status=200):
        """Run a term-context write and map record errors to HTTP status codes."""
        body = request.get_json(silent=True)
        try:
            return jsonify(handler(body)), success_status
        except term_context.TermRecordNotFound as e:
            return jsonify({"error": str(e)}), 404
        except term_context.TermRecordConflict as e:
            return jsonify({"error": str(e)}), 409
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
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

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis agent HTTP API")
    parser.add_argument("--serve", action="store_true", help="run the HTTP server on 127.0.0.1")
    args = parser.parse_args()

    if args.serve:
        port = int(os.environ.get("JARVIS_API_PORT", DEFAULT_PORT))
        print(f"\n🤖  Jarvis agent API on http://127.0.0.1:{port}\n")
        create_app().run(host="127.0.0.1", port=port)
    else:
        parser.print_help()

"""
Jarvis — Telegram Always-On Channel
====================================
Two-way conversation with Jarvis over Telegram, working natively on
your iPhone and Mac (and anywhere else you have Telegram).

HOW IT WORKS:
  1. Long-polls the Telegram Bot API for messages addressed to your bot
  2. Only responds to your authorised chat ID (everyone else is ignored)
  3. Slash commands hit existing Jarvis modules directly
  4. Free-form messages → Claude with full Jarvis context (profile +
     term context + recent memory + last 10 conversation turns)
  5. Every turn is persisted to data/telegram_thread.jsonl so the
     nightly memory_system can ingest it

ONE-TIME SETUP:
  1. Open Telegram, message @BotFather, send /newbot. Pick a name
     and username. BotFather replies with a TOKEN like:
       1234567890:AAH...
     Paste it into config.TELEGRAM_BOT_TOKEN.

  2. Open your new bot in Telegram and send it any message
     (e.g. "hi") so Telegram registers the chat.

  3. Run:
       python jarvis_telegram.py --setup
     It will discover your chat ID and write it to config.py.

  4. Test:
       python jarvis_telegram.py --test
     Sends "Jarvis online." to your Telegram.

  5. Run the daemon:
       python jarvis_telegram.py
     (or wrap it in a launchd plist for always-on.)

SLASH COMMANDS:
  /help              — list commands
  /brief             — run the morning brief now and email it
  /flags             — show today's term/internship/mentor flags
  /proposals         — list pending profile + term proposals
  /done SUB "Name"   — mark assessment submitted (e.g. /done COMP2511 "Assignment 1")
  /internship Co key=val ...   — update internship (status, next_action)
  /mentor "topic" [awaiting]   — log a mentor touchpoint
  /log <free text>             — append a one-line note to episodic memory

Anything else → conversation with Claude, who has your profile + term
context + memory in scope.
"""

import sys
import json
import time
import shlex
import datetime
import argparse
import threading
import subprocess
from pathlib import Path

import pytz
import requests
import anthropic

import config
import evening_checkin

SCRIPT_DIR  = Path(__file__).parent
DATA_DIR    = SCRIPT_DIR / "data"
MEMORY_DIR  = SCRIPT_DIR / "memory"
THREAD_FILE = DATA_DIR / "telegram_thread.jsonl"
OFFSET_FILE = DATA_DIR / "telegram_offset.txt"

DATA_DIR.mkdir(exist_ok=True)
MEMORY_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MODEL    = "claude-sonnet-4-6"
HISTORY_WINDOW = 10  # last N turns sent to Claude


# ── Telegram HTTP helpers ────────────────────────────────────────────────────

def _api(method, params=None, timeout=35):
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError("config.TELEGRAM_BOT_TOKEN is empty — see setup instructions.")
    url = API_BASE.format(token=token, method=method)
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]


def send_message(text, chat_id=None, parse_mode=None):
    """
    Public helper — any Jarvis script can `from jarvis_telegram import send_message`
    and push a message to the user's Telegram.
    """
    if not chat_id:
        chat_id = config.TELEGRAM_CHAT_ID
    if not chat_id:
        return False
    # Telegram caps messages at 4096 chars — chunk if needed
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for chunk in chunks:
        params = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            params["parse_mode"] = parse_mode
        _api("sendMessage", params)
    return True


def get_updates(offset, long_poll=25):
    return _api("getUpdates", {"offset": offset, "timeout": long_poll}, timeout=long_poll + 10)


# ── Setup & test helpers ─────────────────────────────────────────────────────

def discover_chat_id():
    """Reads the most recent getUpdates and writes the chat_id to config.py."""
    print("🔍  Looking for recent messages to your bot...")
    print("    (If nothing's found, open Telegram and send your bot any message first.)")
    result = _api("getUpdates", {"timeout": 0})
    if not result:
        print("❌  No messages found. Send a message to your bot in Telegram, then re-run --setup.")
        sys.exit(1)

    chat_ids = {u["message"]["chat"]["id"]
                for u in result
                if "message" in u and "chat" in u["message"]}

    if len(chat_ids) > 1:
        print(f"⚠️   Multiple chats found: {chat_ids}. Using the most recent.")

    chat_id = result[-1]["message"]["chat"]["id"]
    print(f"✅  Found chat ID: {chat_id}")

    config_path = SCRIPT_DIR / "config.py"
    text = config_path.read_text()
    import re
    new_text = re.sub(
        r"TELEGRAM_CHAT_ID\s*=\s*\d+",
        f"TELEGRAM_CHAT_ID   = {chat_id}",
        text,
    )
    config_path.write_text(new_text)
    print(f"📝  Wrote TELEGRAM_CHAT_ID = {chat_id} to config.py.")
    print("    Now run:  python jarvis_telegram.py --test")


# ── Thread persistence ───────────────────────────────────────────────────────

def _now_iso():
    return datetime.datetime.now(TIMEZONE).isoformat()


def append_turn(role, text, meta=None):
    entry = {"ts": _now_iso(), "role": role, "text": text}
    if meta:
        entry["meta"] = meta
    with THREAD_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def load_recent_turns(n=HISTORY_WINDOW):
    if not THREAD_FILE.exists():
        return []
    lines = THREAD_FILE.read_text().splitlines()
    out = []
    for line in lines[-n * 2:]:  # rough — turns alternate
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_offset():
    if not OFFSET_FILE.exists():
        return 0
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def save_offset(offset):
    OFFSET_FILE.write_text(str(offset))


# ── Jarvis context for Claude ────────────────────────────────────────────────

def build_static_context():
    """
    Returns the slow-changing context Jarvis needs to feel like he knows you:
    profile + term summary + recent semantic memory. Cached via prompt caching
    so we only pay for it on the first message after a cold start.
    """
    parts = []

    profile_path = SCRIPT_DIR / "profile.md"
    if profile_path.exists():
        parts.append("PROFILE:\n" + profile_path.read_text())

    try:
        from term_context import get_term_summary, get_flags
        summary = get_term_summary()
        flags   = get_flags()
        parts.append("TERM CONTEXT:\n" + json.dumps(summary, indent=2, default=str))
        if flags:
            parts.append("TERM FLAGS:\n" + "\n".join(f"- {f}" for f in flags))
    except Exception:
        pass

    semantic = MEMORY_DIR / "semantic.md"
    if semantic.exists():
        parts.append("SEMANTIC MEMORY:\n" + semantic.read_text()[:3000])

    return "\n\n────────────────────────────\n".join(parts)


SYSTEM_PROMPT = """You are Jarvis — Manav's personal AI assistant, talking to him over Telegram.

Voice: direct, concise, intelligent. Like a smart friend who knows him deeply.
No corporate filler. No "Great question!". No "I hope this helps!". Just be useful.

You have his full profile, current term context (subjects, assessments,
internship pipeline, mentor, portfolio targets), and recent memory in scope.
Use them. Be specific to his life, not generic.

Replies should fit on a phone screen — usually 1-4 short sentences.
Only go longer when the topic genuinely needs it.

When he tells you something worth remembering (an interview invite, a
mentor reply, a submitted assignment, a new application), suggest the
matching slash command in one short line — e.g.:
   "Worth logging: /internship Canva status=interview next=\\"Prep system design\\""
Don't apply the change yourself. He approves these manually.
"""


def chat_with_claude(user_message, static_context, recent_turns):
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    messages = []
    for turn in recent_turns:
        role = "assistant" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT},
            {
                "type": "text",
                "text": static_context,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=messages,
    )
    return response.content[0].text.strip()


# ── Slash command router ────────────────────────────────────────────────────

def cmd_help(_args):
    return (
        "Jarvis commands:\n"
        "/brief — run morning brief now\n"
        "/flags — today's term flags\n"
        "/deadlines — upcoming assessments across all courses\n"
        "/proposals — pending profile + term proposals\n"
        "/approve <profile|term> <id> — apply one pending proposal\n"
        "/reject <profile|term> <id> — reject one pending proposal\n"
        "/approveall [profile|term] — apply all pending proposals\n"
        "/done SUB \"Assessment name\" — mark submitted\n"
        "/internship Co key=val ... — update internship\n"
        "/mentor \"topic\" [true|false] — log mentor touchpoint\n"
        "/log <text> — append a note to episodic memory\n"
        "/status — health-check the VPS daemons\n"
        "/checkin — start the evening check-in now\n"
        "/cancel — abort an in-progress check-in\n"
        "\nAnything else → I respond conversationally."
    )


def cmd_brief(_args):
    def _run():
        try:
            from morning_brief import run_brief
            run_brief()
            send_message("✅ Morning brief sent to your inbox.")
        except Exception as e:
            send_message(f"⚠️ Brief failed: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return "⏳ Running morning brief — I'll ping you when it's sent."


def cmd_deadlines(_args):
    """/deadlines — upcoming assessments across all courses, on demand."""
    try:
        import term_context
        ctx = term_context.load_context()
        items = term_context.get_upcoming_assessments(ctx, days_ahead=120)
    except Exception as e:
        return f"⚠️ Could not load deadlines: {e}"
    if not items:
        return "✅ No assessments due in the next 120 days."
    lines = ["📅 Upcoming assessments:"]
    for a in items:
        try:
            due = datetime.datetime.fromisoformat(a["due"]).strftime("%-d %b")
        except Exception:
            due = a.get("due", "?")
        weight = f" [{a['weight']}%]" if a.get("weight") else ""
        lines.append(f"  {due} ({a['days_left']}d) {a['subject']} — {a['name']}{weight}")
    return "\n".join(lines)


def cmd_flags(_args):
    try:
        from term_context import get_flags
        flags = get_flags()
    except Exception as e:
        return f"⚠️ Could not load term flags: {e}"
    if not flags:
        return "✅ No urgent flags today."
    return "🚩 Today's flags:\n" + "\n".join(f"• {f}" for f in flags)


def cmd_proposals(_args):
    out = []
    try:
        from update_profile import format_proposals_for_brief
        t = format_proposals_for_brief()
        if t:
            out.append(t)
    except Exception:
        pass
    try:
        from term_updates import format_proposals_for_brief as ft
        t = ft()
        if t:
            out.append(t)
    except Exception:
        pass
    return "\n\n".join(out) if out else "✅ No pending proposals."


def _resolve_proposal(module_name, pid, new_status):
    """Sets a single pending proposal's status by id. Returns the matched proposal dict, or None."""
    mod = __import__(module_name, fromlist=["load_proposals", "save_proposals"])
    proposals = mod.load_proposals()
    match = next((p for p in proposals if str(p["id"]) == str(pid) and p["status"] == "pending"), None)
    if not match:
        return None
    resolved = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    for p in proposals:
        if str(p["id"]) == str(pid):
            p["status"], p["resolved_date"] = new_status, resolved
    mod.save_proposals(proposals)
    return match


def cmd_approve(args):
    """/approve <profile|term> <id> — applies one pending proposal from Telegram."""
    if len(args) < 2 or args[0].lower() not in ("profile", "term"):
        return "Usage: /approve <profile|term> <id>"
    kind, pid = args[0].lower(), args[1]
    module_name = "update_profile" if kind == "profile" else "term_updates"
    mod = __import__(module_name, fromlist=["load_proposals"])
    proposals = mod.load_proposals()
    match = next((p for p in proposals if str(p["id"]) == str(pid) and p["status"] == "pending"), None)
    if not match:
        return f"⚠️ No pending {kind} proposal with id {pid}."
    try:
        if not mod.apply_update(match):
            return f"❌ Failed to apply {kind} proposal [{pid}] — left pending."
    except Exception as e:
        return f"⚠️ {e}"
    _resolve_proposal(module_name, pid, "approved")
    return f"✅ Applied {kind} proposal [{pid}]."


def cmd_reject(args):
    """/reject <profile|term> <id> — rejects one pending proposal from Telegram."""
    if len(args) < 2 or args[0].lower() not in ("profile", "term"):
        return "Usage: /reject <profile|term> <id>"
    kind, pid = args[0].lower(), args[1]
    module_name = "update_profile" if kind == "profile" else "term_updates"
    try:
        match = _resolve_proposal(module_name, pid, "rejected")
    except Exception as e:
        return f"⚠️ {e}"
    if not match:
        return f"⚠️ No pending {kind} proposal with id {pid}."
    return f"⏭ Rejected {kind} proposal [{pid}]."


def cmd_approveall(args):
    """/approveall [profile|term] — applies every pending proposal in one or both queues."""
    kind = args[0].lower() if args else "both"
    if kind not in ("profile", "term", "both"):
        return "Usage: /approveall [profile|term] — defaults to both."
    resolved = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    results = []

    if kind in ("profile", "both"):
        try:
            from update_profile import get_pending_proposals, load_proposals, save_proposals, apply_all_updates
            pending = get_pending_proposals()
            if not pending:
                results.append("profile: nothing pending")
            elif apply_all_updates(pending):
                proposals = load_proposals()
                for p in proposals:
                    if p["status"] == "pending":
                        p["status"], p["resolved_date"] = "approved", resolved
                save_proposals(proposals)
                results.append(f"profile: applied {len(pending)}")
            else:
                results.append("profile: batch apply failed — left pending")
        except Exception as e:
            results.append(f"profile: ⚠️ {e}")

    if kind in ("term", "both"):
        try:
            from term_updates import get_pending_proposals, load_proposals, save_proposals, apply_update
            pending = get_pending_proposals()
            if not pending:
                results.append("term: nothing pending")
            else:
                proposals = load_proposals()
                applied = 0
                for p in pending:
                    if apply_update(p):
                        for proposal in proposals:
                            if proposal["id"] == p["id"]:
                                proposal["status"], proposal["resolved_date"] = "approved", resolved
                        applied += 1
                save_proposals(proposals)
                results.append(f"term: applied {applied}/{len(pending)}")
        except Exception as e:
            results.append(f"term: ⚠️ {e}")

    return "✅ Approve-all results:\n" + "\n".join(f"• {r}" for r in results)


def cmd_done(args):
    if len(args) < 2:
        return 'Usage: /done SUBJECT "Assessment name"  (e.g. /done COMP2511 "Assignment 1")'
    subject  = args[0]
    name     = " ".join(args[1:])
    try:
        from term_context import mark_assessment_done
        mark_assessment_done(subject, name)
        return f"✅ Marked {subject} — {name} as submitted."
    except Exception as e:
        return f"⚠️ {e}"


def cmd_internship(args):
    if not args:
        return 'Usage: /internship Canva status=interview next_action="Prep system design"'
    company = args[0]
    kwargs  = {}
    for arg in args[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kwargs[k.strip()] = v.strip()
    if not kwargs:
        return "No fields to update. Use key=value pairs (e.g. status=interview)."
    try:
        from term_context import update_internship
        update_internship(company, **kwargs)
        return f"✅ Updated {company}: {kwargs}"
    except Exception as e:
        return f"⚠️ {e}"


def cmd_mentor(args):
    if not args:
        return 'Usage: /mentor "topic discussed" [awaiting=true|false]'
    topic    = args[0]
    awaiting = True
    if len(args) > 1:
        awaiting = args[1].lower() not in ("false", "no", "0")
    try:
        from term_context import update_mentor
        update_mentor(topic, awaiting=awaiting)
        return f"✅ Mentor updated — topic: {topic} | awaiting={awaiting}"
    except Exception as e:
        return f"⚠️ {e}"


def cmd_log(args):
    if not args:
        return "Usage: /log <free text> — appends a note to episodic memory."
    text = " ".join(args)
    today = datetime.datetime.now(TIMEZONE).date()
    episodic = MEMORY_DIR / "episodic.md"
    header   = f"### {today.strftime('%A %d %b %Y')}"
    existing = episodic.read_text() if episodic.exists() else ""
    if header in existing:
        with episodic.open("a") as f:
            f.write(f"\n- (telegram) {text}")
    else:
        with episodic.open("a") as f:
            f.write(f"\n\n{header}\n- (telegram) {text}")
    return f"✅ Logged to episodic: {text}"


# Units checked by /status: the always-on service, then the timer-driven jobs.
STATUS_ALWAYS_ON   = ["jarvis-telegram"]
STATUS_TIMER_UNITS = [
    "jarvis-morningbrief", "jarvis-alerts", "jarvis-gmail",
    "jarvis-memory", "jarvis-jobsearch",
]


def _systemctl(*cmd):
    """Run a read-only shell query (systemctl/df/uptime) and return stripped stdout, '' on error."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:
        return ""


def cmd_status(_args):
    """/status — health-check the VPS daemons: telegram service, all timers, host disk/uptime.

    Replies are sent as plain text (no parse_mode), so this returns a plain string.
    """
    lines = ["🩺 Jarvis status"]

    # Always-on services: the .service itself should be active.
    for unit in STATUS_ALWAYS_ON:
        state = _systemctl("systemctl", "is-active", f"{unit}.service")
        mark = "✅" if state == "active" else "❌"
        lines.append(f"{mark} {unit.replace('jarvis-', '')}: {state or 'unknown'}")

    # Timer-driven units: the timer should be active; flag a failed last run.
    lines.append("\nTimers:")
    for unit in STATUS_TIMER_UNITS:
        active = _systemctl("systemctl", "is-active", f"{unit}.timer")
        failed = _systemctl("systemctl", "is-failed", f"{unit}.service")
        name = unit.replace("jarvis-", "")
        if failed == "failed":
            lines.append(f"⚠️ {name}: last run FAILED")
        elif active == "active":
            lines.append(f"✅ {name}: scheduled")
        else:
            lines.append(f"❌ {name}: {active or 'unknown'}")

    # Host health.
    disk = _systemctl("df", "-h", "/").splitlines()
    if len(disk) >= 2:
        cols = disk[-1].split()
        if len(cols) >= 5:
            lines.append(f"\nDisk /: {cols[4]} used ({cols[2]}/{cols[1]})")
    uptime = _systemctl("uptime", "-p")
    if uptime:
        lines.append(f"Uptime: {uptime.replace('up ', '')}")

    return "\n".join(lines)


COMMANDS = {
    "/help":       cmd_help,
    "/start":      cmd_help,
    "/brief":      cmd_brief,
    "/flags":      cmd_flags,
    "/deadlines":  cmd_deadlines,
    "/proposals":  cmd_proposals,
    "/approve":    cmd_approve,
    "/reject":     cmd_reject,
    "/approveall": cmd_approveall,
    "/done":       cmd_done,
    "/internship": cmd_internship,
    "/mentor":     cmd_mentor,
    "/log":        cmd_log,
    "/status":     cmd_status,
}


def handle_command(text):
    """Returns reply text if `text` is a known slash command, else None."""
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    cmd = parts[0].split("@")[0].lower()  # strip /cmd@BotName if present
    if cmd not in COMMANDS:
        return None
    return COMMANDS[cmd](parts[1:])


# ── Evening check-in session (Telegram-driven) ──────────────────────────────
# The check-in is a stateful conversation: one question per message. State lives
# here (the daemon is the only always-on process). The question plan, answer
# parsing, and artifact writing all live in evening_checkin.py — this is just the
# message-loop driver. Check-in turns are NOT written to the conversation thread,
# so they don't pollute the Claude context window.

CHECKIN = {"active": False, "responses": None, "queue": [], "current": None}
_autostart_done_date = None  # guards against re-triggering the 21:00 auto-start


def _reset_checkin():
    CHECKIN.update(active=False, responses=None, queue=[], current=None)


def _checkin_send_question(step):
    hint = {"yn": "  (y/n, or 'skip')",
            "number": "  (number, or 'skip')"}.get(step["type"], "  (or 'skip')")
    send_message(f"{step['prompt']}{hint}")


def _advance_checkin():
    """Ask the next queued question, or finalise if the queue is empty."""
    if not CHECKIN["queue"]:
        _finalize_checkin()
        return
    step = CHECKIN["queue"].pop(0)
    CHECKIN["current"] = step
    _checkin_send_question(step)


def _finalize_checkin():
    """Persist the check-in, generate the summary, send it, and clear state."""
    responses = CHECKIN["responses"]
    try:
        evening_checkin.save_responses(responses)
        summary = evening_checkin.generate_summary(responses)
        evening_checkin.save_summary(summary, responses["date"])
        send_message("✅ Check-in saved. Here's the read on today:\n\n" + summary)
    except Exception as e:
        send_message(f"⚠️ Check-in error while saving/summarising: {e}")
    finally:
        _reset_checkin()


def start_checkin():
    """Begin a check-in session, sending the intro and first question."""
    if CHECKIN["active"]:
        send_message("A check-in's already in progress — answer the current question, or /cancel.")
        return
    responses = evening_checkin.init_responses()
    CHECKIN.update(
        active=True,
        responses=responses,
        queue=evening_checkin.build_checkin_steps(
            responses["pplrul_today"], responses["pplrul_tomorrow"]),
        current=None,
    )
    send_message(
        f"🌙 Evening check-in. Today was {responses['pplrul_today']}, "
        f"tomorrow is {responses['pplrul_tomorrow']}.\n"
        "Answer what you can — reply 'skip' to skip any question, /cancel to stop."
    )
    _advance_checkin()


def handle_checkin_reply(text):
    """Route a reply into the active check-in: parse, store, splice follow-ups."""
    step = CHECKIN["current"]
    if step is None:
        _advance_checkin()
        return
    value = evening_checkin.parse_answer(step, text)
    CHECKIN["responses"][step["key"]] = value
    followups = step.get("followups", {}).get(value)
    if followups:
        CHECKIN["queue"][0:0] = followups
    _advance_checkin()


def maybe_autostart_checkin():
    """Auto-start the check-in once daily at/after 21:00 if not already done."""
    global _autostart_done_date
    if CHECKIN["active"]:
        return
    now = evening_checkin.now_sydney()
    if now.hour < 21:
        return
    today_str = now.strftime("%Y-%m-%d")
    if _autostart_done_date == today_str:
        return
    # Already completed today (manual /checkin or a prior run)? Don't re-ask.
    if (DATA_DIR / f"summary_{today_str}.txt").exists():
        _autostart_done_date = today_str
        return
    _autostart_done_date = today_str
    print(f"🌙  Auto-starting evening check-in ({now.strftime('%H:%M')})")
    start_checkin()


# ── Main listener loop ──────────────────────────────────────────────────────

def handle_update(update, static_context_holder):
    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    if chat_id != config.TELEGRAM_CHAT_ID:
        print(f"⚠️  Ignoring message from unauthorised chat {chat_id}")
        return

    text = msg.get("text", "").strip()
    if not text:
        return

    print(f"📩  [{datetime.datetime.now(TIMEZONE).strftime('%H:%M:%S')}] {text[:80]}")

    # ── Evening check-in interception (before commands / Claude) ──────────────
    cmd_word = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd_word == "/cancel":
        msg_out = "Check-in cancelled. Nothing saved." if CHECKIN["active"] else "Nothing to cancel."
        _reset_checkin()
        send_message(msg_out)
        return
    if cmd_word == "/checkin":
        start_checkin()
        return
    if CHECKIN["active"]:
        handle_checkin_reply(text)
        return

    # ── Normal path: persist to thread, route command or Claude ───────────────
    append_turn("user", text)

    reply = handle_command(text)
    meta  = None
    if reply is None:
        try:
            reply = chat_with_claude(
                text,
                static_context_holder["text"],
                load_recent_turns(HISTORY_WINDOW),
            )
            meta = {"source": "claude"}
        except Exception as e:
            reply = f"⚠️ Claude error: {e}"
            meta = {"source": "error"}
    else:
        meta = {"source": "command"}

    send_message(reply)
    append_turn("assistant", reply, meta=meta)
    print(f"📤  → {reply[:80]}")


def run_listener():
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌  config.TELEGRAM_BOT_TOKEN is empty. See setup at top of file.")
        sys.exit(1)
    if not config.TELEGRAM_CHAT_ID:
        print("❌  config.TELEGRAM_CHAT_ID is 0. Run: python jarvis_telegram.py --setup")
        sys.exit(1)

    # Reload static context once an hour so term_context edits land without restart
    static_context_holder = {"text": build_static_context(), "ts": time.time()}
    REFRESH_SECONDS = 3600

    print(f"🤖  Jarvis Telegram listener online — chat_id={config.TELEGRAM_CHAT_ID}")
    send_message("🤖 Jarvis online. Send /help for commands.")

    offset = load_offset()
    while True:
        try:
            if time.time() - static_context_holder["ts"] > REFRESH_SECONDS:
                static_context_holder["text"] = build_static_context()
                static_context_holder["ts"]   = time.time()

            maybe_autostart_checkin()

            updates = get_updates(offset)
            for upd in updates:
                handle_update(upd, static_context_holder)
                offset = upd["update_id"] + 1
                save_offset(offset)
        except KeyboardInterrupt:
            print("\n👋  Listener stopped.")
            break
        except requests.exceptions.ReadTimeout:
            continue  # long-poll timeout — totally normal
        except Exception as e:
            print(f"⚠️  Loop error: {e} — backing off 10s")
            time.sleep(10)


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Telegram Channel")
    parser.add_argument("--setup", action="store_true",
                        help="Discover and save your chat ID after messaging the bot")
    parser.add_argument("--test",  action="store_true",
                        help="Send a test 'Jarvis online' message and exit")
    args = parser.parse_args()

    if args.setup:
        discover_chat_id()
    elif args.test:
        if not config.TELEGRAM_CHAT_ID:
            print("❌  Run --setup first.")
            sys.exit(1)
        send_message("🤖 Jarvis online (test message). All wiring works.")
        print("✅  Test message sent.")
    else:
        run_listener()

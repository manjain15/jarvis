"""
Jarvis Evening Check-in (Telegram-driven)
=========================================
The check-in is a daily reflection across all life areas. Its two artifacts
drive the rest of Jarvis:
  - data/checkin_YYYY-MM-DD.json  → read by memory_system.py (nightly, 22:00)
  - data/summary_YYYY-MM-DD.txt   → read by morning_brief.py the next day

ARCHITECTURE (since the VPS migration):
  The terminal version is retired — systemd timers have no TTY. The flow now
  runs over Telegram: jarvis_telegram.py auto-starts it at 21:00 (or on /checkin),
  walks the question plan one message at a time, then calls the functions here to
  persist and summarise. This module owns the question plan, answer parsing, and
  artifact writing — it is the single source of truth for both.

  This file is import-only for the daemon. Run it directly to print the plan:
      python evening_checkin.py

  Since phase 6 the check-in runs in the Grok coach bot through
  GET /checkin/today and POST /checkin (today_checkin / submit_checkin below).
  The Telegram 21:00 check-in is retired.
"""

import json
import datetime
from pathlib import Path

import pytz
import anthropic
import config

# ── Setup ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
DATA_DIR     = SCRIPT_DIR / "data"
PROFILE_FILE = SCRIPT_DIR / "profile.md"

DATA_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)


def now_sydney():
    """Current time in the configured timezone."""
    return datetime.datetime.now(TIMEZONE)


# ── PPLRUL helper ─────────────────────────────────────────────────────────────
# Schedule itself lives in term_context.json (single source of truth).
try:
    from term_context import get_pplrul_day as _get_pplrul_day, VALID_SPLIT_LABELS as _VALID_SPLIT_LABELS
    PPLRUL = sorted(_VALID_SPLIT_LABELS)
except Exception:
    PPLRUL = ["Push", "Pull", "Legs", "Rest", "Upper", "Sharms", "Rest"]
    def _get_pplrul_day(date=None):
        return "Rest"


def get_pplrul_day(date=None):
    """Returns the PPLRUL label for the given date (default: today)."""
    if date is None:
        date = now_sydney().date()
    return _get_pplrul_day(date)


def get_tomorrow_pplrul():
    """Returns tomorrow's PPLRUL label."""
    tomorrow = now_sydney().date() + datetime.timedelta(days=1)
    return get_pplrul_day(tomorrow)


# ── Question plan (single source of truth) ─────────────────────────────────────
# Each step is a dict:
#   key       — the response-dict key (must stay stable: memory_system + the
#               summary prompt read these)
#   prompt    — the question text shown to Manav
#   type      — "yn" | "number" | "text"
#   min/max   — clamp bounds for number questions (optional)
#   followups — dict mapping a parsed answer (True/False) to a list of further
#               steps, spliced in only when that answer is given (optional)

def init_responses(now=None):
    """Returns the base response dict (the fields not asked as questions) for now's date."""
    today = now or now_sydney()
    return {
        "date": today.strftime("%Y-%m-%d"),
        "day_of_week": today.strftime("%A"),
        "pplrul_today": get_pplrul_day(today.date()),
        "pplrul_tomorrow": get_pplrul_day(today.date() + datetime.timedelta(days=1)),
    }


def _course_codes():
    """Course codes for the current term ([] on failure). Prefers course_schedule."""
    try:
        import course_schedule
        codes = [c.get("code") for c in course_schedule.load_courses() if c.get("code")]
        if codes:
            return codes
    except Exception:
        pass
    try:
        import term_context
        ctx = term_context.load_context()
        return [s["code"] for s in ctx.get("subjects", []) if s.get("code")]
    except Exception:
        return []


def build_checkin_steps(pplrul_today, pplrul_tomorrow, weekday=None):
    """
    Build the ordered list of check-in steps for the given PPLRUL context.
    The rest-day vs gym-day branch is decided here, at build time.

    `weekday` is Python's date.weekday() (Mon=0..Sun=6); defaults to today.
    On Sundays a per-course "kept up with lectures?" question is appended so the
    brief can flag a course that's slipping — asked weekly, not nightly.
    """
    if weekday is None:
        weekday = now_sydney().date().weekday()
    steps = []

    # ── Health & Fitness ──────────────────────────────────────────────────────
    if pplrul_today != "Rest":
        steps.append({
            "key": "workout_done",
            "prompt": f"Did you hit the gym today? ({pplrul_today} session)",
            "type": "yn",
            "followups": {
                True:  [{"key": "workout_notes",
                         "prompt": "Any notes on the session? (weight, reps, how it felt)",
                         "type": "text"}],
                False: [{"key": "workout_skip_reason",
                         "prompt": "What got in the way?",
                         "type": "text"}],
            },
        })
    else:
        steps.append({
            "key": "rest_day_active_recovery",
            "prompt": "Rest day today — did you do any active recovery or stretching?",
            "type": "yn",
        })

    steps.append({"key": "sleep_hours_last_night",
                  "prompt": "How many hours did you sleep last night?",
                  "type": "number", "min": 2, "max": 14})
    steps.append({"key": "sleep_target_tonight",
                  "prompt": f"What time are you aiming to be in bed tonight? "
                            f"(target: 7–8hrs before tomorrow's {pplrul_tomorrow} day)",
                  "type": "text"})
    steps.append({"key": "weight_today",
                  "prompt": "Did you weigh yourself today? If yes, what was it? (kg)",
                  "type": "number", "min": 40, "max": 150})
    steps.append({"key": "energy_level",
                  "prompt": "Energy level today — 1 (exhausted) to 10 (great)?",
                  "type": "number", "min": 1, "max": 10})

    # ── Career & Internship ───────────────────────────────────────────────────
    steps.append({"key": "application_sent",
                  "prompt": "Did you send any internship applications today?",
                  "type": "yn",
                  "followups": {True: [{"key": "application_details",
                                        "prompt": "Which company/role?", "type": "text"}]}})
    steps.append({"key": "google_mentor_contact",
                  "prompt": "Did you contact your Google mentor today or recently?",
                  "type": "yn",
                  "followups": {True: [{"key": "mentor_notes",
                                        "prompt": "What did you discuss or what was the outcome?",
                                        "type": "text"}]}})
    steps.append({"key": "career_work_done",
                  "prompt": "Did you do any career-building work today? "
                            "(LeetCode, portfolio, LinkedIn, networking)",
                  "type": "yn",
                  "followups": {True: [{"key": "career_work_details",
                                        "prompt": "What did you work on?", "type": "text"}]}})

    # ── Uni ───────────────────────────────────────────────────────────────────
    steps.append({"key": "uni_work_done",
                  "prompt": "Did you do any uni work today?",
                  "type": "yn",
                  "followups": {True: [{"key": "uni_work_details",
                                        "prompt": "What did you work on?", "type": "text"}]}})
    steps.append({"key": "upcoming_deadline",
                  "prompt": "Any uni deadlines coming up in the next 2 weeks you want Jarvis to track?",
                  "type": "text"})

    # ── Passion Project ───────────────────────────────────────────────────────
    steps.append({"key": "project_work_done",
                  "prompt": "Did you work on your passion project today?",
                  "type": "yn",
                  "followups": {
                      True:  [{"key": "project_details",
                               "prompt": "What did you work on or figure out?", "type": "text"}],
                      False: [{"key": "project_blocker",
                               "prompt": "What's blocking you or why didn't it happen?",
                               "type": "text"}],
                  }})

    # ── Finances ──────────────────────────────────────────────────────────────
    steps.append({"key": "pokemon_plan_progress",
                  "prompt": "Any progress on the Pokemon reselling plan today?",
                  "type": "yn",
                  "followups": {True: [{"key": "pokemon_plan_details",
                                        "prompt": "What did you figure out or decide?",
                                        "type": "text"}]}})
    steps.append({"key": "unusual_spending",
                  "prompt": "Any unusual spending today worth flagging?",
                  "type": "yn",
                  "followups": {True: [{"key": "spending_details",
                                        "prompt": "What was it?", "type": "text"}]}})

    # ── Uni: weekly per-course keep-up (Sundays only) ─────────────────────────
    # Asked once a week so the morning brief can flag a course Manav is falling
    # behind in. A "no" on keepup_<CODE> becomes a drift alert (study_tracker).
    if weekday == 6:  # Sunday
        for code in _course_codes():
            steps.append({
                "key": f"keepup_{code}",
                "prompt": f"Kept up with {code} lectures/content this week?",
                "type": "yn",
                "followups": {
                    False: [{"key": f"behind_{code}",
                             "prompt": f"What are you behind on in {code}?",
                             "type": "text"}],
                },
            })

    # ── General ───────────────────────────────────────────────────────────────
    steps.append({"key": "day_rating",
                  "prompt": "Rate today overall — 1 (wrote-off) to 10 (crushed it)?",
                  "type": "number", "min": 1, "max": 10})
    steps.append({"key": "biggest_win",
                  "prompt": "What was the best thing that happened or that you did today?",
                  "type": "text"})
    steps.append({"key": "tomorrow_focus",
                  "prompt": "What's the ONE thing you most want to get done tomorrow?",
                  "type": "text"})
    steps.append({"key": "anything_else",
                  "prompt": "Anything else on your mind you want Jarvis to know or remember?",
                  "type": "text"})

    return steps


# Tokens that mean "skip this question" — kept small so they don't eat real answers.
_SKIP_TOKENS = {"skip", "-", ""}
_YES_TOKENS  = {"y", "yes", "1", "yep", "yeah", "yup"}
_NO_TOKENS   = {"n", "no", "0", "nope", "nah"}


def parse_answer(step, raw_text):
    """
    Convert a raw text reply into the typed value for `step`.
    Returns the parsed value, or None for a skip/blank/unparseable input
    (None preserves the old terminal behaviour: skipped → 'not provided').
    """
    text = (raw_text or "").strip()
    if text.lower() in _SKIP_TOKENS:
        return None

    kind = step["type"]
    if kind == "yn":
        low = text.lower()
        if low in _YES_TOKENS:
            return True
        if low in _NO_TOKENS:
            return False
        return None
    if kind == "number":
        try:
            val = float(text)
        except ValueError:
            return None
        if "min" in step and val < step["min"]:
            return step["min"]
        if "max" in step and val > step["max"]:
            return step["max"]
        return val
    return text  # free text


# ── Persistence ────────────────────────────────────────────────────────────────

def save_responses(responses):
    """Saves check-in responses to a dated JSON file. Returns the path."""
    path = DATA_DIR / f"checkin_{responses['date']}.json"
    with open(path, "w") as f:
        json.dump(responses, f, indent=2)
    return path


def save_summary(summary, date_str):
    """Saves the Claude summary to a dated text file. Returns the path."""
    path = DATA_DIR / f"summary_{date_str}.txt"
    with open(path, "w") as f:
        f.write(summary)
    return path


# ── Claude summary ──────────────────────────────────────────────────────────────

def generate_summary(responses):
    """
    Sends responses to Claude and returns a short summary + tomorrow's focus.
    This is what the morning brief reads the next day.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    profile_text = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else ""

    r = responses
    pplrul_today    = r.get("pplrul_today", "Unknown")
    pplrul_tomorrow = r.get("pplrul_tomorrow", "Unknown")

    def fmt(key, label):
        val = r.get(key)
        if val is None:
            return f"  {label}: not provided"
        if isinstance(val, bool):
            return f"  {label}: {'Yes' if val else 'No'}"
        return f"  {label}: {val}"

    checkin_text = f"""
DATE: {r.get('date')} ({r.get('day_of_week')})
PPLRUL today: {pplrul_today} | Tomorrow: {pplrul_tomorrow}

HEALTH & FITNESS:
{fmt('workout_done', 'Gym done')}
{fmt('workout_notes', 'Workout notes')}
{fmt('workout_skip_reason', 'Skip reason')}
{fmt('rest_day_active_recovery', 'Active recovery (rest day)')}
{fmt('sleep_hours_last_night', 'Sleep last night (hrs)')}
{fmt('sleep_target_tonight', 'Bed time target tonight')}
{fmt('weight_today', 'Weight today (kg)')}
{fmt('energy_level', 'Energy level (1-10)')}

CAREER & INTERNSHIP:
{fmt('application_sent', 'Application sent today')}
{fmt('application_details', 'Application details')}
{fmt('google_mentor_contact', 'Google mentor contact')}
{fmt('mentor_notes', 'Mentor notes')}
{fmt('career_work_done', 'Career building work')}
{fmt('career_work_details', 'Career work details')}

UNI:
{fmt('uni_work_done', 'Uni work done')}
{fmt('uni_work_details', 'Uni work details')}
{fmt('upcoming_deadline', 'Upcoming deadline flagged')}

PASSION PROJECT:
{fmt('project_work_done', 'Project work done')}
{fmt('project_details', 'Project details')}
{fmt('project_blocker', 'Blocker')}

FINANCES:
{fmt('pokemon_plan_progress', 'Pokemon plan progress')}
{fmt('pokemon_plan_details', 'Pokemon plan details')}
{fmt('unusual_spending', 'Unusual spending')}
{fmt('spending_details', 'Spending details')}

GENERAL:
{fmt('day_rating', 'Day rating (1-10)')}
{fmt('biggest_win', 'Biggest win today')}
{fmt('tomorrow_focus', 'Tomorrow focus')}
{fmt('anything_else', 'Anything else')}
"""

    # Optional: tomorrow's UNSW timetable, so TOMORROW CONTEXT is class-aware.
    timetable_text = ""
    try:
        import uni_timetable
        timetable_text = uni_timetable.get_tomorrow_summary()
    except Exception:
        timetable_text = ""

    prompt = f"""You are Jarvis — Manav's personal AI assistant. You know him well.

Here is his profile:
{profile_text}

Here is his evening check-in for today:
{checkin_text}
{timetable_text}

Write a SHORT end-of-day summary (max 200 words) that will be read by tomorrow morning's briefing.
Structure it as:

YESTERDAY SUMMARY:
[2-3 sentences covering what actually happened — workout, applications, project, energy. Be direct and factual.]

CARRY-FORWARD:
[Bullet points of anything unfinished, flagged, or that needs follow-up tomorrow. Max 5 bullets.]

TOMORROW CONTEXT:
[1-2 sentences setting up tomorrow — what Manav said he wants to focus on, what PPLRUL day it is, anything to be aware of.]

Be direct. No fluff. Write it as a handoff note from tonight-Jarvis to tomorrow-morning-Jarvis.
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


# ── Coach check-in (Grok) ─────────────────────────────────────────────────────
# Since phase 6 the check-in runs in the Grok coach bot, not Telegram. The bot
# calls GET /checkin/today at 21:00: the day's facts come from the ledger, so
# it only asks what the evidence can't answer (2-5 questions). POST /checkin
# takes the answers; one Claude call writes tomorrow's handoff summary and
# turns area answers into next actions. The same checkin_*.json and
# summary_*.txt files are written, so memory_system and the brief are unchanged.

LEFT_STATUSES = ("partial", "no", "no_reply")
AREA_ASK_AFTER_DAYS = 3
MAX_LEFT_QUESTIONS = 2
ANSWER_LIMIT = 500


def _ledger():
    import ledger
    return ledger


def _gym_fact(day, now):
    """The gym item as a fact, or None on a rest day."""
    gym = _ledger().find_item(day, "gym")
    if gym is None:
        return None
    return {"split": gym["title"], "slot": gym["slot"], "status": gym.get("status"),
            "minutes": gym.get("logged_minutes"), "excuse": gym.get("excuse"),
            "early": bool(gym.get("early"))}


def checkin_questions(day, area_list, now):
    """The questions the evidence can't answer, in the order the bot should ask."""
    questions = []
    blocks = [i for i in day.get("items", []) if i.get("kind") == "study"]
    left = [b for b in blocks if b.get("status") in LEFT_STATUSES or (
        b.get("status") == "pending" and datetime.datetime.fromisoformat(b["end"]) <= now)]
    for b in left[:MAX_LEFT_QUESTIONS]:
        questions.append({"id": f"left:{b['id']}", "kind": "text",
                          "text": f"{b['course']} · {b['task']}: what's left?"})

    gym = _ledger().find_item(day, "gym")
    if gym is not None and gym.get("status") in ("pending", "missed") and not gym.get("excuse"):
        questions.append({"id": "gym_reason", "kind": "text",
                          "text": f"{gym['title']} isn't logged. Still going tonight, or what happened?"})

    try:
        import areas as areas_mod
        pick = next(iter(areas_mod.most_neglected(area_list, now.date(), blockable_only=True)), None)
        if pick and areas_mod.days_since(pick, now.date()) >= AREA_ASK_AFTER_DAYS:
            days = areas_mod.days_since(pick, now.date())
            since = "never touched" if not pick.get("last_touched") else f"{days} days untouched"
            questions.append({"id": f"area:{pick['name']}", "kind": "text",
                              "text": f"{pick['label']}: {since}. Next step: {pick.get('next_action') or 'none set'}. "
                                      "What's the next concrete step, or what's blocking it?"})
    except Exception:
        pass

    questions.append({"id": "tomorrow_focus", "kind": "text",
                      "text": "Tomorrow's one non-negotiable?"})
    questions.append({"id": "weight", "kind": "number", "optional": True,
                      "text": "Weight this morning? (kg, or skip)"})
    return questions


def today_checkin(now=None):
    """GET /checkin/today: today's facts and the questions to ask."""
    now = now or now_sydney()
    day = _ledger().get_or_create_day(now.date())
    try:
        import areas as areas_mod
        area_list = areas_mod.refresh_evidence(now)
        area_summary = areas_mod.summary(area_list, now.date())
    except Exception as e:
        print(f"⚠️  check-in: areas unavailable: {e}")
        area_list, area_summary = {}, None
    steps = _ledger().find_item(day, "steps") or {}
    tomorrow = ""
    try:
        import uni_timetable
        tomorrow = uni_timetable.get_tomorrow_summary()
    except Exception:
        pass
    facts = {
        "gym": _gym_fact(day, now),
        "steps": {"count": steps.get("count"), "target": steps.get("target")},
        "study": [{"id": i["id"], "course": i["course"], "type": i["type"], "task": i["task"],
                   "status": i.get("status")} for i in day.get("items", []) if i.get("kind") == "study"],
        "checks": [{"id": i["id"], "label": i["label"], "status": i.get("status")}
                   for i in day.get("items", []) if i.get("kind") == "check"],
        "area_block": next(({"area": i["area"], "task": i["task"], "status": i.get("status")}
                            for i in day.get("items", []) if i.get("kind") == "area"), None),
        "debt": day.get("debt"),
        "tomorrow": {"split": get_tomorrow_pplrul(), "classes": tomorrow},
        "areas": area_summary,
    }
    return {"date": day["date"], "submitted": bool(day.get("checkin")),
            "facts": facts, "questions": checkin_questions(day, area_list, now)}


def _clean_answers(body, question_ids):
    """Validate a POST /checkin body. Returns (answers, notes). Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    extra = sorted(set(body) - {"answers", "notes", "actor", "reason"})
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")
    answers = body.get("answers", {})
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object of question id → answer")
    unknown = sorted(set(answers) - set(question_ids))
    if unknown:
        raise ValueError(f"unknown question id(s): {', '.join(unknown)}")
    cleaned = {}
    for qid, value in answers.items():
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "skip", "-")):
            continue
        if qid == "weight":
            try:
                weight = float(value)
            except (TypeError, ValueError):
                raise ValueError("weight must be a number of kg")
            if not 40 <= weight <= 150:
                raise ValueError("weight must be between 40 and 150 kg")
            cleaned[qid] = round(weight, 1)
            continue
        if not isinstance(value, str) or len(value) > ANSWER_LIMIT:
            raise ValueError(f"{qid} must be text up to {ANSWER_LIMIT} characters")
        cleaned[qid] = value.strip()
    notes = body.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValueError("notes must be text up to 2000 characters")
    return cleaned, notes.strip()


def _keepup(date):
    """On the training week's last day: {f"keepup_{code}": bool} from lecture block check-offs."""
    try:
        import day_plan
        import term_context
        start = term_context.week_start(date)
        if (date - start).days != 6:
            return {}
        done = day_plan.minutes_done(start, date + datetime.timedelta(days=1))
        targets = term_context.get_study_plan().get("targets", {})
    except Exception:
        return {}
    out = {}
    for code, t in targets.items():
        if t.get("lectures"):
            out[f"keepup_{code}"] = done.get((code, "lecture"), 0) >= t["lectures"] * 60 * 0.75
    return out


def plan_actions(facts, answers, notes):
    """
    One Claude call: tomorrow's handoff summary and area next actions, as
    {"summary": str, "areas": {name: {"next_action": str, "worked_today": bool}}}.
    Returns None when Claude isn't reachable; the caller falls back.
    """
    from voice import VOICE_RULES, strip_emoji
    prompt = f"""{VOICE_RULES}

Manav just finished his evening check-in. Facts from Jarvis's data and his answers are below.

FACTS:
{json.dumps(facts, default=str)[:6000]}

ANSWERS (question id → answer):
{json.dumps(answers, default=str)}

NOTES:
{notes or "none"}

Return only JSON with two keys.
"summary": under 120 words, a handoff note for tomorrow morning's brief: what happened
today, what carries forward, and tomorrow's one non-negotiable.
"areas": for each area mentioned in an "area:<name>" answer or in the notes, an object
{{"next_action": one concrete step that fits a 45-minute block, under 80 characters,
  "worked_today": true only if he says he worked on it today}}. Leave out areas he didn't mention.
"""
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(model="claude-sonnet-4-6", max_tokens=500,
                                         messages=[{"role": "user", "content": prompt}])
        text = message.content[0].text
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as e:
        print(f"⚠️  check-in: action planning failed: {e}")
        return None
    out = {"summary": strip_emoji(str(data.get("summary") or ""))[:1200], "areas": {}}
    for name, action in (data.get("areas") or {}).items():
        if isinstance(action, dict) and str(action.get("next_action") or "").strip():
            out["areas"][name] = {"next_action": strip_emoji(str(action["next_action"]))[:120],
                                  "worked_today": bool(action.get("worked_today"))}
    return out


def _fallback_summary(facts, answers):
    """Plain handoff text when Claude isn't available."""
    gym = facts.get("gym")
    lines = ["YESTERDAY SUMMARY:"]
    lines.append(f"  Gym: {gym['split']} {gym['status']}" if gym else "  Gym: rest day")
    steps = facts.get("steps") or {}
    if steps.get("count"):
        lines.append(f"  Steps: {steps['count']:,} / {steps['target']:,}")
    done = [s for s in facts.get("study", []) if s["status"] == "done"]
    lines.append(f"  Study blocks done: {len(done)} / {len(facts.get('study', []))}")
    carry = [f"  - {v}" for k, v in answers.items() if k.startswith(("left:", "area:")) or k == "gym_reason"]
    if carry:
        lines += ["CARRY-FORWARD:"] + carry
    if answers.get("tomorrow_focus"):
        lines += ["TOMORROW CONTEXT:", f"  One non-negotiable: {answers['tomorrow_focus']}"]
    return "\n".join(lines)


def submit_checkin(body, now=None, actions_fn=None):
    """
    POST /checkin: validate answers, apply them, and write the check-in files.
    Returns {"ok", "summary", "actions"}: actions are short lines the bot can read back.
    Raises ValueError on a bad body.
    """
    now = now or now_sydney()
    actions_fn = actions_fn or plan_actions
    current = today_checkin(now)
    answers, notes = _clean_answers(body, [q["id"] for q in current["questions"]])
    facts = current["facts"]
    lines = []

    # Leftover study: tomorrow's plan carries "Finish: <what's left>".
    def _left(day):
        for qid, text in answers.items():
            if qid.startswith("left:"):
                item = _ledger().find_item(day, qid.split(":", 1)[1])
                if item is not None:
                    item["left"] = text
                    if item.get("status") == "pending":
                        item["status"] = "no"
        day["checkin"] = now.isoformat(timespec="seconds")
    _ledger().update_day(now.date(), _left)
    for qid, text in answers.items():
        if qid.startswith("left:"):
            lines.append(f"Tomorrow's plan carries: {text}")

    planned = actions_fn(facts, answers, notes)
    summary = (planned or {}).get("summary") or _fallback_summary(facts, answers)

    try:
        import areas as areas_mod
        area_updates = dict((planned or {}).get("areas") or {})
        for qid, text in answers.items():
            if qid.startswith("area:") and qid.split(":", 1)[1] not in area_updates and planned is None:
                area_updates[qid.split(":", 1)[1]] = {"next_action": text[:120], "worked_today": False}

        def _apply(area_list):
            done = []
            for name, update in area_updates.items():
                if name not in area_list:
                    continue
                area_list[name]["next_action"] = update["next_action"]
                if update.get("worked_today"):
                    areas_mod.touch(area_list, name, now.date(), "checkin")
                done.append(f"{area_list[name]['label']}: next step is \"{update['next_action']}\"")
            if "weight" in answers and "weight" in area_list:
                areas_mod.touch(area_list, "weight", now.date(), "checkin")
            return done
        lines += areas_mod.mutate(_apply)
    except Exception as e:
        print(f"⚠️  check-in: areas update failed: {e}")

    gym = facts.get("gym")
    responses = init_responses(now)
    responses.update({
        "source": "coach",
        "workout_done": bool(gym and gym["status"] in ("done", "partial")) if gym else None,
        "rest_day_active_recovery": None,
        "workout_skip_reason": answers.get("gym_reason"),
        "weight_today": answers.get("weight"),
        "tomorrow_focus": answers.get("tomorrow_focus"),
        "uni_work_done": any(s["status"] in ("done", "partial") for s in facts.get("study", [])),
        "uni_work_details": "; ".join(f"{s['course']} {s['task']} ({s['status']})" for s in facts.get("study", [])),
        "project_work_done": None,
        "anything_else": notes or None,
        "answers": answers,
        "facts": facts,
        **_keepup(now.date()),
    })
    for qid, text in answers.items():
        if qid.startswith("area:"):
            responses[f"area_{qid.split(':', 1)[1]}"] = text
    save_responses(responses)
    save_summary(summary, responses["date"])
    if answers.get("tomorrow_focus"):
        lines.append(f"Tomorrow's non-negotiable: {answers['tomorrow_focus']}")
    return {"ok": True, "date": responses["date"], "summary": summary, "actions": lines}


# ── CLI (plan dump only — terminal check-in is retired) ─────────────────────────

if __name__ == "__main__":
    today = now_sydney()
    steps = build_checkin_steps(get_pplrul_day(), get_tomorrow_pplrul())
    print("Terminal check-in is retired — the flow runs over Telegram "
          "(jarvis_telegram.py).")
    print(f"\nQuestion plan for {today:%A %d %b %Y} "
          f"(today={get_pplrul_day()}, tomorrow={get_tomorrow_pplrul()}):\n")
    n = 0
    for step in steps:
        n += 1
        print(f"  {n:2d}. [{step['type']:6}] {step['key']}: {step['prompt']}")
        for answer, followups in step.get("followups", {}).items():
            for f in followups:
                print(f"          ↳ if {answer}: [{f['type']:6}] {f['key']}: {f['prompt']}")

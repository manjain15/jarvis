"""
Jarvis — Memory System
=======================
Automatically builds and maintains a living memory of Manav's life.
Jarvis observes data from all sources and decides what's worth remembering.

Two memory files:
  memory/episodic.md  — dated log of significant events ("May 13: missed Pull day, 4h46m sleep")
  memory/semantic.md  — current facts about Manav's life (updated when things change)

HOW IT WORKS:
  1. Runs nightly after the evening check-in (or standalone)
  2. Reads all today's data — Hevy, Google Health, finance, check-in responses
  3. Sends everything to Claude and asks: "What's worth remembering from today?"
  4. Claude writes new episodic entries and proposes semantic updates
  5. Episodic entries are appended to episodic.md
  6. Semantic updates are merged into semantic.md
  7. Both files are injected into every Jarvis prompt alongside profile.md

The morning brief and /ask endpoint both read from memory automatically.

SCHEDULE (add to crontab):
  0 22 * * * cd /Users/manavjain/jarvis && venv/bin/python memory_system.py >> jarvis_memory.log 2>&1
"""

import json
import datetime
import re
from pathlib import Path

import pytz
import anthropic
import config

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
MEMORY_DIR   = SCRIPT_DIR / "memory"
EPISODIC     = MEMORY_DIR / "episodic.md"
SEMANTIC     = MEMORY_DIR / "semantic.md"
DATA_DIR     = SCRIPT_DIR / "data"
PROFILE_FILE = SCRIPT_DIR / "profile.md"

MEMORY_DIR.mkdir(exist_ok=True)
TIMEZONE = pytz.timezone(config.TIMEZONE)


# ── Initialise memory files if they don't exist ───────────────────────────────

def init_memory():
    if not EPISODIC.exists():
        EPISODIC.write_text("""# Jarvis — Episodic Memory
> Automatically maintained. Each entry is a significant event or observation.
> Jarvis reads this to understand patterns and history.

""")

    if not SEMANTIC.exists():
        SEMANTIC.write_text("""# Jarvis — Semantic Memory
> Current facts about Manav's life. Updated automatically when things change.
> This supplements profile.md with things Jarvis has learned over time.

## Career
- Actively being mentored by a Google employee
- Applications sent: Google, TikTok, Canva (outcomes pending as of May 2026)

## Health & fitness
- Running PPLRUL split: Push/Pull/Legs/Rest/Upper/Sharms/Rest
- Current weight: ~71kg, target 65-67kg
- Sleep has been inconsistent — averaging below 7hr target

## Finances
- Savings: $10,936 of $35,000 goal (31.2%) as of May 2026
- Projected completion: March 2027 — one month behind target
- Pokemon reselling plan still undefined

## Personal
- No passion project defined yet
- Currently in T2 2026 at UNSW (COMP2511, MATH2601, MATH2901)

""")


# ── Load today's data ──────────────────────────────────────────────────────────

def load_todays_data():
    """
    Aggregates all available data from today for memory processing.
    Returns (formatted string, list of today's voice interactions).
    """
    tz        = TIMEZONE
    today     = datetime.datetime.now(tz).date()
    yesterday = today - datetime.timedelta(days=1)
    today_str = today.strftime("%Y-%m-%d")
    sections  = []

    # Evening check-in
    checkin_path = DATA_DIR / f"checkin_{today_str}.json"
    if checkin_path.exists():
        try:
            checkin = json.loads(checkin_path.read_text())
            sections.append("EVENING CHECK-IN:\n" + json.dumps(checkin, indent=2, default=str))
        except Exception:
            pass

    # Voice interactions (from Siri/dashboard)
    voice_log_path = SCRIPT_DIR / "data" / "voice_log.jsonl"
    today_interactions = []
    if voice_log_path.exists():
        try:
            import datetime as _dt
            tz        = pytz.timezone(config.TIMEZONE)
            today_str = today.strftime("%Y-%m-%d")
            with open(voice_log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("date") == today_str:
                        today_interactions.append(entry)
                        sections.append(
                            f"  [{entry['time']}] Q: {entry['question']}"
                            f"\n          A: {entry['answer'][:150]}"
                        )
            if today_interactions:
                sections.insert(0, "VOICE INTERACTIONS TODAY (Siri/Jarvis conversations):")
        except Exception as e:
            pass

    # Telegram conversation (turns from today)
    telegram_path = SCRIPT_DIR / "data" / "telegram_thread.jsonl"
    if telegram_path.exists():
        try:
            today_turns = []
            with open(telegram_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    ts = entry.get("ts", "")
                    if not ts.startswith(today_str):
                        continue
                    today_turns.append(entry)
            if today_turns:
                tg_lines = ["TELEGRAM CONVERSATION TODAY:"]
                for e in today_turns:
                    role = "Manav" if e["role"] == "user" else "Jarvis"
                    tg_lines.append(f"  [{e['ts'][11:16]}] {role}: {e['text'][:200]}")
                sections.append("\n".join(tg_lines))
        except Exception:
            pass

    # Google Health
    try:
        from google_health import fetch_sleep, fetch_resting_hr, fetch_steps, get_access_token
        token = get_access_token()
        sleep = fetch_sleep(token, yesterday)
        hr    = fetch_resting_hr(token, yesterday)
        steps = fetch_steps(token, yesterday)

        health_lines = ["HEALTH DATA (last night / yesterday):"]
        if sleep:
            vs = sleep["vs_7hr"]
            health_lines.append(
                f"  Sleep: {sleep['duration_str']} "
                f"({'below' if vs < 0 else 'above'} target by {abs(vs)}m) "
                f"— Deep: {sleep['deep_minutes']}m, REM: {sleep['rem_minutes']}m"
            )
        if hr and hr.get("resting_hr"):
            health_lines.append(f"  Resting HR: {hr['resting_hr']} bpm")
        if steps:
            health_lines.append(f"  Steps: {steps['steps']:,}")
        sections.append("\n".join(health_lines))
    except Exception as e:
        sections.append(f"HEALTH DATA: unavailable ({e})")

    # Hevy workouts
    try:
        from hevy import fetch_recent_workouts, parse_workout_date, get_pplrul_day, find_pbs
        workouts = fetch_recent_workouts(page_size=5)
        today_workouts = [
            w for w in workouts
            if parse_workout_date(w) and parse_workout_date(w).date() == today
        ]
        yesterday_workouts = [
            w for w in workouts
            if parse_workout_date(w) and parse_workout_date(w).date() == yesterday
        ]

        workout_lines = [f"HEVY WORKOUT DATA:"]
        workout_lines.append(f"  Today's split: {get_pplrul_day(today)}")
        workout_lines.append(f"  Yesterday's split: {get_pplrul_day(yesterday)}")

        for w in today_workouts + yesterday_workouts:
            dt   = parse_workout_date(w)
            pbs  = find_pbs(w)
            exs  = w.get("exercises", [])
            workout_lines.append(
                f"  {'Today' if dt.date() == today else 'Yesterday'}: "
                f"{w.get('title','Workout')} — {len(exs)} exercises"
                + (f" — PBs: {', '.join(f'{e} {k}kg×{r}' for e,k,r in pbs)}" if pbs else "")
            )
        sections.append("\n".join(workout_lines))
    except Exception as e:
        sections.append(f"HEVY DATA: unavailable ({e})")

    # Finance
    try:
        from finance_tracker import parse_stgeorge_csv, analyse_savings, EVERYDAY_CSV
        if EVERYDAY_CSV.exists():
            savings = analyse_savings()
            sections.append(
                f"FINANCE:\n"
                f"  Savings: ${savings['total']:,.2f} of $35,000 "
                f"({savings['pct']:.1f}%) — "
                f"{'on track' if savings['on_track'] else 'behind — projected ' + savings['projected_date'].strftime('%B %Y')}"
            )
    except Exception:
        pass

    text = "\n\n".join(sections) if sections else "No data available for today."
    return text, today_interactions


# ── Generate memory entries ───────────────────────────────────────────────────

def generate_memory_entries(today_data):
    """
    Sends today's data to Claude and asks it to:
    1. Write an episodic entry for today
    2. Identify any semantic facts that need updating
    Returns (episodic_entry, semantic_updates) as strings.
    """
    tz        = TIMEZONE
    today     = datetime.datetime.now(tz).date()
    today_str = today.strftime("%A, %d %B %Y")

    # Load existing memory for context
    episodic_recent = ""
    if EPISODIC.exists():
        lines = EPISODIC.read_text().split("\n")
        # Last 30 lines of episodic memory
        episodic_recent = "\n".join(lines[-30:])

    semantic_current = SEMANTIC.read_text() if SEMANTIC.exists() else ""
    profile_text     = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else ""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are Jarvis's memory system. Your job is to decide what's worth remembering from today and update the memory files.

TODAY: {today_str}

MANAV'S PROFILE:
{profile_text[:2000]}

CURRENT SEMANTIC MEMORY:
{semantic_current}

RECENT EPISODIC MEMORY (last entries):
{episodic_recent}

TODAY'S DATA:
{today_data}

YOUR TASK — respond in exactly this format with two sections:

---EPISODIC---
[Write a 1-3 line entry for today's date. Be concise and factual.
Only include things that are SIGNIFICANT or show a PATTERN.
Skip days where nothing notable happened — write "Nothing significant."
Format: "- [fact]. [fact]. [fact if any]."
Example: "- Sleep 5h12m (3rd consecutive night below target). Missed Push day. No applications sent (5th day this month)."
Example: "- Hit new PB on bench press: 80kg×5. Sleep 7h45m — good recovery. Sent application to Atlassian."]

---SEMANTIC_UPDATES---
[List ONLY stable facts that have CHANGED or are NEW since the current semantic memory.

CRITICAL — NEVER include in semantic updates:
- Daily sleep duration or sleep flags (belongs in episodic only)
- Daily resting HR readings (belongs in episodic only)
- Daily savings balance (only update if a new CSV was uploaded with a significantly different figure)
- Daily weight readings (only update if weight has been stable at a new value for 2+ weeks)
- Anything that will be different tomorrow

ONLY update semantic memory for:
- New job applications sent (running list)
- Career milestones (interview outcomes, mentor contacts)
- Project status changes (assignment submitted, exam done)
- Permanent schedule changes
- Weight if it has clearly shifted and held for weeks

If nothing stable has changed, write "No updates."
Format: "UPDATE [section]: [old fact] → [new fact]"
Or for new facts: "ADD [section]: [new fact]"
Example: "UPDATE Career: Vontier applications pending → also sent Optus Networks Intern June 10"
Example: "UPDATE Academic: COMP2511 A1 in progress → submitted July 1"]"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    response = message.content[0].text

    # Parse the two sections
    episodic_entry   = ""
    semantic_updates = ""

    if "---EPISODIC---" in response and "---SEMANTIC_UPDATES---" in response:
        parts = response.split("---SEMANTIC_UPDATES---")
        episodic_entry   = parts[0].replace("---EPISODIC---", "").strip()
        semantic_updates = parts[1].strip()
    else:
        episodic_entry = response.strip()

    return episodic_entry, semantic_updates


# ── Write memory updates ──────────────────────────────────────────────────────

def append_episodic(entry, date):
    """Appends a new entry to episodic.md."""
    if not entry or entry.lower() == "nothing significant.":
        return False

    date_str  = date.strftime("%Y-%m-%d")
    day_str   = date.strftime("%A %d %b %Y")
    new_entry = f"### {day_str}\n{entry}\n\n"

    with open(EPISODIC, "a") as f:
        f.write(new_entry)

    return True


def apply_semantic_updates(updates):
    """
    Applies semantic updates to semantic.md.
    Sends the current file + updates to Claude to produce the updated version.
    """
    if not updates or updates.strip().lower() == "no updates.":
        return False

    current = SEMANTIC.read_text()
    client  = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are updating a semantic memory file. Apply the following updates to the current file.

CURRENT FILE:
{current}

UPDATES TO APPLY:
{updates}

Return the complete updated file. Keep the same structure and format.
Only change what the updates specify. Don't add commentary."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    updated = message.content[0].text.strip()
    SEMANTIC.write_text(updated)
    return True


# ── Load memory for injection into prompts ────────────────────────────────────

def load_memory(days_back=14):
    """
    Returns a formatted memory string for injection into Jarvis prompts.
    Includes recent episodic entries and current semantic memory.
    """
    sections = []

    # Semantic memory (current facts)
    if SEMANTIC.exists():
        sections.append("JARVIS MEMORY — CURRENT FACTS:\n" + SEMANTIC.read_text())

    # Recent episodic entries
    if EPISODIC.exists():
        content = EPISODIC.read_text()
        # Get entries from last N days
        tz      = TIMEZONE
        cutoff  = datetime.datetime.now(tz).date() - datetime.timedelta(days=days_back)
        lines   = content.split("\n")
        recent  = []
        include = False
        for line in lines:
            if line.startswith("### "):
                try:
                    # Parse date from header
                    date_str = line.replace("### ", "").strip()
                    entry_date = datetime.datetime.strptime(date_str, "%A %d %b %Y").date()
                    include = entry_date >= cutoff
                except Exception:
                    include = True  # include if can't parse
            if include:
                recent.append(line)

        if recent:
            sections.append("JARVIS MEMORY — RECENT HISTORY (last 2 weeks):\n" + "\n".join(recent))

    return "\n\n".join(sections)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_memory_update():
    # Try to import Mem0 integration
    try:
        from jarvis_mem0 import add_memories_from_data
        MEM0_AVAILABLE = True
    except ImportError:
        MEM0_AVAILABLE = False
    """
    Full memory update cycle. Run nightly at 10pm.
    """
    tz    = TIMEZONE
    today = datetime.datetime.now(tz).date()
    print(f"\n🧠  Jarvis memory update — {today.strftime('%A, %d %B %Y')}")
    print("    ─────────────────────────────────────")

    init_memory()

    print("📊  Loading today's data...")
    today_data, today_interactions = load_todays_data()

    print("🤖  Generating memory entries...")
    episodic_entry, semantic_updates = generate_memory_entries(today_data)

    print(f"\n📝  Episodic entry:\n{episodic_entry}")
    print(f"\n🔄  Semantic updates:\n{semantic_updates}")

    # Generate profile update proposals
    print("💡  Checking for profile update proposals...")
    try:
        from update_profile import generate_proposals, add_proposal
        proposals = generate_proposals(today_data, episodic_entry)
        added_count = 0
        for p in proposals:
            was_added = add_proposal(
                p["section"],
                p["current_value"],
                p["proposed_value"],
                p["reason"]
            )
            if was_added:
                added_count += 1
        if added_count > 0:
            print(f"✅  {added_count} new profile update proposal(s) queued")
        else:
            print("⏭   No new profile updates needed")
    except Exception as e:
        print(f"⚠️   Proposal generation failed: {e}")

    # Generate term-context update proposals
    print("📚  Checking for term-context update proposals...")
    try:
        import term_updates
        term_proposals = term_updates.generate_proposals(today_data, episodic_entry)
        added_count = 0
        for p in term_proposals:
            was_added = term_updates.add_proposal(
                p.get("action"),
                p.get("params", {}),
                p.get("summary", ""),
                p.get("reason", ""),
            )
            if was_added:
                added_count += 1
        if added_count > 0:
            print(f"✅  {added_count} new term-context proposal(s) queued")
        else:
            print("⏭   No new term-context updates needed")
    except Exception as e:
        print(f"⚠️   Term proposal generation failed: {e}")

    # Write episodic
    added = append_episodic(episodic_entry, today)
    if added:
        print(f"\n✅  Episodic entry written to {EPISODIC}")
    else:
        print(f"\n⏭   Nothing significant today — episodic entry skipped")

    # Apply semantic updates
    updated = apply_semantic_updates(semantic_updates)
    if updated:
        print(f"✅  Semantic memory updated")
    else:
        print(f"⏭   No semantic updates needed")

    # Feed today's data into Mem0 vector memory
    if MEM0_AVAILABLE:
        print("🧠  Updating Mem0 vector memory...")
        try:
            added = add_memories_from_data(today_data, episodic_entry)
            if added:
                print(f"✅  {added} Mem0 memory operation(s)")
            else:
                print("⏭   No new Mem0 memories extracted")
        except Exception as e:
            print(f"⚠️   Mem0 update failed: {e}")

        # Feed voice interactions into Mem0 separately
        # Each Q&A is added individually so Mem0 can extract specific facts
        if today_interactions:
            print(f"🎙️   Adding {len(today_interactions)} voice interaction(s) to Mem0...")
            try:
                from jarvis_mem0 import add_memory
                added_voice = 0
                for interaction in today_interactions:
                    q = interaction.get("question", "")
                    a = interaction.get("answer", "")
                    if q and len(q) > 5:
                        text = f"Manav asked Jarvis: '{q}'. Jarvis responded: '{a[:200]}'"
                        result = add_memory(text, metadata={
                            "source": "voice_interaction",
                            "date":   interaction.get("date", today.strftime("%Y-%m-%d")),
                            "time":   interaction.get("time", ""),
                        })
                        if result.get("results"):
                            added_voice += 1
                print(f"✅  {added_voice} voice interaction(s) added to Mem0")
            except Exception as e:
                print(f"⚠️   Voice memory failed: {e}")

    print("\n✅  Memory update complete.\n")


if __name__ == "__main__":
    run_memory_update()

"""
Jarvis — Profile Update System
================================
Jarvis proposes updates to profile.md based on what it observes.
You approve or reject them each morning.

HOW IT WORKS:
  1. memory_system.py calls propose_profile_updates() nightly
  2. Proposed updates saved to memory/proposed_updates.json
  3. Morning brief includes pending proposals
  4. You run: python update_profile.py to review and approve
  5. Approved updates are applied to profile.md automatically

USAGE:
  python update_profile.py          — review and approve/reject proposals
  python update_profile.py --list   — just list pending proposals
"""

import json
import datetime
import re
from pathlib import Path

import pytz
import anthropic
import config
from json_store import file_lock, atomic_write_json

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR       = Path(__file__).parent
MEMORY_DIR       = SCRIPT_DIR / "memory"
PROPOSALS_FILE   = MEMORY_DIR / "proposed_updates.json"
PROFILE_FILE     = SCRIPT_DIR / "profile.md"

MEMORY_DIR.mkdir(exist_ok=True)
TIMEZONE = pytz.timezone(config.TIMEZONE)


def proposals_lock():
    """Context manager guarding the full load -> mutate -> save cycle for PROPOSALS_FILE."""
    return file_lock(PROPOSALS_FILE)


# ── Transient-proposal guard ──────────────────────────────────────────────────
# The generation prompt bans these, but the LLM doesn't always comply. This is
# the deterministic backstop: anything matching these patterns is daily/weekly
# noise and must never become a permanent profile.md edit.
TRANSIENT_PATTERNS = [
    r"savings balance",
    r"\$[\d,]+(\.\d{2})?\s*of\s*\$35",   # "$9,942.30 of $35,000" balance snapshots
    r"behind pace",
    r"\bprojected\b",
    r"\bsleep\b",
    r"last night",
    r"this week",
    r"\btoday\b|\byesterday\b",
    r"feeling sick|is sick|illness|currently paused|paused due to",
]


def is_transient_proposal(proposed_value, reason=""):
    """
    Returns True if a proposal looks like transient/daily data that should
    never be written into profile.md (savings snapshots, sleep, temporary
    illness, this-week schedule quirks).

    Only the proposed value is checked — the reason field legitimately
    references "today's check-in" as evidence, which says nothing about
    whether the proposed content itself is transient.
    """
    text = proposed_value.lower()
    return any(re.search(p, text) for p in TRANSIENT_PATTERNS)


# ── Proposal management ───────────────────────────────────────────────────────

def load_proposals():
    """Loads pending proposals from file."""
    if not PROPOSALS_FILE.exists():
        return []
    try:
        return json.loads(PROPOSALS_FILE.read_text())
    except Exception:
        return []


def save_proposals(proposals):
    """Saves proposals to file."""
    atomic_write_json(PROPOSALS_FILE, proposals)


def _normalise(text):
    """Collapses whitespace and lowercases for fuzzy proposal comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def add_proposal(section, current_value, proposed_value, reason):
    """
    Adds a new proposal if it passes the transient filter and isn't a
    near-duplicate of an existing one. Returns True if added.
    """
    # Deterministic backstop: never queue transient/daily data
    if is_transient_proposal(proposed_value, reason):
        return False

    with proposals_lock():
        proposals = load_proposals()

        # Near-duplicate check: same section + same opening text. Balance figures
        # and dates mutate slightly each night, so exact match isn't enough.
        new_key = _normalise(proposed_value)[:100]
        for p in proposals:
            if p["section"] == section and _normalise(p["proposed_value"])[:100] == new_key:
                return False

        tz  = pytz.timezone(config.TIMEZONE)
        now = datetime.datetime.now(tz)

        proposals.append({
            "id":             len(proposals) + 1,
            "date":           now.strftime("%Y-%m-%d"),
            "section":        section,
            "current_value":  current_value,
            "proposed_value": proposed_value,
            "reason":         reason,
            "status":         "pending",
        })

        save_proposals(proposals)
        return True


def get_pending_proposals():
    """Returns only pending proposals."""
    return [p for p in load_proposals() if p["status"] == "pending"]


def format_proposals_for_brief():
    """
    Returns a formatted string of pending proposals for the morning brief.
    Returns empty string if no pending proposals.
    """
    pending = get_pending_proposals()
    if not pending:
        return ""

    lines = [f"JARVIS SUGGESTS {len(pending)} PROFILE UPDATE(S):"]
    for p in pending:
        lines.append(f"  [{p['id']}] {p['section']}")
        lines.append(f"       Currently: {p['current_value'][:80]}")
        lines.append(f"       Proposed:  {p['proposed_value'][:80]}")
        lines.append(f"       Why: {p['reason'][:100]}")

    lines.append("")
    lines.append("  → Run: python update_profile.py to approve or reject")

    return "\n".join(lines)


# ── Generate proposals from today's data ─────────────────────────────────────

def generate_proposals(today_data, episodic_entry):
    """
    Sends today's data to Claude and asks it to identify
    any profile.md updates worth proposing.
    Returns a list of proposal dicts.
    """
    if not PROFILE_FILE.exists():
        return []

    profile_text = PROFILE_FILE.read_text()
    client       = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are Jarvis's profile update system. Your job is to identify when facts in profile.md are outdated or missing based on new data.

CURRENT PROFILE.MD:
{profile_text}

TODAY'S DATA:
{today_data}

TODAY'S EPISODIC ENTRY:
{episodic_entry}

YOUR TASK:
Identify specific facts in profile.md that should be updated based on today's data.
Only propose updates for things that are CLEARLY different from what's in the profile.
Don't propose updates for things that are already accurate.
Don't propose vague or speculative updates.

Respond in this exact JSON format (array of proposals, or empty array if nothing to update):
[
  {{
    "section": "which section of profile.md this relates to (e.g. '6. Health & fitness baseline')",
    "current_value": "the exact current text in profile.md that should change",
    "proposed_value": "the exact new text it should become",
    "reason": "one sentence explaining why this update is warranted"
  }}
]

CRITICAL RULE — NEVER propose updates for transient/dynamic data. These change daily and must NOT be written into profile.md:
- Sleep flags or last night's sleep duration
- Current savings balance or savings projections
- Today's or yesterday's workout
- Current weight (unless it's changed significantly and held for 2+ weeks)
- Any data that will be different tomorrow

ONLY propose updates for stable, slowly-changing facts:
- Schedule changes (e.g. tutoring hours changed permanently)
- New applications sent (running total, not individual nights)
- Career milestones (mentor last contact, interview outcomes)
- Projects changing phase or status
- University course changes

Examples of GOOD proposals:
- Applications sent this month was None, but a new application was logged → update applications count
- Pokemon reselling plan now defined based on check-in → update from undefined to defined
- Tutoring schedule permanently changed → update fixed commitments

Examples of BAD proposals (never make these):
- Anything mentioning sleep, last night, last night's sleep, sleep flag
- Savings balance or projected date (changes with every CSV upload)
- Weight unless it's been stable at a new value for weeks
- Vague suggestions like "consider updating goals"
- Things already accurate in the profile
- Speculative updates not backed by actual data

Return ONLY the JSON array, nothing else."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        response = message.content[0].text.strip()

        # Strip markdown code fences if present
        response = re.sub(r"^```json\s*", "", response)
        response = re.sub(r"\s*```$", "", response)

        # Strip markdown code blocks if Claude wrapped the JSON
        clean = response.strip()
        if '```' in clean:
            for part in clean.split('```'):
                part = part.strip().lstrip('json').strip()
                if part.startswith('[') or part.startswith('{'):
                    clean = part
                    break
        # Try parsing, fall back to empty list on any error
        try:
            proposals = json.loads(clean)
        except json.JSONDecodeError:
            # Try to extract just the array portion

            match = re.search(r'\[.*\]', clean, re.DOTALL)
            if match:
                try:
                    proposals = json.loads(match.group())
                except Exception:
                    proposals = []
            else:
                proposals = []
        return proposals if isinstance(proposals, list) else []
    except Exception as e:
        print(f"⚠️   Proposal generation failed: {e}")
        return []


# ── Apply an approved update to profile.md ───────────────────────────────────

def apply_update(proposal):
    """
    Applies an approved proposal to profile.md.
    Uses Claude to make the edit cleanly.
    Returns True if successful.
    """
    if not PROFILE_FILE.exists():
        return False

    profile_text = PROFILE_FILE.read_text()
    client       = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Update the last updated date
    tz       = pytz.timezone(config.TIMEZONE)
    today    = datetime.datetime.now(tz).date()
    date_str = today.strftime("%B %Y")

    prompt = f"""Update this profile.md document by applying exactly one change.

CURRENT PROFILE.MD:
{profile_text}

CHANGE TO APPLY:
Section: {proposal['section']}
Find this text: {proposal['current_value']}
Replace with: {proposal['proposed_value']}

Also update the "Last updated" date at the top to: {date_str}

Return the complete updated profile.md document. Make ONLY the specified change plus the date update.
Do not add commentary, do not change anything else."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
        # A truncated rewrite silently destroys the tail of profile.md
        if message.stop_reason == "max_tokens":
            print("  ❌  Rewrite hit the token limit — profile NOT updated (would truncate)")
            return False
        updated = message.content[0].text.strip()

        # Strip markdown fences if Claude wrapped it
        updated = re.sub(r"^```markdown\s*", "", updated)
        updated = re.sub(r"^```\s*", "", updated)
        updated = re.sub(r"\s*```$", "", updated)

        # Back up the current profile before overwriting
        backup_path = SCRIPT_DIR / f"profile_backup_{today.strftime('%Y%m%d')}.md"
        if not backup_path.exists():
            backup_path.write_text(profile_text)
            print(f"  📦  Backup saved to {backup_path.name}")

        PROFILE_FILE.write_text(updated)
        return True
    except Exception as e:
        print(f"  ❌  Failed to apply update: {e}")
        return False


def apply_all_updates(proposals_to_apply):
    """
    Applies a batch of proposals to profile.md in a single Claude call.
    Returns True if the profile was updated.
    """
    if not PROFILE_FILE.exists() or not proposals_to_apply:
        return False

    profile_text = PROFILE_FILE.read_text()
    client       = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    tz       = pytz.timezone(config.TIMEZONE)
    today    = datetime.datetime.now(tz).date()
    date_str = today.strftime("%B %Y")

    changes = "\n\n".join(
        f"CHANGE {i}:\n"
        f"Section: {p['section']}\n"
        f"Find this text: {p['current_value']}\n"
        f"Replace with: {p['proposed_value']}"
        for i, p in enumerate(proposals_to_apply, 1)
    )

    prompt = f"""Update this profile.md document by applying every change listed below.

CURRENT PROFILE.MD:
{profile_text}

CHANGES TO APPLY:
{changes}

Also update the "Last updated" date at the top to: {date_str}

Return the complete updated profile.md document. Make ONLY the specified changes plus the date update.
Do not add commentary, do not change anything else."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
        # A truncated rewrite silently destroys the tail of profile.md
        if message.stop_reason == "max_tokens":
            print("  ❌  Rewrite hit the token limit — profile NOT updated (would truncate)")
            return False
        updated = message.content[0].text.strip()

        updated = re.sub(r"^```markdown\s*", "", updated)
        updated = re.sub(r"^```\s*", "", updated)
        updated = re.sub(r"\s*```$", "", updated)

        # Back up the current profile before overwriting
        backup_path = SCRIPT_DIR / f"profile_backup_{today.strftime('%Y%m%d')}.md"
        if not backup_path.exists():
            backup_path.write_text(profile_text)
            print(f"  📦  Backup saved to {backup_path.name}")

        PROFILE_FILE.write_text(updated)
        return True
    except Exception as e:
        print(f"  ❌  Failed to apply updates: {e}")
        return False


def _record_trust(approved):
    """Records an approve/reject outcome for the "profile" trust category. Never raises."""
    try:
        from proposal_trust import record_outcome
        record_outcome("profile", approved)
    except Exception:
        pass


# ── Interactive review CLI ────────────────────────────────────────────────────

def review_proposals():
    """
    Interactive CLI to review and approve/reject pending proposals.
    """
    pending = get_pending_proposals()

    if not pending:
        print("\n✅  No pending profile updates.\n")
        return

    print(f"\n📋  {len(pending)} pending profile update(s) from Jarvis:\n")

    proposals = load_proposals()

    for p in pending:
        print(f"  ┌─ [{p['id']}] {p['section']}")
        print(f"  │  Currently: {p['current_value'][:100]}")
        print(f"  │  Proposed:  {p['proposed_value'][:100]}")
        print(f"  │  Why: {p['reason']}")
        print(f"  └─ ", end="")

        response = input("Apply? (y/n/skip) → ").strip().lower()

        if response == "y":
            print(f"  ⏳  Applying update...")
            if apply_update(p):
                # Mark as approved
                for proposal in proposals:
                    if proposal["id"] == p["id"]:
                        proposal["status"] = "approved"
                        proposal["resolved_date"] = datetime.datetime.now(
                            pytz.timezone(config.TIMEZONE)
                        ).strftime("%Y-%m-%d")
                save_proposals(proposals)
                _record_trust(approved=True)
                print(f"  ✅  Update applied to profile.md")
            else:
                print(f"  ❌  Update failed — profile unchanged")

        elif response == "n":
            # Mark as rejected
            for proposal in proposals:
                if proposal["id"] == p["id"]:
                    proposal["status"] = "rejected"
                    proposal["resolved_date"] = datetime.datetime.now(
                        pytz.timezone(config.TIMEZONE)
                    ).strftime("%Y-%m-%d")
            save_proposals(proposals)
            _record_trust(approved=False)
            print(f"  ⏭   Rejected")

        else:
            print(f"  ⏭   Skipped (will ask again tomorrow)")

        print()

    remaining = get_pending_proposals()
    if not remaining:
        print("✅  All proposals resolved.\n")
    else:
        print(f"⏳  {len(remaining)} proposal(s) still pending.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis Profile Update System")
    parser.add_argument("--list",  action="store_true", help="List pending proposals")
    parser.add_argument("--yes",   action="store_true", help="Accept all pending proposals in one API call")
    parser.add_argument("--prune", action="store_true", help="Auto-reject pending proposals that are transient or near-duplicates")
    args = parser.parse_args()

    if args.prune:
        with proposals_lock():
            proposals = load_proposals()
            resolved  = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            seen_keys = set()
            pruned    = 0
            for p in proposals:
                if p["status"] != "pending":
                    continue
                key = (p["section"], _normalise(p["proposed_value"])[:100])
                if is_transient_proposal(p["proposed_value"], p.get("reason", "")):
                    p["status"], p["resolved_date"] = "rejected", resolved
                    print(f"  🧹  [{p['id']}] transient — {p['proposed_value'][:70]}")
                    pruned += 1
                elif key in seen_keys:
                    p["status"], p["resolved_date"] = "rejected", resolved
                    print(f"  🧹  [{p['id']}] duplicate — {p['proposed_value'][:70]}")
                    pruned += 1
                else:
                    seen_keys.add(key)
            save_proposals(proposals)
        remaining = len(get_pending_proposals())
        print(f"\n✅  Pruned {pruned} proposal(s); {remaining} still pending.\n")

    elif args.list:
        pending = get_pending_proposals()
        if not pending:
            print("\n✅  No pending proposals.\n")
        else:
            print(f"\n📋  {len(pending)} pending proposal(s):\n")
            for p in pending:
                print(f"  [{p['id']}] {p['section']}: {p['proposed_value'][:80]}")
            print()
    elif args.yes:
        pending = get_pending_proposals()
        if not pending:
            print("\n✅  No pending proposals.\n")
        else:
            print(f"\n⚡  Accepting all {len(pending)} proposal(s) in one shot...\n")
            for p in pending:
                print(f"  ✔  [{p['id']}] {p['section']}: {p['proposed_value'][:80]}")
            print()
            with proposals_lock():
                if apply_all_updates(pending):
                    proposals = load_proposals()
                    resolved = datetime.datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")
                    for proposal in proposals:
                        if proposal["status"] == "pending":
                            proposal["status"] = "approved"
                            proposal["resolved_date"] = resolved
                    save_proposals(proposals)
                    for p in pending:
                        _record_trust(approved=True)
                    print(f"\n✅  All {len(pending)} update(s) applied to profile.md\n")
                else:
                    print("\n❌  Batch apply failed — profile unchanged\n")
    else:
        review_proposals()

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

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR       = Path(__file__).parent
MEMORY_DIR       = SCRIPT_DIR / "memory"
PROPOSALS_FILE   = MEMORY_DIR / "proposed_updates.json"
PROFILE_FILE     = SCRIPT_DIR / "profile.md"

MEMORY_DIR.mkdir(exist_ok=True)
TIMEZONE = pytz.timezone(config.TIMEZONE)


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
    PROPOSALS_FILE.write_text(json.dumps(proposals, indent=2, default=str))


def add_proposal(section, current_value, proposed_value, reason):
    """
    Adds a new proposal if it's not already pending.
    Returns True if added, False if duplicate.
    """
    proposals = load_proposals()

    # Check for duplicate
    for p in proposals:
        if p["section"] == section and p["proposed_value"] == proposed_value:
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

Examples of GOOD proposals:
- Current weight was 71kg, Hevy data shows consistent training suggesting progress → propose weight check
- Applications sent this month was None, but a new application was logged → update applications count
- Pokemon reselling plan now defined based on check-in → update from undefined to defined

Examples of BAD proposals (don't make these):
- Vague suggestions like "consider updating goals"
- Things already accurate in the profile
- Speculative updates not backed by actual data
- Updates to stable facts like name, degree, target companies

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
            import re
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
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
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
    parser.add_argument("--list", action="store_true", help="List pending proposals")
    args = parser.parse_args()

    if args.list:
        pending = get_pending_proposals()
        if not pending:
            print("\n✅  No pending proposals.\n")
        else:
            print(f"\n📋  {len(pending)} pending proposal(s):\n")
            for p in pending:
                print(f"  [{p['id']}] {p['section']}: {p['proposed_value'][:80]}")
            print()
    else:
        review_proposals()

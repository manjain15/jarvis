"""
Jarvis — Term Context Update System
====================================
Jarvis proposes updates to term_context.json based on what it observes in
nightly memory updates (evening check-in, conversation, episodic memory).
You approve or reject them each morning.

HOW IT WORKS:
  1. memory_system.py calls generate_proposals() nightly
  2. Proposed updates saved to memory/term_proposed_updates.json
  3. Morning brief surfaces pending proposals
  4. You run: python term_updates.py to review and approve
  5. Approved updates are applied to term_context.json automatically
     (via update_internship / update_mentor / mark_assessment_done / set_due)

USAGE:
  python term_updates.py          — review and approve/reject proposals
  python term_updates.py --list   — just list pending proposals
"""

import json
import datetime
import re
from pathlib import Path

import pytz
import anthropic
import config

import term_context
from json_store import file_lock, atomic_write_json

SCRIPT_DIR     = Path(__file__).parent
MEMORY_DIR     = SCRIPT_DIR / "memory"
PROPOSALS_FILE = MEMORY_DIR / "term_proposed_updates.json"
CONTEXT_FILE   = SCRIPT_DIR / "term_context.json"


def proposals_lock():
    """Context manager guarding the full load -> mutate -> save cycle for PROPOSALS_FILE."""
    return file_lock(PROPOSALS_FILE)

MEMORY_DIR.mkdir(exist_ok=True)
TIMEZONE = pytz.timezone(config.TIMEZONE)


# Allowed proposal actions and required params.
ALLOWED_ACTIONS = {
    "internship_status_change": ["company", "status"],
    "internship_next_action":   ["company", "next_action"],
    "mentor_update":            ["last_topic", "awaiting_response"],
    "assessment_due_set":       ["subject_code", "assessment_name", "due"],
    "assessment_submitted":     ["subject_code", "assessment_name"],
    "workout_schedule_change":  ["schedule"],
    "finance_goal_change":      ["goals"],
}


# ── Proposal storage ──────────────────────────────────────────────────────────

def load_proposals():
    if not PROPOSALS_FILE.exists():
        return []
    try:
        return json.loads(PROPOSALS_FILE.read_text())
    except Exception:
        return []


def save_proposals(proposals):
    atomic_write_json(PROPOSALS_FILE, proposals)


def _dedup_key(action, params):
    return (action, json.dumps(params, sort_keys=True))


def add_proposal(action, params, summary, reason):
    """Adds a new proposal if not already pending. Returns True if added."""
    if action not in ALLOWED_ACTIONS:
        return False
    required = ALLOWED_ACTIONS[action]
    if not all(k in params for k in required):
        return False

    with proposals_lock():
        proposals = load_proposals()
        key = _dedup_key(action, params)
        # Check every status, not just pending: a rejected proposal must not be
        # re-queued nightly from the same evidence, and an approved one is already
        # applied so re-proposing it is redundant.
        for p in proposals:
            if _dedup_key(p["action"], p["params"]) == key:
                return False

        now = datetime.datetime.now(TIMEZONE)
        proposals.append({
            "id":      len(proposals) + 1,
            "date":    now.strftime("%Y-%m-%d"),
            "action":  action,
            "params":  params,
            "summary": summary,
            "reason":  reason,
            "status":  "pending",
        })
        save_proposals(proposals)
        return True


def get_pending_proposals():
    return [p for p in load_proposals() if p["status"] == "pending"]


def format_proposals_for_brief():
    """Formatted string for the morning brief; empty when nothing pending."""
    pending = get_pending_proposals()
    if not pending:
        return ""

    lines = [f"JARVIS SUGGESTS {len(pending)} TERM CONTEXT UPDATE(S):"]
    for p in pending:
        lines.append(f"  [{p['id']}] {p['summary']}")
        lines.append(f"       Why: {p['reason'][:120]}")
    lines.append("")
    lines.append("  → Run: python term_updates.py to approve or reject")
    return "\n".join(lines)


# ── Generate proposals from today's data ─────────────────────────────────────

def generate_proposals(today_data, episodic_entry):
    """
    Ask Claude to identify term_context.json updates worth proposing
    based on today's data + episodic entry. Returns a list of proposal dicts.
    """
    try:
        ctx_text = CONTEXT_FILE.read_text()
    except Exception:
        return []

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are Jarvis's term-context update detector. You watch for changes
in Manav's day-to-day data that imply his term_context.json is now out of date.

CURRENT term_context.json:
{ctx_text}

TODAY'S DATA:
{today_data}

TODAY'S EPISODIC ENTRY:
{episodic_entry}

YOUR TASK:
Identify concrete changes that are CLEARLY supported by the data — not speculation.
Only propose changes whose evidence is explicit (a check-in line, an email, an episodic entry).
Do NOT propose changes for things that are already accurate in term_context.json.
Do NOT propose vague status changes ("might be interviewing soon").

Respond ONLY with a JSON array of proposals. Each proposal must use one of these actions:

  • "internship_status_change" — params: {{"company": "<Co>", "status": "<applied|OA_completed|interview|offer|rejected|withdrawn>", "next_action": "<optional new next action>"}}
  • "internship_next_action"   — params: {{"company": "<Co>", "next_action": "<new next action text>"}}
  • "mentor_update"            — params: {{"last_topic": "<topic just discussed>", "awaiting_response": true|false}}
  • "assessment_due_set"       — params: {{"subject_code": "COMP2511", "assessment_name": "Assignment 1", "due": "YYYY-MM-DD", "weight": <int or null>}}
  • "assessment_submitted"     — params: {{"subject_code": "COMP2511", "assessment_name": "Assignment 1"}}

Each proposal also needs:
  • "summary" — one-line human-readable summary ("Canva: OA_completed → interview")
  • "reason"  — one sentence pointing to the evidence ("Check-in mentions Canva sent an invite for next Tuesday")

Examples of GOOD proposals:
- Check-in says "got Canva interview invite for Tuesday" → internship_status_change Canva→interview
- Check-in says "spoke to my Google mentor about exchange" → mentor_update last_topic="exchange plans", awaiting_response=false
- Episodic says "submitted COMP2511 assignment 1" → assessment_submitted
- Email subject "MATH2601 Assignment 1 — Due 12 July" → assessment_due_set

Examples of BAD proposals (do NOT make these):
- "Manav might apply to Atlassian soon" — speculative, no concrete change
- "Status: applied" when already applied — no change
- New companies not already in the pipeline (out of scope for proposals)

Return ONLY the JSON array. No commentary, no code fences."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        response = message.content[0].text.strip()

        response = re.sub(r"^```json\s*", "", response)
        response = re.sub(r"^```\s*", "", response)
        response = re.sub(r"\s*```$", "", response)

        try:
            proposals = json.loads(response)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", response, re.DOTALL)
            proposals = json.loads(match.group()) if match else []

        return proposals if isinstance(proposals, list) else []
    except Exception as e:
        print(f"⚠️   Term proposal generation failed: {e}")
        return []


# ── Apply an approved proposal to term_context.json ──────────────────────────

def apply_update(proposal):
    """Mutates term_context.json via the term_context helpers. Returns True on success."""
    action = proposal.get("action")
    params = proposal.get("params", {})

    try:
        if action == "internship_status_change":
            kwargs = {"status": params["status"]}
            if params.get("next_action"):
                kwargs["next_action"] = params["next_action"]
            term_context.update_internship(params["company"], **kwargs)
            return True

        if action == "internship_next_action":
            term_context.update_internship(params["company"], next_action=params["next_action"])
            return True

        if action == "mentor_update":
            term_context.update_mentor(
                params["last_topic"],
                awaiting=bool(params.get("awaiting_response", True)),
            )
            return True

        if action == "assessment_submitted":
            term_context.mark_assessment_done(params["subject_code"], params["assessment_name"])
            return True

        if action == "assessment_due_set":
            matched = {}

            def _mutate(ctx):
                for subject in ctx.get("subjects", []):
                    if subject["code"].upper() != params["subject_code"].upper():
                        continue
                    for a in subject.get("assessments", []):
                        if params["assessment_name"].lower() in a["name"].lower():
                            a["due"] = params["due"]
                            if params.get("weight") is not None:
                                a["weight"] = params["weight"]
                            matched["name"] = a["name"]
                            return

            term_context.mutate_context(_mutate)
            if matched:
                return True
            print(f"  ❌  Assessment not found: {params['subject_code']} / {params['assessment_name']}")
            return False

        if action == "workout_schedule_change":
            term_context.update_workout_schedule(params["schedule"])
            return True

        if action == "finance_goal_change":
            term_context.update_finance_goals(params["goals"])
            return True

        print(f"  ❌  Unknown action: {action}")
        return False
    except Exception as e:
        print(f"  ❌  Failed to apply update: {e}")
        return False


def _record_trust(proposal, approved):
    """Records an approve/reject outcome for this proposal's `action` category. Never raises."""
    try:
        from proposal_trust import record_outcome
        record_outcome(f"term:{proposal['action']}", approved)
    except Exception:
        pass


# ── Interactive review CLI ───────────────────────────────────────────────────

def review_proposals():
    pending = get_pending_proposals()

    if not pending:
        print("\n✅  No pending term-context updates.\n")
        return

    print(f"\n📋  {len(pending)} pending term-context update(s) from Jarvis:\n")

    proposals = load_proposals()
    today_iso = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    for p in pending:
        print(f"  ┌─ [{p['id']}] {p['summary']}")
        print(f"  │  Action: {p['action']}")
        print(f"  │  Params: {json.dumps(p['params'])}")
        print(f"  │  Why: {p['reason']}")
        print(f"  └─ ", end="")

        response = input("Apply? (y/n/skip) → ").strip().lower()

        if response == "y":
            print("  ⏳  Applying update...")
            if apply_update(p):
                for proposal in proposals:
                    if proposal["id"] == p["id"]:
                        proposal["status"] = "approved"
                        proposal["resolved_date"] = today_iso
                save_proposals(proposals)
                _record_trust(p, approved=True)
                print("  ✅  term_context.json updated")
            else:
                print("  ❌  Update failed — file unchanged")
        elif response == "n":
            for proposal in proposals:
                if proposal["id"] == p["id"]:
                    proposal["status"] = "rejected"
                    proposal["resolved_date"] = today_iso
            save_proposals(proposals)
            _record_trust(p, approved=False)
            print("  ⏭   Rejected")
        else:
            print("  ⏭   Skipped (will ask again tomorrow)")
        print()

    remaining = get_pending_proposals()
    if not remaining:
        print("✅  All term proposals resolved.\n")
    else:
        print(f"⏳  {len(remaining)} term proposal(s) still pending.\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis Term Context Update System")
    parser.add_argument("--list", action="store_true", help="List pending proposals")
    parser.add_argument("--yes",  action="store_true", help="Apply all pending proposals without prompting")
    args = parser.parse_args()

    if args.yes:
        pending = get_pending_proposals()
        if not pending:
            print("\n✅  No pending term proposals.\n")
        else:
            print(f"\n⚡  Applying all {len(pending)} term proposal(s)...\n")
            today_iso = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            applied = 0
            with proposals_lock():
                proposals = load_proposals()
                for p in pending:
                    if apply_update(p):
                        for proposal in proposals:
                            if proposal["id"] == p["id"]:
                                proposal["status"] = "approved"
                                proposal["resolved_date"] = today_iso
                        applied += 1
                        _record_trust(p, approved=True)
                        print(f"  ✔  [{p['id']}] {p['summary']}")
                    else:
                        print(f"  ❌  [{p['id']}] {p['summary']} — failed, left pending")
                save_proposals(proposals)
            print(f"\n✅  {applied}/{len(pending)} applied to term_context.json\n")

    elif args.list:
        pending = get_pending_proposals()
        if not pending:
            print("\n✅  No pending term proposals.\n")
        else:
            print(f"\n📋  {len(pending)} pending term proposal(s):\n")
            for p in pending:
                print(f"  [{p['id']}] {p['summary']}")
            print()
    else:
        review_proposals()

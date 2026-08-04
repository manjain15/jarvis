"""
Jarvis — Proposal Trust Scorer
=================================
Tracks how often each proposal category gets approved vs. rejected, as a
Beta-Bernoulli estimate per category. This is observability only for now:
nothing reads these scores to change behaviour yet. The goal is to build
up real approve/reject history before any auto-apply decision is wired
to it.

Categories:
  "profile"                    — all profile.md proposals (one bucket;
                                  the freeform `section` field doesn't
                                  repeat enough to key on directly)
  "term:<action>"               — term_context.json proposals, keyed by
                                  their fixed `action` type (5 known
                                  values in term_updates.ALLOWED_ACTIONS)

USAGE:
  from proposal_trust import record_outcome, trust_score, format_trust_summary

  record_outcome("profile", approved=True)
  record_outcome(f"term:{proposal['action']}", approved=False)
"""

from pathlib import Path

from json_store import file_lock, atomic_write_json

SCRIPT_DIR  = Path(__file__).parent
MEMORY_DIR  = SCRIPT_DIR / "memory"
TRUST_FILE  = MEMORY_DIR / "proposal_trust.json"

MEMORY_DIR.mkdir(exist_ok=True)


def _load():
    if not TRUST_FILE.exists():
        return {}
    try:
        import json
        return json.loads(TRUST_FILE.read_text())
    except Exception:
        return {}


def record_outcome(category, approved):
    """
    Records one human approve/reject decision for `category`. Race-safe
    across concurrent Telegram commands and interactive CLI review.
    """
    with file_lock(TRUST_FILE):
        data = _load()
        counts = data.setdefault(category, {"approved": 0, "rejected": 0})
        counts["approved" if approved else "rejected"] += 1
        atomic_write_json(TRUST_FILE, data)


def trust_score(category):
    """
    Returns {"approved": int, "rejected": int, "total": int, "rate": float|None}
    for `category`. `rate` is the raw approval rate (Beta mean with a
    Beta(1,1) uniform prior), None if there's no history yet.
    """
    counts = _load().get(category, {"approved": 0, "rejected": 0})
    approved, rejected = counts["approved"], counts["rejected"]
    total = approved + rejected
    rate = (approved + 1) / (total + 2) if total else None
    return {"approved": approved, "rejected": rejected, "total": total, "rate": rate}


def format_trust_summary():
    """Formatted string of all scored categories, for the /trust command."""
    data = _load()
    if not data:
        return "No proposal history yet."
    lines = ["📊 Proposal trust (approved/rejected):"]
    for category in sorted(data):
        s = trust_score(category)
        pct = f"{s['rate']*100:.0f}%" if s["rate"] is not None else "—"
        lines.append(f"  {category}: {s['approved']}/{s['rejected']} ({pct} est.)")
    return "\n".join(lines)

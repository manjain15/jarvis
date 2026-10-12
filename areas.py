"""
Jarvis — Areas
==============
The non-study things Jarvis pushes: the passion project, portfolio, career
(applications), networking and weight. Each area has a next action, the
date it was last touched, a weekly target and an optional pause.

Last touched comes from evidence where Jarvis has it, and from the evening
check-in otherwise:
  portfolio   GitHub commits to the repo in "evidence" (GITHUB_TOKEN)
  career      the newest internship last_update in term_context.json
  networking  mentor.last_contact in term_context.json
  weight      the newest check-in with a weight
  project     check-in answers (no repo yet)

The 05:30 plan gives the most neglected unpaused area a 45-min block, and
the morning brief's "One push" picks from the same list. Grok bots read
GET /areas and update with PATCH /areas/<name>. Stored in data/areas.json
(VPS-authoritative, gitignored). See docs/REWORK.md §8.

    python areas.py     # refresh evidence and print the areas
"""

import datetime
import json
import os
from pathlib import Path

import pytz

import config
from json_store import atomic_write_json, file_lock

SCRIPT_DIR = Path(__file__).parent
AREAS_FILE = SCRIPT_DIR / "data" / "areas.json"
DATA_DIR   = SCRIPT_DIR / "data"
TIMEZONE   = pytz.timezone(config.TIMEZONE)

# blockable: gets a planned work block when it's the most neglected area.
DEFAULT_AREAS = {
    "project":    {"label": "Passion project",          "target_per_week": 2, "blockable": True,
                   "next_action": "Write the one-page concept"},
    "portfolio":  {"label": "Portfolio / GitHub",       "target_per_week": 2, "blockable": True,
                   "evidence": "github:manjain15/jarvis",
                   "next_action": "Clean up the Jarvis README for a public demo"},
    "career":     {"label": "Internship applications",  "target_per_week": 2, "blockable": True,
                   "next_action": "Apply to one SWE or ML internship"},
    "networking": {"label": "Networking",                "target_per_week": 1, "blockable": True,
                   "next_action": "Message one engineer at a target company"},
    "weight":     {"label": "Weight",                    "target_per_week": 3, "blockable": False,
                   "next_action": "Weigh in tomorrow morning"},
}
WRITABLE_FIELDS = ("next_action", "last_touched", "paused", "paused_reason", "notes", "target_per_week")
NEVER_TOUCHED_DAYS = 30
EVIDENCE_TTL = datetime.timedelta(hours=1)
GITHUB_TIMEOUT = 15


def _today():
    return datetime.datetime.now(TIMEZONE).date()


def load():
    """All areas: stored state over the defaults."""
    try:
        stored = json.loads(AREAS_FILE.read_text())
    except Exception:
        stored = {}
    areas = {}
    for name, default in DEFAULT_AREAS.items():
        state = stored.get("areas", {}).get(name, {})
        areas[name] = {"name": name, "next_action": "", "last_touched": None, "touches": [],
                       "paused": False, "paused_reason": "", "notes": "", **default, **state}
    return areas


def _save(areas, meta=None):
    payload = {"areas": {n: {k: v for k, v in a.items() if k != "name"} for n, a in areas.items()}}
    try:
        old = json.loads(AREAS_FILE.read_text())
    except Exception:
        old = {}
    payload["evidence_checked"] = (meta or {}).get("evidence_checked", old.get("evidence_checked"))
    atomic_write_json(AREAS_FILE, payload)


def mutate(fn):
    """Locked read-modify-write of the areas. fn(areas) edits in place; its return value is returned."""
    with file_lock(AREAS_FILE):
        areas = load()
        result = fn(areas)
        _save(areas)
    return result


def touch(areas, name, date, source):
    """Record that an area was worked on. Keeps the newest last_touched and a 28-day touch log."""
    area = areas[name]
    iso = date.isoformat()
    if not area.get("last_touched") or iso > area["last_touched"]:
        area["last_touched"] = iso
    touches = [t for t in area.get("touches", []) if t["date"] != iso or t["source"] != source]
    touches.append({"date": iso, "source": source})
    cutoff = (date - datetime.timedelta(days=28)).isoformat()
    area["touches"] = sorted([t for t in touches if t["date"] >= cutoff], key=lambda t: t["date"])


def days_since(area, today=None):
    """Days since an area was last touched (NEVER_TOUCHED_DAYS when never)."""
    today = today or _today()
    if not area.get("last_touched"):
        return NEVER_TOUCHED_DAYS
    return (today - datetime.date.fromisoformat(area["last_touched"])).days


def this_week(area, week_start):
    """Distinct days the area was touched since week_start."""
    return len({t["date"] for t in area.get("touches", []) if t["date"] >= week_start.isoformat()})


def most_neglected(areas, today=None, blockable_only=False):
    """Unpaused areas, most neglected first (days since touched, then weekly shortfall)."""
    today = today or _today()
    try:
        from term_context import week_start
        start = week_start(today)
    except Exception:
        start = today - datetime.timedelta(days=today.weekday())
    candidates = [a for a in areas.values() if not a.get("paused")
                  and (a.get("blockable") or not blockable_only)]
    return sorted(candidates, key=lambda a: (-days_since(a, today),
                                             this_week(a, start) - a.get("target_per_week", 1)))


# ── Evidence ─────────────────────────────────────────────────────────────────

def _github_last_commit(repo):
    """Date of the newest commit on repo's default branch, or None."""
    import requests
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/commits", params={"per_page": 1},
                         headers=headers, timeout=GITHUB_TIMEOUT)
        if r.status_code != 200 or not r.json():
            return None
        raw = r.json()[0]["commit"]["committer"]["date"]
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TIMEZONE).date()
    except Exception as e:
        print(f"⚠️  areas: GitHub check failed for {repo}: {e}")
        return None


def _term_evidence():
    """{area: date} from term_context: newest internship update and mentor contact."""
    out = {}
    try:
        import term_context
        ctx = term_context.load_context()
    except Exception:
        return out
    updates = [i.get("last_update") for i in ctx.get("internships", []) if i.get("last_update")]
    if updates:
        out["career"] = datetime.date.fromisoformat(max(updates))
    contact = (ctx.get("mentor") or {}).get("last_contact")
    if contact:
        out["networking"] = datetime.date.fromisoformat(contact)
    return out


def _weight_evidence():
    """Date of the newest check-in that recorded a weight, or None."""
    for path in sorted(DATA_DIR.glob("checkin_*.json"), reverse=True)[:30]:
        try:
            if json.loads(path.read_text()).get("weight_today"):
                return datetime.date.fromisoformat(path.stem.split("_", 1)[1])
        except Exception:
            continue
    return None


def refresh_evidence(now=None, github=None, force=False):
    """Fold evidence into last_touched. GitHub is checked at most hourly. Returns the areas."""
    now = now or datetime.datetime.now(TIMEZONE)
    github = github or _github_last_commit
    try:
        checked = json.loads(AREAS_FILE.read_text()).get("evidence_checked")
    except Exception:
        checked = None
    if checked and not force and now - datetime.datetime.fromisoformat(checked) < EVIDENCE_TTL:
        return load()

    found = _term_evidence()
    weighed = _weight_evidence()
    if weighed:
        found["weight"] = weighed
    commits = {}
    for name, default in DEFAULT_AREAS.items():
        source = default.get("evidence", "")
        if source.startswith("github:"):
            date = github(source.split(":", 1)[1])
            if date:
                commits[name] = date

    with file_lock(AREAS_FILE):
        areas = load()
        for name, date in found.items():
            touch(areas, name, date, "evidence")
        for name, date in commits.items():
            touch(areas, name, date, "github")
        _save(areas, {"evidence_checked": now.isoformat(timespec="seconds")})
    return areas


# ── API helpers ──────────────────────────────────────────────────────────────

def summary(areas=None, today=None):
    """GET /areas body: each area with days since touched, this week's count, and the pick."""
    today = today or _today()
    areas = areas or load()
    try:
        from term_context import week_start
        start = week_start(today)
    except Exception:
        start = today - datetime.timedelta(days=today.weekday())
    rows = []
    for a in areas.values():
        rows.append({k: a.get(k) for k in ("name", "label", "next_action", "last_touched",
                                            "paused", "paused_reason", "notes", "target_per_week")}
                    | {"days_since": days_since(a, today), "this_week": this_week(a, start)})
    pick = next(iter(most_neglected(areas, today, blockable_only=True)), None)
    return {"areas": rows, "most_neglected": pick["name"] if pick else None}


def patch(name, body):
    """Apply a PATCH /areas/<name> body. Raises ValueError on bad input, KeyError on an unknown area."""
    if name not in DEFAULT_AREAS:
        raise KeyError(name)
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    extra = sorted(set(body) - set(WRITABLE_FIELDS) - {"actor", "reason", "touched"})
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")
    for key in ("next_action", "paused_reason", "notes"):
        if key in body and (not isinstance(body[key], str) or len(body[key]) > 300):
            raise ValueError(f"{key} must be a string up to 300 characters")
    if "paused" in body and not isinstance(body["paused"], bool):
        raise ValueError("paused must be true or false")
    if "target_per_week" in body and (not isinstance(body["target_per_week"], int)
                                      or not 0 <= body["target_per_week"] <= 7):
        raise ValueError("target_per_week must be 0-7")
    touched = body.get("touched") or body.get("last_touched")
    if touched is not None:
        try:
            touched = datetime.date.fromisoformat(touched)
        except Exception:
            raise ValueError("touched must be YYYY-MM-DD")
        if touched > _today():
            raise ValueError("touched can't be in the future")

    def _apply(areas):
        area = areas[name]
        for key in ("next_action", "paused", "paused_reason", "notes", "target_per_week"):
            if key in body:
                area[key] = body[key]
        if touched:
            touch(areas, name, touched, "bot")
        return {k: area.get(k) for k in ("name", "next_action", "last_touched", "paused", "paused_reason")}

    return mutate(_apply)


if __name__ == "__main__":
    print(json.dumps(summary(refresh_evidence(force=True)), indent=2))

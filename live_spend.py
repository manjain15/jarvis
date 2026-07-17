"""
Jarvis — Live Spend Logger
===========================
Real-time spending entries logged from the iPhone Back Tap shortcut.

HOW IT WORKS:
  1. Double-tap the back of the iPhone → runs an iOS Shortcut
  2. Shortcut prompts for amount + category, then POSTs to this server:
       POST http://<vps>:5556/spend
       Headers: X-Jarvis-Token: <token from data/.spend_token>
       Body (JSON): {"amount": 14.50, "category": "Food & dining", "note": "coffee"}
  3. Entry is appended to data/live_spend.jsonl
  4. finance_tracker.get_finance_summary() shows live-logged spend alongside
     the CSV data and reconciles the two (CSV remains the source of truth)

SETUP:
  python live_spend.py --gen-token   # create data/.spend_token (once)
  python live_spend.py --serve       # run the server (systemd on VPS)
  python live_spend.py --test        # log a fake entry + print summary
  python live_spend.py               # print current summary

Categories must match finance_tracker's CATEGORY_RULES names so the two
data sources aggregate cleanly.
"""

import argparse
import datetime
import json
import secrets
from pathlib import Path

import pytz
import config

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
SPEND_FILE = DATA_DIR / "live_spend.jsonl"
TOKEN_FILE = DATA_DIR / ".spend_token"

TIMEZONE = pytz.timezone(config.TIMEZONE)

SERVER_PORT = 5556
MAX_AMOUNT  = 5000.00  # sanity cap — reject fat-finger / junk entries

# Must mirror the category names in finance_tracker.CATEGORY_RULES (+ Other)
VALID_CATEGORIES = {
    "Food & dining",
    "Entertainment",
    "Shopping",
    "Sport & leisure",
    "Education",
    "Transport",
    "Subscriptions",
    "Other",
}


# ── Token ─────────────────────────────────────────────────────────────────────

def load_token():
    """Returns the shared-secret token, or None if not yet generated."""
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip() or None
    return None


def generate_token():
    """Creates a new random token in data/.spend_token and returns it."""
    DATA_DIR.mkdir(exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


# ── Logging + reading ─────────────────────────────────────────────────────────

def log_spend(amount, category, note=""):
    """
    Validates and appends one spend entry to data/live_spend.jsonl.
    Returns the stored entry dict. Raises ValueError on bad input.
    """
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number")
    if not (0 < amount <= MAX_AMOUNT):
        raise ValueError(f"amount must be between 0 and {MAX_AMOUNT:.0f}")

    category = str(category or "").strip()
    # Case-insensitive match against the known set, default to Other
    match = next((c for c in VALID_CATEGORIES if c.lower() == category.lower()), None)
    if match is None:
        raise ValueError(f"unknown category '{category}' — valid: {sorted(VALID_CATEGORIES)}")

    note = str(note or "").strip()[:200]

    entry = {
        "ts":       datetime.datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "amount":   amount,
        "category": match,
        "note":     note,
    }
    DATA_DIR.mkdir(exist_ok=True)
    with open(SPEND_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load_entries(days=7):
    """Returns live-spend entries from the last N days, newest first."""
    if not SPEND_FILE.exists():
        return []
    cutoff = datetime.datetime.now(TIMEZONE) - datetime.timedelta(days=days)
    entries = []
    with open(SPEND_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.datetime.fromisoformat(e["ts"])
                if ts >= cutoff:
                    e["date"] = ts.date()
                    entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return sorted(entries, key=lambda e: e["ts"], reverse=True)


# ── Reconciliation against bank CSV ──────────────────────────────────────────

def reconcile(entries, transactions, window_days=4):
    """
    Matches live entries against bank CSV transactions by amount (±$0.01)
    within window_days of the entry date. Each CSV transaction can satisfy
    at most one entry. Returns (matched_count, unmatched_entries).
    """
    used = set()
    unmatched = []
    for e in entries:
        hit = None
        for i, t in enumerate(transactions):
            if i in used or t.get("debit", 0) <= 0:
                continue
            if (abs(t["debit"] - e["amount"]) <= 0.01
                    and abs((t["date"] - e["date"]).days) <= window_days):
                hit = i
                break
        if hit is not None:
            used.add(hit)
        else:
            unmatched.append(e)
    return len(entries) - len(unmatched), unmatched


def get_live_summary(days=7, transactions=None):
    """
    Returns a dict summarising live-logged spend over the last N days:
      {available, total, count, by_category, matched, unmatched}
    `transactions` (optional) enables reconciliation against the bank CSV.
    """
    entries = load_entries(days=days)
    if not entries:
        return {"available": False}

    by_cat = {}
    for e in entries:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]

    matched, unmatched = (None, [])
    if transactions is not None:
        matched, unmatched = reconcile(entries, transactions)

    return {
        "available":   True,
        "total":       round(sum(e["amount"] for e in entries), 2),
        "count":       len(entries),
        "by_category": by_cat,
        "days":        days,
        "matched":     matched,
        "unmatched":   unmatched,
    }


# ── HTTP server ───────────────────────────────────────────────────────────────

def create_app():
    """Builds the Flask app with the /spend and /health routes."""
    from flask import Flask, request, jsonify

    app = Flask(__name__)
    token = load_token()
    if not token:
        raise RuntimeError("No token — run: python live_spend.py --gen-token")

    @app.post("/spend")
    def spend():
        """Auth-checked endpoint the iOS Shortcut POSTs entries to."""
        if request.headers.get("X-Jarvis-Token", "") != token:
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        try:
            entry = log_spend(body.get("amount"), body.get("category"), body.get("note", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        week = get_live_summary(days=7)
        return jsonify({
            "ok": True,
            "logged": f"${entry['amount']:.2f} {entry['category']}",
            "week_total": week["total"] if week["available"] else entry["amount"],
        })

    @app.get("/health")
    def health():
        """Unauthenticated liveness probe for the watchdog."""
        return jsonify({"ok": True})

    return app


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_summary():
    """Prints the 7-day live-spend summary to stdout."""
    s = get_live_summary(days=7)
    if not s["available"]:
        print("No live spend entries in the last 7 days.")
        return
    print(f"Live-logged (7d): ${s['total']:.2f} across {s['count']} entries")
    for cat, amt in sorted(s["by_category"].items(), key=lambda x: -x[1]):
        print(f"  {cat:<18} ${amt:.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis live spend logger")
    parser.add_argument("--serve", action="store_true", help="run the HTTP server")
    parser.add_argument("--gen-token", action="store_true", help="generate the shared secret")
    parser.add_argument("--test", action="store_true", help="log a $1 test entry")
    args = parser.parse_args()

    if args.gen_token:
        print(f"Token written to {TOKEN_FILE}:\n{generate_token()}")
    elif args.test:
        entry = log_spend(1.00, "Other", "test entry — safe to ignore")
        print(f"Logged test entry: {entry}")
        _print_summary()
    elif args.serve:
        create_app().run(host="0.0.0.0", port=SERVER_PORT)
    else:
        _print_summary()

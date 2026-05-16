"""
Jarvis — Subscription Audit
=============================
Scans your St. George CSV for recurring charges.
Identifies anything that appears monthly and flags it.

RUN:
  python subscription_audit.py

UPDATE YOUR CSV FIRST:
  Export last 90 days from St. George and save to finance/everyday.csv
"""

import re
import datetime
from pathlib import Path
from collections import defaultdict

import pytz
import config
from finance_tracker import parse_stgeorge_csv, EVERYDAY_CSV

TIMEZONE = pytz.timezone(config.TIMEZONE)

# Known legitimate subscriptions (won't be flagged as unexpected)
# These recurring purchases are known/expected — not forgotten subscriptions
KNOWN_SPENDING = [
    "eb games",       # Pokemon card buying
    "woolworths",     # groceries
    "opal",           # transport
    "unsw",           # uni fees
]

KNOWN_SUBS = {
    "claude":       ("Claude Pro",         20.00),
    "anthropic":    ("Claude Pro",         20.00),
    "spotify":      ("Spotify",            11.99),
    "netflix":      ("Netflix",            10.99),
    "apple":        ("Apple (App/iCloud)", None),
    "playstation":  ("PlayStation",        None),
    "youtube":      ("YouTube Premium",    None),
    "disney":       ("Disney+",            None),
    "stan ":        ("Stan",               None),
    "icloud":       ("iCloud",             None),
}


# Your own savings account numbers — exclude from audit
OWN_ACCOUNTS = ["0000206850220", "0000436436454", "0000444502124"]

# Recurring spending that's normal (not subscriptions to review)
NORMAL_RECURRING = [
    "woolworths", "coles", "aldi",           # groceries
    "transportfornsw", "opal",               # transport
    "unsw", "university",                     # uni fees
    "mcdonalds", "kfc", "subway",            # food
]

def normalise(description):
    """Strip dates and noise to get a comparable merchant name."""
    desc = description.lower()
    # Remove date patterns like 13May, 08Apr
    desc = re.sub(r'\d{2}[a-z]{3}', '', desc)
    # Remove times
    desc = re.sub(r'\d{2}:\d{2}', '', desc)
    # Remove transaction type prefixes
    for prefix in ["visa purchase", "eftpos debit", "osko withdrawal",
                   "osko deposit", "internet withdrawal", "internet deposit",
                   "sct deposit", "foreign currency"]:
        desc = desc.replace(prefix, "")
    # Remove extra whitespace
    desc = re.sub(r'\s+', ' ', desc).strip()
    # Take first 30 chars as the merchant identifier
    return desc[:30].strip()


def find_recurring(transactions, months=3):
    """
    Finds transactions that recur monthly.
    Groups by normalised merchant name, checks if it appears in multiple months.
    """
    tz    = TIMEZONE
    today = datetime.datetime.now(tz).date()
    cutoff = today - datetime.timedelta(days=months * 31)

    recent = [
        t for t in transactions
        if t["date"] >= cutoff
        and t["debit"] > 0
        and not any(acc in t["description"] for acc in OWN_ACCOUNTS)
        and not any(kw in t["description"].lower() for kw in NORMAL_RECURRING)
    ]

    # Group by normalised merchant
    by_merchant = defaultdict(list)
    for t in recent:
        key = normalise(t["description"])
        if key and len(key) > 3:
            by_merchant[key].append(t)

    # Find merchants that appear in multiple different months
    recurring = []
    for merchant, txns in by_merchant.items():
        months_seen = set(t["date"].strftime("%Y-%m") for t in txns)
        if len(months_seen) >= 2:
            avg_amount = sum(t["debit"] for t in txns) / len(txns)
            # Skip tiny amounts (rounding, fees)
            if avg_amount < 3:
                continue
            recurring.append({
                "merchant":   merchant,
                "count":      len(txns),
                "months":     sorted(months_seen),
                "avg_amount": round(avg_amount, 2),
                "total":      round(sum(t["debit"] for t in txns), 2),
                "last_date":  max(t["date"] for t in txns),
                "sample":     txns[-1]["description"][:45],
            })

    return sorted(recurring, key=lambda x: -x["avg_amount"])


def run_audit():
    if not EVERYDAY_CSV.exists():
        print("❌  No everyday.csv found. Export 90 days from St. George first.")
        return

    print("\n🔍  Subscription Audit — scanning last 90 days\n")
    print("    Export 90 days of transactions for best results.")
    print("    ─────────────────────────────────────────────\n")

    txns      = parse_stgeorge_csv(EVERYDAY_CSV)
    recurring = find_recurring(txns, months=3)

    if not recurring:
        print("✅  No recurring charges detected.\n")
        return

    # Separate known vs unknown subscriptions
    known   = []
    unknown = []

    for r in recurring:
        is_known = any(kw in r["merchant"] for kw in KNOWN_SUBS)
        if is_known:
            known.append(r)
        else:
            unknown.append(r)

    # Monthly cost estimate
    monthly_known   = sum(r["avg_amount"] for r in known)
    monthly_unknown = sum(r["avg_amount"] for r in unknown)
    monthly_total   = monthly_known + monthly_unknown

    print(f"📋  KNOWN SUBSCRIPTIONS ({len(known)}):\n")
    for r in known:
        label = next((v[0] for k, v in KNOWN_SUBS.items() if k in r["merchant"]), r["merchant"])
        print(f"  ✓ {label:<25} ~${r['avg_amount']:.2f}/month")
        print(f"    Seen {r['count']}x across {len(r['months'])} months | Last: {r['last_date']}")

    if unknown:
        print(f"\n❓  UNRECOGNISED RECURRING CHARGES ({len(unknown)}) — review these:\n")
        for r in unknown:
            print(f"  ? {r['merchant']:<25} ~${r['avg_amount']:.2f}/month")
            print(f"    Seen {r['count']}x | Example: {r['sample']}")
            print(f"    Last charged: {r['last_date']}")
            print()

    print(f"─────────────────────────────────────────────")
    print(f"  Known subs:        ~${monthly_known:.2f}/month")
    if unknown:
        print(f"  Unrecognised:      ~${monthly_unknown:.2f}/month  ← investigate")
    print(f"  Total recurring:   ~${monthly_total:.2f}/month")
    annual = monthly_total * 12
    print(f"  Annual cost:       ~${annual:.2f}/year\n")

    if unknown:
        print("💡  Check your unrecognised charges — these might be:")
        print("    • Free trials that converted to paid")
        print("    • Forgotten subscriptions")
        print("    • Legitimate recurring bills (gym, insurance, etc.)\n")


if __name__ == "__main__":
    run_audit()

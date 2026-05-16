"""
Jarvis — Finance Tracker
=========================
Reads St. George Bank CSV exports and produces a finance summary
for the morning brief.

HOW IT WORKS:
  1. You export CSVs from St. George Internet Banking (takes 30 seconds)
  2. Drop them in the jarvis/finance/ folder:
       finance/everyday.csv   — your spending account
       finance/savings1.csv   — general savings
       finance/savings2.csv   — trip/exchange savings
  3. Morning brief calls get_finance_summary() and injects the result

CSV FORMAT (St. George):
  Date,Description,Debit,Credit,Balance
  13/05/2026,Visa Purchase ...,48.50,,657.47,

CATEGORIES:
  Food & dining  — supermarkets, restaurants, cafes, delivery
  Entertainment  — streaming, tickets, games, bars, nightlife
  Shopping       — retail, clothing, online shopping, Amazon
  Transport      — Uber, fuel, parking, public transport
  Subscriptions  — recurring charges (Netflix, Spotify, etc.)
  Other          — anything uncategorised

UPDATE WEEKLY:
  Export last 30 days from St. George → save to finance/ folder → done.
"""

import csv
import datetime
import json
import re
from pathlib import Path

import pytz
import config

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
FINANCE_DIR  = SCRIPT_DIR / "finance"
EVERYDAY_CSV = FINANCE_DIR / "everyday.csv"
SAVINGS1_CSV = FINANCE_DIR / "savings1.csv"
SAVINGS2_CSV = FINANCE_DIR / "savings2.csv"

FINANCE_DIR.mkdir(exist_ok=True)

TIMEZONE = pytz.timezone(config.TIMEZONE)

# ── Savings goal ──────────────────────────────────────────────────────────────

SAVINGS_GOAL      = 35000.00
SAVINGS_DEADLINE  = datetime.date(2027, 1, 1)   # January 2027 exchange
MONTHLY_INCOME    = 2800.00
MONTHLY_BUDGET    = 300.00   # ~$300/month spending (~$75/week)
WEEKLY_BUDGET     = 75.00


# ── Category rules ────────────────────────────────────────────────────────────
# Each rule is a list of keywords (case-insensitive) found in the description.
# First match wins.

# ── Income source rules ──────────────────────────────────────────────────────
# Keywords in credit transaction descriptions → income category

INCOME_RULES = [
    # Automation/PropWealth salary — main job
    ("Automation",  ["propwealth", "prop wealth"]),
    # Tutoring — Axis Education platform
    ("Tutoring",    ["axis ed", "axis education", "tutoring", "tutor"]),
    # Pokemon / marketplace sales
    ("Pokemon Sales", ["ivan christian", "celestino"]),
    # Note: internet deposits and sct deposits are excluded above as own-account transfers
    # Refunds and one-offs
    ("Refund",      ["refund", "cashback", "rebate", "exchange application"]),
    # Friends splitting bills — NOT income, filter these out
    # These are identified by being small Osko deposits from friends
]

# Names/keywords that indicate friend bill-splits (not income)
# Osko deposits from these are excluded from income
FRIEND_KEYWORDS = [
    "sagar ahuja", "adesh sunkari", "nilesh banga", "banga n",
    "samuel selvadoss", "sai satpute", "muhunthan", "michael",
    "paint sprayer",  # one-off reimbursement
    "banga", "grace", "mother s day", "bangas",
    "exchange application fee",  # deposit from own savings
]

def is_friend_split(description):
    desc_lower = description.lower()
    return any(kw in desc_lower for kw in FRIEND_KEYWORDS)

def categorise_income(description):
    desc_lower = description.lower()
    for category, keywords in INCOME_RULES:
        for kw in keywords:
            if kw in desc_lower:
                return category
    return "Other"


CATEGORY_RULES = [
    ("Food & dining", [
        "woolworths", "coles", "aldi", "iga", "harris farm", "costco",
        "mcdonald", "kfc", "subway", "domino", "pizza", "hungry jack",
        "uber eat", "doordash", "menulog", "deliveroo",
        "restaurant", "cafe", "coffee", "bakery", "sushi", "noodle",
        "food", "dining", "eat", "grill", "burger", "thai", "indian",
        "chinese", "italian", "kebab", "chatime", "gong cha", "boost",
    ]),
    ("Entertainment", [
        "cinema", "event cinema", "hoyts", "village cinema",
        "ticketek", "ticketmaster", "tickets", "ticket",
        "spotify", "netflix", "disney", "stan ", "binge", "paramount",
        "playstation", "xbox", "steam", "nintendo", "epic games",
        "apple.com/bill", "itunes", "google play",
        "bar ", "pub ", "club ", "nightclub", "drinks",
        "bowling", "escape room", "laser tag", "entertainment",
        "ko ticket", "ko ", "exodus", "concert", "festival", "show ", "rave",
        "moshtix", "humanitix", "oztix",
    ]),
    ("Shopping", [
        "amazon", "ebay", "kmart", "target", "big w", "myer", "david jones",
        "apple.com/bill", "apple.com",
        "uniqlo", "h&m", "zara", "cotton on", "glue store",
        "jb hi-fi", "officeworks", "apple store", "harvey norman",
        "chemist warehouse", "priceline", "pharmacy",
        "rebel sport", "sports", "asos", "the iconic",
        "shopping", "retail", "store", "shop",
    ]),
    ("Sport & leisure", [
        "golf", "muirfield", "tennis", "swimming", "aquatic",
        "sport", "leisure", "recreation", "fitness", "yoga", "pilates",
        "surfing", "climbing", "crossfit",
    ]),
    ("Education", [
        "unsw", "university", "tafe", "tuition", "textbook", "course fee",
        "student fee", "enrolment",
    ]),
    ("Transport", [
        "uber ", "ola ", "didi ", "taxi",
        "opal", "transport nsw", "train", "bus ",
        "fuel", "petrol", "shell", "bp ", "caltex", "ampol",
        "parking", "wilson parking", "care park",
        "service nsw", "car wash",
    ]),
    ("Subscriptions", [
        "netflix", "spotify", "disney+", "stan ", "binge", "prime video",
        "icloud", "google one", "microsoft", "adobe", "canva",
        "anthropic", "openai", "chatgpt",
        "gym", "anytime fitness", "fitness first", "goodlife",
        "subscription", "monthly", "annual",
    ]),
]


def categorise(description):
    """Returns a category string for a transaction description."""
    desc_lower = description.lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in desc_lower:
                return category
    return "Other"


# ── CSV parsing ───────────────────────────────────────────────────────────────

def parse_stgeorge_csv(filepath):
    """
    Parses a St. George CSV export.
    Returns a list of transaction dicts:
      {date, description, debit, credit, balance}
    """
    transactions = []
    if not filepath.exists():
        return transactions

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Parse date: DD/MM/YYYY
                date_str = row.get("Date", "").strip()
                if not date_str:
                    continue
                date = datetime.datetime.strptime(date_str, "%d/%m/%Y").date()

                description = row.get("Description", "").strip()
                # Clean up extra whitespace in description
                description = re.sub(r'\s+', ' ', description)

                debit  = float(row.get("Debit",  "") or 0)
                credit = float(row.get("Credit", "") or 0)
                # Balance column sometimes has trailing comma — strip it
                balance_str = row.get("Balance", "").strip().rstrip(",")
                balance = float(balance_str) if balance_str else 0.0

                transactions.append({
                    "date":        date,
                    "description": description,
                    "debit":       debit,
                    "credit":      credit,
                    "balance":     balance,
                    "category":    categorise(description),
                })
            except (ValueError, KeyError):
                continue

    return sorted(transactions, key=lambda t: t["date"], reverse=True)


def get_latest_balance(filepath):
    """Returns the most recent balance from a CSV file."""
    transactions = parse_stgeorge_csv(filepath)
    if not transactions:
        return None
    # Most recent entry = first after sort by date descending
    return transactions[0]["balance"]


# ── Spending analysis ─────────────────────────────────────────────────────────

def analyse_spending(transactions, days=7):
    """
    Analyses spending from the everyday account over the last N days.
    Returns a dict with category totals, big transactions, and overall total.
    """
    today    = datetime.datetime.now(TIMEZONE).date()
    cutoff   = today - datetime.timedelta(days=days)

    recent = [
        t for t in transactions
        if t["date"] >= cutoff
        and t["debit"] > 0
        and "internet withdrawal" not in t["description"].lower()
    ]

    # Category totals
    category_totals = {}
    for t in recent:
        cat = t["category"]
        category_totals[cat] = category_totals.get(cat, 0) + t["debit"]

    # Big transactions (over $50, excluding internal transfers)
    big_transactions = [
        t for t in recent
        if t["debit"] >= 80
        and "internet withdrawal" not in t["description"].lower()
        and "transfer" not in t["description"].lower()
        and "osko withdrawal" not in t["description"].lower()
    ]

    total_spend = sum(t["debit"] for t in recent
                      if "internet withdrawal" not in t["description"].lower()
                      and "transfer" not in t["description"].lower())

    return {
        "category_totals": category_totals,
        "big_transactions": big_transactions,
        "total_spend":      total_spend,
        "transaction_count": len(recent),
        "transactions":     recent,
        "days":             days,
    }


# ── Savings progress ──────────────────────────────────────────────────────────

def analyse_savings():
    """
    Reads balances from both savings CSVs and calculates
    progress toward the $35k goal.
    """
    bal1 = get_latest_balance(SAVINGS1_CSV)
    bal2 = get_latest_balance(SAVINGS2_CSV)

    total = (bal1 or 0) + (bal2 or 0)
    remaining = max(0, SAVINGS_GOAL - total)
    pct = (total / SAVINGS_GOAL * 100) if SAVINGS_GOAL > 0 else 0

    # Project completion date based on current savings rate
    today = datetime.datetime.now(TIMEZONE).date()
    days_to_deadline = (SAVINGS_DEADLINE - today).days

    # Monthly savings rate (~95% of income)
    monthly_savings = 2500.00  # ~$2,800 income - ~$300 spending
    months_needed   = remaining / monthly_savings if monthly_savings > 0 else 999
    projected_date  = today + datetime.timedelta(days=months_needed * 30.4)

    on_track = projected_date <= SAVINGS_DEADLINE

    return {
        "balance1":        bal1,
        "balance2":        bal2,
        "total":           total,
        "goal":            SAVINGS_GOAL,
        "remaining":       remaining,
        "pct":             pct,
        "on_track":        on_track,
        "projected_date":  projected_date,
        "days_to_deadline": days_to_deadline,
        "monthly_savings": monthly_savings,
    }


# ── Monthly review check ──────────────────────────────────────────────────────

def is_monthly_review_day():
    """Returns True on the 1st of each month."""
    return datetime.datetime.now(TIMEZONE).day == 1


# ── Main summary function ─────────────────────────────────────────────────────

def analyse_income(transactions, days=30):
    """
    Analyses income (credits) from the everyday account over the last N days.
    Returns dict with income by source and total.
    """
    import datetime as _dt
    import pytz as _pytz
    tz      = _pytz.timezone(config.TIMEZONE)
    today   = _dt.datetime.now(tz).date()
    cutoff  = today - _dt.timedelta(days=days)

    # Credits only, excluding internal transfers and friend bill-splits
    income_txns = [
        t for t in transactions
        if t["date"] >= cutoff
        and t["credit"] > 0
        and "internet withdrawal" not in t["description"].lower()
        and "osko withdrawal" not in t["description"].lower()
        and "from 0000" not in t["description"].lower()   # own savings transfers
        and "internet deposit" not in t["description"].lower()  # own account transfers
        and "sct deposit" not in t["description"].lower()       # own account transfers
        and not is_friend_split(t["description"])
    ]

    # Categorise each credit
    by_source = {}
    for t in income_txns:
        cat = categorise_income(t["description"])
        if cat not in by_source:
            by_source[cat] = {"total": 0, "transactions": []}
        by_source[cat]["total"] += t["credit"]
        by_source[cat]["transactions"].append({
            "date":        t["date"].strftime("%d %b"),
            "description": t["description"][:35],
            "amount":      round(t["credit"], 2),
        })

    total = sum(v["total"] for v in by_source.values())

    return {
        "by_source":   by_source,
        "total":       round(total, 2),
        "days":        days,
        "txn_count":   len(income_txns),
    }


def get_finance_summary():
    """
    Returns a formatted finance summary string for the morning brief.
    Called by morning_brief.py.
    """
    lines = []

    # ── Check if files exist ──────────────────────────────────────────────────
    if not EVERYDAY_CSV.exists():
        return (
            "FINANCE: No data available.\n"
            "  → Export your St. George CSV and save to jarvis/finance/everyday.csv"
        )

    # ── Spending analysis ─────────────────────────────────────────────────────
    everyday_txns = parse_stgeorge_csv(EVERYDAY_CSV)
    spending      = analyse_spending(everyday_txns, days=7)

    lines.append("SPENDING (last 7 days):")

    tracked_cats = ["Food & dining", "Entertainment", "Shopping", "Sport & leisure", "Education", "Subscriptions"]
    for cat in tracked_cats:
        amt = spending["category_totals"].get(cat, 0)
        if amt > 0:
            lines.append(f"  {cat:<18} ${amt:.2f}")

    other = sum(v for k, v in spending["category_totals"].items()
                if k not in tracked_cats and k != "Other")
    if other > 0:
        lines.append(f"  {'Other':<18} ${other:.2f}")

    weekly_budget = 75.00
    over_budget = spending["total_spend"] > weekly_budget
    budget_str = f"  (⚠ over ~${weekly_budget:.0f}/week budget)" if over_budget else f"  (within ~${weekly_budget:.0f}/week budget)"
    lines.append(f"  {'Total spend':<18} ${spending['total_spend']:.2f}{budget_str}")

    # Flag big transactions
    if spending["big_transactions"]:
        lines.append("  Flagged transactions (>$50):")
        for t in spending["big_transactions"][:4]:  # max 4
            lines.append(f"    • {t['date'].strftime('%d %b')}  ${t['debit']:.2f}  {t['description'][:40]}")

    lines.append("")

    # ── Income tracking ───────────────────────────────────────────────────────
    income = analyse_income(everyday_txns, days=30)
    if income["total"] > 0:
        lines.append("INCOME (last 30 days):")
        for source, data in sorted(income["by_source"].items(), key=lambda x: -x[1]["total"]):
            if source not in ("Transfer In", "Other", "Refund") and data["total"] > 0:
                lines.append(f"  {source:<18} ${data['total']:.2f}")
        if "Transfer In" in income["by_source"]:
            ti = income["by_source"]["Transfer In"]["total"]
            if ti > 0:
                lines.append(f"  {'Transfers In':<18} ${ti:.2f}  (internal)")
        lines.append(f"  {'Total income':<18} ${income['total']:.2f}")
        import datetime as _dt, pytz as _ptz
        _tz    = _ptz.timezone(config.TIMEZONE)
        _today = _dt.datetime.now(_tz)
        _days_elapsed   = _today.day
        _days_in_month  = 30
        expected        = 2880.0
        prorated        = expected * (_days_elapsed / _days_in_month)
        if income["total"] < prorated * 0.75:
            lines.append(f"  ⚠ Behind pace — ${income['total']:.0f} earned, ~${prorated:.0f} expected by day {_days_elapsed}")
        else:
            lines.append(f"  ✓ On pace — ${income['total']:.0f} of ~${expected:.0f} expected this month")
        lines.append("")

    # ── Savings progress ──────────────────────────────────────────────────────
    savings = analyse_savings()

    if savings["total"] > 0:
        bar_filled = int(savings["pct"] / 5)  # 20 char bar
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        lines.append(f"SAVINGS GOAL ($35k — US exchange Jan 2027):")
        lines.append(f"  [{bar}] {savings['pct']:.1f}%")
        lines.append(f"  Current: ${savings['total']:,.2f}  |  Remaining: ${savings['remaining']:,.2f}")

        if savings["on_track"]:
            lines.append(f"  ✓ On track — projected to hit goal {savings['projected_date'].strftime('%B %Y')}")
        else:
            lines.append(f"  ✗ Behind — projected {savings['projected_date'].strftime('%B %Y')} (deadline: Jan 2027)")

        lines.append(f"  Saving ~$2,500/month (~$2,800 income - ~$300 spending)")
    else:
        lines.append("SAVINGS: Add savings CSV files to see progress.")

    # ── Monthly review prompt ─────────────────────────────────────────────────
    if is_monthly_review_day():
        lines.append("")
        lines.append("📅 MONTHLY REVIEW DAY — review last month's spending and update your budget.")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n💰  Finance summary\n")
    print(get_finance_summary())
    print()

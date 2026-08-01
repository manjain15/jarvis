"""
reselling_tracker.py — Jarvis reselling intelligence module
Reads from Google Sheets via gspread and computes P&L summary.

Sheet: manj → general tab
Columns (0-based, row 1 = headers skipped):
  B(1):STATUS  C(2):Category  D(3):Name  E(4):Purchase Date  F(5):Sale Date
  G(6):Cost($) H(7):Sale Price($) I(8):Platform J(9):Fees($) K(10):Net P/L  L(11):% Margin  M(12):Notes

Statuses: Sold, In Stock, Paid Pending Shipping, Deposit Paid
"""

import gspread
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from collections import defaultdict
import os

# ── Config ────────────────────────────────────────────────────────────────────

SPREADSHEET_NAME = "manj"
SHEET_TAB_NAME   = "general"
CREDENTIALS_FILE = os.path.expanduser("~/jarvis/credentials.json")

# 0-based column indices (gspread returns rows as lists, col A = index 0)
COL_STATUS        = 1   # B
COL_CATEGORY      = 2   # C
COL_NAME          = 3   # D
COL_PURCHASE_DATE = 4   # E
COL_SALE_DATE     = 5   # F
COL_COST          = 6   # G
COL_SALE_PRICE    = 7   # H
COL_PLATFORM      = 8   # I
COL_FEES          = 9   # J
COL_NOTES         = 12  # M

VALID_STATUSES = {"sold", "in stock", "paid pending shipping", "deposit paid"}
SKIP_CATEGORIES = {"membership"}  # Don't include these in P&L

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Item:
    status: str
    category: str
    name: str
    cost: float
    sale_price: Optional[float]
    fees: float = 0.0
    platform: Optional[str] = None
    purchase_date: Optional[datetime] = None
    sale_date: Optional[datetime] = None
    notes: str = ""

    @property
    def net_profit(self) -> Optional[float]:
        if self.sale_price is None:
            return None
        return self.sale_price - self.fees - self.cost

    @property
    def margin_pct(self) -> Optional[float]:
        if self.sale_price is None or self.cost == 0:
            return None
        return (self.net_profit / self.cost) * 100

    @property
    def is_sold(self) -> bool:
        return self.status == "sold"

    @property
    def is_in_stock(self) -> bool:
        return self.status == "in stock"

    @property
    def is_pending(self) -> bool:
        return self.status in ("paid pending shipping", "deposit paid")


# ── Sheet reading ─────────────────────────────────────────────────────────────

def _connect_sheet() -> gspread.Worksheet:
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google.auth.transport.requests import Request

    token_file = os.path.expanduser("~/jarvis/token.json")
    creds = OAuthCredentials.from_authorized_user_file(token_file)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tmp_file = token_file + ".tmp"
        with open(tmp_file, "w") as f:
            f.write(creds.to_json())
        os.replace(tmp_file, token_file)

    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)
    return sh.worksheet(SHEET_TAB_NAME)


def _safe_float(val: str) -> Optional[float]:
    """Parse a cell value to float, return None if empty/invalid."""
    if not val or str(val).strip() in ("", "-", "N/A", "#N/A", "#REF!"):
        return None
    try:
        return abs(float(str(val).replace("$", "").replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _safe_date(val: str) -> Optional[datetime]:
    if not val or str(val).strip() in ("", "-"):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_row(row: list) -> Optional[Item]:
    """Parse a sheet row into an Item. Returns None if row should be skipped."""

    def get(idx):
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    status_raw = get(COL_STATUS).lower()
    if status_raw not in VALID_STATUSES:
        return None

    name = get(COL_NAME)
    if not name or name == "-":
        return None

    category = get(COL_CATEGORY) or "Other"
    if category.lower() in SKIP_CATEGORIES:
        return None

    # Cost
    cost = _safe_float(get(COL_COST)) or 0.0

    sale_price = _safe_float(get(COL_SALE_PRICE))
    fees       = _safe_float(get(COL_FEES)) if COL_FEES is not None else 0.0
    fees       = fees or 0.0
    platform   = get(COL_PLATFORM) if COL_PLATFORM is not None else None
    p_date     = _safe_date(get(COL_PURCHASE_DATE)) if COL_PURCHASE_DATE is not None else None
    s_date     = _safe_date(get(COL_SALE_DATE)) if COL_SALE_DATE is not None else None
    notes      = get(COL_NOTES) if COL_NOTES is not None else ""

    return Item(
        status        = status_raw,
        category      = category,
        name          = name,
        cost          = cost,
        sale_price    = sale_price if status_raw == "sold" else None,
        fees          = fees,
        platform      = platform or None,
        purchase_date = p_date,
        sale_date     = s_date,
        notes         = notes,
    )


def load_items() -> list[Item]:
    ws   = _connect_sheet()
    rows = ws.get_all_values()
    items = []
    for row in rows[1:]:  # skip header
        item = _parse_row(row)
        if item is not None:
            items.append(item)
    return items


# ── Analytics ─────────────────────────────────────────────────────────────────

def compute_summary(items: list[Item]) -> dict:
    sold    = [i for i in items if i.is_sold]
    stock   = [i for i in items if i.is_in_stock]
    pending = [i for i in items if i.is_pending]

    total_revenue = sum(i.sale_price for i in sold if i.sale_price)
    total_cogs    = sum(i.cost for i in sold)
    total_fees    = sum(i.fees for i in sold)
    gross_pl      = total_revenue - total_cogs
    net_pl        = gross_pl - total_fees

    capital_stock   = sum(i.cost for i in stock)
    capital_pending = sum(i.cost for i in pending)

    # Per-category P&L (sold items only)
    by_cat: dict[str, dict] = defaultdict(lambda: {"profit": 0.0, "count": 0, "revenue": 0.0})
    for i in sold:
        cat = i.category or "Other"
        by_cat[cat]["profit"]  += i.net_profit or 0.0
        by_cat[cat]["revenue"] += i.sale_price or 0.0
        by_cat[cat]["count"]   += 1

    # Best and worst flips (by net profit)
    profitable = sorted([i for i in sold if i.net_profit is not None], key=lambda x: x.net_profit or 0)
    best  = profitable[-1] if profitable else None
    worst = profitable[0]  if profitable else None

    # Average margin on sold items
    margins = [i.margin_pct for i in sold if i.margin_pct is not None]
    avg_margin = sum(margins) / len(margins) if margins else 0.0

    return {
        "sold_count":       len(sold),
        "stock_count":      len(stock),
        "pending_count":    len(pending),
        "total_revenue":    total_revenue,
        "total_cogs":       total_cogs,
        "total_fees":       total_fees,
        "gross_pl":         gross_pl,
        "net_pl":           net_pl,
        "avg_margin_pct":   avg_margin,
        "capital_stock":    capital_stock,
        "capital_pending":  capital_pending,
        "by_category":      dict(by_cat),
        "best_flip":        best,
        "worst_flip":       worst,
        "stock_items":      stock,
        "pending_items":    pending,
    }


# ── Jarvis output ─────────────────────────────────────────────────────────────

def get_reselling_summary() -> str:
    """
    Returns a formatted multi-line string for inclusion in the Jarvis morning brief.
    Call this from morning_brief.py the same way pokemon_tracker.py was called.
    """
    try:
        items = load_items()
    except Exception as e:
        return f"  ⚠️  Reselling tracker error: {e}"

    if not items:
        return "  No reselling data found — check sheet name / credentials."

    s = compute_summary(items)

    pl_sign  = "+" if s["net_pl"] >= 0 else ""
    pl_emoji = "✓" if s["net_pl"] >= 0 else "✗"

    lines = [
        f"RESELLING  {pl_emoji}  Net P/L: {pl_sign}${s['net_pl']:,.2f}",
        f"  Revenue: ${s['total_revenue']:,.2f}  |  COGS: ${s['total_cogs']:,.2f}  |  Fees: ${s['total_fees']:,.2f}",
        f"  Sold: {s['sold_count']}  |  In stock: {s['stock_count']}  |  Pending shipping: {s['pending_count']}",
        f"  Avg margin on sold: {s['avg_margin_pct']:.1f}%",
    ]

    # Capital tied up
    if s["capital_stock"] > 0 or s["capital_pending"] > 0:
        lines.append(
            f"  Capital tied up: ${s['capital_stock']:,.0f} (inventory)  +  ${s['capital_pending']:,.0f} (pending shipping)"
        )

    # Per-category breakdown
    if s["by_category"]:
        lines.append("  By category:")
        for cat, data in sorted(s["by_category"].items(), key=lambda x: -x[1]["profit"]):
            sign = "+" if data["profit"] >= 0 else ""
            lines.append(f"    {cat:<12} {sign}${data['profit']:,.0f}  ({data['count']} sold)")

    # Best / worst flip
    if s["best_flip"]:
        b = s["best_flip"]
        lines.append(f"  Best flip:  {b.name}  (+${b.net_profit:,.2f}, {b.margin_pct:.0f}% margin)")
    if s["worst_flip"] and s["worst_flip"] != s["best_flip"]:
        w = s["worst_flip"]
        wp = w.net_profit or 0
        sign = "+" if wp >= 0 else ""
        lines.append(f"  Worst flip: {w.name}  ({sign}${wp:,.2f})")

    # Pending shipping items (named)
    if s["pending_items"]:
        names = ", ".join(i.name for i in s["pending_items"][:3])
        if len(s["pending_items"]) > 3:
            names += f" +{len(s['pending_items']) - 3} more"
        lines.append(f"  ⏳ Awaiting shipping: {names}")

    # Unsold stock (named)
    if s["stock_items"]:
        names = ", ".join(i.name for i in s["stock_items"][:3])
        if len(s["stock_items"]) > 3:
            names += f" +{len(s['stock_items']) - 3} more"
        lines.append(f"  📦 In stock: {names}")

    return "\n".join(lines)


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🃏  Reselling Tracker\n")
    print(get_reselling_summary())
    print()

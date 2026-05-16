"""
Jarvis — Pokemon Reselling Tracker
====================================
Reads your Pokemon/reselling Excel spreadsheet and surfaces
P&L, inventory value, and performance in the morning brief.

SETUP:
  Copy your Excel file to: jarvis/pokemon/inventory.xlsx
  (Create the pokemon/ folder first)

UPDATE:
  Replace inventory.xlsx with your latest version whenever you update it.
  Jarvis reads it fresh each time — no sync needed.

STRUCTURE EXPECTED:
  Sheet: 'general'
  Row 0: Headers (STATUS, Company, NAME, COST, SALE PRICE, PROFIT/LOSS, etc.)
  Row 1: Totals row
  Rows 2+: Individual items

STATUS values: Sold, In Stock, Paid Pending Shipping, Deposit Paid
"""

import datetime
from pathlib import Path

import pytz
import config

SCRIPT_DIR    = Path(__file__).parent
POKEMON_DIR   = SCRIPT_DIR / "pokemon"
INVENTORY_FILE = POKEMON_DIR / "inventory.xlsx"

TIMEZONE = pytz.timezone(config.TIMEZONE)


def load_inventory():
    """
    Loads the Pokemon inventory Excel file.
    Returns a dict with all key metrics, or None if file not found.
    """
    if not INVENTORY_FILE.exists():
        return None

    try:
        import pandas as pd
        import numpy as np

        df = pd.read_excel(INVENTORY_FILE, sheet_name="general", header=0)
        # Use row 0 as headers
        df.columns = df.iloc[0]
        df = df.iloc[1:].reset_index(drop=True)

        # Extract totals from the summary row (row 0 after header shift)
        total_cost  = pd.to_numeric(df.iloc[0].get("TOTAL COST",  0), errors="coerce") or 0
        total_sales = pd.to_numeric(df.iloc[0].get("TOTAL SALES", 0), errors="coerce") or 0
        total_pl    = pd.to_numeric(df.iloc[0].get("P/L",         0), errors="coerce") or 0

        # Filter to actual items
        statuses = ["Sold", "In Stock", "Deposit Paid", "Paid Pending Shipping"]
        items    = df[df["STATUS"].isin(statuses)].copy()

        def num(col):
            return pd.to_numeric(items[col], errors="coerce").fillna(0)

        items["cost_n"]   = num("COST")
        items["sale_n"]   = num("SALE PRICE")
        items["profit_n"] = num("PROFIT/LOSS")

        # Sold items
        sold         = items[items["STATUS"] == "Sold"]
        sold_count   = len(sold)
        sold_revenue = sold["sale_n"].sum()
        sold_cost    = sold["cost_n"].sum()  # negative in your sheet
        sold_profit  = sold["profit_n"].sum()

        # Best and worst flips
        best_flip  = sold.loc[sold["profit_n"].idxmax()] if len(sold) > 0 else None
        worst_flip = sold.loc[sold["profit_n"].idxmin()] if len(sold) > 0 else None

        # Unsold inventory
        unsold_statuses = ["In Stock", "Deposit Paid", "Paid Pending Shipping"]
        unsold          = items[items["STATUS"].isin(unsold_statuses)]
        unsold_count    = len(unsold)
        unsold_cost     = abs(unsold["cost_n"].sum())  # make positive

        # Average margin on sold items
        avg_margin = (sold_profit / abs(sold_cost) * 100) if sold_cost != 0 else 0

        # Win rate
        wins     = len(sold[sold["profit_n"] > 0])
        win_rate = (wins / sold_count * 100) if sold_count > 0 else 0

        return {
            "total_cost":    abs(total_cost),
            "total_sales":   total_sales,
            "total_pl":      total_pl,
            "sold_count":    sold_count,
            "sold_revenue":  sold_revenue,
            "sold_profit":   sold_profit,
            "avg_margin":    avg_margin,
            "win_rate":      win_rate,
            "unsold_count":  unsold_count,
            "unsold_cost":   unsold_cost,
            "best_flip":     {
                "name":   str(best_flip["NAME"])[:35] if best_flip is not None else "",
                "profit": float(best_flip["profit_n"]) if best_flip is not None else 0,
            },
            "worst_flip": {
                "name":   str(worst_flip["NAME"])[:35] if worst_flip is not None else "",
                "profit": float(worst_flip["profit_n"]) if worst_flip is not None else 0,
            },
            "items": items[["STATUS","Company","NAME","cost_n","sale_n","profit_n"]].to_dict("records"),
        }

    except Exception as e:
        print(f"⚠️  Pokemon tracker error: {e}")
        return None


def get_pokemon_summary():
    """
    Returns a formatted summary string for the morning brief.
    """
    inv = load_inventory()

    if inv is None:
        return (
            "POKEMON RESELLING: No inventory file found.\n"
            f"  → Copy your Excel to: jarvis/pokemon/inventory.xlsx"
        )

    pl     = inv["total_pl"]
    status = "profitable ✓" if pl > 0 else f"loss of ${abs(pl):.2f} ✗"

    lines = ["POKEMON RESELLING:"]
    lines.append(f"  Overall P/L:     ${pl:+.2f}  ({status})")
    lines.append(f"  Total invested:  ${inv['total_cost']:.2f}")
    lines.append(f"  Total revenue:   ${inv['total_sales']:.2f}")
    lines.append(f"  Items sold:      {inv['sold_count']}  (win rate: {inv['win_rate']:.0f}%)")
    lines.append(f"  Avg margin:      {inv['avg_margin']:.1f}%")
    lines.append(f"  Inventory held:  {inv['unsold_count']} items (${inv['unsold_cost']:.2f} tied up)")

    if inv["best_flip"]["profit"] > 0:
        lines.append(f"  Best flip:       {inv['best_flip']['name']} (+${inv['best_flip']['profit']:.2f})")
    if inv["worst_flip"]["profit"] < 0:
        lines.append(f"  Worst flip:      {inv['worst_flip']['name']} (-${abs(inv['worst_flip']['profit']):.2f})")

    # Advice based on numbers
    if inv["unsold_cost"] > 500:
        lines.append(f"\n  ⚠️  ${inv['unsold_cost']:.0f} tied up in unsold inventory — prioritise selling.")
    if inv["total_pl"] < 0:
        lines.append(f"  ⚠️  Still in the red — need ${abs(inv['total_pl']):.2f} more profit to break even.")
    if inv["avg_margin"] > 20:
        lines.append(f"  ✓  Strong {inv['avg_margin']:.0f}% avg margin on sold items.")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🃏  Pokemon Reselling Tracker\n")

    inv = load_inventory()
    if inv is None:
        print(f"❌  No inventory file found.")
        print(f"   Create folder: jarvis/pokemon/")
        print(f"   Copy your Excel to: jarvis/pokemon/inventory.xlsx\n")
    else:
        print(get_pokemon_summary())
        print()

"""Sold Membership rows count as reselling costs.

On 17 Oct 2026 the sheet's net P/L was $1,903.37 (46 sold, COGS $8,711.09,
revenue $10,614.46). Skipping Membership left $2,743.37 (39 sold, COGS
$7,871.09). The $840 gap is those seven sold rows.
"""

import sys
from types import SimpleNamespace

# reselling_tracker imports gspread at load time. These tests only parse
# rows and summarise items, so a stub is enough when gspread is absent.
sys.modules.setdefault("gspread", SimpleNamespace(Worksheet=object))

import pytest

import reselling_tracker
from reselling_tracker import Item, _is_flip, _parse_row, compute_summary


def _row(status, category, name, cost, sale_price="", fees=""):
    """Build a sheet row. Column A is unused; status starts at index 1."""
    row = [""] * 15
    row[1] = status
    row[2] = category
    row[3] = name
    row[8] = str(cost)
    row[9] = str(sale_price)
    row[11] = str(fees)
    return row


def test_sold_membership_row_is_parsed():
    item = _parse_row(_row("Sold", "Membership", "Bot sub", "$120.00", "$0.00"))
    assert item is not None
    assert item.status == "sold"
    assert item.category == "Membership"
    assert item.cost == 120.0
    assert item.sale_price == 0.0
    assert item.margin_pct == pytest.approx(-100.0)
    assert _is_flip(item) is False


def test_blank_category_falls_back_to_other():
    blank = _parse_row(_row("Sold", "", "Loose card", "10", "15"))
    spaces = _parse_row(_row("Sold", "   ", "Another card", "10", "15"))
    assert blank.category == "Other"
    assert spaces.category == "Other"


def test_in_stock_membership_stays_in_inventory():
    item = _parse_row(_row("In Stock", "Membership", "Store club", "40"))
    assert item is not None
    assert item.sale_price is None
    summary = compute_summary([item])
    assert summary["sold_count"] == 0
    assert summary["stock_count"] == 1
    assert summary["capital_stock"] == 40
    assert summary["by_category"] == {}


def test_zero_sale_costs_stay_out_of_flip_stats():
    good = Item(
        status="sold", category="Cards", name="Good flip",
        cost=10, sale_price=40,
    )
    bad = Item(
        status="sold", category="Cards", name="Bad flip",
        cost=40, sale_price=25,
    )
    sub = Item(
        status="sold", category="Membership", name="Bot sub",
        cost=500, sale_price=0,
    )
    summary = compute_summary([good, bad, sub])

    assert summary["sold_count"] == 3
    assert summary["total_revenue"] == 65
    assert summary["total_cogs"] == 550
    assert summary["net_pl"] == 65 - 550
    assert summary["by_category"]["Membership"] == {
        "profit": -500.0, "count": 1, "revenue": 0.0,
    }
    assert summary["best_flip"].name == "Good flip"
    assert summary["worst_flip"].name == "Bad flip"
    # Good margin 300%, bad margin -37.5%. The -100% membership is excluded.
    assert summary["avg_margin_pct"] == pytest.approx((300.0 + -37.5) / 2)


def test_seven_memberships_close_the_oct_2026_gap():
    """Including $840 of membership COGS moves the old Jarvis totals to the sheet."""
    items = [
        Item(
            status="sold", category="Cards", name=f"Flip {i}",
            cost=200.0, sale_price=270.0,
        )
        for i in range(38)
    ]
    items.append(Item(
        status="sold", category="Cards", name="Remainder",
        cost=271.09, sale_price=354.46,
    ))
    membership_costs = [100.0] * 6 + [240.0]
    items.extend(
        Item(
            status="sold", category="Membership", name=f"Sub {i}",
            cost=cost, sale_price=0.0,
        )
        for i, cost in enumerate(membership_costs)
    )

    summary = compute_summary(items)

    assert summary["sold_count"] == 46
    assert round(summary["total_revenue"], 2) == 10614.46
    assert round(summary["total_cogs"], 2) == 8711.09
    assert round(summary["net_pl"], 2) == 1903.37
    assert summary["by_category"]["Membership"]["count"] == 7
    assert summary["by_category"]["Membership"]["profit"] == pytest.approx(-840.0)
    assert summary["by_category"]["Membership"]["revenue"] == 0.0
    assert summary["best_flip"].category == "Cards"
    assert summary["worst_flip"].category == "Cards"
    assert summary["avg_margin_pct"] > 0


def test_summary_text_lists_membership_without_calling_it_a_flip(monkeypatch):
    monkeypatch.setattr(reselling_tracker, "load_items", lambda: [
        Item(status="sold", category="Cards", name="Good flip", cost=10, sale_price=40),
        Item(status="sold", category="Cards", name="Bad flip", cost=40, sale_price=25),
        Item(status="sold", category="Membership", name="Bot sub", cost=500, sale_price=0),
    ])
    text = reselling_tracker.get_reselling_summary()
    assert "Membership" in text
    assert "Best flip:  Good flip" in text
    assert "Worst flip: Bad flip" in text
    assert "Bot sub" not in text
    assert "Avg margin on flips:" in text

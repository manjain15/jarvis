"""Own-account transfers are not personal spending.

The 22–28 Sep 2026 everyday-account week is the fixture: Osko withdrawals
paid to Manav Jain and Revolut**5228 card top-ups must drop out of totals,
categories, flagged debits, and the subscription audit. A real Osko payment
to someone else (Knockout R Jaiswal) must still count. No bank exports.
"""

import csv
import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import finance_tracker
import subscription_audit
import term_context

WEEK_START = datetime.date(2026, 9, 22)
WEEK_END = datetime.date(2026, 9, 28)

# Genuine personal spend that week: 120+120+25+14.95+3.60+6.90+4.80+10.
GENUINE_TOTAL = 305.25


@pytest.fixture(autouse=True)
def _isolated_term_context(tmp_path, monkeypatch):
    """Defaults apply unless a test writes own_accounts. Never read a real file."""
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")


def _write_stgeorge(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Description", "Debit", "Credit", "Balance"])
        for row in rows:
            date, description, debit, credit = row
            writer.writerow([date, description, debit, credit, "1000.00"])


def _week_rows():
    """Everyday CSV rows for 22–28 Sep 2026, including padded self-transfers."""
    return [
        ("22/09/2026", "Osko Withdrawal 22Sep Knockout R Jaiswal", "120.00", ""),
        ("23/09/2026", "Osko Withdrawal 23Sep Knockout R Jaiswal", "120.00", ""),
        ("24/09/2026", "Osko Withdrawal 24Sep Doomsday Tix Abhishek Goyal", "25.00", ""),
        ("24/09/2026", "Visa Purchase 24Sep Playstation London", "14.95", ""),
        ("24/09/2026", "Visa Purchase 24Sep Paypal *Guzmanygome", "3.60", ""),
        ("22/09/2026", "Visa Purchase 22Sep Tfnsw Opal", "6.90", ""),
        ("23/09/2026", "Visa Purchase 23Sep Tfnsw Opal", "4.80", ""),
        ("25/09/2026", "Osko Withdrawal 25Sep Change For Hot Wheels W Tynan", "10.00", ""),
        ("24/09/2026", "Osko Withdrawal  24Sep12:03   Kmart 30Th   Manav Jain", "5000.00", ""),
        ("24/09/2026", "Osko Withdrawal 24Sep12:12 Test Manav Jain", "1.00", ""),
        ("27/09/2026", "OSKO WITHDRAWAL 27Sep20:42 Op 17 Blisters MANAV JAIN", "54.00", ""),
        ("24/09/2026", "Visa Purchase 24Sep Revolut**5228* Melbourne", "6000.00", ""),
        ("22/09/2026", "Visa Purchase 22Sep Revolut**5228* Melbourne", "390.00", ""),
        ("22/09/2026", "Visa Purchase 22Sep Revolut**5228* Melbourne", "135.00", ""),
        ("24/09/2026", "Sct Deposit 24Sep Sent From Revolut Manav Jain", "", "1.00"),
        ("26/09/2026", "Sct Withdrawal 26Sep Float Manav Jain", "20.00", ""),
        ("22/09/2026", "Internet Withdrawal To 0000206850220", "500.00", ""),
        ("22/09/2026", "Transfer to savings", "40.00", ""),
    ]


def _parsed_week(tmp_path):
    path = tmp_path / "everyday.csv"
    _write_stgeorge(path, _week_rows())
    return finance_tracker.parse_stgeorge_csv(path)


def test_self_transfer_is_not_a_spending_category():
    """'ko ' inside 'osko ' used to mark every Osko row Entertainment.

    Kmart is a shopping keyword, but a withdrawal paid to Manav Jain is a
    transfer and must not reach those rules. Knockout paid to someone else
    still matches Entertainment via 'knockout', not via the 'osko ' token.
    """
    kmart = "Osko Withdrawal  24Sep12:03   Kmart 30Th   Manav Jain"
    assert finance_tracker.categorise(kmart) == "Internal transfer"
    assert finance_tracker.categorise(kmart) != "Entertainment"
    assert finance_tracker.categorise(
        "Osko Withdrawal 24Sep12:12 Test Manav Jain"
    ) == "Internal transfer"
    assert finance_tracker.categorise(
        "Visa Purchase 24Sep Revolut**5228* Melbourne"
    ) == "Internal transfer"
    assert finance_tracker.categorise(
        "Visa Purchase Revolut Melbourne"
    ) == "Internal transfer"

    knockout = "Osko Withdrawal 22Sep Knockout R Jaiswal"
    assert finance_tracker.categorise(knockout) == "Entertainment"
    assert finance_tracker._is_internal_spend(knockout) is False

    # 'ko ' no longer matches inside 'osko ', so this is not Entertainment.
    hot_wheels = "Osko Withdrawal 25Sep Change For Hot Wheels W Tynan"
    assert finance_tracker.categorise(hot_wheels) == "Other"
    assert finance_tracker.categorise("Visa Purchase Kmart Broadway") == "Shopping"
    assert finance_tracker.categorise("Visa Purchase 22Sep Tfnsw Opal") == "Transport"


def test_internal_rules_cover_owner_payee_and_existing_markers():
    assert finance_tracker._is_internal_spend(
        "Sct Withdrawal 26Sep Float Manav Jain"
    ) is True
    assert finance_tracker._is_internal_spend(
        "Internet Withdrawal To 0000206850220"
    ) is True
    assert finance_tracker._is_internal_spend("Transfer to savings") is True
    assert finance_tracker._is_internal_spend(
        "Osko Withdrawal 22Sep Revolut Friend Jane Doe"
    ) is False
    assert finance_tracker._is_internal_spend(
        "Sct Withdrawal 26Sep Landlord Pty Ltd"
    ) is False
    assert finance_tracker._is_internal_spend("Revolut**5228* Melbourne") is True


def test_owner_name_comes_from_term_context(tmp_path):
    (tmp_path / "term_context.json").write_text(json.dumps({
        "own_accounts": {"owner_names": ["ada lovelace"]},
    }))
    assert finance_tracker._is_internal_spend(
        "Osko Withdrawal 1Sep Ada Lovelace"
    ) is True
    assert finance_tracker._is_internal_spend(
        "Osko Withdrawal 1Sep Manav Jain"
    ) is False
    # Card patterns still fall back when that field is omitted.
    assert finance_tracker._is_internal_spend(
        "Visa Purchase 24Sep Revolut**5228* Melbourne"
    ) is True


def test_week_of_22_sep_2026_counts_only_genuine_spend(tmp_path):
    txns = _parsed_week(tmp_path)
    summary = finance_tracker.summarise_spending(txns, WEEK_START, WEEK_END, 75)

    assert summary["total_spend"] == GENUINE_TOTAL
    assert summary["by_category"] == {
        "Entertainment": 254.95,
        "Other": 38.60,
        "Transport": 11.70,
    }
    assert "Internal transfer" not in summary["by_category"]
    assert summary["transaction_count"] == 8
    assert summary["over_budget"] is True
    assert [row["amount"] for row in summary["flagged"]] == [120.0, 120.0]
    assert all("knockout" in row["description"].lower() for row in summary["flagged"])
    assert all("manav" not in row["description"].lower() for row in summary["flagged"])
    assert all("revolut" not in row["description"].lower() for row in summary["flagged"])

    by_desc = {t["description"]: t for t in txns}
    assert by_desc["Osko Withdrawal 24Sep12:03 Kmart 30Th Manav Jain"]["category"] == (
        "Internal transfer"
    )
    assert by_desc["Osko Withdrawal 22Sep Knockout R Jaiswal"]["category"] == "Entertainment"


def test_analyse_spending_matches_the_finance_route_for_that_week(tmp_path, monkeypatch):
    """Morning brief and weekly review both call analyse_spending."""
    txns = _parsed_week(tmp_path)

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime.datetime(2026, 9, 28, 8, 0, 0)
            if tz is None:
                return naive
            return tz.localize(naive)

    monkeypatch.setattr(finance_tracker.datetime, "datetime", FrozenDateTime)
    spending = finance_tracker.analyse_spending(txns, days=7)

    assert round(spending["total_spend"], 2) == GENUINE_TOTAL
    assert round(spending["category_totals"]["Entertainment"], 2) == 254.95
    assert round(spending["category_totals"]["Transport"], 2) == 11.70
    assert round(spending["category_totals"].get("Other", 0), 2) == 38.60
    assert "Internal transfer" not in spending["category_totals"]
    assert sorted(t["debit"] for t in spending["big_transactions"]) == [120.0, 120.0]
    assert spending["transaction_count"] == 8


def test_subscription_audit_skips_self_transfers_and_keeps_real_merchants(tmp_path, monkeypatch):
    everyday = tmp_path / "everyday.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    today = datetime.datetime.now(subscription_audit.TIMEZONE).date()
    previous = today - datetime.timedelta(days=32)

    def row(day, description, debit):
        return (day.strftime("%d/%m/%Y"), description, f"{debit:.2f}", "")

    _write_stgeorge(everyday, [
        row(today, "Visa Purchase SPOTIFY P1234", 11.99),
        row(previous, "Visa Purchase SPOTIFY P1234", 11.99),
        row(today, "Osko Withdrawal Knockout R Jaiswal", 120),
        row(previous, "Osko Withdrawal Knockout R Jaiswal", 120),
        row(today, "Visa Purchase Revolut**5228* Melbourne", 6000),
        row(previous, "Visa Purchase Revolut**5228* Melbourne", 390),
        row(today, "Osko Withdrawal Kmart Manav Jain", 5000),
        row(previous, "Osko Withdrawal Test Manav Jain", 54),
    ])

    recurring = subscription_audit.find_recurring(
        finance_tracker.parse_stgeorge_csv(everyday), months=3,
    )
    merchants = [item["merchant"] for item in recurring]
    blob = " ".join(merchants)
    assert "revolut" not in blob
    assert "manav" not in blob
    assert any("knockout" in merchant for merchant in merchants)
    assert any("spotify" in merchant for merchant in merchants)

    summary = subscription_audit.summarise_subscriptions(months=3)
    assert summary["monthly_known"] == 11.99
    assert all("revolut" not in item["merchant"] for item in summary["review"])
    assert summary["monthly_review"] == 120.0


def test_reselling_still_ignores_own_topups_and_counts_real_cashflow(tmp_path, monkeypatch):
    path = tmp_path / "revolut.csv"
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", path)
    monkeypatch.setattr(finance_tracker, "INVESTING_CSV", tmp_path / "investing.csv")
    stamp = datetime.datetime.now(finance_tracker.TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerow(["COMPLETED", "TOPUP", stamp, "Topup", "6000", "0", "AUD", "6000"])
        writer.writerow(["COMPLETED", "CARD_PAYMENT", stamp, "EB Games", "-40", "0", "AUD", "5960"])
        writer.writerow(["COMPLETED", "TRANSFER", stamp, "From Manav Jain", "100", "0", "AUD", "6060"])
        writer.writerow(["COMPLETED", "TRANSFER", stamp, "Buyer payout", "80", "0", "AUD", "6140"])
        writer.writerow(["COMPLETED", "TRANSFER", stamp, "Supplier invoice", "-30", "0", "AUD", "6110"])

    cash = finance_tracker.analyse_reselling(days=30)
    assert cash["source"] == "revolut"
    assert cash["available"] is True
    assert cash["deployed"] == 70.0
    assert cash["returned"] == 80.0
    assert cash["net"] == 10.0


def test_morning_brief_week_lists_genuine_categories_only(tmp_path, monkeypatch):
    everyday = tmp_path / "everyday.csv"
    _write_stgeorge(everyday, _week_rows())
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    monkeypatch.setattr(finance_tracker, "SAVINGS1_CSV", tmp_path / "savings1.csv")
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", tmp_path / "revolut.csv")
    monkeypatch.setattr(finance_tracker, "INVESTING_CSV", tmp_path / "investing.csv")
    monkeypatch.setattr(finance_tracker, "LIVE_SPEND_AVAILABLE", False)

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime.datetime(2026, 9, 28, 8, 0, 0)
            if tz is None:
                return naive
            return tz.localize(naive)

    monkeypatch.setattr(finance_tracker.datetime, "datetime", FrozenDateTime)
    text = finance_tracker.get_finance_summary()

    def line_amount(label):
        for line in text.splitlines():
            if line.strip().startswith(label):
                return line
        return ""

    assert "$254.95" in line_amount("Entertainment")
    assert "$11.70" in line_amount("Transport")
    assert "$38.60" in line_amount("Other")
    assert "$305.25" in line_amount("Total spend")
    assert "5000" not in text
    assert "6000" not in text
    assert "Revolut" not in text
    assert "Manav Jain" not in text
    assert "Knockout" in text


def _refund_week_rows():
    """22–28 Sep 2026, with the first Knockout ticket refunded the next morning."""
    rows = []
    for date, description, debit, credit in _week_rows():
        if description == "Osko Withdrawal 22Sep Knockout R Jaiswal":
            rows.append((
                "22/09/2026",
                "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal",
                "120.00",
                "",
            ))
            rows.append((
                "23/09/2026",
                "Sct Deposit 23Sep09:11 Rishi Jaiswal",
                "",
                "120.00",
            ))
        elif description == "Osko Withdrawal 23Sep Knockout R Jaiswal":
            rows.append((
                "23/09/2026",
                "Osko Withdrawal 23Sep10:30 Knockout R Jaiswal",
                "120.00",
                "",
            ))
        else:
            rows.append((date, description, debit, credit))
    rows.extend([
        ("24/09/2026", "Osko Deposit 24Sep Pokemon Gemma Johnston", "", "390.00"),
        ("24/09/2026", "Osko Deposit 24Sep Bank Carlos Santos", "", "135.00"),
        ("25/09/2026", "Osko Deposit 25Sep Bank Carlos Santos", "", "54.00"),
        ("26/09/2026", "Sct Deposit 26Sep Advance", "", "500.00"),
        ("27/09/2026", "Osko Deposit 27Sep Nilesh Banga", "", "17000.00"),
        ("24/09/2026", "Sct Deposit 24Sep Sent From Revolut Manav Jain Extra", "", "120.00"),
    ])
    return rows


def test_refunded_knockout_brings_the_week_to_185(tmp_path, monkeypatch):
    path = tmp_path / "everyday.csv"
    _write_stgeorge(path, _refund_week_rows())
    txns = finance_tracker.parse_stgeorge_csv(path)
    summary = finance_tracker.summarise_spending(txns, WEEK_START, WEEK_END, 75)

    assert summary["total_spend"] == 185.25
    assert summary["by_category"]["Entertainment"] == 134.95
    assert summary["by_category"]["Transport"] == 11.70
    assert summary["by_category"]["Other"] == 38.60
    assert [row["amount"] for row in summary["flagged"]] == [120.0]
    assert "jaiswal" in summary["flagged"][0]["description"].lower()
    assert summary["transaction_count"] == 7

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime.datetime(2026, 9, 28, 8, 0, 0)
            if tz is None:
                return naive
            return tz.localize(naive)

    monkeypatch.setattr(finance_tracker.datetime, "datetime", FrozenDateTime)
    spending = finance_tracker.analyse_spending(txns, days=7)
    assert round(spending["total_spend"], 2) == 185.25
    assert round(spending["category_totals"]["Entertainment"], 2) == 134.95
    assert [t["debit"] for t in spending["big_transactions"]] == [120.0]
    knockouts = [
        t for t in spending["transactions"]
        if "knockout" in t["description"].lower()
    ]
    assert len(knockouts) == 1
    assert "10:30" in knockouts[0]["description"]


def _txn(day, description, debit=0, credit=0):
    return {
        "date": day,
        "description": description,
        "debit": float(debit or 0),
        "credit": float(credit or 0),
        "balance": 0.0,
        "category": finance_tracker.categorise(description),
    }


def test_refund_matching_is_conservative():
    """Same amount is not enough. Income, friends, and a different Jaiswal stay."""
    day = datetime.date(2026, 9, 22)
    later = datetime.date(2026, 9, 23)
    txns = [
        _txn(day, "Visa Purchase 22Sep Woolworths Sydney", debit=390),
        _txn(later, "Osko Deposit 23Sep Pokemon Gemma Johnston", credit=390),
        _txn(day, "Visa Purchase 22Sep Coles Sydney", debit=135),
        _txn(later, "Osko Deposit 23Sep Bank Carlos Santos", credit=135),
        _txn(day, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", debit=54),
        _txn(later, "Osko Deposit 23Sep09:11 Bank Carlos Santos", credit=54),
        _txn(day, "Visa Purchase 22Sep Kmart Broadway", debit=80),
        _txn(later, "Sct Deposit 23Sep Advance", credit=80),
        _txn(day, "Osko Withdrawal 22Sep Nilesh Banga", debit=100),
        _txn(later, "Osko Deposit 23Sep Nilesh Banga", credit=100),
        _txn(day, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", debit=120),
        _txn(later, "Sct Deposit 23Sep09:11 Priya Jaiswal", credit=120),
        _txn(day, "Visa Purchase 22Sep Woolworths Sydney", debit=40),
        _txn(later, "Visa Refund 23Sep Woolworths Sydney", credit=15),
    ]
    summary = finance_tracker.summarise_spending(
        txns, day, datetime.date(2026, 9, 28), 75,
    )
    # The $15 Visa refund reduces a Woolworths debit. The other credits do not.
    assert summary["total_spend"] == 904.0
    assert summary["by_category"]["Food & dining"] == 550.0  # 390 + 135 + 40 - 15
    assert summary["by_category"]["Entertainment"] == 174.0  # 54 + 120, Priya is not a refund
    assert summary["by_category"]["Shopping"] == 80.0


def test_one_debit_takes_one_refund_only():
    day = datetime.date(2026, 9, 22)
    txns = [
        _txn(day, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", debit=120),
        _txn(datetime.date(2026, 9, 23), "Sct Deposit 23Sep09:11 Rishi Jaiswal", credit=120),
        _txn(datetime.date(2026, 9, 24), "Sct Deposit 24Sep10:00 Rishi Jaiswal", credit=120),
        _txn(datetime.date(2026, 9, 22), "Visa Purchase 22Sep Woolworths Sydney", debit=120),
    ]
    summary = finance_tracker.summarise_spending(
        txns, day, datetime.date(2026, 9, 28), 75,
    )
    assert summary["total_spend"] == 120.0
    assert summary["by_category"] == {"Food & dining": 120.0}
    assert summary["flagged"][0]["category"] == "Food & dining"


def test_refund_window_is_configurable(tmp_path):
    day = datetime.date(2026, 9, 1)
    txns = [
        _txn(day, "Osko Withdrawal 01Sep18:35 Knockout R Jaiswal", debit=120),
        _txn(datetime.date(2026, 9, 10), "Sct Deposit 10Sep09:11 Rishi Jaiswal", credit=120),
    ]
    inside = finance_tracker.summarise_spending(txns, day, datetime.date(2026, 9, 20), 75)
    assert inside["total_spend"] == 0.0

    (tmp_path / "term_context.json").write_text(json.dumps({
        "spending": {"refund_window_days": 1},
    }))
    outside = finance_tracker.summarise_spending(txns, day, datetime.date(2026, 9, 20), 75)
    assert outside["total_spend"] == 120.0


def test_savings_balance_is_unchanged_by_the_spend_filter(tmp_path, monkeypatch):
    savings = tmp_path / "savings1.csv"
    monkeypatch.setattr(finance_tracker, "SAVINGS1_CSV", savings)
    _write_stgeorge(savings, [
        ("28/09/2026", "Interest", "", "1.25"),
    ])
    result = finance_tracker.analyse_savings()
    assert result["total"] == 1000.0
    assert result["goal"] == 35000.0

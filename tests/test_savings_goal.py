"""The savings goal counts every fund he holds, not only the savings account."""

import csv
import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import finance_tracker
from reselling_tracker import Item, paid_capital
from test_finance_routes import AUTH, _client, _dmy, _patch_files, _write_csv

OCT17 = datetime.date(2026, 10, 17)
OCT17_CAPITAL = {"stock": 2015, "pending_shipping": 1261.98, "deposits_paid": 400}


def _write_revolut(path, balance, stamp="2026-10-17 12:00:00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "State", "Type", "Completed Date", "Description",
            "Amount", "Fee", "Currency", "Balance",
        ])
        writer.writerow([
            "COMPLETED", "TOPUP", stamp, "Balance", balance, "0", "AUD", balance,
        ])


def _savings_row(balance):
    return [[_dmy(OCT17), "Interest", "", "1.00", balance]]


def test_paid_capital_counts_deposits_paid_not_full_cost():
    deposit = Item(
        status="deposit paid", category="Cards", name="Case",
        cost=900, sale_price=None, deposit_paid=400, deposit_owing=500,
    )
    assert deposit.capital_committed == 900.0
    shipping = Item(
        status="paid pending shipping", category="Cards", name="Etb",
        cost=1261.98, sale_price=None, deposit_paid=1261.98,
    )
    stock = Item(status="in stock", category="Cards", name="Box", cost=2015, sale_price=None)
    sold = Item(status="sold", category="Cards", name="Gone", cost=50, sale_price=80)
    assert paid_capital([stock, shipping, deposit, sold]) == {
        "stock": 2015.0,
        "pending_shipping": 1261.98,
        "deposits_paid": 400.0,
        "total": 3676.98,
    }


def test_paid_capital_includes_unsold_membership():
    """Membership is a real cost. Unsold rows count; a sold subscription does not."""
    held = Item(
        status="in stock", category="Membership", name="Store club",
        cost=40, sale_price=None,
    )
    deposit = Item(
        status="deposit paid", category="Membership", name="Fee",
        cost=100, sale_price=None, deposit_paid=30, deposit_owing=70,
    )
    sold = Item(
        status="sold", category="Membership", name="Bot sub",
        cost=120, sale_price=0,
    )
    assert deposit.capital_committed == 100.0
    assert paid_capital([held, deposit, sold]) == {
        "stock": 40.0,
        "pending_shipping": 0.0,
        "deposits_paid": 30.0,
        "total": 70.0,
    }


def test_friend_share_defaults_to_zero_and_parses_dollars(monkeypatch):
    monkeypatch.delenv("JARVIS_REVOLUT_FRIEND_SHARE", raising=False)
    assert finance_tracker.revolut_friend_share() == 0.0
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "nope")
    assert finance_tracker.revolut_friend_share() == 0.0
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "-5")
    assert finance_tracker.revolut_friend_share() == 0.0
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "$2,000")
    assert finance_tracker.revolut_friend_share() == 2000.0


def test_oct17_total_excludes_everyday_and_unpaid_deposits(tmp_path, monkeypatch):
    """17 Oct 2026 figures: cash 18511.19 + capital paid 3676.98 = 22188.17."""
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "2000")
    monkeypatch.setattr(finance_tracker, "load_paid_capital", lambda: dict(OCT17_CAPITAL))
    _write_csv(savings, _savings_row("8233.35"))
    _write_revolut(tmp_path / "revolut.csv", "12277.84")

    res = _client().get("/finance/savings", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    breakdown = body["breakdown"]
    assert breakdown["savings"] == 8233.35
    assert breakdown["revolut_balance"] == 12277.84
    assert breakdown["revolut_source"] == "revolut"
    assert breakdown["friend_share"] == 2000
    assert breakdown["friend_share_applied"] is True
    assert breakdown["revolut_own"] == 10277.84
    assert breakdown["everyday"] is None
    assert breakdown["stock"] == 2015
    assert breakdown["pending_shipping"] == 1261.98
    assert breakdown["deposits_paid"] == 400
    assert breakdown["cash"] == 18511.19
    assert breakdown["capital"] == 3676.98
    assert breakdown["total"] == 22188.17
    assert body["balance"] == 22188.17
    assert body["remaining"] == 12811.83
    assert body["pct"] == 63.4
    assert body["available"] is True
    assert isinstance(body["on_track"], bool)
    assert body["projected_date"]
    assert "savings1" not in res.get_data(as_text=True)

    text = "\n".join(finance_tracker.savings_breakdown_lines(breakdown))
    assert "Cash $18,511.19" in text
    assert "savings $8,233.35" in text
    assert "Revolut own $10,277.84" in text
    assert "less $2,000.00 held for a friend" in text
    assert "Tied up $3,676.98" in text
    assert "stock $2,015.00" in text
    assert "pending shipping $1,261.98" in text
    assert "deposits paid $400.00" in text
    assert "everyday" not in text


def test_unset_friend_share_counts_the_whole_revolut_balance(tmp_path, monkeypatch):
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    monkeypatch.setattr(finance_tracker, "load_paid_capital", lambda: dict(OCT17_CAPITAL))
    _write_csv(savings, _savings_row("8233.35"))
    _write_revolut(tmp_path / "revolut.csv", "12277.84")
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["friend_share"] == 0
    assert breakdown["revolut_own"] == 12277.84
    assert breakdown["cash"] == 20511.19
    assert breakdown["total"] == 24188.17


def test_projection_and_brief_use_the_combined_total(tmp_path, monkeypatch):
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "2000")
    _write_csv(savings, _savings_row("8233.35"))
    _write_revolut(tmp_path / "revolut.csv", "12277.84")
    everyday.write_text("Date,Description,Debit,Credit,Balance\n", encoding="utf-8")

    cash_only = finance_tracker.analyse_savings()
    monkeypatch.setattr(finance_tracker, "load_paid_capital", lambda: dict(OCT17_CAPITAL))
    full = finance_tracker.analyse_savings()
    assert cash_only["breakdown"]["capital"] is None
    assert cash_only["breakdown"]["total"] == 18511.19
    assert full["remaining"] == 12811.83
    assert full["remaining"] < cash_only["remaining"]
    assert full["projected_date"] < cash_only["projected_date"]
    assert round(full["pct"], 1) == 63.4

    monkeypatch.setattr(finance_tracker, "LIVE_SPEND_AVAILABLE", False)
    summary = finance_tracker.get_finance_summary()
    assert "Total: $22,188.17" in summary
    assert "Cash $18,511.19" in summary
    assert "Tied up $3,676.98" in summary
    assert "everyday" not in summary


def test_everyday_balance_is_counted_when_the_csv_exists(tmp_path, monkeypatch):
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write_csv(savings, _savings_row("100.00"))
    _write_csv(everyday, [[_dmy(OCT17), "Woolworths", "5.00", "", "42.50"]])
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] == 42.5
    assert breakdown["cash"] == 142.5
    assert breakdown["total"] == 142.5


def test_missing_everyday_is_skipped(tmp_path, monkeypatch):
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write_csv(savings, _savings_row("100.00"))
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["everyday"] is None
    assert breakdown["total"] == 100


def test_friend_share_is_not_taken_from_the_investing_fallback(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "2000")
    _write_csv(tmp_path / "investing.csv", [[
        _dmy(OCT17), "Transfer", "", "10.00", "1000.00",
    ]])
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["revolut_source"] == "investing"
    assert breakdown["revolut_balance"] == 1000
    assert breakdown["friend_share"] == 2000
    assert breakdown["friend_share_applied"] is False
    assert breakdown["revolut_own"] == 1000
    assert breakdown["total"] == 1000


def test_friend_share_cannot_make_his_revolut_share_negative(tmp_path, monkeypatch):
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_REVOLUT_FRIEND_SHARE", "5000")
    _write_csv(savings, _savings_row("100.00"))
    _write_revolut(tmp_path / "revolut.csv", "2000")
    breakdown = finance_tracker.analyse_savings()["breakdown"]
    assert breakdown["revolut_own"] == 0
    assert breakdown["cash"] == 100
    assert breakdown["total"] == 100


def test_sheet_failure_leaves_capital_out(tmp_path, monkeypatch):
    real_loader = finance_tracker.load_paid_capital
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write_csv(savings, _savings_row("80.00"))

    def boom():
        raise OSError("token.json missing at /home/secret")

    monkeypatch.setattr(finance_tracker, "_read_paid_capital", boom)
    monkeypatch.setattr(finance_tracker, "load_paid_capital", real_loader)
    assert finance_tracker.load_paid_capital() is None
    result = finance_tracker.analyse_savings()
    assert result["breakdown"]["capital"] is None
    assert result["breakdown"]["stock"] is None
    assert result["total"] == 80
    res = _client().get("/finance/savings", headers=AUTH)
    text = res.get_data(as_text=True)
    assert "/home/secret" not in text
    assert "token.json" not in text


def test_goal_still_returned_when_nothing_is_readable(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    res = _client().get("/finance/savings", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["available"] is False
    assert body["balance"] == 0
    assert body["remaining"] == 35000
    assert body["pct"] == 0
    assert body["exchange_target"]["savings_goal"] == 35000
    assert body["exchange_target"]["savings_deadline"] == "2027-01-01"
    assert body["breakdown"]["savings"] is None
    assert body["breakdown"]["revolut_own"] is None
    assert body["breakdown"]["everyday"] is None
    assert body["breakdown"]["capital"] is None
    assert body["breakdown"]["cash"] == 0
    assert body["breakdown"]["total"] == 0

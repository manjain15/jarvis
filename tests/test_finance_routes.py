"""Read-only finance routes. CSV fixtures only — no live bank files, no network."""

import csv
import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import agent_api
import finance_tracker
import term_context

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCOUNT = "0000206850220"
OTHER_ACCOUNT = "000099998888"


def _today():
    return datetime.datetime.now(agent_api.TIMEZONE).date()


def _dmy(day):
    return day.strftime("%d/%m/%Y")


def _client():
    return agent_api.create_app(token=TOKEN).test_client()


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Description", "Debit", "Credit", "Balance"])
        writer.writerows(rows)


def _patch_files(monkeypatch, tmp_path):
    everyday = tmp_path / "everyday.csv"
    savings = tmp_path / "savings1.csv"
    monkeypatch.setattr(finance_tracker, "EVERYDAY_CSV", everyday)
    monkeypatch.setattr(finance_tracker, "SAVINGS1_CSV", savings)
    monkeypatch.setattr(finance_tracker, "REVOLUT_CSV", tmp_path / "revolut.csv")
    monkeypatch.setattr(finance_tracker, "INVESTING_CSV", tmp_path / "investing.csv")
    monkeypatch.setattr(term_context, "CONTEXT_FILE", tmp_path / "term_context.json")
    (tmp_path / "term_context.json").write_text(json.dumps({
        "us_exchange": {
            "savings_goal": 35000,
            "savings_deadline": "2027-01-01",
            "weekly_budget": 75,
            "monthly_budget": 300,
            "monthly_income": 2800,
        }
    }))
    import live_spend
    monkeypatch.setattr(live_spend, "SPEND_FILE", tmp_path / "live_spend.jsonl")
    return everyday, savings


def _seed_spend(everyday, today):
    _write_csv(everyday, [
        [_dmy(today), "Visa Purchase WOOLWORTHS SYDNEY", "22.50", "", "800.00"],
        [_dmy(today - datetime.timedelta(days=1)), f"Visa Purchase EB GAMES {ACCOUNT}", "90.00", "", "822.50"],
        [_dmy(today - datetime.timedelta(days=2)), f"Internet Withdrawal To {ACCOUNT}", "500.00", "", "912.50"],
        [_dmy(today - datetime.timedelta(days=3)), "Transfer to savings", "40.00", "", "1412.50"],
        [_dmy(today - datetime.timedelta(days=40)), "Visa Purchase COLES", "18.00", "", "2000.00"],
    ])


def test_spending_range_budget_and_redaction(tmp_path, monkeypatch):
    today = _today()
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _seed_spend(everyday, today)
    ts = datetime.datetime.now(agent_api.TIMEZONE).isoformat(timespec="seconds")
    (tmp_path / "live_spend.jsonl").write_text(json.dumps({
        "ts": ts,
        "amount": 5.0,
        "category": "Food & dining",
        "note": f"card {ACCOUNT}",
    }) + "\n")
    client = _client()

    res = client.get("/finance/spending", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["available"] is True
    assert body["days"] == 7
    assert body["by_category"]["Food & dining"] == 22.5
    assert body["total_spend"] == 112.5
    assert body["weekly_budget"] == 75
    assert body["budget_for_range"] == 75
    assert body["over_budget"] is True
    assert body["weekly_equivalent"] == 112.5
    assert ACCOUNT not in res.get_data(as_text=True)
    flagged = body["flagged"]
    assert len(flagged) == 1
    assert flagged[0]["amount"] == 90
    assert "[redacted]" in flagged[0]["description"]
    assert body["live"]["total"] == 5.0
    assert body["live"]["by_category"]["Food & dining"] == 5.0
    assert "note" not in body["live"]

    start = (today - datetime.timedelta(days=40)).isoformat()
    wider = client.get(f"/finance/spending?start={start}&end={today.isoformat()}", headers=AUTH)
    assert wider.status_code == 200
    wide = wider.get_json()
    assert wide["by_category"]["Food & dining"] == 40.5
    assert wide["days"] == 41
    assert wide["budget_for_range"] == round(75 * 41 / 7.0, 2)
    assert ACCOUNT not in wider.get_data(as_text=True)


def test_spending_validation_and_missing_csv(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    client = _client()
    today = _today()
    missing = client.get("/finance/spending", headers=AUTH)
    assert missing.status_code == 200
    assert missing.get_json()["available"] is False
    assert missing.get_json()["weekly_budget"] == 75

    assert client.get("/finance/spending?start=2026-09-01", headers=AUTH).status_code == 400
    future = (today + datetime.timedelta(days=2)).isoformat()
    assert client.get(
        f"/finance/spending?start={today.isoformat()}&end={future}", headers=AUTH,
    ).status_code == 400
    assert client.get(
        "/finance/spending?start=2020-01-01&end=2026-09-01", headers=AUTH,
    ).status_code == 400


def test_savings_against_exchange_target(tmp_path, monkeypatch):
    today = _today()
    _everyday, savings = _patch_files(monkeypatch, tmp_path)
    _write_csv(savings, [
        [_dmy(today), "Interest", "", "1.25", "10000.00"],
        [_dmy(today - datetime.timedelta(days=10)), f"From {ACCOUNT}", "", "50.00", "9998.75"],
    ])
    res = _client().get("/finance/savings", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    text = res.get_data(as_text=True)
    assert body["available"] is True
    assert body["exchange_target"]["savings_goal"] == 35000
    assert body["exchange_target"]["savings_deadline"] == "2027-01-01"
    assert body["exchange_target"]["weekly_budget"] == 75
    assert body["balance"] == 10000
    assert body["remaining"] == 25000
    assert body["pct"] == 28.6
    assert "savings_deadline" in body["exchange_target"]
    assert ACCOUNT not in text
    assert "savings1" not in text


def test_subscriptions_split_and_redact(tmp_path, monkeypatch):
    today = _today()
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    this_month = today.replace(day=10)
    prev_month = (this_month.replace(day=1) - datetime.timedelta(days=1)).replace(day=10)
    _write_csv(everyday, [
        [_dmy(this_month), "Visa Purchase SPOTIFY P1234", "11.99", "", "100.00"],
        [_dmy(prev_month), "Visa Purchase SPOTIFY P1234", "11.99", "", "100.00"],
        [_dmy(this_month), f"Visa Purchase ACME CLOUD {OTHER_ACCOUNT}", "15.00", "", "100.00"],
        [_dmy(prev_month), f"Visa Purchase ACME CLOUD {OTHER_ACCOUNT}", "15.00", "", "100.00"],
        [_dmy(this_month), f"Internet Withdrawal To {ACCOUNT}", "200.00", "", "100.00"],
        [_dmy(prev_month), f"Internet Withdrawal To {ACCOUNT}", "200.00", "", "100.00"],
    ])
    res = _client().get("/finance/subscriptions?months=3", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    text = res.get_data(as_text=True)
    assert body["available"] is True
    assert body["monthly_known"] == 11.99
    assert body["known"][0]["label"] == "Spotify"
    assert any("acme cloud" in row["merchant"] for row in body["review"])
    assert body["monthly_review"] == 15.0
    assert ACCOUNT not in text
    assert OTHER_ACCOUNT not in text
    assert "[redacted]" in text
    assert _client().get("/finance/subscriptions?months=0", headers=AUTH).status_code == 400


def test_reselling_cashflow_and_sheet_failure(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_api, "load_reselling_inventory", lambda: {
        "net_pl": 80.0,
        "sold_count": 2,
        "by_category": {"Cards": {"profit": 80.0, "revenue": 120.0, "count": 2}},
    })
    res = _client().get("/finance/reselling?days=14", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["cashflow"]["available"] is False
    assert body["cashflow"]["source"] == "none"
    assert body["cashflow"]["days"] == 14
    assert body["inventory"]["net_pl"] == 80.0
    assert body["inventory_error"] is None

    def boom():
        raise OSError("token.json missing at /home/secret")

    monkeypatch.setattr(agent_api, "load_reselling_inventory", boom)
    failed = _client().get("/finance/reselling", headers=AUTH)
    assert failed.status_code == 200
    payload = failed.get_json()
    assert payload["inventory"] is None
    assert payload["inventory_error"] == "reselling sheet unavailable"
    assert "token.json" not in failed.get_data(as_text=True)
    assert _client().get("/finance/reselling?days=0", headers=AUTH).status_code == 400


def test_finance_bundle_isolates_section_errors(tmp_path, monkeypatch):
    today = _today()
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    _seed_spend(everyday, today)

    def fail():
        raise RuntimeError("disk path /secret")

    monkeypatch.setattr(agent_api, "get_savings_payload", fail)
    monkeypatch.setattr(agent_api, "load_reselling_inventory", lambda: {"net_pl": 1})
    res = _client().get("/finance", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["spending"]["total_spend"] == 112.5
    assert body["savings"]["error"] == "savings unavailable"
    assert body["subscriptions"]["available"] is True
    assert body["reselling"]["cashflow"]["source"] == "none"
    assert body["unavailable"] == ["savings"]
    assert "disk path" not in res.get_data(as_text=True)
    assert ACCOUNT not in res.get_data(as_text=True)


def test_own_account_transfers_are_not_spending(tmp_path, monkeypatch):
    """The 22–28 Sep 2026 pattern: self-Osko and Revolut top-ups are not spend.

    Dated on today so the default 7-day window includes them. GET /finance
    and GET /finance/spending share summarise_spending.
    """
    today = _today()
    everyday, savings = _patch_files(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_api, "load_reselling_inventory", lambda: {"net_pl": 0})

    def row(description, debit, credit=""):
        return [_dmy(today), description, debit, credit, "1000.00"]

    _write_csv(everyday, [
        row("Osko Withdrawal 22Sep Knockout R Jaiswal", "120.00"),
        row("Osko Withdrawal 23Sep Knockout R Jaiswal", "120.00"),
        row("Osko Withdrawal 24Sep Doomsday Tix Abhishek Goyal", "25.00"),
        row("Visa Purchase 24Sep Playstation London", "14.95"),
        row("Visa Purchase 24Sep Paypal *Guzmanygome", "3.60"),
        row("Visa Purchase 22Sep Tfnsw Opal", "6.90"),
        row("Visa Purchase 23Sep Tfnsw Opal", "4.80"),
        row("Osko Withdrawal 25Sep Change For Hot Wheels W Tynan", "10.00"),
        row("Osko Withdrawal 24Sep12:03 Kmart 30Th Manav Jain", "5000.00"),
        row("Osko Withdrawal 24Sep12:12 Test Manav Jain", "1.00"),
        row("Osko Withdrawal 27Sep20:42 Op 17 Blisters Manav Jain", "54.00"),
        row("Visa Purchase 24Sep Revolut**5228* Melbourne", "6000.00"),
        row("Visa Purchase 22Sep Revolut**5228* Melbourne", "390.00"),
        row("Visa Purchase 22Sep Revolut**5228* Melbourne", "135.00"),
        row("Sct Deposit 24Sep Sent From Revolut Manav Jain", "", "1.00"),
    ])
    _write_csv(savings, [[_dmy(today), "Interest", "", "1.00", "8000.00"]])

    client = _client()
    spending = client.get("/finance/spending", headers=AUTH)
    assert spending.status_code == 200
    body = spending.get_json()
    assert body["total_spend"] == 305.25
    assert body["by_category"] == {
        "Entertainment": 254.95,
        "Other": 38.60,
        "Transport": 11.70,
    }
    assert sorted(item["amount"] for item in body["flagged"]) == [120.0, 120.0]
    text = spending.get_data(as_text=True).lower()
    assert "revolut" not in text
    assert "manav" not in text
    assert "knockout" in text

    bundle = client.get("/finance", headers=AUTH)
    assert bundle.status_code == 200
    bundled = bundle.get_json()
    assert bundled["spending"]["total_spend"] == 305.25
    assert bundled["savings"]["balance"] == 8000.0
    assert bundled["savings"]["available"] is True
    assert "revolut" not in bundle.get_data(as_text=True).lower()


def test_refund_reduces_spending_for_the_week(tmp_path, monkeypatch):
    """First Knockout $120 is refunded next morning; the repurchase still counts."""
    today = _today()
    yesterday = today - datetime.timedelta(days=1)
    everyday, _savings = _patch_files(monkeypatch, tmp_path)

    def row(day, description, debit, credit=""):
        return [_dmy(day), description, debit, credit, "1000.00"]

    _write_csv(everyday, [
        row(yesterday, "Osko Withdrawal 22Sep18:35 Knockout R Jaiswal", "120.00"),
        row(today, "Sct Deposit 23Sep09:11 Rishi Jaiswal", "", "120.00"),
        row(today, "Osko Withdrawal 23Sep10:30 Knockout R Jaiswal", "120.00"),
        row(today, "Osko Withdrawal 24Sep Doomsday Tix Abhishek Goyal", "25.00"),
        row(today, "Visa Purchase 24Sep Playstation London", "14.95"),
        row(today, "Visa Purchase 24Sep Paypal *Guzmanygome", "3.60"),
        row(yesterday, "Visa Purchase 22Sep Tfnsw Opal", "6.90"),
        row(today, "Visa Purchase 23Sep Tfnsw Opal", "4.80"),
        row(today, "Osko Withdrawal 25Sep Change For Hot Wheels W Tynan", "10.00"),
        row(today, "Osko Deposit 24Sep Pokemon Gemma Johnston", "", "390.00"),
        row(today, "Osko Deposit 24Sep Bank Carlos Santos", "", "135.00"),
        row(today, "Osko Deposit 25Sep Bank Carlos Santos", "", "54.00"),
        row(today, "Sct Deposit 26Sep Advance", "", "500.00"),
        row(today, "Osko Deposit 27Sep Nilesh Banga", "", "17000.00"),
    ])
    res = _client().get("/finance/spending", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["total_spend"] == 185.25
    assert body["by_category"]["Entertainment"] == 134.95
    assert body["by_category"]["Transport"] == 11.70
    assert body["by_category"]["Other"] == 38.60
    assert [item["amount"] for item in body["flagged"]] == [120.0]


def test_redact_leaves_dates_and_amounts():
    payload = {"when": "2026-09-28", "amount": 22.5, "note": f"paid {ACCOUNT}"}
    cleaned = agent_api.redact_account_numbers(payload)
    assert cleaned["when"] == "2026-09-28"
    assert cleaned["amount"] == 22.5
    assert ACCOUNT not in cleaned["note"]
    assert "[redacted]" in cleaned["note"]
    assert agent_api.redact_account_numbers("total $14.50") == "total $14.50"
    assert agent_api.redact_account_numbers("2026-09-22 2026-09-28") == "2026-09-22 2026-09-28"


def test_redact_each_sensitive_shape():
    shapes = {
        "card spaces": "4111 1111 1111 1111",
        "card dashes": "4111-1111-1111-1111",
        "spaced account": "000 020 685 0220",
        "spaced phone": "0412 345 678",
        "dashed phone": "0412-345-678",
        "masked last4": "xx1234",
        "masked stars": "card **5228",
        "payid email": "alex.jain+rent@payid.com.au",
    }
    for label, secret in shapes.items():
        cleaned = agent_api.redact_account_numbers(f"paid {secret} today")
        assert secret not in cleaned, label
        assert "[redacted]" in cleaned, label

    osko = agent_api.redact_account_numbers("Osko Withdrawal 22Sep Knockout R Jaiswal")
    assert "Jaiswal" not in osko
    assert "Knockout" in osko
    assert "[redacted]" in osko
    sct = agent_api.redact_account_numbers("Sct Deposit 23Sep09:11 Rishi Jaiswal")
    assert "Jaiswal" not in sct
    assert "Rishi" not in sct


def test_flagged_description_is_redacted_before_truncation(tmp_path, monkeypatch):
    today = _today()
    everyday, _savings = _patch_files(monkeypatch, tmp_path)
    pan = "4111222233334444"
    desc = ("M" * 73) + pan
    fragment = desc[:80][73:]
    assert len(fragment) == 7
    spaced = "Visa Purchase SHOP 4111 1111 1111 1111"
    _write_csv(everyday, [
        [_dmy(today), desc, "200.00", "", "100.00"],
        [_dmy(today), "Osko Withdrawal 22Sep Knockout R Jaiswal", "120.00", "", "100.00"],
        [_dmy(today), spaced, "90.00", "", "100.00"],
    ])
    res = _client().get("/finance/spending", headers=AUTH)
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    flagged = res.get_json()["flagged"]
    assert pan not in text
    assert fragment not in text
    assert "4111" not in text
    assert "Jaiswal" not in text
    assert "Knockout" in text
    assert all(len(row["description"]) <= 80 for row in flagged)
    long = next(row for row in flagged if row["amount"] == 200)
    assert "[redacted]" in long["description"]
    assert long["description"].endswith("[redacted]")

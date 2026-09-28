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


def test_redact_leaves_dates_and_amounts():
    payload = {"when": "2026-09-28", "amount": 22.5, "note": f"paid {ACCOUNT}"}
    cleaned = agent_api.redact_account_numbers(payload)
    assert cleaned["when"] == "2026-09-28"
    assert cleaned["amount"] == 22.5
    assert ACCOUNT not in cleaned["note"]
    assert "[redacted]" in cleaned["note"]

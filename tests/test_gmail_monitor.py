"""Gmail monitor skip rules and interval guard. No Gmail or ntfy calls."""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import gmail_monitor


def _mail(sender, email, subject):
    return {
        "id": subject,
        "sender": sender,
        "sender_email": email,
        "subject": subject,
        "snippet": "preview",
    }


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail_monitor, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(gmail_monitor, "RUN_STAMP", tmp_path / "stamp.json")
    monkeypatch.delenv("JARVIS_GMAIL_SKIP_CATEGORIES", raising=False)
    monkeypatch.delenv("JARVIS_GMAIL_MIN_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("JARVIS_GMAIL_HOURS_BACK", raising=False)


def test_external_skip_keeps_local_alerts():
    linkedin = _mail("LinkedIn Jobs", "jobs@linkedin.com", "Your application was viewed")
    assert gmail_monitor.externally_owned_category(linkedin) == "internship"

    mentor = _mail("Emily", "emily@example.com", "Mentor check-in")
    assert gmail_monitor.externally_owned_category(mentor) == "mentor"

    amazon = _mail("Amazon", "auto@amazon.com.au", "Your package was delivered")
    assert gmail_monitor.externally_owned_category(amazon) is None

    interview = _mail("Amazon", "recruiting@amazon.com", "Interview confirmation — SDE intern")
    assert gmail_monitor.externally_owned_category(interview) == "internship"

    uni = _mail("UNSW", "noreply@unsw.edu.au", "Assignment result posted")
    assert gmail_monitor.externally_owned_category(uni) is None

    bank = _mail("St.George", "alerts@stgeorge.com.au", "Payment received")
    assert gmail_monitor.externally_owned_category(bank) is None

    security = _mail("LinkedIn", "security@linkedin.com", "Security alert: new sign-in")
    assert gmail_monitor.externally_owned_category(security) is None


def test_skip_list_is_configurable(monkeypatch):
    mentor = _mail("Emily", "emily@example.com", "Mentor check-in")
    monkeypatch.setenv("JARVIS_GMAIL_SKIP_CATEGORIES", "")
    assert gmail_monitor.externally_owned_category(mentor) is None
    monkeypatch.setenv("JARVIS_GMAIL_SKIP_CATEGORIES", "internship")
    assert gmail_monitor.externally_owned_category(mentor) is None
    monkeypatch.setenv("JARVIS_GMAIL_SKIP_CATEGORIES", "mentor")
    assert gmail_monitor.externally_owned_category(mentor) == "mentor"


def test_run_skips_career_mail_and_still_alerts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    emails = [
        _mail("LinkedIn Jobs", "jobs@linkedin.com", "Your application was viewed"),
        _mail("UNSW", "results@student.unsw.edu.au", "Assignment result posted"),
        _mail("Emily", "emily@example.com", "Feedback on the draft"),
    ]
    emails[0]["id"] = "job"
    emails[1]["id"] = "uni"
    emails[2]["id"] = "feedback"
    fetched = {}

    def fetch(hours_back=1, max_emails=20):
        fetched["hours_back"] = hours_back
        return emails

    classified = []

    def classify(email):
        classified.append(email["id"])
        if email["id"] == "feedback":
            return {
                "needs_action": True, "category": "mentor", "priority": "high",
                "summary": "mentor reply", "suggested_action": "reply",
            }
        return {
            "needs_action": True, "category": "academic", "priority": "high",
            "summary": "grade posted", "suggested_action": "read it",
        }

    notified = []
    monkeypatch.setattr(gmail_monitor, "fetch_recent_emails", fetch)
    monkeypatch.setattr(gmail_monitor, "classify_email", classify)
    monkeypatch.setattr(gmail_monitor, "send_notification", lambda *args, **kwargs: notified.append(args))

    gmail_monitor.run_monitor()

    assert fetched["hours_back"] == 2
    assert classified == ["uni", "feedback"]
    assert len(notified) == 1
    assert "Assignment result posted" in notified[0][0]
    seen = json.loads((tmp_path / "seen.json").read_text())["ids"]
    assert set(seen) == {"job", "uni", "feedback"}


def test_min_interval_skips_fetch_unless_forced(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    now = datetime.datetime.now(gmail_monitor.TIMEZONE)
    (tmp_path / "stamp.json").write_text(json.dumps({"ts": now.isoformat()}))
    calls = []
    monkeypatch.setattr(gmail_monitor, "fetch_recent_emails", lambda **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr(gmail_monitor, "send_notification", lambda *args, **kwargs: None)

    gmail_monitor.run_monitor()
    assert calls == []

    gmail_monitor.run_monitor(force=True)
    assert calls and calls[0]["hours_back"] == 2

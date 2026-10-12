"""Telegram command parsing: iOS smart quotes and /done feedback."""

import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import jarvis_telegram
import term_context


def test_smart_quotes_group_like_straight_quotes(monkeypatch):
    seen = {}
    monkeypatch.setattr(term_context, "mark_assessment_done",
                        lambda subject, name: seen.update(subject=subject, name=name) or "In-class Test")
    reply = jarvis_telegram.handle_command("/done MATH2931 “In-class Test”")
    assert seen == {"subject": "MATH2931", "name": "In-class Test"}
    assert reply == "✅ Marked MATH2931 — In-class Test as submitted."


def test_done_reports_a_miss(monkeypatch):
    monkeypatch.setattr(term_context, "mark_assessment_done", lambda subject, name: None)
    reply = jarvis_telegram.handle_command('/done MATH2931 "Quiz 9"')
    assert reply.startswith("⚠️ No MATH2931 assessment matching")

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
    assert reply == "Marked MATH2931 — In-class Test as submitted."


def test_done_reports_a_miss(monkeypatch):
    monkeypatch.setattr(term_context, "mark_assessment_done", lambda subject, name: None)
    reply = jarvis_telegram.handle_command('/done MATH2931 "Quiz 9"')
    assert reply.startswith("No MATH2931 assessment matching")


def test_replies_carry_no_emoji(monkeypatch):
    from voice import _EMOJI
    monkeypatch.setattr(term_context, "mark_assessment_done", lambda subject, name: "Quiz")
    for reply in (jarvis_telegram.handle_command('/done MATH2931 "Quiz"'), jarvis_telegram.cmd_help([])):
        assert not _EMOJI.search(reply)


def test_send_message_drops_emoji_but_keeps_layout(monkeypatch):
    sent = []
    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_CHAT_ID", 1)
    monkeypatch.setattr(jarvis_telegram, "_api", lambda method, params: sent.append(params["text"]))
    jarvis_telegram.send_message("🔥 Logged.\n  • 💪 Pull · 50 min\n\n✅ Done")
    assert sent == ["Logged.\n  • Pull · 50 min\n\nDone"]


def test_errors_are_logged_without_the_bot_token(monkeypatch):
    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_BOT_TOKEN", "8605553805:AAHsecret")
    err = ConnectionError("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
                          "with url: /bot8605553805:AAHsecret/getUpdates?offset=1")
    assert "AAHsecret" not in jarvis_telegram.redact(err)
    assert "/bot<token>/getUpdates" in jarvis_telegram.redact(err)

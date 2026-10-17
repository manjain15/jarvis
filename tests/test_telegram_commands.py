"""Telegram command parsing: iOS smart quotes and /done feedback."""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import day_plan
import jarvis_telegram
import ledger
import term_context


SATURDAY = datetime.date(2026, 10, 17)
WHEN = ledger.TIMEZONE.localize(datetime.datetime(2026, 10, 17, 15, 10))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Ledger, clock and Telegram sends that stay off the network and the real data dir."""
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(ledger, "now_local", lambda: WHEN)
    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_CHAT_ID", 42)
    was_active = jarvis_telegram.CHECKIN["active"]
    jarvis_telegram.CHECKIN["active"] = False
    sent = []
    claude = []
    monkeypatch.setattr(jarvis_telegram, "send_message", lambda text, chat_id=None, parse_mode=None: sent.append(text) or 1)
    monkeypatch.setattr(jarvis_telegram, "chat_with_claude", lambda text, *args, **kwargs: claude.append(text) or "Alright, I hear you. Rest up.")
    monkeypatch.setattr(jarvis_telegram, "append_turn", lambda *args, **kwargs: None)
    try:
        yield sent, claude
    finally:
        jarvis_telegram.CHECKIN["active"] = was_active


def _update(text, reply_to=None):
    msg = {"message_id": 900, "chat": {"id": 42}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to, "chat": {"id": 42}, "text": "question"}
    return {"update_id": 1, "message": msg}


def _block(item_id, course, status, message_id, hour):
    asked = ledger.TIMEZONE.localize(datetime.datetime(2026, 10, 17, hour, 0))
    ledger.update_day(SATURDAY, lambda d, item={
        "id": item_id, "kind": "study", "type": "lecture", "course": course,
        "task": "task", "minutes": 60, "status": status,
        "start": asked.isoformat(), "end": asked.isoformat(),
        "asked_at": asked.isoformat(), "ask_message_id": message_id,
        "ask_message_ids": [message_id],
    }: d["items"].append(dict(item)))


def _deliver(update):
    jarvis_telegram.handle_update(update, {"text": ""})


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
    assert jarvis_telegram.send_message("🔥 Logged.\n  • 💪 Pull · 50 min\n\n✅ Done") is True
    assert sent == ["Logged.\n  • Pull · 50 min\n\nDone"]


def test_send_message_returns_the_telegram_message_id(monkeypatch):
    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_CHAT_ID", 1)
    monkeypatch.setattr(jarvis_telegram, "_api", lambda method, params: {"message_id": 42, "text": params["text"]})
    assert jarvis_telegram.send_message("MATH2931 · task. Done? done / partial / no") == 42


def test_check_question_stores_the_sent_message_id(monkeypatch):
    monkeypatch.setattr(jarvis_telegram, "OUTLINES_AVAILABLE", False)
    monkeypatch.setattr(jarvis_telegram, "ENFORCER_AVAILABLE", False)
    monkeypatch.setattr(jarvis_telegram, "PACE_AVAILABLE", False)
    monkeypatch.setattr(day_plan, "tick", lambda now=None: None)
    monkeypatch.setattr(day_plan, "check_tick", lambda now=None: ["MATH2931 · task. Done? done / partial / no"])
    seen = []
    monkeypatch.setattr(day_plan, "record_ask_message", lambda message_id, now=None: seen.append(message_id))
    monkeypatch.setattr(jarvis_telegram, "send_message", lambda text, chat_id=None, parse_mode=None: 4242)
    jarvis_telegram.CHECKIN["active"] = False
    jarvis_telegram.run_enforcer_tick()
    assert seen == [4242]


def test_reply_to_expired_check_in_logs_it(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "no_reply", 101, 13)
    _block("study-3", "COMP3121", "no_reply", 202, 14)
    _deliver(_update("done", reply_to=101))
    assert sent == ["Logged MATH2931."]
    assert claude == []
    items = {i["id"]: i["status"] for i in ledger.load_day(SATURDAY)["items"] if i.get("kind") == "study"}
    assert items == {"study-2": "done", "study-3": "no_reply"}


def test_reply_to_pending_check_in_logs_it(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "pending", 101, 13)
    _deliver(_update("done", reply_to=101))
    assert sent == ["Logged MATH2931."]
    assert claude == []
    assert ledger.load_day(SATURDAY)["items"][-1]["status"] == "done"


def test_reply_to_unknown_message_falls_through(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "no_reply", 101, 13)
    _deliver(_update("done", reply_to=999))
    assert claude == ["done"]
    assert sent == ["Alright, I hear you. Rest up."]
    assert ledger.load_day(SATURDAY)["items"][-1]["status"] == "no_reply"


def test_reply_to_known_question_with_other_words_falls_through(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "no_reply", 101, 13)
    _deliver(_update("thanks", reply_to=101))
    assert claude == ["thanks"]
    assert sent == ["Alright, I hear you. Rest up."]
    assert ledger.load_day(SATURDAY)["items"][-1]["status"] == "no_reply"


def test_plain_chat_still_reaches_claude(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "no_reply", 101, 13)
    _deliver(_update("what's next"))
    assert claude == ["what's next"]
    assert day_plan.LATE_HINT not in sent


def test_plain_late_done_hints_instead_of_claude(isolated):
    sent, claude = isolated
    _block("study-2", "MATH2931", "no_reply", 101, 13)
    _deliver(_update("done"))
    assert sent == [day_plan.LATE_HINT]
    assert claude == []
    assert ledger.load_day(SATURDAY)["items"][-1]["status"] == "no_reply"


def test_reply_to_area_touches_it(isolated, monkeypatch, tmp_path):
    import areas
    monkeypatch.setattr(areas, "AREAS_FILE", tmp_path / "areas.json")
    sent, claude = isolated
    asked = ledger.TIMEZONE.localize(datetime.datetime(2026, 10, 17, 18, 0))
    ledger.update_day(SATURDAY, lambda d: d["items"].append({
        "id": "area-1", "kind": "area", "area": "networking", "title": "Networking",
        "task": "Message one engineer", "minutes": 45, "status": "no_reply",
        "start": asked.isoformat(), "end": asked.isoformat(),
        "asked_at": asked.isoformat(), "ask_message_id": 303, "ask_message_ids": [303],
    }))
    _deliver(_update("done", reply_to=303))
    assert sent == ["Logged Networking."]
    assert claude == []
    assert areas.load()["networking"]["last_touched"] == "2026-10-17"
    assert ledger.load_day(SATURDAY)["items"][-1]["status"] == "done"


def test_errors_are_logged_without_the_bot_token(monkeypatch):
    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_BOT_TOKEN", "8605553805:AAHsecret")
    err = ConnectionError("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
                          "with url: /bot8605553805:AAHsecret/getUpdates?offset=1")
    assert "AAHsecret" not in jarvis_telegram.redact(err)
    assert "/bot<token>/getUpdates" in jarvis_telegram.redact(err)

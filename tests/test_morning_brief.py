"""Morning brief: prompt limits, the note call, data builders and rendering. Never sends mail."""

import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import morning_brief

_GOALS = {
    "savings_goal": 35000.0,
    "savings_deadline": "2027-01-01",
    "monthly_income": 2800.0,
    "monthly_budget": 300.0,
    "weekly_budget": 75.0,
}


class _Block:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, stop):
        self.content = [_Block(text)]
        self.stop_reason = stop


class _Scripted:
    """Anthropic stand-in. Records prompts and max_tokens, returns a script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, api_key):
        self.api_key = api_key
        client = self

        class _Messages:
            def create(_self, **kwargs):
                client.calls.append(kwargs)
                item = client.script[len(client.calls) - 1]
                if isinstance(item, BaseException):
                    raise item
                text, stop = item
                return _Msg(text, stop)

        self.messages = _Messages()
        return self


def _install(monkeypatch, script):
    scripted = _Scripted(script)
    monkeypatch.setattr(morning_brief.anthropic, "Anthropic", scripted)
    monkeypatch.setattr(morning_brief, "send_email", _must_not_send)
    monkeypatch.setattr(morning_brief, "get_finance_goals", lambda: dict(_GOALS))
    return scripted


def _must_not_send(*_args, **_kwargs):
    raise AssertionError("send_email must not run in tests")


def test_prompt_caps_sections_and_states_limits(monkeypatch):
    monkeypatch.setattr(morning_brief, "get_finance_goals", lambda: dict(_GOALS))
    emails = [
        {"sender": f"Person {i}", "subject": f"Subject {i}", "snippet": "x" * 400}
        for i in range(10)
    ]
    prompt = morning_brief.build_prompt(
        "Z" * 8000,
        [],
        emails,
        "Monday, 28 September 2026",
        finance_data="F" * 5000,
    )
    assert "Z" * 4000 not in prompt
    assert "…(section shortened)" in prompt
    assert "F" * 3000 not in prompt
    assert "+4 more unread" in prompt
    assert "$75" in prompt
    assert "35,000" in prompt
    assert "January 2027" in prompt
    assert prompt.startswith(morning_brief.VOICE_RULES)
    assert '{"note": "...", "push": "..."}' in prompt
    assert "You are Jarvis" not in prompt


def test_generate_note_parses_json_without_sending(monkeypatch):
    scripted = _install(monkeypatch, [(
        'Sure.\n{"note": "Quiz Thursday. 🔥 Do the 13:00 block.", "push": "Message Priya."}', "end_turn")])
    note = morning_brief.generate_note("prompt")
    assert note == {"note": "Quiz Thursday. Do the 13:00 block.", "push": "Message Priya."}
    assert scripted.calls[0]["max_tokens"] == morning_brief.NOTE_MAX_TOKENS


def test_unparseable_or_failed_note_still_lets_the_brief_send(monkeypatch):
    _install(monkeypatch, [("no json here", "end_turn")])
    assert morning_brief.generate_note("prompt") == {"note": "", "push": ""}
    _install(monkeypatch, [ConnectionError("reset")])
    assert morning_brief.generate_note("prompt") == {"note": "", "push": ""}


def _api_status(code):
    """Build an Anthropic status error without calling the network."""
    http_mod = None
    for name in ("httpx", "httpx2"):
        try:
            http_mod = __import__(name)
            break
        except ImportError:
            continue
    request = http_mod.Request("POST", "https://api.anthropic.com/v1/messages")
    response = http_mod.Response(code, request=request)
    return morning_brief.anthropic.APIStatusError("overloaded", response=response, body=None)


def test_overload_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    scripted = _install(monkeypatch, [_api_status(529), ('{"note": "n", "push": "p"}', "end_turn")])
    assert morning_brief.generate_note("prompt") == {"note": "n", "push": "p"}
    assert len(scripted.calls) == 2


# ── Data builders ───────────────────────────────────────────────────────────

import datetime  # noqa: E402

import email_template  # noqa: E402

TZ = morning_brief.pytz.timezone("Australia/Sydney")
MON = datetime.date(2026, 10, 12)


def _at(h, m=0):
    return TZ.localize(datetime.datetime.combine(MON, datetime.time(h, m)))


DAY = {
    "date": "2026-10-12", "split": "Pull",
    "debt": {"gym_minutes": 20, "steps": 1160,
             "makeup": {"split": "Push", "minutes": 55, "due": "2026-10-13", "from": "2026-10-11"}},
    "items": [
        {"id": "gym", "kind": "gym", "title": "Pull", "slot": "06:30", "base_minutes": 45,
         "minutes": 65, "status": "pending"},
        {"id": "steps", "kind": "steps", "title": "Steps", "target": 11160, "count": 0, "status": "pending"},
        {"id": "study-1", "kind": "study", "title": "Study · MATH2931", "task": "W5 tutorial Qs",
         "start": "2026-10-12T19:00+11:00", "end": "2026-10-12T20:00+11:00", "minutes": 60},
        {"id": "scif1000-pre", "kind": "check", "label": "SCIF1000 pre-class activities",
         "ask_at": "2026-10-12T11:00+11:00"},
    ],
}


def test_today_rows_merge_and_sort():
    cal = [{"start": _at(9, 30), "title": "Automation work", "location": "Office"},
           {"start": None, "title": "Mum's birthday", "location": ""}]
    rows = morning_brief.today_rows(DAY, cal, [], travel_minutes=5)
    assert [r["time"] for r in rows] == ["All day", "06:30", "09:30", "11:00", "19:00"]
    assert rows[1] == {"time": "06:30", "title": "Pull", "sub": "Leave by 06:25 · 65 min with debt", "miss": False}
    assert rows[2]["sub"] == "Office"
    assert rows[4]["sub"] == "W5 tutorial Qs · 60 min"


def test_owed_rows():
    rows = morning_brief.owed_rows(DAY, MON)
    assert rows[0] == {"label": "Gym make-up · Push · by Tue", "value": "55 min", "miss": True}
    assert rows[1]["value"] == "+20 min"
    assert rows[2] == {"label": "Steps carried from yesterday", "value": "+1,160", "miss": False}
    assert morning_brief.owed_rows({"debt": {"gym_minutes": 0, "steps": 0, "makeup": None}}, MON) == []


def test_due_rows():
    upcoming = [{"subject": "MATH2931", "name": "Assignment 2", "due": "2026-11-11", "days_left": 30, "weight": 20},
                {"subject": "COMP3121", "name": "Quiz", "due": "2026-10-13", "days_left": 1, "weight": None}]
    rows = morning_brief.due_rows(upcoming)
    assert rows[0] == {"label": "MATH2931 Assignment 2 (20%)", "value": "Wed 11 Nov · 30d", "miss": False}
    assert rows[1] == {"label": "COMP3121 Quiz", "value": "Tue 13 Oct · tomorrow", "miss": True}


def test_yesterday_stats():
    prev = {"items": [{"id": "gym", "kind": "gym", "title": "Push", "status": "missed"},
                      {"id": "steps", "kind": "steps", "count": 8840, "target": 10000}]}
    cells = morning_brief.yesterday_stats(prev, sleep_hours=7.1, weight=78.4, weight_delta=-0.3)
    assert cells == [
        {"value": "Missed", "label": "Push", "miss": True},
        {"value": "8,840", "label": "Steps", "miss": True},
        {"value": "7.1h", "label": "Sleep", "miss": False},
        {"value": "78.4", "label": "kg · -0.3 wk", "miss": False},
    ]
    rest = morning_brief.yesterday_stats(None, health_steps=12000)
    assert rest[0]["value"] == "Rest" and rest[1] == {"value": "12,000", "label": "Steps", "miss": False}
    assert rest[2]["value"] == "—" and rest[3]["value"] == "—"


def test_meta_line():
    assert morning_brief.meta_line(5, "Pull", 2) == "Week 5 · Pull day · 2 classes"
    assert morning_brief.meta_line(5, "Rest", 1) == "Week 5 · Rest day · 1 class"
    assert morning_brief.meta_line(None, None, 0) == "Rest day"


def test_render_leaves_out_empty_sections_and_escapes():
    page, text = email_template.render_brief({
        "title": "Monday 12 October", "meta": "Week 5 · Pull day", "note": "<b>Quiz</b> & test",
        "owed": [], "today": [{"time": "06:30", "title": "Pull", "sub": None}], "due": [],
        "yesterday": [{"value": "Done", "label": "Legs", "miss": False}], "push": "",
        "footer": "jarvis · 07:05",
    })
    assert "&lt;b&gt;Quiz&lt;/b&gt; &amp; test" in page
    assert "Owed" not in page and "Due soon" not in page and "One push" not in page
    assert "Today" in page and "Yesterday" in page
    assert 'name="color-scheme" content="dark"' in page
    assert "u + .body .gmail-blend-screen" in page
    assert f"linear-gradient({email_template.OUTER}, {email_template.OUTER})" in page
    assert "TODAY\n  06:30  Pull" in text

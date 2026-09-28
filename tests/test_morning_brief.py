"""Morning brief length limits and truncation fallback. Never sends mail."""

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
    assert "Under 320 words" in prompt
    assert "At most 2 sentences" in prompt
    assert "+4 more unread" in prompt
    assert "$75" in prompt
    assert "35,000" in prompt
    assert "January 2027" in prompt


def test_generate_brief_returns_complete_reply_without_sending(monkeypatch):
    scripted = _install(monkeypatch, [("<h2>Good morning.</h2><p>Ship it.</p>", "end_turn")])
    html = morning_brief.generate_brief("prompt")
    assert html == "<h2>Good morning.</h2><p>Ship it.</p>"
    assert len(scripted.calls) == 1
    assert scripted.calls[0]["max_tokens"] == 2500


def test_truncated_brief_retries_once_then_uses_the_shorter_reply(monkeypatch):
    scripted = _install(monkeypatch, [
        ("<h2>Good morning", "max_tokens"),
        ("<h2>Good morning.</h2><p>Short enough.</p>", "end_turn"),
    ])
    html = morning_brief.generate_brief("facts")
    assert html == "<h2>Good morning.</h2><p>Short enough.</p>"
    assert len(scripted.calls) == 2
    assert scripted.calls[1]["max_tokens"] == 2500
    assert "280 words" in scripted.calls[1]["messages"][0]["content"]
    assert scripted.calls[0]["messages"][0]["content"] == "facts"


def test_double_truncation_is_closed_html(monkeypatch):
    scripted = _install(monkeypatch, [
        ("<h2>Good morning.</h2><p>Cut off mid", "max_tokens"),
        ("<h3>Finance</h3><p>Still cut", "max_tokens"),
    ])
    html = morning_brief.generate_brief("facts")
    assert len(scripted.calls) == 2
    assert "Brief shortened to fit" in html
    assert html.count("<h3>") == html.count("</h3>")
    assert html.count("<p>") == html.count("</p>")
    assert not html.rstrip().endswith("<")


def test_salvage_drops_a_dangling_tag():
    html = morning_brief.salvage_truncated_html("<h2>Hello</h2><p")
    body, note = html.split("<p><em>", 1)
    assert body == "<h2>Hello</h2>"
    assert note.startswith("Brief shortened to fit")


def test_salvage_closes_nested_and_unclosed_tags():
    nested = morning_brief.salvage_truncated_html("<div><section><p>Nested.")
    assert "Nested." in nested
    assert nested.count("<div>") == nested.count("</div>")
    assert nested.count("<section>") == nested.count("</section>")
    assert nested.count("<p>") == nested.count("</p>")
    assert nested.index("</p>") < nested.index("</section>") < nested.index("</div>")

    unclosed = morning_brief.salvage_truncated_html("<h2>Title<p>Body")
    assert unclosed.count("<h2>") == unclosed.count("</h2>")
    assert unclosed.count("<p>") == unclosed.count("</p>")
    assert unclosed.index("</p>") < unclosed.index("</h2>")
    assert "Body" in unclosed

    dangling = morning_brief.salvage_truncated_html("<div><p>Hi</p><span")
    assert "<span" not in dangling
    assert dangling.startswith("<div><p>Hi</p>")
    assert "</div>" in dangling


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
    return morning_brief.anthropic.APIStatusError("retry failed", response=response, body=None)


def test_failed_retry_returns_the_salvaged_first_body(monkeypatch):
    first = "<h2>Good morning.</h2><p>Keep this first draft.</p><ul><li>open."
    for error in (_api_status(500), ConnectionError("connection reset")):
        scripted = _install(monkeypatch, [(first, "max_tokens"), error])
        html = morning_brief.generate_brief("facts")
        assert "Keep this first draft" in html
        assert "Brief shortened to fit" in html
        assert html.count("<ul>") == html.count("</ul>")
        assert html.count("<li>") == html.count("</li>")
        assert "connection reset" not in html
        assert len(scripted.calls) == 2

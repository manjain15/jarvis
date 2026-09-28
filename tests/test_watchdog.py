"""Watchdog unit list. Does not call systemctl."""

import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import jarvis_telegram
import watchdog


def test_agent_api_is_always_on():
    assert "jarvis-agent-api" in watchdog.ALWAYS_ON
    assert watchdog.ALWAYS_ON == jarvis_telegram.STATUS_ALWAYS_ON


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_probe_ok_and_failures():
    def ok(_request, timeout=5):
        return _Resp(200, b'{"ok": true, "service": "jarvis-agent-api"}')

    assert watchdog.probe_agent_api(opener=ok, token="t" * 32, url="http://127.0.0.1:5557/health") is None

    def down(_request, timeout=5):
        raise OSError("connection refused")

    failed = watchdog.probe_agent_api(opener=down, token="t" * 32, url="http://127.0.0.1:5557/health")
    assert failed == "❌ jarvis-agent-api /health probe failed"
    assert "connection refused" not in failed

    import urllib.error

    def denied(_request, timeout=5):
        raise urllib.error.HTTPError("http://127.0.0.1/health", 401, "no", None, None)

    assert "401" in watchdog.probe_agent_api(opener=denied, token="t" * 32, url="http://127.0.0.1:5557/health")
    assert watchdog.probe_agent_api(opener=ok, token="", url="http://127.0.0.1:5557/health") == (
        "❌ jarvis-agent-api /health probe has no API token"
    )


def test_probe_reads_token_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    monkeypatch.delenv("JARVIS_API_TOKENS", raising=False)
    env = tmp_path / ".env"
    env.write_text('JARVIS_API_TOKEN="abc"\n')
    assert watchdog.agent_api_token(env) == "abc"
    env.write_text("JARVIS_API_TOKENS=career:bot-secret,study:other\n")
    assert watchdog.agent_api_token(env) == "bot-secret"


def test_check_problems_includes_http_probe(monkeypatch):
    monkeypatch.setattr(watchdog, "_systemctl", lambda *args: "active")
    usage = type("Usage", (), {"total": 100, "used": 10})()
    monkeypatch.setattr(watchdog.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(watchdog, "probe_agent_api", lambda: "❌ jarvis-agent-api /health probe failed")
    problems = watchdog.check_problems()
    assert problems == ["❌ jarvis-agent-api /health probe failed"]


def test_repeated_failure_alerts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(watchdog, "_notify", lambda text: calls.append(text) or True)
    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-agent-api /health probe failed"])

    assert watchdog.run() == ["❌ jarvis-agent-api /health probe failed"]
    assert watchdog.run() == ["❌ jarvis-agent-api /health probe failed"]
    assert len(calls) == 1

    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-telegram service is inactive (should be active)"])
    watchdog.run()
    assert len(calls) == 2

    monkeypatch.setattr(watchdog, "check_problems", lambda: [])
    assert watchdog.run() == []
    assert len(calls) == 2

    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-telegram service is inactive (should be active)"])
    watchdog.run()
    assert len(calls) == 3

    watchdog.run(daily=True)
    watchdog.run(daily=True)
    assert len(calls) == 5

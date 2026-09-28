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

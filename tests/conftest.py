"""Shared pytest setup: put the repo root on sys.path so tests can import
Jarvis's top-level modules (study_tracker, uni_timetable, etc.)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_api_files(tmp_path, monkeypatch):
    """Keep agent API audit and usage files out of the real data/ directory."""
    try:
        import agent_api
    except Exception:
        return
    monkeypatch.setattr(agent_api, "AUDIT_FILE", tmp_path / "agent_api_audit.jsonl")
    monkeypatch.setattr(agent_api, "USAGE_FILE", tmp_path / "agent_api_usage.json")

"""Shared pytest setup: put the repo root on sys.path so tests can import
Jarvis's top-level modules (study_tracker, uni_timetable, etc.)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

"""
Jarvis — Locked JSON Store
============================
Shared helpers for race-safe access to Jarvis's shared JSON state files
(proposal queues, term_context.json). Cron jobs and the always-on
Telegram bot can touch the same file within seconds of each other;
without a lock spanning the full read-modify-write, a slower write can
silently clobber a faster one's changes (a "lost update").

USAGE:
  from json_store import file_lock, atomic_write_json

  with file_lock(PROPOSALS_FILE):
      proposals = load_proposals()
      ...mutate...
      save_proposals(proposals)   # save_proposals uses atomic_write_json
"""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(target_path):
    """
    Holds an exclusive advisory lock keyed on `target_path` for the
    duration of the `with` block. Callers must wrap their entire
    load -> mutate -> save cycle in this, not just the save.
    """
    target_path = Path(target_path)
    lock_path = target_path.parent / (target_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def atomic_write_json(path, data):
    """Writes JSON via temp-file + rename so readers never see a torn file."""
    path = Path(path)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)

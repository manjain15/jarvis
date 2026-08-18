"""
Jarvis — Memory Backfill (one-time recovery)
===============================================
Rebuilds memory/episodic.md and memory/semantic.md from surviving history
in data/ after the Aug 2026 VPS migration wiped memory/ but left
data/checkin_*.json and data/summary_*.txt intact.

Replays each historical day, oldest first, through the same
generate_memory_entries() / append_episodic() / apply_semantic_updates()
functions the nightly cron uses — just with an explicit historical date
instead of "today", and today_data built from the surviving checkin/summary
files instead of live API calls (Hevy/Health/Finance history for arbitrary
past days isn't available).

USAGE:
  python3 backfill_memory.py --dry-run   # list the dates it would process
  python3 backfill_memory.py             # actually rebuild memory/
"""

import json
import sys
import time
import datetime
from pathlib import Path

import memory_system as ms

DATA_DIR = ms.DATA_DIR


def find_backfill_dates():
    """Returns sorted dates that have a checkin and/or summary file to replay."""
    dates = set()
    for f in DATA_DIR.glob("checkin_*.json"):
        dates.add(f.stem.removeprefix("checkin_"))
    for f in DATA_DIR.glob("summary_*.txt"):
        dates.add(f.stem.removeprefix("summary_"))
    return sorted(datetime.date.fromisoformat(d) for d in dates)


def build_days_data(date):
    """Builds a today_data-shaped string for `date` from its surviving files."""
    date_str = date.strftime("%Y-%m-%d")
    sections = []

    checkin_path = DATA_DIR / f"checkin_{date_str}.json"
    if checkin_path.exists():
        try:
            checkin = json.loads(checkin_path.read_text())
            sections.append("EVENING CHECK-IN:\n" + json.dumps(checkin, indent=2, default=str))
        except Exception:
            pass

    summary_path = DATA_DIR / f"summary_{date_str}.txt"
    if summary_path.exists():
        try:
            sections.append("EVENING SUMMARY:\n" + summary_path.read_text())
        except Exception:
            pass

    return "\n\n".join(sections) if sections else None


def run_backfill(dry_run=False):
    ms.init_memory()
    dates = find_backfill_dates()

    if not dates:
        print("No checkin/summary history found in data/ — nothing to backfill.")
        return

    print(f"Found {len(dates)} day(s) to replay: {dates[0]} → {dates[-1]}")
    if dry_run:
        for d in dates:
            print(f"  would process {d}")
        return

    for i, date in enumerate(dates, 1):
        today_data = build_days_data(date)
        if not today_data:
            continue

        print(f"[{i}/{len(dates)}] {date} ...", end=" ", flush=True)
        try:
            episodic_entry, semantic_updates = ms.generate_memory_entries(today_data, date=date)
            wrote_episodic = ms.append_episodic(episodic_entry, date)
            wrote_semantic = ms.apply_semantic_updates(semantic_updates)
            print(f"episodic={'yes' if wrote_episodic else 'no'} semantic={'yes' if wrote_semantic else 'no'}")
        except Exception as e:
            print(f"FAILED ({e}) — skipping, continuing with next day")

        time.sleep(1)  # be polite to the API, no need to hammer it

    print("\n✅  Backfill complete.")


if __name__ == "__main__":
    run_backfill(dry_run="--dry-run" in sys.argv)

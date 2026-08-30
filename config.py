# ─────────────────────────────────────────────────────────────────────────────
# config.py — Personal Jarvis settings
# Secrets are loaded from .env (gitignored). Edit non-secret values here.
# ─────────────────────────────────────────────────────────────────────────────

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Secrets (from .env) ──────────────────────────────────────────────────────
YOUR_EMAIL         = os.environ["YOUR_EMAIL"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
HEVY_API_KEY       = os.environ["HEVY_API_KEY"]
NTFY_CHANNEL       = os.environ.get("NTFY_CHANNEL", "")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

# Fine-grained PAT, Contents + Pull requests read/write, scoped to your own repos.
# Used by remote_work.py to look up/clone your projects and open PRs. Empty = dormant.
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")

# UNSW timetable: subscribed .ics feed URL (webcal:// or https://...ics).
# Empty = integration dormant; see uni_timetable.py.
TIMETABLE_ICS_URL  = os.environ.get("TIMETABLE_ICS_URL", "")

# ── Non-secret config ────────────────────────────────────────────────────────
# Full timezone list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE         = "Australia/Sydney"
EMAIL_HOURS_BACK = 18    # how far back to scan email for the brief
MAX_EMAILS       = 15    # cap emails included in the prompt (cost lever)

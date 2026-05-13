"""
config.py — Jarvis configuration
Reads from environment variables when deployed (Railway),
falls back to local values for development on your Mac.
"""
import os

# ── Core ──────────────────────────────────────────────────────────────────────
YOUR_EMAIL        = os.environ.get("YOUR_EMAIL", "manavj0707@gmail.com")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TIMEZONE          = os.environ.get("TIMEZONE", "Australia/Sydney")
EMAIL_HOURS_BACK  = 18
MAX_EMAILS        = 15

# ── Hevy ──────────────────────────────────────────────────────────────────────
HEVY_API_KEY = os.environ.get("HEVY_API_KEY", "")

# ── Fitbit / Google Health ────────────────────────────────────────────────────
# Not used on Railway (no token files) — local only
FITBIT_CLIENT_ID     = ""
FITBIT_CLIENT_SECRET = ""

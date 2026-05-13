# ─────────────────────────────────────────────────────────────────────────────
# config.py — Your personal Jarvis settings
# Edit this file before running morning_brief.py
# ─────────────────────────────────────────────────────────────────────────────

# Your email address (Gmail)
# This is where the brief gets sent — and sent FROM
YOUR_EMAIL = "manavj0707@gmail.com"

# Your Anthropic API key
# Get one at: https://console.anthropic.com
# It looks like: sk-ant-api03-...
ANTHROPIC_API_KEY = "***REMOVED***"

# Your timezone
# Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE = "Australia/Sydney"

# How many hours back to look for emails
# 18 hours catches anything since yesterday afternoon
EMAIL_HOURS_BACK = 18

# Max emails to include in the brief
# More emails = longer prompt = slightly higher API cost
MAX_EMAILS = 15

# Your Hevy API Key
HEVY_API_KEY= "***REMOVED***"
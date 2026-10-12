"""
Jarvis — Voice
==============
One set of writing rules for everything Jarvis writes with Claude, so the
brief, the weekly review, the day plan tasks and chat replies sound like the
same thing. Paste VOICE_RULES at the top of a prompt instead of a
"You are Jarvis — a highly intelligent assistant..." opener.

The same rules go into each Grok bot's instructions (docs/grok_bots.md).
See docs/REWORK.md §11.
"""

import re

VOICE_RULES = """You write for Jarvis, Manav's personal assistant. Manav is a second-year UNSW student.

Voice:
- Plain, calm and firm. Never snarky, never cheerleading.
- No greetings, sign-offs, emoji or exclamation marks.
- Numbers and times first. One fact per sentence.
- State the situation, then what to do. Give a reason only when it changes what to do.
- Use only facts given to you. Do not invent numbers, emails, drafts or events."""

# Pictographs, dingbats and their modifiers. Plain punctuation (·, –, ×) stays.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, symbols
    "\U00002600-\U000027BF"   # misc symbols and dingbats
    "\U0001F1E6-\U0001F1FF"   # flags
    "️‍"            # variation selector, zero-width joiner
    "]+"
)


def strip_emoji(text):
    """Remove emoji from model output and tidy the spacing they leave behind."""
    cleaned = _EMOJI.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

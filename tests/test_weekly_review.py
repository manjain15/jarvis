"""Weekly review: data sections, the note call's parsing, and rendering. Never sends mail."""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import email_template
import weekly_review as wr

MON = datetime.date(2026, 10, 12)


def _day(i, gym=None, steps=None, planned=True):
    items = []
    if gym:
        items.append({"id": "gym", "kind": "gym", **gym})
    if steps is not None:
        items.append({"id": "steps", "kind": "steps", "count": steps, "target": 10_000})
    return (MON + datetime.timedelta(days=i), {"items": items, "planned": "x" if planned else None})


WEEK = [
    _day(0, {"title": "Pull", "status": "done", "logged_minutes": 52}, 12_100),
    _day(1, {"title": "Legs", "status": "missed"}, 8_200),
    _day(2, None, 10_400),
    _day(3, {"title": "Upper", "status": "excused", "excuse": "sick"}, 4_000),
    _day(4, {"title": "Sharms", "status": "partial", "logged_minutes": 30}, 0),
    (MON + datetime.timedelta(days=5), None),
]


def test_gym_section():
    rows = wr.gym_section(WEEK)
    assert rows[0] == {"text": "Mon · Pull", "right": "Done · 52 min", "sub": None, "miss": False}
    assert rows[1]["miss"] is True and rows[1]["right"] == "Missed"
    assert rows[2]["sub"] == "skip: sick"
    assert rows[-2] == {"text": "Sessions", "right": "2 / 4", "miss": True}
    assert rows[-1] == {"text": "Unexcused misses", "right": "1", "miss": True}


def test_unenforced_days_are_not_planned_sessions():
    rows = wr.gym_section([_day(0, {"title": "Push", "status": "unenforced"})])
    assert rows == [{"text": "Mon · Push", "right": "Not tracked", "sub": None, "miss": False}]


def test_steps_stats_ignore_days_without_readings():
    stats = wr.steps_stats(WEEK)
    assert stats[0] == {"value": "2/4", "label": "Days at target", "miss": True}
    assert stats[1]["value"] == "8,675"
    assert stats[2]["value"] == "4,000"
    assert wr.steps_stats([(MON, None)]) == []


def test_study_rows():
    targets = {"COMP3121": {"study": 8, "lectures": 4}, "SCIF1000": {"study": 0, "lectures": 0}}
    rows = wr.study_rows({("COMP3121", "study"): 420, ("COMP3121", "lecture"): 120}, targets, 6)
    assert rows == [
        {"text": "COMP3121 lectures", "right": "2 / 4h", "miss": True},
        {"text": "COMP3121 study", "right": "7 / 8h", "miss": False},
    ]


def test_money_rows():
    rows = wr.money_rows({"available": True, "total_spend": 92.4, "weekly_budget": 75},
                         {"available": True, "balance": 21000, "on_track": True,
                          "exchange_target": {"savings_goal": 35000}})
    assert rows[0] == {"text": "Spent this week", "right": "$92 / $75", "miss": True}
    assert rows[1]["right"] == "$21,000 / $35,000" and rows[1]["miss"] is False
    assert wr.money_rows({"available": False}, None) == []


def test_next_week_rows():
    rows = wr.next_week_rows([{"code": "MATH2931", "topics": ["Flexibility week"]},
                              {"code": "SCIF1000", "topics": []}])
    assert rows == [{"text": "MATH2931", "sub": "Flexibility week"}]


def test_parse_weekly():
    parsed = wr.parse_weekly('{"note": "Strong start, weak finish. ✅", "priorities": ["a", "b", "c", "d"]}')
    assert parsed == {"note": "Strong start, weak finish.", "priorities": ["a", "b", "c"]}
    assert wr.parse_weekly("nope") == {"note": "", "priorities": []}


def test_prompt_uses_the_shared_voice():
    from voice import VOICE_RULES
    prompt = wr.weekly_prompt("CONTEXT", "12 Oct – 18 Oct 2026")
    assert prompt.startswith(VOICE_RULES)
    assert "You are Jarvis" not in prompt
    assert '"priorities"' in prompt


def test_render_sections_leaves_out_empty_ones():
    page, text = email_template.render_sections(
        "Week 5 review", "12 Oct – 17 Oct · T3 2026", "Good week.",
        [{"label": "Gym", "rows": wr.gym_section(WEEK)},
         {"label": "Steps", "stats": []},
         {"label": "Upkeep", "rows": wr.UPKEEP_ROWS}],
        "jarvis · 07:05")
    assert "Gym" in page and "Upkeep" in page and "Steps" not in page
    assert "GYM\n  Mon · Pull  Done · 52 min" in text

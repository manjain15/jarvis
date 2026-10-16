"""Study-block style: COMP defaults to practice problems, other courses to notes."""

import datetime
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import day_plan
import term_context as tc


def test_default_style_is_practice_for_comp_prefixes():
    assert tc.study_style("COMP3121", {}) == "practice"
    assert tc.study_style("comp2511", {}) == "practice"
    assert tc.study_style("COMP3311", {}) == "practice"
    assert tc.study_style("MATH2931", {}) == "notes"
    assert tc.study_style("SCIF1000", {}) == "notes"
    assert tc.study_style("math2931", {}) == "notes"


def test_exact_course_beats_prefix_and_longest_prefix_wins():
    plan = {"study_styles": {
        "courses": {"COMP3121": "notes", "MATH2931": "practice"},
        "prefixes": {"COMP": "practice", "COMP9": "notes"},
    }}
    assert tc.study_style("COMP3121", plan) == "notes"
    assert tc.study_style("COMP3311", plan) == "practice"
    assert tc.study_style("COMP9001", plan) == "notes"
    assert tc.study_style("MATH2931", plan) == "practice"
    assert tc.study_style("SCIF1000", plan) == "notes"


def test_get_study_plan_fills_styles_when_the_file_omits_them(monkeypatch):
    monkeypatch.setattr(tc, "load_context", lambda: {"study_plan": {
        "targets": {"COMP3121": {"study": 8}},
    }})
    styles = tc.get_study_plan()["study_styles"]
    assert styles["default"] == "notes"
    assert styles["prefixes"]["COMP"] == "practice"
    assert tc.study_style("COMP3121") == "practice"
    assert tc.study_style("MATH2931") == "notes"


def test_partial_course_override_keeps_the_comp_prefix(monkeypatch):
    monkeypatch.setattr(tc, "load_context", lambda: {"study_plan": {
        "study_styles": {"courses": {"COMP3121": "Notes"}},
    }})
    assert tc.study_style("COMP3121") == "notes"
    assert tc.study_style("COMP3311") == "practice"
    assert tc.get_study_plan()["study_styles"]["prefixes"]["COMP"] == "practice"
    assert tc.get_study_plan()["daily_cap_hours"]["Monday"] == 4


def test_course_context_stamps_the_style(monkeypatch):
    import course_schedule
    import study_tracker

    monkeypatch.setattr(tc, "load_context", lambda: {})
    monkeypatch.setattr(course_schedule, "_current_week", lambda: 5)

    def topics(week, code):
        if code == "COMP3121":
            return [{"topics": [
                "Dynamic Programming I (overlapping subproblems)",
                "Dynamic Programming II (making change, knapsack)",
            ], "activities": ["Seminar: Dynamic Programming"]}]
        return [{"topics": ["Categorical Features"], "activities": []}]

    monkeypatch.setattr(course_schedule, "get_week_topics", topics)
    monkeypatch.setattr(study_tracker, "_load_assessments", lambda date: [])
    ctx = day_plan.course_context(
        datetime.date(2026, 10, 13), ["COMP3121", "MATH2931", "SCIF1000"])
    assert ctx["COMP3121"]["study_style"] == "practice"
    assert ctx["MATH2931"]["study_style"] == "notes"
    assert ctx["SCIF1000"]["study_style"] == "notes"
    assert "Dynamic Programming I" in ctx["COMP3121"]["notes"][0]

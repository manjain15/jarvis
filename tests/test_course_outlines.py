"""UNSW course outline parsing, refresh, and the Sunday tick."""

import datetime
import json

import pytest

import course_outlines as co

# Trimmed from the live T3 2026 outlines.
MATH = {
    "integrat_coursename": "Higher Linear Models",
    "integrat_lastpublishedon": "09 Sep 2026",
    "integrat_CO_Schedule": [
        {"integrat_content": "<p>Introduction to supervised learning; linear algebra review.</p>\n",
         "integrat_teachingweek": "Week 1 : 14 September - 20 September", "integrat_type": "Lecture"},
        {"integrat_content": "<p>Categorical Features and Nested Models.</p>\n",
         "integrat_teachingweek": "Week 5 : 12 October - 18 October", "integrat_type": "Lecture"},
        {"integrat_content": "<p>Flexibility week (no classes, tutorials or labs).</p>",
         "integrat_teachingweek": "Week 6 : 19 October - 25 October", "integrat_type": "Lecture"},
        {"integrat_content": "<p>Problem set 5</p>",
         "integrat_teachingweek": "Week 5 : 12 October - 18 October", "integrat_type": "Tutorial"},
    ],
    "integrat_CO_Assessment": [
        {"integrat_title": "In-classTest", "integrat_duedate": "Week 4", "integrat_weight": "20%"},
        {"integrat_title": "Assignment 2", "integrat_duedate": "Week 9", "integrat_weight": "20%"},
        {"integrat_title": "Final exam", "integrat_duedate": "Exam Period", "integrat_weight": "60%"},
    ],
}
COMP = {
    "integrat_coursename": "Algorithm Design and Analysis",
    "integrat_lastpublishedon": "17 Sep 2026",
    "integrat_CO_Schedule": [],
    "integrat_scheduleinfo": (
        "<p><strong>Week 4</strong><br>Lecture 7: cancelled due to public holiday (Labour Day)<br>"
        "Lecture 8: Greedy Algorithms IV (single source shortest paths, minimum spanning tree)<br>"
        "Seminar: Greedy Algorithms<br>Formatif: Greedy Algorithms</p>"
        "<p><strong>Week 5</strong><br>Lecture 9: Dynamic Programming I (overlapping subproblems)<br>"
        "Lecture 10: Dynamic Programming II (making change, knapsack)<br>"
        "Seminar: Dynamic Programming<br>Formatif: Dynamic Programming</p>"
        "<p><strong>Week 6</strong><br>No classes held or exercises released in flexibility week</p>"
        "<p><strong>Week 10</strong><br>Lecture 17: Intractable Problems II<br>Formatif: -<br>&nbsp;</p>"
    ),
    "integrat_CO_Assessment": [{"integrat_title": "Portfolio", "integrat_duedate": "", "integrat_weight": "60%"}],
}


def test_structured_schedule():
    weeks = co.parse_schedule(MATH)
    assert weeks[1]["topics"] == ["Introduction to supervised learning; linear algebra review"]
    assert weeks[5] == {"topics": ["Categorical Features and Nested Models"],
                        "activities": ["Tutorial: Problem set 5"]}
    assert weeks[6]["topics"] == ["Flexibility week"]


def test_free_text_schedule():
    weeks = co.parse_schedule(COMP)
    assert weeks[4]["topics"] == ["Greedy Algorithms IV (single source shortest paths, minimum spanning tree)"]
    assert weeks[5]["topics"] == ["Dynamic Programming I (overlapping subproblems)",
                                  "Dynamic Programming II (making change, knapsack)"]
    assert weeks[5]["activities"] == ["Seminar: Dynamic Programming", "Formatif: Dynamic Programming"]
    assert weeks[6]["topics"] == ["Flexibility week"]
    assert weeks[10]["activities"] == []


def test_assessments_attach_to_their_week():
    record = co.build_course("MATH2931", MATH, "T3 2026")
    assert record["name"] == "Higher Linear Models"
    assert record["published"] == "09 Sep 2026"
    by_week = {w["week"]: w["assessments"] for w in record["schedule"]}
    assert by_week[4] == ["In-classTest (20%)"]
    assert by_week[9] == ["Assignment 2 (20%)"]
    assert len(record["schedule"]) == 10


def test_term_params():
    assert co.term_params("T3 2026") == {"year": "2026", "term": "Term 3", "teachingPeriod": "T3"}
    with pytest.raises(ValueError):
        co.term_params("Summer 2026")


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "OUTLINES_DIR", tmp_path / "outlines")
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "outlines" / "_state.json")
    monkeypatch.setattr(co, "COURSES_DIR", tmp_path / "courses")
    return tmp_path


def test_refresh_writes_then_skips_unchanged(dirs):
    calls = []

    def get(url, params, timeout):
        calls.append(params)
        return _Response(200, MATH if params["courseCode"] == "MATH2931" else COMP)

    subjects = ("T3 2026", ["MATH2931", "COMP3121"])
    lines = co.refresh(get=get, subjects=subjects)
    assert lines[0].startswith("MATH2931: updated, 3/10 weeks with topics")
    assert calls[0]["term"] == "Term 3" and calls[0]["deliveryLocation"] == "Kensington"
    assert (dirs / "outlines" / "comp3121.json").exists()
    assert co.refresh(get=get, subjects=subjects)[0] == "MATH2931: unchanged (published 09 Sep 2026)"
    assert co.refresh(get=get, subjects=subjects, force=True)[0].startswith("MATH2931: updated")


def test_failed_fetch_keeps_the_saved_copy(dirs):
    co.refresh(get=lambda url, params, timeout: _Response(200, MATH), subjects=("T3 2026", ["MATH2931"]))
    saved = (dirs / "outlines" / "math2931.json").read_text()
    for bad in (_Response(500, {}), _Response(400, {"type": "ValidationError"})):
        lines = co.refresh(get=lambda url, params, timeout, r=bad: r, subjects=("T3 2026", ["MATH2931"]))
        assert lines == ["MATH2931: couldn't fetch, kept the saved copy"]
    assert (dirs / "outlines" / "math2931.json").read_text() == saved


def test_per_course_overrides(dirs):
    (dirs / "courses").mkdir()
    (dirs / "courses" / "math2931.json").write_text(json.dumps({"outline_params": {"deliveryMode": "Online"}}))
    seen = {}
    co.fetch_outline("MATH2931", "T3 2026",
                     get=lambda url, params, timeout: seen.update(params) or _Response(200, MATH))
    assert seen["deliveryMode"] == "Online"


def test_refresh_tick_runs_once_on_sunday(dirs, monkeypatch):
    runs = []
    monkeypatch.setattr(co, "refresh", lambda: runs.append(1) or [])
    tz = datetime.timezone(datetime.timedelta(hours=11))
    saturday = datetime.datetime(2026, 10, 17, 9, 0, tzinfo=tz)
    sunday_early = datetime.datetime(2026, 10, 18, 3, 0, tzinfo=tz)
    sunday = datetime.datetime(2026, 10, 18, 4, 30, tzinfo=tz)
    co.refresh_tick(saturday)
    co.refresh_tick(sunday_early)
    assert runs == []

    def fake_refresh():
        runs.append(1)
        co.atomic_write_json(co.STATE_FILE, {"last_run": "2026-10-18"})
        return []

    monkeypatch.setattr(co, "refresh", fake_refresh)
    co.refresh_tick(sunday)
    co.refresh_tick(sunday + datetime.timedelta(hours=1))
    assert runs == [1]

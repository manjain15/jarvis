"""course_schedule loader — week/course filtering and the week-0 empty case."""

import json
import course_schedule as cs


def _seed(tmp_path, monkeypatch, *courses):
    """Point course_schedule at a temp courses/ dir holding the given course dicts."""
    cdir = tmp_path / "courses"
    cdir.mkdir()
    for c in courses:
        (cdir / f"{c['code'].lower()}.json").write_text(json.dumps(c))
    monkeypatch.setattr(cs, "COURSES_DIR", cdir)


_COMP = {"code": "COMP2511", "name": "SD&A",
         "schedule": [{"week": 1, "topics": ["Intro"], "assessments": []},
                      {"week": 5, "topics": ["Patterns"], "assessments": ["Asg1 due"]}]}
_MATH = {"code": "MATH2601", "name": "HLA",
         "schedule": [{"week": 1, "topics": ["Groups"], "assessments": []}]}


def test_load_courses(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP, _MATH)
    codes = sorted(c["code"] for c in cs.load_courses())
    assert codes == ["COMP2511", "MATH2601"]


def test_get_week_topics_all_courses(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP, _MATH)
    rows = cs.get_week_topics(1)
    assert {r["code"] for r in rows} == {"COMP2511", "MATH2601"}


def test_get_week_topics_filtered_by_course(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP, _MATH)
    rows = cs.get_week_topics(1, course="COMP2511")
    assert len(rows) == 1 and rows[0]["code"] == "COMP2511"


def test_get_week_topics_carries_assessments(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP)
    rows = cs.get_week_topics(5)
    assert rows[0]["assessments"] == ["Asg1 due"]


def test_get_week_topics_empty_for_unknown_week(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP)
    assert cs.get_week_topics(99) == []


def test_current_week_summary_empty_when_no_week(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP)
    monkeypatch.setattr(cs, "_current_week", lambda: None)  # before term / unavailable
    assert cs.get_current_week_summary() == ""


def test_current_week_summary_renders(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _COMP)
    monkeypatch.setattr(cs, "_current_week", lambda: 5)
    out = cs.get_current_week_summary()
    assert "Week 5" in out and "Patterns" in out and "COMP2511" in out

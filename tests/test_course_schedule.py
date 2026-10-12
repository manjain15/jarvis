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
    monkeypatch.setattr(cs, "OUTLINES_DIR", tmp_path / "no-outlines")


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


def test_outline_weeks_fill_in_and_manual_weeks_win(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, {
        "code": "MATH2931", "name": "HLM", "lectures": None,
        "schedule": [{"week": 4, "topics": ["Hand-written W4"], "assessments": ["ICT due Wed 7 Oct"]},
                     {"week": 5, "topics": [], "assessments": []}]})
    odir = tmp_path / "outlines"
    odir.mkdir()
    (odir / "math2931.json").write_text(json.dumps({
        "code": "MATH2931", "name": "Higher Linear Models",
        "schedule": [{"week": 4, "topics": ["PRESS, GCV"], "activities": [], "assessments": ["In-class Test (20%)"]},
                     {"week": 5, "topics": ["Categorical Features"], "activities": ["Tutorial: W5"], "assessments": []}]}))
    (odir / "_state.json").write_text("{}")
    monkeypatch.setattr(cs, "OUTLINES_DIR", odir)

    w4 = cs.get_week_topics(4, course="MATH2931")[0]
    assert w4["topics"] == ["Hand-written W4"]
    assert w4["assessments"] == ["In-class Test (20%)", "ICT due Wed 7 Oct"]
    w5 = cs.get_week_topics(5, course="MATH2931")[0]
    assert w5["topics"] == ["Categorical Features"]
    assert w5["activities"] == ["Tutorial: W5"]
    assert w5["name"] == "HLM"

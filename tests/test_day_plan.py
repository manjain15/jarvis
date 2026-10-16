"""Day plan: sizing, placement, gym slot, calendar writes, and block checks."""

import datetime
import json
import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import day_plan
import enforcer
import ledger

MONDAY = datetime.date(2026, 10, 12)
TUESDAY = MONDAY + datetime.timedelta(days=1)
WEDNESDAY = MONDAY + datetime.timedelta(days=2)

PLAN = {
    "targets": {
        "COMP3121": {"study": 8, "lectures": 4},
        "MATH2931": {"study": 7, "lectures": 4},
        "SCIF1000": {"study": 0, "lectures": 0},
    },
    "routine": {
        "Monday": [{"start": "09:30", "end": "17:30", "label": "Work"}],
        "Wednesday": [{"start": "09:30", "end": "14:30", "label": "COMP3121 lab + travel"}],
        "Thursday": [{"start": "13:30", "end": "18:30", "label": "SCIF1000 tut + travel"}],
        "Friday": [{"start": "09:30", "end": "14:30", "label": "COMP3121 seminar + travel"}],
    },
    "daily_cap_hours": {"Monday": 1.5, "Tuesday": 4, "Wednesday": 3.5, "Thursday": 3.5,
                        "Friday": 3.5, "Saturday": 5, "Sunday": 5},
}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    splits = {TUESDAY: "Legs", WEDNESDAY: "Rest"}
    monkeypatch.setattr(ledger, "_split_label", lambda d: splits.get(d, "Push"))
    monkeypatch.setattr(ledger, "DEFAULT_GYM_SLOT", "06:30")
    monkeypatch.setattr(enforcer, "TRAVEL_MINUTES", 5)
    monkeypatch.setattr(day_plan, "load_study_plan", lambda: PLAN)
    monkeypatch.setattr(day_plan, "assessment_boosts", lambda date, courses: {})
    monkeypatch.setattr(day_plan, "course_context", lambda date, courses: {c: {"week": 5, "notes": []} for c in courses})
    monkeypatch.setattr(day_plan, "ANTHROPIC_AVAILABLE", False)
    monkeypatch.setattr(day_plan, "_last_attempt", {})
    monkeypatch.setattr(day_plan, "area_pick", lambda date: None)  # area tests pass area_fn
    monkeypatch.setattr(day_plan, "timetable_lectures", fake_lectures)
    import course_schedule
    import term_context
    # Lecture labels read the term week from gitignored term_context.json.
    monkeypatch.setattr(course_schedule, "_current_week", lambda: 5)
    monkeypatch.setattr(term_context, "get_workout_schedule", lambda: dict(term_context.DEFAULT_WORKOUT_SCHEDULE))


# The real T3 pattern: COMP3121 Mon + Thu 14-16, MATH2931 Tue + Thu 11-13.
LECTURES = {"COMP3121": ((0, 14), (3, 14)), "MATH2931": ((1, 11), (3, 11))}


def fake_lectures(code, start, end):
    out = []
    for weekday, hour in LECTURES.get(code, ()):
        day = start + datetime.timedelta(days=(weekday - start.weekday()) % 7)
        if start <= day <= end:
            out.append((at(hour, 0, day), at(hour + 2, 0, day)))
    return sorted(out)


def at(hour, minute=0, date=TUESDAY):
    return ledger.TIMEZONE.localize(datetime.datetime.combine(date, datetime.time(hour, minute)))


class FakeCalendar:
    def __init__(self):
        self.created, self.deleted = [], []

    def create(self, title, start, end, description, date_str):
        self.created.append((title, start, end, description))
        return f"ev{len(self.created)}"

    def delete_all(self, date_str):
        self.deleted.append(date_str)

    def delete(self, event_id):
        self.deleted.append(event_id)


def plain(placed, context, carry, labels=None):
    return day_plan.write_tasks(placed, context, carry, labels)


# ── Sizing ───────────────────────────────────────────────────────────────────

def test_to_blocks():
    assert day_plan.to_blocks(30) == []
    assert day_plan.to_blocks(50) == [60]
    assert day_plan.to_blocks(80) == [90]
    assert day_plan.to_blocks(150) == [90, 60]
    assert day_plan.to_blocks(200) == [90, 90]


def test_tuesday_allocation_fits_the_cap_and_front_loads_lectures():
    remaining = day_plan.remaining_minutes(TUESDAY, PLAN["targets"])
    assert remaining == {("COMP3121", "study"): 480, ("COMP3121", "lecture"): 240,
                         ("MATH2931", "study"): 420, ("MATH2931", "lecture"): 240}
    alloc = day_plan.allocate(remaining, TUESDAY, PLAN["daily_cap_hours"], {})
    assert sum(alloc.values()) <= 240
    # Tuesday has 4 of the 28.5 cap-hours left this week (~14%); lectures get 1.5x.
    assert alloc[("COMP3121", "lecture")] > alloc[("COMP3121", "study")] * 240 / 480


def test_done_and_partial_reduce_what_is_left():
    ledger.update_day(MONDAY, lambda d: d["items"].extend([
        {"kind": "study", "course": "MATH2931", "type": "lecture", "minutes": 90, "status": "done"},
        {"kind": "study", "course": "MATH2931", "type": "study", "minutes": 60, "status": "partial"},
        {"kind": "study", "course": "COMP3121", "type": "study", "minutes": 60, "status": "no"},
    ]))
    remaining = day_plan.remaining_minutes(TUESDAY, PLAN["targets"])
    assert remaining[("MATH2931", "lecture")] == 150
    assert remaining[("MATH2931", "study")] == 390
    assert remaining[("COMP3121", "study")] == 480


def test_lecture_backlog_counts_only_planned_days():
    last_monday = MONDAY - datetime.timedelta(days=7)
    assert day_plan.lecture_backlog(TUESDAY, PLAN["targets"]) == {}
    # One planned day last week with nothing watched: 1/7 of the lecture target owed.
    ledger.update_day(last_monday, lambda d: d.update(planned="2026-10-05T05:30:00+11:00"))
    assert day_plan.lecture_backlog(TUESDAY, PLAN["targets"]) == {"COMP3121": 34, "MATH2931": 34}


def test_boost_shifts_time_to_the_course_with_an_assessment():
    remaining = day_plan.remaining_minutes(TUESDAY, PLAN["targets"])
    plain_alloc = day_plan.allocate(remaining, TUESDAY, PLAN["daily_cap_hours"], {})
    boosted = day_plan.allocate(remaining, TUESDAY, PLAN["daily_cap_hours"], {"MATH2931": 2.0})
    assert boosted[("MATH2931", "study")] > plain_alloc[("MATH2931", "study")]


def test_last_day_of_the_week_takes_everything_left_up_to_the_cap():
    saturday = MONDAY + datetime.timedelta(days=5)   # the week runs Sunday to Saturday
    alloc = day_plan.allocate({("MATH2931", "study"): 120}, saturday, PLAN["daily_cap_hours"], {})
    assert alloc == {("MATH2931", "study"): 120}


def test_sunday_starts_a_new_week():
    sunday = MONDAY + datetime.timedelta(days=6)
    ledger.update_day(MONDAY, lambda d: d["items"].append(
        {"kind": "study", "course": "MATH2931", "type": "study", "minutes": 120, "status": "done"}))
    assert day_plan.remaining_minutes(sunday, PLAN["targets"])[("MATH2931", "study")] == 420
    alloc = day_plan.allocate({("MATH2931", "study"): 420}, sunday, PLAN["daily_cap_hours"], {})
    assert alloc[("MATH2931", "study")] < 420


def test_zero_cap_day_gets_nothing():
    caps = dict(PLAN["daily_cap_hours"], Tuesday=0)
    assert day_plan.allocate({("MATH2931", "study"): 120}, TUESDAY, caps, {}) == {}


# ── Placement ────────────────────────────────────────────────────────────────

def test_free_gaps_respect_buffers_and_day_bounds():
    busy = [(at(10), at(11)), (at(13), at(14))]
    gaps = day_plan.free_gaps(busy, TUESDAY, at(5, 45))
    assert gaps == [(at(8), at(9, 45)), (at(11, 15), at(12, 45)), (at(14, 15), at(21))]


def test_place_drops_what_does_not_fit():
    gaps = [(at(8), at(9, 45))]
    placed = day_plan.place([("MATH2931", "lecture", 60), ("COMP3121", "lecture", 60)], gaps)
    assert placed == [("MATH2931", "lecture", at(8), at(9))]


def test_requested_blocks_put_lectures_first_and_interleave():
    alloc = {("COMP3121", "study"): 150, ("MATH2931", "study"): 60, ("MATH2931", "lecture"): 60}
    assert day_plan.requested_blocks(alloc) == [
        ("MATH2931", "lecture", 60), ("COMP3121", "study", 90),
        ("MATH2931", "study", 60), ("COMP3121", "study", 60)]


def test_gym_slot_moves_only_on_a_clash():
    gym = {"slot": "06:30", "minutes": 45}
    assert day_plan.choose_gym_slot(gym, TUESDAY, [], 5) == "06:30"
    clash = [(at(7, 0), at(8, 0))]
    assert day_plan.choose_gym_slot(gym, TUESDAY, clash, 5) == "08:15"


def test_routine_blocks_the_day():
    assert day_plan.routine_busy(PLAN, WEDNESDAY) == [(at(9, 30, WEDNESDAY), at(14, 30, WEDNESDAY))]
    assert day_plan.routine_busy(PLAN, TUESDAY) == []


# ── Build ────────────────────────────────────────────────────────────────────

def test_build_writes_calendar_and_ledger():
    cal = FakeCalendar()
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=cal)
    assert items, "Tuesday should get study blocks"
    assert cal.deleted == ["2026-10-13"]
    assert len(cal.created) == len(items)
    assert all(i["event_id"] for i in items)
    total = sum(i["minutes"] for i in items)
    assert total <= 240
    stored = ledger.load_day(TUESDAY)
    assert stored["planned"]
    gym = ledger.find_item(stored, "gym")
    assert gym["base_minutes"] == 45
    assert gym["minutes"] == 45
    # Gym period is 06:25–07:50 (45 min + 5 travel + 30 after). The 15-min
    # buffer puts the first study gap at 08:05.
    for i in items:
        assert datetime.datetime.fromisoformat(i["start"]) >= at(8, 5)
    assert [i["type"] for i in items][0] == "lecture"


def test_build_plans_today_from_reconciled_step_debt(monkeypatch):
    """A Health total that lands after close clears yesterday's debt before today is built."""
    def _close(day):
        day["closed"] = True
        item = ledger.find_item(day, "steps")
        item["count"] = 7_140
        item["evidence"] = "shortcut"
        day["debt"]["steps"] = 2_860
        day["events"].append({"ts": "2026-10-13T21:00:00+11:00", "type": "steps", "value": 7_140})

    ledger.update_day(TUESDAY, _close)
    monkeypatch.setattr(ledger, "pull_health_steps", lambda date, max_age_s=0: 13_000)
    day_plan.build_plan(now=at(5, 30, WEDNESDAY), busy_fn=lambda: [],
                        tasks_fn=plain, calendar=FakeCalendar())
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 0
    assert ledger.find_item(ledger.load_day(TUESDAY), "steps")["count"] == 13_000
    assert ledger.find_item(ledger.load_day(TUESDAY), "steps")["evidence"] == "shortcut+google_health"
    assert ledger.find_item(ledger.load_day(WEDNESDAY), "steps")["target"] == 10_000


def test_build_keeps_step_debt_when_health_is_missing(monkeypatch):
    def _close(day):
        day["closed"] = True
        item = ledger.find_item(day, "steps")
        item["count"] = 7_140
        day["debt"]["steps"] = 2_860

    ledger.update_day(TUESDAY, _close)
    monkeypatch.setattr(ledger, "pull_health_steps", lambda date, max_age_s=0: None)
    day_plan.build_plan(now=at(5, 30, WEDNESDAY), busy_fn=lambda: [],
                        tasks_fn=plain, calendar=FakeCalendar())
    assert ledger.load_day(TUESDAY)["debt"]["steps"] == 2_860
    assert ledger.find_item(ledger.load_day(WEDNESDAY), "steps")["target"] == 12_860


def test_build_survives_a_step_reconcile_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("health down")
    monkeypatch.setattr(ledger, "reconcile_steps", boom)
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    assert items
    assert ledger.load_day(TUESDAY)["planned"]


def test_dry_run_does_not_reconcile_steps(monkeypatch):
    called = []
    monkeypatch.setattr(ledger, "reconcile_steps", lambda *a, **k: called.append(a))
    day_plan.build_plan(now=at(5, 30), dry_run=True, busy_fn=lambda: [],
                        tasks_fn=plain, calendar=FakeCalendar())
    assert called == []


def test_dry_run_writes_nothing():
    cal = FakeCalendar()
    result = day_plan.build_plan(now=at(5, 30), dry_run=True, busy_fn=lambda: [],
                                 tasks_fn=plain, calendar=cal)
    assert result["items"]
    assert cal.created == [] and cal.deleted == []
    assert ledger.load_day(TUESDAY) is None


def test_build_moves_the_gym_on_a_clash():
    cal = FakeCalendar()
    day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [(at(6, 0), at(7, 30))],
                        tasks_fn=plain, calendar=cal)
    gym = ledger.find_item(ledger.load_day(TUESDAY), "gym")
    assert gym["slot"] == "07:45"
    assert gym["moved_from"] == "06:30"


def test_wednesday_blocks_avoid_the_routine():
    cal = FakeCalendar()
    items = day_plan.build_plan(now=at(5, 30, WEDNESDAY), busy_fn=lambda: [],
                                tasks_fn=plain, calendar=cal)
    for i in items:
        start = datetime.datetime.fromisoformat(i["start"])
        end = datetime.datetime.fromisoformat(i["end"])
        assert end <= at(9, 15, WEDNESDAY) or start >= at(14, 45, WEDNESDAY)


def test_unreadable_calendar_does_not_plan():
    with pytest.raises(RuntimeError):
        day_plan.build_plan(now=at(5, 30), busy_fn=lambda: None, tasks_fn=plain, calendar=FakeCalendar())


def test_leftovers_become_tasks():
    ledger.update_day(MONDAY, lambda d: d["items"].append(
        {"kind": "study", "course": "MATH2931", "type": "lecture", "task": "W5 lecture 2",
         "minutes": 60, "status": "partial"}))
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    math_lecture = [i for i in items if i["course"] == "MATH2931" and i["type"] == "lecture"]
    assert math_lecture[0]["task"] == "Finish: W5 lecture 2"


def test_claude_failure_falls_back(monkeypatch):
    monkeypatch.setattr(day_plan, "ANTHROPIC_AVAILABLE", True)

    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("no network")

    monkeypatch.setattr(day_plan.anthropic, "Anthropic", Broken)
    placed = [("MATH2931", "study", at(9), at(10))]
    assert day_plan.write_tasks(placed, {"MATH2931": {"week": 5, "notes": []}}, []) == ["W5 problems"]


# ── Tick ─────────────────────────────────────────────────────────────────────

def test_tick_builds_once_in_the_window_and_retries():
    calls = []

    def build(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("calendar down")
        ledger.update_day(now.date(), lambda d: d.update(planned=now.isoformat()))

    day_plan.tick(at(5, 0), build=build)
    assert calls == []
    day_plan.tick(at(5, 30), build=build)
    day_plan.tick(at(5, 45), build=build)
    assert len(calls) == 1
    day_plan.tick(at(6, 0), build=build)
    day_plan.tick(at(6, 30), build=build)
    assert len(calls) == 2
    day_plan.tick(at(12, 0, WEDNESDAY), build=build)
    assert len(calls) == 2


# ── Block checks ─────────────────────────────────────────────────────────────

def _blocks(*specs):
    def _add(d):
        for n, (course, kind, start, end) in enumerate(specs, 1):
            d["items"].append({"id": f"study-{n}", "kind": "study", "type": kind, "course": course,
                               "task": f"task {n}", "start": start.isoformat(), "end": end.isoformat(),
                               "minutes": 60, "status": "pending", "asked_at": None})
    ledger.update_day(TUESDAY, _add)


def test_one_question_at_a_time():
    _blocks(("MATH2931", "lecture", at(8), at(9)), ("COMP3121", "study", at(9, 15), at(10, 15)))
    assert day_plan.check_tick(at(8, 59)) == []
    assert day_plan.check_tick(at(10, 20)) == ["MATH2931 · task 1. Watched? done / partial / no"]
    assert day_plan.check_tick(at(10, 21)) == []
    assert day_plan.handle_reply("partial", now=at(10, 22)) == "Partial. The rest rolls to tomorrow."
    assert day_plan.check_tick(at(10, 23)) == ["COMP3121 · task 2. Done? done / partial / no"]
    assert day_plan.handle_reply("Done.", now=at(10, 24)) == "Logged."
    assert day_plan.handle_reply("done", now=at(10, 25)) is None
    statuses = [i["status"] for i in ledger.load_day(TUESDAY)["items"] if i.get("kind") == "study"]
    assert statuses == ["partial", "done"]


def test_no_reply_after_an_hour_moves_on():
    _blocks(("MATH2931", "lecture", at(8), at(9)), ("COMP3121", "study", at(9, 15), at(10, 15)))
    day_plan.check_tick(at(9, 0))
    assert day_plan.check_tick(at(10, 16)) == ["COMP3121 · task 2. Done? done / partial / no"]
    first = ledger.load_day(TUESDAY)["items"][-2]
    assert first["status"] == "no_reply"


def test_stale_blocks_are_not_asked():
    _blocks(("MATH2931", "lecture", at(8), at(9)))
    assert day_plan.check_tick(at(13, 0)) == []
    assert ledger.load_day(TUESDAY)["items"][-1]["status"] == "no_reply"


def test_replies_are_ignored_without_a_question():
    _blocks(("MATH2931", "lecture", at(8), at(9)))
    assert day_plan.handle_reply("no", now=at(8, 30)) is None
    assert day_plan.handle_reply("what's next", now=at(9, 5)) is None


def test_fit_cap_never_exceeds_the_cap():
    blocks = [("A", "lecture", 60), ("B", "lecture", 60), ("A", "study", 90), ("B", "study", 60)]
    assert day_plan.fit_cap(blocks, 210) == [("A", "lecture", 60), ("B", "lecture", 60), ("A", "study", 90)]
    assert day_plan.fit_cap(blocks, 200) == [("A", "lecture", 60), ("B", "lecture", 60), ("A", "study", 60)]
    assert day_plan.fit_cap(blocks, 0) == []


# ── Weekly checks ────────────────────────────────────────────────────────────

THURSDAY = MONDAY + datetime.timedelta(days=3)
CHECKS = [
    {"id": "scif1000-pre", "label": "SCIF1000 pre-class activities", "day": "Thursday",
     "ask": "11:00", "followup": "12:45", "requires_class": "SCIF1000"},
    {"id": "scif1000-post", "label": "SCIF1000 post-class activities", "day": "Thursday",
     "ask": "19:30", "followup": "20:45", "requires_class": "SCIF1000"},
]


def _thursday_checks(monkeypatch, has_class=True):
    monkeypatch.setattr(day_plan, "load_study_plan", lambda: dict(PLAN, weekly_checks=CHECKS))
    day_plan.build_plan(now=at(5, 30, THURSDAY), busy_fn=lambda: [], tasks_fn=plain,
                        calendar=FakeCalendar(), class_fn=lambda d, c: has_class)


def _checks():
    return [i for i in ledger.load_day(THURSDAY)["items"] if i.get("kind") == "check"]


def test_checks_only_on_their_day_and_with_a_class(monkeypatch):
    plan = dict(PLAN, weekly_checks=CHECKS)
    assert day_plan.weekly_check_items(plan, TUESDAY, class_fn=lambda d, c: True) == []
    assert len(day_plan.weekly_check_items(plan, THURSDAY, class_fn=lambda d, c: True)) == 2
    assert day_plan.weekly_check_items(plan, THURSDAY, class_fn=lambda d, c: False) == []


def test_check_done_first_time(monkeypatch):
    _thursday_checks(monkeypatch)
    # Study blocks end earlier; answer those so the check is next in line.
    for item in ledger.load_day(THURSDAY)["items"]:
        if item.get("kind") == "study":
            ledger.update_day(THURSDAY, lambda d, i=item["id"]: [x.update(status="done") for x in d["items"] if x.get("id") == i])
    assert day_plan.check_tick(at(11, 0, THURSDAY)) == ["SCIF1000 pre-class activities. Done? done / no"]
    assert day_plan.handle_reply("done", now=at(11, 2, THURSDAY)) == "Logged."
    assert day_plan.check_tick(at(12, 45, THURSDAY)) == []
    assert _checks()[0]["status"] == "done"


def test_check_no_gets_one_followup(monkeypatch):
    _thursday_checks(monkeypatch)
    ledger.update_day(THURSDAY, lambda d: [x.update(status="done") for x in d["items"] if x.get("kind") == "study"])
    day_plan.check_tick(at(11, 0, THURSDAY))
    assert day_plan.handle_reply("no", now=at(11, 1, THURSDAY)) == "Asking again at 12:45."
    assert day_plan.check_tick(at(12, 0, THURSDAY)) == []
    assert day_plan.check_tick(at(12, 45, THURSDAY)) == ["Still open: SCIF1000 pre-class activities. Done? done / no"]
    assert day_plan.handle_reply("partial", now=at(12, 50, THURSDAY)) == "Logged as not done."
    assert day_plan.check_tick(at(14, 0, THURSDAY)) == []


def test_unanswered_check_still_gets_its_followup(monkeypatch):
    _thursday_checks(monkeypatch)
    ledger.update_day(THURSDAY, lambda d: [x.update(status="done") for x in d["items"] if x.get("kind") == "study"])
    day_plan.check_tick(at(19, 30, THURSDAY))
    assert day_plan.check_tick(at(20, 45, THURSDAY)) == ["Still open: SCIF1000 post-class activities. Done? done / no"]


def test_replan_keeps_one_set_of_checks(monkeypatch):
    _thursday_checks(monkeypatch)
    _thursday_checks(monkeypatch)
    assert [c["id"] for c in _checks()] == ["scif1000-pre", "scif1000-post"]



# ── Area block ───────────────────────────────────────────────────────────────

AREA = {"name": "project", "label": "Passion project", "next_action": "Write the one-page concept"}


def test_area_block_follows_the_study_blocks_outside_the_cap(monkeypatch):
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain,
                                calendar=FakeCalendar(), area_fn=lambda d: AREA)
    study = [i for i in items if i["kind"] == "study"]
    area = [i for i in items if i["kind"] == "area"]
    assert sum(i["minutes"] for i in study) <= 240
    assert len(area) == 1 and area[0]["minutes"] == 45
    assert area[0]["title"] == "Passion project" and area[0]["task"] == "Write the one-page concept"
    a_start = datetime.datetime.fromisoformat(area[0]["start"])
    a_end = datetime.datetime.fromisoformat(area[0]["end"])
    for i in study:   # fits a free gap without overlapping any study block
        assert a_end <= datetime.datetime.fromisoformat(i["start"]) or a_start >= datetime.datetime.fromisoformat(i["end"])
    stored = [i for i in ledger.load_day(TUESDAY)["items"] if i.get("kind") == "area"]
    assert len(stored) == 1


def test_area_block_needs_room():
    gaps = [(at(8), at(9))]
    placed = [("MATH2931", "lecture", at(8), at(9))]
    assert day_plan.place_area_block(AREA, gaps, placed) is None
    assert day_plan.place_area_block(AREA, [(at(8), at(10))], placed) == (at(9, 15), at(10, 0))


def test_area_block_check_touches_the_area(monkeypatch, tmp_path):
    import areas
    monkeypatch.setattr(areas, "AREAS_FILE", tmp_path / "areas.json")
    day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain,
                        calendar=FakeCalendar(), area_fn=lambda d: AREA)
    ledger.update_day(TUESDAY, lambda d: [x.update(status="done") for x in d["items"] if x.get("kind") == "study"])
    area = [i for i in ledger.load_day(TUESDAY)["items"] if i.get("kind") == "area"][0]
    end = datetime.datetime.fromisoformat(area["end"])
    assert day_plan.check_tick(end) == ["Passion project · Write the one-page concept. Done? done / partial / no"]
    assert day_plan.handle_reply("partial", now=end) == "Logged. It stays the push until it moves."
    assert areas.load()["project"]["last_touched"] == "2026-10-13"


def test_check_in_leftover_replaces_the_task():
    ledger.update_day(MONDAY, lambda d: d["items"].append(
        {"kind": "study", "course": "MATH2931", "type": "study", "task": "W5 tutorial Q1-6",
         "left": "Q5-6", "minutes": 60, "status": "partial"}))
    assert day_plan.leftovers(TUESDAY) == [("MATH2931", "study", "Q5-6", "partial")]


# ── Lecture recordings ───────────────────────────────────────────────────────

def _lecture_items(items, course=None):
    return [i for i in items if i["kind"] == "study" and i["type"] == "lecture"
            and (course is None or i["course"] == course)]


def test_lecture_blocks_wait_for_the_lecture():
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    math = _lecture_items(items, "MATH2931")
    comp = _lecture_items(items, "COMP3121")
    # MATH2931's Tuesday lecture ends 13:00; COMP3121's Monday one is already out.
    assert math and all(datetime.datetime.fromisoformat(i["start"]) >= at(13) for i in math)
    assert comp and datetime.datetime.fromisoformat(comp[0]["start"]) < at(13)
    # Only Monday's 2h COMP3121 lecture exists so far; Thursday's isn't planned.
    assert sum(i["minutes"] for i in comp) <= 120


def test_lecture_tasks_name_the_lecture():
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    tasks = {i["course"]: i["task"] for i in _lecture_items(items)}
    assert tasks["COMP3121"] == "Watch W5 lecture 1 (Mon 14:00)"
    assert tasks["MATH2931"] == "Watch W5 lecture 1 (Tue 11:00)"


def test_no_lecture_blocks_before_any_lecture_has_happened():
    sunday = TUESDAY - datetime.timedelta(days=2)
    items = day_plan.build_plan(now=at(5, 30, sunday), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    assert _lecture_items(items) == []


def test_watched_lectures_use_up_the_supply():
    ledger.update_day(MONDAY, lambda d: d["items"].append(
        {"kind": "study", "course": "COMP3121", "type": "lecture", "minutes": 120, "status": "done"}))
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    assert _lecture_items(items, "COMP3121") == []


def test_unreadable_timetable_falls_back_to_unconstrained(monkeypatch):
    def broken(code, start, end):
        raise RuntimeError("feed down")
    monkeypatch.setattr(day_plan, "timetable_lectures", broken)
    assert day_plan.lecture_supply(TUESDAY, PLAN["targets"]) is None


def test_study_tasks_that_are_lecture_watching_are_replaced(monkeypatch):
    monkeypatch.setattr(day_plan, "ANTHROPIC_AVAILABLE", True)

    class Msg:
        content = [type("B", (), {"text": '["Watch W5 lecture 2", "Finish Q5-6"]'})()]

    class Client:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            return Msg()

    monkeypatch.setattr(day_plan.anthropic, "Anthropic", Client)
    placed = [("COMP3121", "study", at(9), at(10)), ("MATH2931", "study", at(10, 15), at(11, 15))]
    tasks = day_plan.write_tasks(placed, {"COMP3121": {"week": 5, "notes": []}, "MATH2931": {"week": 5, "notes": []}}, [])
    assert tasks == ["COMP3121: W5 practice problems (60 min)", "Finish Q5-6"]


# ── COMP practice vs notes review ────────────────────────────────────────────

DP_NOTES = (
    "Week 5 topics: Dynamic Programming I (overlapping subproblems); "
    "Dynamic Programming II (making change, knapsack)"
)
PRACTICE = "COMP3121: W5 Dynamic Programming practice problems (60 min)"


def _claude(monkeypatch, replies, seen=None):
    """Stub the day-plan Claude call. replies is a list of task strings."""
    monkeypatch.setattr(day_plan, "ANTHROPIC_AVAILABLE", True)
    payload = json.dumps(replies)

    class Msg:
        content = [type("B", (), {"text": payload})()]

    class Client:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            if seen is not None:
                seen["prompt"] = kw["messages"][0]["content"]
            return Msg()

    monkeypatch.setattr(day_plan.anthropic, "Anthropic", Client)


def test_fallback_comp_is_practice_and_other_courses_stay_notes():
    placed = [
        ("COMP3121", "study", at(9), at(10)),
        ("MATH2931", "study", at(10, 15), at(11, 15)),
        ("SCIF1000", "study", at(11, 30), at(12, 30)),
        ("COMP3121", "lecture", at(14), at(15)),
        ("COMP2511", "study", at(15, 15), at(16, 45)),
    ]
    context = {
        "COMP3121": {"week": 5, "notes": [DP_NOTES]},
        "MATH2931": {"week": 5, "notes": ["Week 5 topics: Categorical Features"]},
        "SCIF1000": {"week": 5, "notes": ["Week 5 topics: Scientific method"]},
        "COMP2511": {"week": 3, "notes": ["Week 3 topics: Design patterns I (intro)"]},
    }
    tasks = day_plan.write_tasks(placed, context, [], labels={3: "W5 lecture 1 (Mon 14:00)"})
    assert tasks == [
        PRACTICE,
        "W5 problems",
        "W5 problems",
        "Watch W5 lecture 1 (Mon 14:00)",
        "COMP2511: W3 Design patterns practice problems (90 min)",
    ]


def test_comp_due_assessment_still_wins_over_a_generic_problem_set():
    context = {"COMP3121": {"week": 5, "notes": [
        DP_NOTES, "Assignment 2 due Fri 16 Oct (3d, 20%)",
    ]}}
    tasks = day_plan.write_tasks([("COMP3121", "study", at(9), at(10))], context, [])
    assert tasks == ["Assignment 2"]


def test_comp_note_review_leftover_becomes_practice():
    context = {"COMP3121": {"week": 5, "notes": [DP_NOTES]}}
    placed = [("COMP3121", "study", at(9), at(10))]
    review = [("COMP3121", "study", "Review W5 Dynamic Programming I notes", "partial")]
    assert day_plan.write_tasks(placed, context, review) == [PRACTICE]
    kept = [("COMP3121", "study", "Knapsack Q3-4", "partial")]
    assert day_plan.write_tasks(placed, context, kept) == ["Finish: Knapsack Q3-4"]
    math = [("MATH2931", "study", "Review W5 tutorial notes", "partial")]
    assert day_plan.write_tasks(
        [("MATH2931", "study", at(10), at(11))],
        {"MATH2931": {"week": 5, "notes": ["Week 5 topics: Categorical Features"]}},
        math,
    ) == ["Finish: Review W5 tutorial notes"]


def test_study_style_override_changes_the_task(monkeypatch):
    monkeypatch.setattr(day_plan, "load_study_plan", lambda: {
        **PLAN,
        "study_styles": {"courses": {"COMP3121": "notes"}, "prefixes": {"MATH": "practice"}},
    })
    placed = [
        ("COMP3121", "study", at(9), at(10)),
        ("MATH2931", "study", at(10), at(11)),
    ]
    context = {
        "COMP3121": {"week": 5, "notes": [DP_NOTES]},
        "MATH2931": {"week": 5, "notes": ["Week 5 topics: Categorical Features"]},
    }
    assert day_plan.write_tasks(placed, context, []) == [
        "W5 problems",
        "MATH2931: W5 Categorical Features practice problems (60 min)",
    ]


@pytest.mark.parametrize("bad", [
    "Read COMP3121 W5 notes: Dynamic Programming I and II",
    "COMP3121: Review W5 Dynamic Programming I notes (60 min)",
    "Review notes",
    "Reading the W5 notes",
    "Re-read W5 lecture notes",
    "note review for DP",
    "notes review",
])
def test_comp_study_note_review_is_rewritten(monkeypatch, bad):
    _claude(monkeypatch, [bad])
    tasks = day_plan.write_tasks(
        [("COMP3121", "study", at(9), at(10))],
        {"COMP3121": {"week": 5, "notes": [DP_NOTES]}},
        [],
    )
    assert tasks == [PRACTICE]
    assert not day_plan.is_note_review(tasks[0])


@pytest.mark.parametrize("kept", [
    "COMP3121: W5 Dynamic Programming practice problems (60 min)",
    "COMP3121: W5 knapsack Q1-4 (60 min)",
    "Assignment 2 Q1-3",
    "Read the problem sheet",
    "Review the tutorial problems",
])
def test_comp_study_practice_wording_is_kept(monkeypatch, kept):
    _claude(monkeypatch, [kept])
    tasks = day_plan.write_tasks(
        [("COMP3121", "study", at(9), at(10))],
        {"COMP3121": {"week": 5, "notes": [DP_NOTES]}},
        [],
    )
    assert tasks == [kept]


def test_note_review_rewrite_leaves_other_courses_and_lectures(monkeypatch):
    seen = {}
    _claude(monkeypatch, [
        "Read COMP3121 W5 notes: Dynamic Programming I and II",
        "COMP3121: Review W5 Dynamic Programming I notes (60 min)",
        "Review W5 tutorial notes",
        "Watch W5 lecture 1 + notes",
    ], seen)
    placed = [
        ("COMP3121", "study", at(9), at(10)),
        ("COMP2511", "study", at(10, 15), at(11, 15)),
        ("MATH2931", "study", at(11, 30), at(12, 30)),
        ("COMP3121", "lecture", at(14), at(15)),
    ]
    context = {
        "COMP3121": {"week": 5, "notes": [DP_NOTES]},
        "COMP2511": {"week": 5, "notes": []},
        "MATH2931": {"week": 5, "notes": ["Week 5 topics: Categorical Features"]},
    }
    tasks = day_plan.write_tasks(placed, context, [], labels={3: "W5 lecture 1 (Mon 14:00)"})
    assert tasks[0] == PRACTICE
    assert tasks[1] == "COMP2511: W5 practice problems (60 min)"
    assert tasks[2] == "Review W5 tutorial notes"
    assert tasks[3] == "Watch W5 lecture 1 + notes"
    prompt = seen["prompt"]
    assert prompt.startswith("You write for Jarvis")
    assert "study_style is practice (COMP2511, COMP3121)" in prompt
    assert 'Do not write "review notes", "read notes"' in prompt
    assert "study_style is notes (MATH2931)" in prompt
    assert "W5 lecture 2 + notes" in prompt
    assert '"study_style": "practice"' in prompt
    assert '"study_style": "notes"' in prompt


def test_comp_note_review_without_a_topic_is_still_practice(monkeypatch):
    _claude(monkeypatch, ["Read COMP3121 W5 notes"])
    tasks = day_plan.write_tasks(
        [("COMP3121", "study", at(9), at(10))],
        {"COMP3121": {"week": 5, "notes": []}},
        [],
    )
    assert tasks == ["COMP3121: W5 practice problems (60 min)"]


# ── Schedule changes and re-planning ─────────────────────────────────────────

def test_override_moves_the_routine():
    day_plan.set_override(MONDAY, {"remove": ["Automation work"]})
    day_plan.set_override(TUESDAY, {"add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]})
    plan = dict(PLAN, routine={"Monday": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]})
    assert day_plan.routine_busy(plan, MONDAY) == []
    assert day_plan.routine_busy(plan, TUESDAY) == [(at(9, 30), at(17, 30))]


@pytest.mark.parametrize("change", [
    {}, [], {"add": [{"start": "9am", "end": "17:00"}]}, {"add": [{"start": "17:00", "end": "09:00"}]},
    {"remove": [""]}, {"move": []}, {"add": [{"start": "09:00", "end": "10:00", "extra": 1}]},
])
def test_bad_changes(change):
    with pytest.raises(ValueError):
        day_plan.set_override(TUESDAY, change)


def test_build_respects_an_override():
    day_plan.set_override(TUESDAY, {"add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]})
    items = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=FakeCalendar())
    for i in items:
        start, end = datetime.datetime.fromisoformat(i["start"]), datetime.datetime.fromisoformat(i["end"])
        assert end <= at(9, 15) or start >= at(17, 45)


def test_replan_keeps_done_blocks_and_replaces_only_future_events():
    cal = FakeCalendar()
    first = day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=cal)
    done_id = first[0]["id"]
    ledger.update_day(TUESDAY, lambda d: ledger.find_item(d, done_id).update(status="done"))
    # Everything not answered is replaced, including blocks whose time has started.
    future_events = [i["event_id"] for i in first if i["id"] != done_id]

    day_plan.set_override(TUESDAY, {"add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]})
    cal2 = FakeCalendar()
    new = day_plan.replan_today(now=at(9, 44), busy_fn=lambda: [], tasks_fn=plain, calendar=cal2)

    stored = [i for i in ledger.load_day(TUESDAY)["items"] if i.get("kind") in ("study", "area")]
    assert ledger.find_item(ledger.load_day(TUESDAY), done_id)["status"] == "done"
    assert cal2.deleted == future_events            # only the dropped future events
    for i in new:
        assert datetime.datetime.fromisoformat(i["start"]) >= at(17, 45)
    assert len({i["id"] for i in stored}) == len(stored)
    kept_minutes = sum(i["minutes"] for i in stored if i["kind"] == "study")
    assert kept_minutes <= 240



def test_replan_drops_a_started_but_unanswered_block():
    cal = FakeCalendar()
    day_plan.build_plan(now=at(5, 30), busy_fn=lambda: [], tasks_fn=plain, calendar=cal)
    first = sorted([i for i in ledger.load_day(TUESDAY)["items"] if i.get("kind") == "study"],
                   key=lambda i: i["start"])[0]
    # 10:18, the first block started at 08:20 and was never asked about.
    day_plan.set_override(TUESDAY, {"add": [{"start": "09:30", "end": "17:30", "label": "Automation work"}]})
    cal2 = FakeCalendar()
    day_plan.replan_today(now=at(10, 18), busy_fn=lambda: [], tasks_fn=plain, calendar=cal2)
    assert ledger.find_item(ledger.load_day(TUESDAY), first["id"]) is None or \
        ledger.find_item(ledger.load_day(TUESDAY), first["id"])["start"] >= "2026-10-13T17:45"
    assert first["event_id"] in cal2.deleted

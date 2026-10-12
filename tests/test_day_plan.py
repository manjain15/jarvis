"""Day plan: sizing, placement, gym slot, calendar writes, and block checks."""

import datetime
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


def plain(placed, context, carry):
    return day_plan.write_tasks(placed, context, carry)


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


def test_sunday_takes_everything_left_up_to_the_cap():
    sunday = MONDAY + datetime.timedelta(days=6)
    alloc = day_plan.allocate({("MATH2931", "study"): 120}, sunday, PLAN["daily_cap_hours"], {})
    assert alloc == {("MATH2931", "study"): 120}


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
    gym = {"slot": "06:30", "minutes": 60}
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
    # Nothing overlaps the gym (06:25–08:05) and nothing starts before 08:15.
    for i in items:
        assert datetime.datetime.fromisoformat(i["start"]) >= at(8, 15)
    assert [i["type"] for i in items][0] == "lecture"


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

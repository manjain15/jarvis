"""study_tracker engine: weight-scaled lead windows, escalation thresholds,
revision triggers, and check-in drift detection. All term data is injected via
monkeypatch so tests never touch real term_context.json or data/."""

import datetime
import json

import study_tracker as st


# ── Fixtures: inject synthetic assessments ────────────────────────────────────

def _patch_assessments(monkeypatch, assessments):
    """Make study_tracker see exactly `assessments` (list of dicts) as the term."""
    import term_context
    ctx = {"subjects": [{"code": "TEST", "assessments": assessments}]}
    monkeypatch.setattr(term_context, "load_context", lambda: ctx)


# ── _lead_days weight bands ───────────────────────────────────────────────────

def test_lead_days_bands():
    assert st._lead_days(30) == 21    # heavy (>=25)
    assert st._lead_days(25) == 21
    assert st._lead_days(15) == 14    # medium (>=10)
    assert st._lead_days(10) == 14
    assert st._lead_days(5) == 7      # light
    assert st._lead_days(None) == 10  # unknown weight → default


# ── Ramp-up escalation ────────────────────────────────────────────────────────

def _due_in(days, weight=15, **extra):
    due = (datetime.date(2026, 7, 1) + datetime.timedelta(days=days)).isoformat()
    a = {"name": "Assignment", "due": due, "weight": weight, "status": "pending"}
    a.update(extra)
    return a


def test_rampup_red_when_imminent(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(2)])
    alerts = st.get_assessment_alerts(datetime.date(2026, 7, 1))
    assert any(a.startswith("🔴") for a in alerts)


def test_rampup_orange_within_week(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(5)])
    alerts = st.get_assessment_alerts(datetime.date(2026, 7, 1))
    assert any(a.startswith("🟠") for a in alerts)


def test_rampup_yellow_when_far_but_in_window(monkeypatch):
    # 12 days out, 15% weight → lead window is 14d, so it's in-window but >7d → 🟡.
    _patch_assessments(monkeypatch, [_due_in(12, weight=15)])
    alerts = st.get_assessment_alerts(datetime.date(2026, 7, 1))
    assert any(a.startswith("🟡") for a in alerts)


def test_rampup_silent_outside_lead_window(monkeypatch):
    # 5% test, 10 days out → lead window only 7d → no alert yet.
    _patch_assessments(monkeypatch, [_due_in(10, weight=5)])
    assert st.get_assessment_alerts(datetime.date(2026, 7, 1)) == []


def test_heavy_assessment_warns_earlier_than_light(monkeypatch):
    # 18 days out: a 30% (lead 21) fires; a 5% (lead 7) stays silent.
    _patch_assessments(monkeypatch, [_due_in(18, weight=30)])
    assert st.get_assessment_alerts(datetime.date(2026, 7, 1))
    _patch_assessments(monkeypatch, [_due_in(18, weight=5)])
    assert st.get_assessment_alerts(datetime.date(2026, 7, 1)) == []


def test_submitted_assessments_ignored(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(2, status="submitted")])
    assert st.get_assessment_alerts(datetime.date(2026, 7, 1)) == []


def test_past_due_ignored(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(-3)])
    assert st.get_assessment_alerts(datetime.date(2026, 7, 1)) == []


# ── Revision triggers ─────────────────────────────────────────────────────────

def test_revision_alert_lists_covered_weeks(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(3, weight=21, covers_weeks=[1, 2, 3])])
    # No course_schedule topics in test env → falls back to "see course schedule".
    alerts = st.get_revision_alerts(datetime.date(2026, 7, 1))
    assert len(alerts) == 1
    assert "Weeks 1-3" in alerts[0]
    assert alerts[0].startswith("📖")


def test_revision_single_week_label(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(3, weight=21, covers_weeks=[7])])
    alerts = st.get_revision_alerts(datetime.date(2026, 7, 1))
    assert "Week 7" in alerts[0]


def test_no_revision_without_covers_weeks(monkeypatch):
    _patch_assessments(monkeypatch, [_due_in(3, weight=21)])
    assert st.get_revision_alerts(datetime.date(2026, 7, 1)) == []


# ── Drift detection ───────────────────────────────────────────────────────────

def _write_checkin(tmp_path, date_str, payload):
    (tmp_path / f"checkin_{date_str}.json").write_text(json.dumps(payload))


def test_drift_flags_behind_course(tmp_path):
    _write_checkin(tmp_path, "2026-06-14",
                   {"keepup_COMP2511": True, "keepup_MATH2601": False,
                    "behind_MATH2601": "Ch3 linear maps"})
    alerts = st.get_drift_alerts(datetime.date(2026, 6, 15), data_dir=tmp_path)
    assert len(alerts) == 1
    assert "MATH2601" in alerts[0]
    assert "Ch3 linear maps" in alerts[0]


def test_drift_ignores_kept_up(tmp_path):
    _write_checkin(tmp_path, "2026-06-14", {"keepup_COMP2511": True})
    assert st.get_drift_alerts(datetime.date(2026, 6, 15), data_dir=tmp_path) == []


def test_drift_stale_checkin_ignored(tmp_path):
    _write_checkin(tmp_path, "2026-06-01", {"keepup_MATH2601": False})
    # 14 days later, default max_age 8 → ignored.
    assert st.get_drift_alerts(datetime.date(2026, 6, 15), data_dir=tmp_path) == []


def test_drift_no_checkins(tmp_path):
    assert st.get_drift_alerts(datetime.date(2026, 6, 15), data_dir=tmp_path) == []


def test_drift_uses_latest_checkin(tmp_path):
    _write_checkin(tmp_path, "2026-06-07", {"keepup_MATH2601": False})
    _write_checkin(tmp_path, "2026-06-14", {"keepup_MATH2601": True})
    # Latest (14th) says kept-up → no alert, even though older one was behind.
    assert st.get_drift_alerts(datetime.date(2026, 6, 15), data_dir=tmp_path) == []

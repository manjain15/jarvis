"""
Jarvis Dashboard Server
========================
A Flask web server that serves the Jarvis dashboard.
Aggregates data from all Jarvis modules and exposes it via /api/data.
The frontend is a single dark-mode HTML page served at /.

INSTALL:
  pip install flask

RUN LOCALLY:
  python dashboard.py
  Open: http://localhost:5555

DEPLOY TO RAILWAY:
  Follow DEPLOY_GUIDE.md
"""

import json
import datetime
import os
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

import pytz
import config

# ── Import Jarvis modules ──────────────────────────────────────────────────────
# Each import is wrapped — dashboard works even if a module fails

# ── Memory system ────────────────────────────────────────────────────────────
try:
    from memory_system import load_memory
    MEMORY_AVAILABLE = True
except Exception:
    MEMORY_AVAILABLE = False

def safe_import(module_name, func_name):
    try:
        mod = __import__(module_name)
        return getattr(mod, func_name)
    except Exception:
        return None

fetch_health   = safe_import("google_health",   "fetch_health_data")
fetch_workouts = safe_import("hevy",            "fetch_workout_data")
fetch_finance  = safe_import("finance_tracker", "get_finance_summary")

# ── Also import individual health functions for trend data ────────────────────
try:
    from google_health import fetch_sleep, fetch_resting_hr, fetch_steps, get_access_token, TIMEZONE
    HEALTH_DETAIL = True
except Exception:
    HEALTH_DETAIL = False

try:
    from hevy import fetch_recent_workouts, parse_workout_date, get_pplrul_day, PPLRUL, ANCHOR_DATE, ANCHOR_DAY
    HEVY_DETAIL = True
except Exception:
    HEVY_DETAIL = False

try:
    from finance_tracker import (
        parse_stgeorge_csv, analyse_spending, analyse_savings,
        EVERYDAY_CSV, SAVINGS1_CSV, INVESTING_CSV
    )
    FINANCE_DETAIL = True
except Exception:
    FINANCE_DETAIL = False

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="dashboard_static")

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
TZ         = pytz.timezone(config.TIMEZONE)


# ─────────────────────────────────────────────────────────────────────────────
# API ENDPOINT — aggregates all data for the frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    now       = datetime.datetime.now(TZ)
    today     = now.date()
    yesterday = today - datetime.timedelta(days=1)

    data = {
        "updated_at": now.strftime("%I:%M %p"),
        "date_str":   now.strftime("%A, %d %B %Y"),
        "health":     {},
        "workouts":   {},
        "finance":    {},
        "brief":      "",
    }

    # ── Health data ───────────────────────────────────────────────────────────
    if HEALTH_DETAIL:
        try:
            token = get_access_token()

            # Today's sleep (woke up today)
            sleep = fetch_sleep(token, yesterday)
            if sleep:
                data["health"]["sleep"] = {
                    "duration_str": sleep["duration_str"],
                    "duration":     sleep["duration_str"],
                    "total_minutes": sleep["total_minutes"],
                    "vs_7hr":       sleep["vs_7hr"],
                    "deep":         sleep["deep_minutes"],
                    "rem":          sleep["rem_minutes"],
                    "light":        sleep["light_minutes"],
                    "wake_time":    sleep["wake_time"],
                }

            # Resting HR
            hr = fetch_resting_hr(token, yesterday)
            if hr:
                data["health"]["resting_hr"] = hr.get("resting_hr")

            # Steps
            steps = fetch_steps(token, yesterday)
            if steps:
                data["health"]["steps"] = steps.get("steps", 0)

            # Sleep trend — last 7 nights
            sleep_trend = []
            for i in range(6, -1, -1):
                d      = today - datetime.timedelta(days=i+1)  # sleep ending on day d+1
                s      = fetch_sleep(token, d)
                label  = d.strftime("%a")
                if s:
                    sleep_trend.append({
                        "day":     label,
                        "minutes": s["total_minutes"],
                        "hours":   round(s["total_minutes"] / 60, 1),
                    })
                else:
                    sleep_trend.append({"day": label, "minutes": 0, "hours": 0})
            data["health"]["sleep_trend"] = sleep_trend

        except Exception as e:
            data["health"]["error"] = str(e)

    # ── Workout data ──────────────────────────────────────────────────────────
    if HEVY_DETAIL:
        try:
            workouts = fetch_recent_workouts(page_size=10)
            if workouts:
                # Weekly consistency
                week_start  = today - datetime.timedelta(days=today.weekday())
                week_days   = []
                trained_dates = set()

                for w in workouts:
                    dt = parse_workout_date(w)
                    if dt and dt.date() >= week_start:
                        trained_dates.add(dt.date())

                for i in range(7):
                    d       = week_start + datetime.timedelta(days=i)
                    split   = get_pplrul_day(d)
                    is_past = d < today
                    is_today = d == today
                    trained  = d in trained_dates
                    rest     = split == "Rest"

                    week_days.append({
                        "day":      d.strftime("%a"),
                        "split":    split,
                        "trained":  trained,
                        "rest":     rest,
                        "is_today": is_today,
                        "is_future": d > today,
                        "missed":   is_past and not trained and not rest,
                    })

                sessions_done     = len([d for d in week_days if d["trained"]])
                sessions_expected = len([d for d in week_days
                                        if not d["rest"] and not d["is_future"]])

                data["workouts"] = {
                    "week_days":        week_days,
                    "sessions_done":    sessions_done,
                    "sessions_expected": sessions_expected,
                    "today_split":      get_pplrul_day(today),
                }

                # Last workout detail
                last = workouts[0]
                last_dt = parse_workout_date(last)
                exercises = []
                for ex in last.get("exercises", [])[:6]:
                    sets = ex.get("sets", [])
                    weight_sets = [s for s in sets if s.get("weight_kg") and s.get("reps")]
                    if weight_sets:
                        top = max(weight_sets, key=lambda s: s["weight_kg"])
                        cnt = len([s for s in sets if s.get("weight_kg") and s.get("reps")])
                        exercises.append({
                            "name":   ex.get("title", ""),
                            "sets":   cnt,
                            "reps":   top["reps"],
                            "weight": top["weight_kg"],
                        })

                data["workouts"]["last_workout"] = {
                    "title":     last.get("title", "Workout"),
                    "date":      last_dt.strftime("%a %d %b") if last_dt else "",
                    "exercises": exercises,
                }

        except Exception as e:
            data["workouts"]["error"] = str(e)

    # ── Finance data ──────────────────────────────────────────────────────────
    if FINANCE_DETAIL:
        try:
            if EVERYDAY_CSV.exists():
                txns     = parse_stgeorge_csv(EVERYDAY_CSV)
                spending = analyse_spending(txns, days=7)
                savings  = analyse_savings()

                # Category breakdown for chart
                tracked = ["Food & dining", "Entertainment", "Shopping", "Sport & leisure", "Transport"]
                categories = []
                for cat in tracked:
                    amt = spending["category_totals"].get(cat, 0)
                    if amt > 0:
                        cat_txns = [
                            {
                                "date": t["date"].strftime("%d %b"),
                                "description": t["description"][:35],
                                "amount": round(t["debit"], 2),
                            }
                            for t in spending.get("transactions", [])
                            if t["category"] == cat
                        ]
                        categories.append({
                            "name": cat,
                            "amount": round(amt, 2),
                            "transactions": cat_txns,
                        })
                other = spending["category_totals"].get("Other", 0)
                other_txns = [
                    {
                        "date": t["date"].strftime("%d %b"),
                        "description": t["description"][:35],
                        "amount": round(t["debit"], 2),
                    }
                    for t in spending.get("transactions", [])
                    if t["category"] == "Other"
                ]
                if other > 0:
                    categories.append({"name": "Other", "amount": round(other, 2), "transactions": other_txns})

                data["finance"] = {
                    "total_spend":   round(spending["total_spend"], 2),
                    "weekly_budget": 75,
                    "categories":    categories,
                    "savings": {
                        "current":    round(savings["total"], 2),
                        "goal":       savings["goal"],
                        "remaining":  round(savings["remaining"], 2),
                        "pct":        round(savings["pct"], 1),
                        "on_track":   savings["on_track"],
                        "projected":  savings["projected_date"].strftime("%B %Y"),
                    }
                }
        except Exception as e:
            data["finance"]["error"] = str(e)

    # ── Today's brief (from last saved file) ──────────────────────────────────
    try:
        today_str   = today.strftime("%Y-%m-%d")
        summary_path = DATA_DIR / f"summary_{today_str}.txt"
        if not summary_path.exists():
            # Try yesterday's
            yesterday_str = yesterday.strftime("%Y-%m-%d")
            summary_path  = DATA_DIR / f"summary_{yesterday_str}.txt"
        if summary_path.exists():
            data["brief"] = summary_path.read_text()
    except Exception:
        pass

    return jsonify(data)


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Jarvis</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #080b10;
    --bg2:       #0d1117;
    --bg3:       #131920;
    --border:    #1e2a35;
    --accent:    #00e5ff;
    --accent2:   #7b61ff;
    --green:     #00ff88;
    --red:       #ff4757;
    --yellow:    #ffd32a;
    --text:      #e8f4f8;
    --muted:     #4a6070;
    --font-head: 'Syne', sans-serif;
    --font-mono: 'Space Mono', monospace;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Scanline overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,229,255,0.015) 2px,
      rgba(0,229,255,0.015) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 20px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .header-left { display: flex; align-items: center; gap: 12px; }

  .logo {
    font-family: var(--font-head);
    font-size: 22px;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: -0.5px;
    text-shadow: 0 0 20px rgba(0,229,255,0.4);
  }

  .status-dot {
    width: 8px; height: 8px;
    background: var(--green);
    border-radius: 50%;
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .updated {
    font-size: 11px;
    color: var(--muted);
    font-family: var(--font-mono);
  }

  .refresh-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }

  .grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 16px;
    max-width: 480px;
    margin: 0 auto;
  }

  @media (min-width: 900px) {
    .grid {
      grid-template-columns: 1fr 1fr;
      max-width: 100%;
      gap: 16px;
      padding: 20px 24px;
    }
    .card.wide { grid-column: 1 / -1; }
  }

  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 18px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: rgba(0,229,255,0.2); }

  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0.3;
  }

  .card-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .card-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* ── Sleep card ── */
  .sleep-main {
    font-family: var(--font-head);
    font-size: 42px;
    font-weight: 800;
    line-height: 1;
    margin-bottom: 4px;
  }
  .sleep-main.good { color: var(--green); text-shadow: 0 0 20px rgba(0,255,136,0.3); }
  .sleep-main.warn { color: var(--yellow); text-shadow: 0 0 20px rgba(255,211,42,0.3); }
  .sleep-main.bad  { color: var(--red); text-shadow: 0 0 20px rgba(255,71,87,0.3); }

  .sleep-sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }

  .sleep-stages {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 12px;
  }

  .stage {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px;
    text-align: center;
  }
  .stage-val { font-size: 16px; font-weight: 700; color: var(--accent2); }
  .stage-label { font-size: 10px; color: var(--muted); margin-top: 2px; }

  /* Sleep bar chart */
  .sleep-trend {
    margin-top: 14px;
    display: flex;
    align-items: flex-end;
    gap: 5px;
    height: 48px;
  }
  .trend-bar-wrap {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    height: 100%;
    justify-content: flex-end;
  }
  .trend-bar {
    width: 100%;
    border-radius: 3px 3px 0 0;
    background: var(--accent2);
    opacity: 0.6;
    min-height: 2px;
    transition: height 0.5s ease;
  }
  .trend-bar.target { opacity: 1; background: var(--accent); }
  .trend-day { font-size: 9px; color: var(--muted); }

  /* ── HR + Steps ── */
  .metric-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .metric {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
  }
  .metric-val {
    font-family: var(--font-head);
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
  }
  .metric-unit { font-size: 11px; color: var(--muted); }
  .metric-label { font-size: 10px; color: var(--muted); margin-top: 4px; }

  /* ── Workouts ── */
  .week-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 5px;
    margin-bottom: 14px;
  }
  .week-day {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }
  .week-day-label { font-size: 9px; color: var(--muted); }
  .week-day-split { font-size: 8px; color: var(--muted); }
  .week-day-dot {
    width: 28px; height: 28px;
    border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    border: 1px solid var(--border);
  }
  .week-day-dot.trained  { background: rgba(0,255,136,0.15); border-color: var(--green); }
  .week-day-dot.missed   { background: rgba(255,71,87,0.1);  border-color: var(--red); }
  .week-day-dot.rest     { background: var(--bg3); border-color: var(--border); }
  .week-day-dot.future   { background: var(--bg3); border-color: var(--border); opacity: 0.4; }
  .week-day-dot.today    { border-color: var(--accent); box-shadow: 0 0 8px rgba(0,229,255,0.3); }

  .consistency-label {
    font-family: var(--font-head);
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 12px;
  }
  .consistency-label span { color: var(--text); font-weight: 600; }

  .last-workout { margin-top: 14px; }
  .last-workout-title {
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .exercise-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--bg3);
    font-size: 12px;
  }
  .exercise-row:last-child { border-bottom: none; }
  .exercise-name { color: var(--text); }
  .exercise-stats { color: var(--accent); font-family: var(--font-mono); font-size: 11px; }

  /* ── Savings ── */
  .savings-big {
    font-family: var(--font-head);
    font-size: 36px;
    font-weight: 800;
    color: var(--accent);
    text-shadow: 0 0 20px rgba(0,229,255,0.3);
    margin-bottom: 4px;
  }
  .savings-goal { color: var(--muted); font-size: 12px; margin-bottom: 16px; }

  .progress-track {
    height: 6px;
    background: var(--bg3);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 8px;
  }
  .progress-fill {
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, var(--accent2), var(--accent));
    box-shadow: 0 0 10px rgba(0,229,255,0.4);
    transition: width 1s ease;
  }

  .progress-labels {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 10px;
  }

  .track-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
  }
  .track-badge.behind { background: rgba(255,71,87,0.1); color: var(--red); border: 1px solid rgba(255,71,87,0.3); }
  .track-badge.ontrack { background: rgba(0,255,136,0.1); color: var(--green); border: 1px solid rgba(0,255,136,0.3); }

  /* ── Spending ── */
  .spend-total {
    font-family: var(--font-head);
    font-size: 32px;
    font-weight: 800;
    margin-bottom: 4px;
  }
  .spend-total.over { color: var(--red); }
  .spend-total.ok   { color: var(--green); }

  .spend-label { color: var(--muted); font-size: 12px; margin-bottom: 14px; }

  .spend-bars { display: flex; flex-direction: column; gap: 8px; }
  .spend-bar-row { display: flex; flex-direction: column; gap: 4px; }
  .spend-bar-header {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
  }
  .spend-bar-name { color: var(--text); }
  .spend-bar-amt  { color: var(--accent); font-family: var(--font-mono); }
  .spend-bar-track {
    height: 4px;
    background: var(--bg3);
    border-radius: 2px;
    overflow: hidden;
  }
  .spend-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--accent2);
    transition: width 0.8s ease;
  }

  /* ── Transaction rows ── */
  .txn-list {
    margin-top: 8px;
    border-top: 1px solid var(--border);
    padding-top: 6px;
  }
  .txn-row {
    display: grid;
    grid-template-columns: 42px 1fr auto;
    gap: 8px;
    padding: 5px 0;
    border-bottom: 1px solid rgba(255,255,255,0.03);
    font-size: 11px;
    align-items: center;
  }
  .txn-row:last-child { border-bottom: none; }
  .txn-date  { color: var(--muted); font-family: var(--font-mono); }
  .txn-desc  { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .txn-amt   { color: var(--accent); font-family: var(--font-mono); text-align: right; white-space: nowrap; }
  .txn-empty { color: var(--muted); font-size: 11px; padding: 4px 0; }
  .txn-toggle { transition: transform 0.2s; }

  /* ── Loading ── */
  .loading {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 60vh;
    gap: 16px;
  }
  .loading-text { color: var(--muted); font-size: 12px; letter-spacing: 0.1em; }
  .loader {
    width: 40px; height: 40px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .error-msg { color: var(--muted); font-size: 12px; text-align: center; padding: 20px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="status-dot" id="statusDot"></div>
    <div class="logo">JARVIS</div>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <span class="updated" id="updatedAt">loading...</span>
    <button class="refresh-btn" onclick="loadData()">↻ refresh</button>
  </div>
</div>

<div id="main">
  <div class="loading">
    <div class="loader"></div>
    <div class="loading-text">INITIALISING SYSTEMS</div>
  </div>
</div>

<script>
const CATEGORY_COLORS = {
  "Food & dining":    "#00e5ff",
  "Entertainment":   "#7b61ff",
  "Shopping":        "#ffd32a",
  "Sport & leisure": "#00ff88",
  "Transport":       "#ff9f43",
  "Other":           "#4a6070",
};

function toggleTxns(id) {
  const el = document.getElementById(id);
  const toggle = document.getElementById('toggle-' + id);
  if (!el) return;
  const isOpen = el.style.display !== 'none';
  el.style.display = isOpen ? 'none' : 'block';
  if (toggle) toggle.textContent = isOpen ? '▼' : '▲';
}

async function loadData() {
  document.getElementById("updatedAt").textContent = "refreshing...";
  try {
    const res  = await fetch("/api/data");
    const data = await res.json();
    render(data);
    document.getElementById("updatedAt").textContent = "updated " + data.updated_at;
    document.getElementById("statusDot").style.background = "#00ff88";
  } catch(e) {
    document.getElementById("main").innerHTML = `<div class="error-msg">⚠ Could not load data.<br>${e}</div>`;
    document.getElementById("statusDot").style.background = "#ff4757";
  }
}

function fmt(n) {
  return n?.toLocaleString("en-AU", {minimumFractionDigits:2, maximumFractionDigits:2}) ?? "—";
}

function render(d) {
  const h = d.health || {};
  const w = d.workouts || {};
  const f = d.finance || {};
  const sleep = h.sleep || {};
  const savings = f.savings || {};
  const trend = h.sleep_trend || [];

  // Sleep quality
  let sleepClass = "bad", sleepIcon = "⚡";
  const sm = sleep.total_minutes || 0;
  if (sm >= 420) { sleepClass = "good"; sleepIcon = "◉"; }
  else if (sm >= 360) { sleepClass = "warn"; sleepIcon = "◎"; }

  // Sleep trend bars
  const maxMins = Math.max(...trend.map(t => t.minutes), 480);
  const trendBars = trend.map(t => {
    const pct = maxMins > 0 ? (t.minutes / maxMins * 100) : 0;
    const isTarget = t.minutes >= 420;
    return `<div class="trend-bar-wrap">
      <div class="trend-bar ${isTarget ? "target" : ""}" style="height:${Math.max(pct,3)}%"></div>
      <div class="trend-day">${t.day}</div>
    </div>`;
  }).join("");

  // Week grid
  const weekGrid = (w.week_days || []).map(day => {
    let cls = "future", icon = "";
    if (day.rest) { cls = "rest"; icon = "—"; }
    else if (day.trained) { cls = "trained"; icon = "✓"; }
    else if (day.missed) { cls = "missed"; icon = "✗"; }
    else if (day.is_today) { cls = "today"; icon = "·"; }
    else { icon = "·"; }
    const todayCls = day.is_today ? " today" : "";
    return `<div class="week-day">
      <div class="week-day-label">${day.day}</div>
      <div class="week-day-dot ${cls}${todayCls}">${icon}</div>
      <div class="week-day-split">${day.split.substring(0,3)}</div>
    </div>`;
  }).join("");

  // Last workout exercises
  const lastW = w.last_workout || {};
  const exRows = (lastW.exercises || []).map(ex =>
    `<div class="exercise-row">
      <span class="exercise-name">${ex.name}</span>
      <span class="exercise-stats">${ex.sets}×${ex.reps} @ ${ex.weight}kg</span>
    </div>`
  ).join("");

  // Spending bars
  const cats   = f.categories || [];
  const maxAmt = Math.max(...cats.map(c => c.amount), 1);
  const spendBars = cats.map((c, idx) => {
    const pct = (c.amount / maxAmt * 100);
    const col = CATEGORY_COLORS[c.name] || "#4a6070";
    const txnId = `txn-${idx}`;
    const txnRows = (c.transactions || []).map(t =>
      `<div class="txn-row">
        <span class="txn-date">${t.date}</span>
        <span class="txn-desc">${t.description}</span>
        <span class="txn-amt">$${fmt(t.amount)}</span>
      </div>`
    ).join("");
    return `<div class="spend-bar-row" onclick="toggleTxns('${txnId}')" style="cursor:pointer">
      <div class="spend-bar-header">
        <span class="spend-bar-name">${c.name}</span>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="spend-bar-amt">$${fmt(c.amount)}</span>
          <span class="txn-toggle" id="toggle-${txnId}" style="color:var(--muted);font-size:10px">▼</span>
        </div>
      </div>
      <div class="spend-bar-track">
        <div class="spend-bar-fill" style="width:${pct}%;background:${col}"></div>
      </div>
      <div class="txn-list" id="${txnId}" style="display:none">
        ${txnRows || '<div class="txn-empty">No transactions</div>'}
      </div>
    </div>`;
  }).join("");

  const totalSpend  = f.total_spend || 0;
  const overBudget  = totalSpend > 75;
  const spendClass  = overBudget ? "over" : "ok";
  const savingsPct  = savings.pct || 0;
  const onTrack     = savings.on_track;

  document.getElementById("main").innerHTML = `
  <div class="grid">

    <!-- Sleep -->
    <div class="card">
      <div class="card-label">Sleep</div>
      <div class="sleep-main ${sleepClass}">${sleep.duration || sleep.duration_str || (sm > 0 ? Math.floor(sm/60)+"h "+(sm%60)+"m" : "—")}</div>
      <div class="sleep-sub">${
        sleep.vs_7hr != null
          ? (sleep.vs_7hr >= 0
              ? `+${sleep.vs_7hr}m above target`
              : `${Math.abs(sleep.vs_7hr)}m below 7hr target`)
          : "no data"
      } · woke ${sleep.wake_time || "—"}</div>

      <div class="sleep-stages">
        <div class="stage">
          <div class="stage-val">${sleep.deep || 0}<span style="font-size:10px">m</span></div>
          <div class="stage-label">deep</div>
        </div>
        <div class="stage">
          <div class="stage-val">${sleep.rem || 0}<span style="font-size:10px">m</span></div>
          <div class="stage-label">rem</div>
        </div>
        <div class="stage">
          <div class="stage-val">${sleep.light || 0}<span style="font-size:10px">m</span></div>
          <div class="stage-label">light</div>
        </div>
      </div>

      <div class="sleep-trend">${trendBars}</div>
    </div>

    <!-- Vitals -->
    <div class="card">
      <div class="card-label">Vitals</div>
      <div class="metric-row">
        <div class="metric">
          <div class="metric-val">${h.resting_hr || "—"}</div>
          <div class="metric-unit">bpm</div>
          <div class="metric-label">resting HR</div>
        </div>
        <div class="metric">
          <div class="metric-val">${h.steps?.toLocaleString() || "—"}</div>
          <div class="metric-unit">steps</div>
          <div class="metric-label">yesterday</div>
        </div>
      </div>
    </div>

    <!-- Training -->
    <div class="card wide">
      <div class="card-label">Training</div>
      <div class="consistency-label">
        <span>${w.sessions_done || 0}/${w.sessions_expected || 0}</span> sessions this week · today is <span>${w.today_split || "—"}</span>
      </div>
      <div class="week-grid">${weekGrid}</div>
      ${lastW.title ? `
      <div class="last-workout">
        <div class="last-workout-title">Last session — ${lastW.title} (${lastW.date})</div>
        ${exRows}
      </div>` : ""}
    </div>

    <!-- Savings -->
    <div class="card">
      <div class="card-label">Savings — US exchange Jan 2027</div>
      <div class="savings-big">$${(savings.current || 0).toLocaleString("en-AU", {minimumFractionDigits:0, maximumFractionDigits:0})}</div>
      <div class="savings-goal">of $35,000 goal · $${(savings.remaining || 0).toLocaleString("en-AU", {minimumFractionDigits:0, maximumFractionDigits:0})} remaining</div>
      <div class="progress-track">
        <div class="progress-fill" style="width:${Math.min(savingsPct,100)}%"></div>
      </div>
      <div class="progress-labels">
        <span>${savingsPct.toFixed(1)}%</span>
        <span>projected ${savings.projected || "—"}</span>
      </div>
      <div class="track-badge ${onTrack ? "ontrack" : "behind"}">
        ${onTrack ? "✓ on track" : "⚠ behind schedule"}
      </div>
    </div>

    <!-- Spending -->
    <div class="card">
      <div class="card-label">Spending — last 7 days</div>
      <div class="spend-total ${spendClass}">$${fmt(totalSpend)}</div>
      <div class="spend-label">${overBudget ? "⚠ over" : "within"} ~$75/week budget</div>
      <div class="spend-bars">${spendBars || '<div style="color:var(--muted);font-size:12px">No spending data</div>'}</div>
    </div>

  </div>`;
}

loadData();
setInterval(loadData, 5 * 60 * 1000); // refresh every 5 min
</script>
</body>
</html>'''


@app.route("/")
def index():
    return DASHBOARD_HTML


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────────────────────
# /ask — Voice interface endpoint
# ─────────────────────────────────────────────────────────────────────────────
# Accepts a question via POST or GET, sends to Claude with full context,
# returns a short spoken-friendly answer.
# Called by the Siri Shortcut on your iPhone.

@app.route("/ask", methods=["GET", "POST"])
def ask():
    from flask import request
    import anthropic as _anthropic

    # Get the question
    if request.method == "POST":
        data     = request.get_json(silent=True) or {}
        question = data.get("question", "") or request.form.get("question", "")
    else:
        question = request.args.get("q", "")

    if not question:
        return jsonify({"answer": "I didn't catch that. Try asking again."})

    # Load profile
    profile_text = ""
    profile_path = SCRIPT_DIR / "profile.md"
    if profile_path.exists():
        profile_text = profile_path.read_text()

    # Fetch live data to give Jarvis context
    context_lines = []

    # Hevy — today's split and last workout
    if HEVY_DETAIL:
        try:
            from hevy import get_pplrul_day, fetch_recent_workouts, parse_workout_date
            import datetime as _dt
            tz    = pytz.timezone(config.TIMEZONE)
            today = _dt.datetime.now(tz).date()
            context_lines.append(f"Today's training split: {get_pplrul_day(today)}")
            workouts = fetch_recent_workouts(page_size=3)
            if workouts:
                last    = workouts[0]
                last_dt = parse_workout_date(last)
                if last_dt:
                    days_ago = (today - last_dt.date()).days
                    context_lines.append(
                        f"Last workout: {last.get('title','Workout')} "
                        f"— {days_ago} day(s) ago ({last_dt.strftime('%a %d %b')})"
                    )
        except Exception:
            pass

    # Health — sleep and HR
    if HEALTH_DETAIL:
        try:
            import datetime as _dt
            tz        = pytz.timezone(config.TIMEZONE)
            today     = _dt.datetime.now(tz).date()
            yesterday = today - _dt.timedelta(days=1)
            token     = get_access_token()
            sleep = fetch_sleep(token, yesterday)
            if sleep:
                context_lines.append(
                    f"Last night's sleep: {sleep['duration_str']} "
                    f"({'below' if sleep['vs_7hr'] < 0 else 'above'} 7hr target by {abs(sleep['vs_7hr'])}m)"
                )
            hr = fetch_resting_hr(token, yesterday)
            if hr and hr.get("resting_hr"):
                context_lines.append(f"Resting HR yesterday: {hr['resting_hr']} bpm")
        except Exception:
            pass

    # Finance — savings progress
    if FINANCE_DETAIL:
        try:
            if EVERYDAY_CSV.exists():
                savings = analyse_savings()
                context_lines.append(
                    f"Savings: ${savings['total']:,.2f} of $35,000 goal "
                    f"({savings['pct']:.1f}%) — "
                    f"{'on track' if savings['on_track'] else 'behind schedule'}"
                )
        except Exception:
            pass

    live_context = "\n".join(context_lines) if context_lines else "No live data available right now."

    # Load memory
    memory_text = ""
    if MEMORY_AVAILABLE:
        try:
            memory_text = load_memory(days_back=14)
        except Exception:
            pass

    # Build the prompt
    prompt = f"""You are Jarvis — Manav's personal AI assistant. He is talking to you via voice through a Siri Shortcut.

IMPORTANT: Your response will be READ ALOUD by Siri. So:
- Keep it SHORT — 2-4 sentences maximum
- No bullet points, no markdown, no lists
- Speak naturally, like a smart assistant talking to someone
- Be direct and specific — use real numbers and facts when available
- Don't say "Great question" or any filler — just answer

MANAV'S PROFILE:
{profile_text[:3000]}

LIVE DATA RIGHT NOW:
{live_context}

QUESTION: {question}

Answer in 2-4 sentences, spoken naturally, no formatting."""

    # Call Claude
    try:
        client  = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = message.content[0].text.strip()
    except Exception as e:
        answer = f"Sorry, I couldn't process that right now. Error: {str(e)[:100]}"

    return jsonify({"answer": answer, "question": question})



# ─────────────────────────────────────────────────────────────────────────────
# Orb state — shared between wake.py and the orb page
# ─────────────────────────────────────────────────────────────────────────────
_orb_state = {"state": "idle", "transcript": "", "response": ""}


@app.route("/state", methods=["GET", "POST"])
def orb_state():
    from flask import request as _req
    global _orb_state
    if _req.method == "POST":
        data = _req.get_json(silent=True) or {}
        _orb_state.update(data)
        return jsonify({"ok": True})
    return jsonify(_orb_state)


@app.route("/orb")
def orb():
    orb_path = SCRIPT_DIR / "orb.html"
    if orb_path.exists():
        return orb_path.read_text()
    return "<h1>orb.html not found in jarvis folder</h1>", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    print(f"\n🤖  Jarvis Dashboard running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)

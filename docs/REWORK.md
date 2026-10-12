# Jarvis rework — commit, verify, escalate

Status: **draft for sign-off**. No code until approved.
Reference design: `docs/brief_design.html` (dark, approved).

## 1. Why

Jarvis today records and reports. The check-in asks ~20 questions, the answers
become a summary, and the summary becomes one paragraph in the next brief.
Nothing is committed, checked, or followed up.

The new loop: **commit → verify → escalate → adapt**. Every day has a short
list of concrete commitments. Jarvis checks each one against evidence, not
self-report. A miss costs something.

## 2. Channels

| Channel | Owns | Never |
|---|---|---|
| **Grok bots** (Grok app, pull-only) | Coaching, evening check-in, study plan talk, career / networking / project pushes, weekly reflection | Time-critical enforcement |
| **Telegram** (`jarvis_telegram.py`) | Wake sequence, gym escalation, step pace, study-block checks, debt notices. Plus the existing `/approve`-style commands | Free chat, summaries, advice |
| **Email** | Morning brief (07:05), Sunday weekly review. Read, not pinged | — |

Retired: ntfy alerts (`alerts.py`, timer disabled), the Telegram 21:00
check-in, and Telegram free-form chat (`chat_with_claude` stays for `POST /ask`).

Grok bots cannot receive pushes. They pull from the agent API, either when
Manav opens them or on a scheduled Grok task.

## 3. The ledger

New module `ledger.py`. One file per day: `data/ledger/YYYY-MM-DD.json`.

```json
{
  "date": "2026-10-12",
  "items": [
    {
      "id": "gym",
      "kind": "gym",               // gym | steps | study | area
      "title": "Push",
      "slot": "06:30",
      "minutes": 60,
      "evidence": "geofence+hevy", // what counts as proof
      "status": "pending",         // pending | done | partial | missed | excused | moved
      "excuse": null,
      "events": []                 // timestamped log: pings sent, replies, proofs
    }
  ],
  "debt":   {"gym_minutes": 10, "steps": 1160}
}
```

Debt carries forward from the previous day's file when the day is
built. All writes go through `json_store` (lock + atomic replace), the same
way `term_context` writes.

## 4. Building the day (05:30)

`day_plan.py`, run from the Telegram daemon's loop at 05:30, with retries
every 30 min until noon. It runs in the daemon rather than on a new systemd
timer, so there's no unit to install. The plan is built from:

- **Config**: `term_context.json → study_plan`:
  - `targets`: hours per week per course, study and lectures.
  - `routine`: work and travel the calendar doesn't know about.
  - `daily_cap_hours`.
  - `ignore_busy`: `["Lecture"]`, because lectures are watched as recordings.
- **Gym**: 06:30 is kept unless the calendar clashes. Otherwise the session moves to the first clear 15-min step, up to 20:00.
- **Sizing**:
  - Remaining time = weekly target + last week's lecture backlog − done (partial counts as half).
  - That's spread over the days left in proportion to each day's cap.
  - Lectures × 1.5 (front-loaded).
  - Study × 1.5 inside an assessment's lead window, × 2 within 3 days.
  - Never over the day's cap.
- **Blocks**: 60 or 90 min, 08:00–21:00, 15 min clear of anything busy, lectures first, courses interleaved.
- **Calendar**: written as `Study · MATH2931` / `Lecture · MATH2931`, tagged `jarvis=study`. A re-plan only deletes tagged events.
- **Tasks**: one Claude call writes a concrete task per block. Yesterday's unfinished tasks go first. If the call fails, the tasks fall back to plain text.

At each block's end, Telegram asks: `MATH2931 · Assignment 2 Q1-3. Done? done / partial / no`.
- One question at a time.
- No reply in 60 min counts as `no_reply`.
- Anything not done rolls into tomorrow's tasks.

`python day_plan.py --dry-run` prints today's plan and writes nothing.

## 5. Gym — wake sequence

Wake signal: an iOS Shortcut runs on **alarm stopped** and sends `POST /events {"type":"wake"}`.
Fallback: if there's no wake event by `slot − 30 min`, Jarvis starts anyway.

Telegram, on a gym day:

| t | Message | Proof | If missed |
|---|---|---|---|
| wake | `06:00 · Push · leave by 06:25` | reply `up` within 3 min | resend + "3 min" |
| +3 | `Clothes on.` | photo within 10 min | resend |
| slot −0:10 | `Leave now.` | — | — |
| slot +0:30 | — | geofence `POST /events {"type":"gym_arrive"}` | `No arrival. Reason, or go.` |
| slot +2:00 | — | Hevy workout today | mark missed |

**Excuse gate.** A reply starting with `skip` must include a reason. Jarvis checks it:

- Sleep < 6h (Shortcut-reported or Google Health) → moved to the evening slot, not excused.
- Calendar has < 75 min free before 21:00 → 35-min version allowed.
- Sick, or anything else Manav marks with `skip sick` → excused, logged, and shown in the weekly review.
- Anything else → rejected. The sequence continues.

**Minimum:** on a bad day the deal is "arrive and do the first exercise". A
session of 25+ min in Hevy counts as `partial`, not missed.

## 6. Steps

An iOS Shortcut personal automation runs at 10:00, 13:00, 16:00, 19:00 and 21:00.
It reads today's steps from Apple Health and sends `POST /events {"type":"steps","count":N}`.
Google Health stays as the next-day check.

| Time | Pace |
|---|---|
| 10:00 | 2,500 |
| 13:00 | 5,000 |
| 16:00 | 7,000 |
| 19:00 | 8,500 |
| 21:00 | 10,000 (+ debt) |

The pace scales with the day's target (25 / 50 / 70 / 85 / 100%), so step
debt raises every checkpoint.

Jarvis only messages when Manav is behind. The fix is sized at ~100 steps/min
and placed in the calendar: `2,000 behind 5,000 pace. 20 min walk. Next free 14:00–15:00.`
No Claude call.

- Each checkpoint waits up to 20 min for its own Shortcut reading, so an
  earlier app-open reading can't trigger a false nudge.
- No usable reading means unknown, not behind. A count of 0 (locked phone) is
  not a reading.
- Several checkpoints due at once send one message. Ones over an hour old
  (daemon down) are skipped.

At 23:00 the shortfall against the day's target becomes tomorrow's debt
(cap +3,000). A day with no readings carries nothing.

## 7. Debt

Settled when the day closes (23:00, or on the next tick if the daemon was down).

| Outcome | Rule | Debt |
|---|---|---|
| done | Hevy minutes ≥ target − 15 | clears `gym_minutes` |
| partial | ≥ 25 min | none |
| excused | `skip sick` | none, shown in the weekly review |
| unenforced | the sequence never ran that day (before go-live, daemon down all morning) | none |
| missed | anything else | a **make-up**: the session + 10 min within 2 days |

- **Make-up**: an extra workout. Either a rest-day session, or minutes beyond that day's target. It clears when the extra minutes are ≥ make-up − 15.
- **Make-up not done by its due date**: +20 min on every gym session until one hits the longer target.
- **A second miss while a make-up is owed**: +10 min on the next session.
- **Steps**: a shortfall carries to the next day (cap +3,000). Built in phase 3.
- Messages are sent only inside their window. After a restart, overdue reminders are dropped instead of sent all at once.

**Money stakes are deferred.** The first 4 weeks are a trial with debt only.
At the end of the trial, the weekly review shows, per week:
gym sessions planned against done, unexcused misses, days at 10k steps, and
how long debt took to clear. If unexcused misses are still happening by weeks
3–4, stakes get designed then, as a separate change. The ledger already logs
every miss with its reason, so no history is lost.

## 8. Areas (career, project, networking, portfolio, weight)

New `data/areas.json`:

```json
{"project": {"next_action": "Ship login flow", "last_touched": "2026-10-06",
             "evidence": "github:manjain15/<repo>", "target_per_week": 3}}
```

- `last_touched` updates from evidence where it exists: GitHub commits (`GITHUB_TOKEN`), applications in `term_context`, mentor contact, and weight from Google Health. Otherwise it comes from the check-in.
- Days since last touched drives the daily "One push". The push is concrete and Jarvis does the prep work: a drafted LinkedIn or email message in Gmail (draft only, like `followups.py`), a Google Task with a date, or a project block in the calendar.
- Grok bots read areas and update them with `PATCH /areas/<name>`.

## 9. Evening check-in (Grok)

Scheduled Grok task at 21:00 calls `GET /checkin/today`. It returns:

- **facts**, prefilled from the ledger and evidence: gym status, steps, study blocks done or partial, weight, spend, commits.
- **2–4 questions**, only about what the evidence can't tell: partial study blocks ("what's left?"), a neglected area ("what's blocking X?"), and tomorrow's one non-negotiable.

The bot asks those questions and sends `POST /checkin` with the answers. Jarvis turns each answer into an action:

- Leftover study goes into tomorrow's blocks.
- A blocker becomes an area `next_action` plus a scheduled block.
- A networking intent becomes a Gmail draft.

Compatibility: `data/checkin_YYYY-MM-DD.json` keeps its current keys, filled
from evidence where possible, so `memory_system.py` is unchanged.
`summary_YYYY-MM-DD.txt` is still written for the brief.

Study blocks get a Telegram check at block end:
`MATH2931 block done?  done / partial / no`.

## 10. Agent API changes

New routes:

| Route | Bots | Purpose |
|---|---|---|
| `GET /today` | all | Ledger and debt |
| `GET /checkin/today`, `POST /checkin` | coach | Evening check-in |
| `GET /areas`, `PATCH /areas/<name>` | coach, career | Area state |
| `GET /plan/week` | study | Study blocks and hours vs target |
| `POST /events` | phone | wake, gym_arrive, steps, sleep |

New bot tokens in `JARVIS_API_TOKENS`: `coach` (gym, steps, check-in, areas)
and `phone` (`/events` only). Same auth, audit and throttling as today.

## 11. Voice

New `voice.py`. One shared rule block imported by every Claude prompt
(brief, weekly review, plan, `/ask`), replacing the five "You are Jarvis"
openers. It is also pasted into each Grok bot's instructions (`docs/grok_bots.md`).

- No greetings, sign-offs, emoji or exclamation marks.
- Numbers and times first. One fact per line.
- State the situation, then the instruction. Give a reason only when it changes the instruction.
- Firm and calm, never snarky.

Telegram enforcement messages are fixed templates in `voice.py` (zero cost).
The ~45 emoji in existing output strings are removed.

## 12. Email

The brief is rendered by Python from the approved design (dark, table layout,
inline styles). Fixed sections, in order, each left out when empty:
**Note, Owed, Today, Due soon, Yesterday, One push.**

Claude writes only the Note (≤ 2 sentences) and the One push line. Every number
comes from data, which makes the prompt much shorter than today's.
The weekly review uses the same template with different sections:
**Week, Gym, Steps, Study hours vs target, Areas, Debt, Next week.**

Dark email risk: the Gmail iOS app can partly invert dark designs. Mitigation:
`color-scheme: dark` meta tags, solid background colours on every table cell,
and a real-device test before cut-over.

## 13. Phone setup (one-time, manual)

1. Shortcut "Jarvis wake": Automation → Alarm → is stopped → `POST /events wake`.
2. Shortcut "Jarvis gym": Automation → Arrive at gym location → `POST /events gym_arrive`.
3. Shortcut "Jarvis steps": 5 time-of-day automations → Health: steps today → `POST /events steps`.
4. Each Shortcut uses the `phone` token. Each automation has "Ask before running" turned off.

## 14. Cost

| Change | Claude calls/day |
|---|---|
| 05:30 plan | +1 (small) |
| Brief | same count, roughly half the tokens |
| Telegram enforcement | 0 (templates) |
| Check-in action mapping | +1 (small) |
| Old Telegram check-in summary | −1 |

Net: about +1 small call a day, well under +$1/month. The Grok bots' cost is on xAI.

## 15. Build order

Each phase ships and gets tested on its own.

1. **Ledger + events.** `ledger.py`, `POST /events`, `GET /today`, `phone` token. Tests.
2. **Gym wake sequence + debt** in the Telegram daemon. Enforcer state lives in the ledger file, so it survives restarts.
3. **Steps pace.**
4. **05:30 plan + study blocks in Calendar + block checks.**
5. **Email redesign + `voice.py`.** Touches `morning_brief.py` and `weekly_review.py` (diff + `--test` shown).
6. **Areas + Grok check-in routes + `docs/grok_bots.md`.** Retire the Telegram check-in.
7. **Retire ntfy.** Disable the `jarvis-alerts` timer (needs explicit OK).

## 16. Files touched

New: `ledger.py`, `enforcer.py`, `voice.py`, `day_plan.py`, `email_template.py`,
`docs/grok_bots.md`, `deploy/systemd/jarvis-plan.{service,timer}`, tests for each.

Changed: `agent_api.py`, `jarvis_telegram.py`, `morning_brief.py`,
`weekly_review.py`, `evening_checkin.py`, `jarvis_calendar.py` (tagged event
helpers), `term_context.json` (weekly hour targets, entered manually).

Unchanged: `config.py`, `memory_system.py`, cron.

## 17. Risks

- **Geofence reliability.** iOS can fire late or not at all. Hevy at slot +2h is the backstop. Arrival is never the only proof.
- **Hevy logged late.** A session logged after the cutoff flips `missed` to `done` and clears the debt raised for it.
- **Telegram daemon restarts** mid-sequence. State is in the ledger, so the sequence resumes.
- **Notification fatigue.** Hard cap of 12 Telegram messages a day, with the wake sequence exempt.
- **Over-strict on real bad days.** `skip sick` always works, but every use shows in the weekly review.

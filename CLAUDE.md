# Jarvis — Claude Code Agent Instructions

## Project
Personal AI assistant built by Manav (UNSW Year 2). Stack: Python, Flask, Google APIs
(Calendar, Tasks, Gmail, Health), Hevy API, St. George finance CSV, Anthropic API,
Mem0, claude-mem for session memory.

Key files:
- `morning_brief.py` — daily 7am email, Sunday triggers weekly review
- `weekly_review.py` — Sunday deep-dive
- `weekly_intelligence.py` — trend analysis across weeks
- `finance_tracker.py` — St. George CSV parser
- `term_context.py` + `term_context.json` — uni/internship/mentor tracking (new)
- `jarvis_calendar.py` — Google Calendar + Tasks read/write
- `jarvis_mem0.py` — Mem0 episodic memory
- `dashboard.py` — Flask server at localhost:5555
- `config.py` — API keys and settings (never modify without asking)

---

## Agent Workflow

Every task follows this pipeline. Do not skip phases.

### Phase 1 — INPUT VERIFICATION
Before writing any code:
- Restate what you understand the task to be in 1-2 sentences
- Flag any ambiguity or missing information
- Ask ONE clarifying question if needed (not multiple)
- If no ambiguity: state "Understood — proceeding" and move on

### Phase 2 — REASONING (for non-trivial tasks)
For any task involving architecture, a new module, or significant changes:
- State the approach in bullet points before writing code
- Identify which existing files will be touched
- Call out any risks (breaking existing behaviour, API costs, etc.)
- Wait for confirmation if the approach involves risk

For small tasks (bug fixes, minor edits): skip to Phase 3 directly.

### Phase 3 — IMPLEMENTATION
- Make the smallest change that solves the problem
- Preserve existing patterns (safe_import pattern, try/except wrapping, TIMEZONE from config)
- Never modify `config.py` without explicit instruction
- Never hardcode credentials, paths, or email addresses
- Add a docstring to every new function

### Phase 4 — VALIDATION
After every code change:
- State what you changed and why
- Identify what could break
- Provide the exact test command to verify it works
- If you touched morning_brief.py or weekly_review.py: provide `--test` command

### Phase 5 — HANDOFF
At the end of every session:
- Summarise what was completed (2-3 bullets)
- List any open items or follow-ups
- State any `term_context.json` updates the user should make manually

---

## Memory Usage (claude-mem)

claude-mem is installed. Use the 3-layer pattern to minimise tokens:
1. `search` first — get the compact index
2. `timeline` if you need chronological context
3. `get_observations` only for the specific IDs that are relevant

At session start: search for the current task area before asking Manav to re-explain.
Never load full observation history upfront.

---

## Code Standards

**Imports:** always wrap new module imports in try/except so the dashboard/brief
still runs if a module fails.

```python
try:
    from term_context import get_term_summary, get_flags
    TERM_CONTEXT_AVAILABLE = True
except Exception:
    TERM_CONTEXT_AVAILABLE = False
```

**API calls:** all Anthropic API calls go through the existing client in the file.
Do not instantiate new clients. Reuse the pattern already in the file.

**Costs:** flag any change that increases API call frequency. Jarvis runs on ~$3/month —
any feature that adds >$1/month needs explicit approval.

**File writes:** only write to `data/`, `finance/`, or the root jarvis dir.
Never write outside `/Users/manavjain/jarvis/`.

---

## Reasoning Depth by Task Type

| Task | Reasoning required |
|------|--------------------|
| Bug fix, typo, config change | None — just fix it |
| New function in existing module | Brief — state approach first |
| New module or file | Full Phase 2 before any code |
| Architecture change | Full Phase 2 + wait for confirmation |
| Anything touching `morning_brief.py` | Always show diff + test command |

---


---

## Newer behaviours (auto)

- **Mentor follow-up drafts** (`followups.py`): morning brief calls
  `maybe_draft_mentor_followup` when `get_mentor_flags` would fire (~7 days
  silent + awaiting_response). Creates a Gmail *draft* only (never sends).
  Idempotent via `data/mentor_followup_draft.json`. Needs `mentor.email` in
  `term_context.json` and `gmail.compose` scope (`python morning_brief.py --setup`
  if drafts 403). Manual: `python followups.py --draft` or Telegram `/draftmentor`.

- **Calendar deadline sync**: removed — UNSW ICS already syncs assessment dates
  into Google Calendar, so Jarvis does not create `[Jarvis]` events.

- **Daily ledger + gym enforcer** (`ledger.py`, `enforcer.py`, spec in
  `docs/REWORK.md`): `data/ledger/YYYY-MM-DD.json` per day. iOS Shortcuts post
  wake / gym_arrive / steps / sleep to `POST /events` (phone token). The
  Telegram daemon calls `enforcer.tick()` each loop to run the wake sequence,
  handles `up`, `skip <reason>` and photos, verifies with Hevy, and settles
  make-up debt at 23:00. `pace.tick()` nudges when behind step pace (10/13/16/19/21h)
  and the 23:00 close turns the shortfall into tomorrow's step debt.
  `day_plan.py` builds the day at 05:30 (one Claude call for block tasks):
  study/lecture blocks from `term_context.json → study_plan`, written to Google
  Calendar tagged `jarvis=study`, with a done/partial/no check at each block's end.
  Weekly yes/no checks (`study_plan.weekly_checks`, e.g. SCIF1000 pre/post-class).
  Week topics: `course_outlines.py` pulls the public UNSW outline API on Sundays
  (and `/outlines`) into `data/outlines/`; `courses/*.json` hand entries win.
  Money stakes are deferred (trial).
- **Morning brief** is rendered by Python (`email_template.py`, dark design in
  `docs/brief_design.html`). Claude writes only the note + one push as JSON.
  `voice.VOICE_RULES` opens every Jarvis prompt. Preview without sending:
  `python morning_brief.py --preview /tmp/brief.html`.
- **Weekly review** (Sundays) uses the same template (`render_sections`): gym by
  day, steps, study hours vs target, money, debt, next week's topics. One Claude
  call writes the note + three priorities (JSON); the intelligence report's
  trends are context, not a second call. `python weekly_review.py --preview FILE`.

- **Training week** starts on the workout schedule's first day (Sunday):
  `term_context.week_start()` sets the boundary for day_plan targets, the weekly
  review (last complete Sun–Sat week) and Hevy's "this week". A session logged on
  a rest day counts for the next training day (`hevy.assign_sessions`; the
  enforcer skips the morning sequence when it finds one).

## Git

- Never include Co-Authored-By lines (or any AI attribution) in commit messages.

## What NOT to do

- Do not refactor working code unless asked
- Do not add dependencies without checking they're in venv
- Do not generate long explanations after a simple fix — be concise
- Do not ask multiple questions at once
- Do not suggest cloud deployments or database migrations unprompted
- Do not touch the cron setup without explicit instruction

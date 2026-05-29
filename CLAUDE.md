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

## Current State (as of term start T2 2026)

**Live modules:** morning_brief, weekly_review, weekly_intelligence, finance_tracker,
google_health, hevy, jarvis_calendar, jarvis_mem0, term_context (new — needs wiring)

**Pending:** wire term_context into morning_brief.py and weekly_review.py
(see `TERM_CONTEXT_INTEGRATION.py` for exact patch locations)

**Internship pipeline:** Canva (OA done), Amazon (applied), Dolby (applied)

**Mentor:** Google mentor — awaiting reply on startup opportunities list

**Subjects:** COMP2511, MATH2601, MATH2901 — due dates not yet in term_context.json

---

## What NOT to do

- Do not refactor working code unless asked
- Do not add dependencies without checking they're in venv
- Do not generate long explanations after a simple fix — be concise
- Do not ask multiple questions at once
- Do not suggest cloud deployments or database migrations unprompted
- Do not touch the cron setup without explicit instruction

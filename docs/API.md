# Jarvis agent API

A small HTTP API so an external assistant can query Jarvis and correct stale
mentor or internship records. It is a separate process from the localhost
dashboard (`dashboard.py`, port 5555, no auth) and from the iPhone spend
logger (`live_spend.py`, port 5556, its own token).

The agent API reuses existing code:

- `POST /ask` uses the Telegram conversational brain (`chat_with_claude`),
  without the tools that queue profile edits or start remote-work sessions.
- `GET /flags` uses `term_context.get_flags` and `study_tracker.get_academic_alerts`.
- `GET /context` uses the term summary, upcoming assessments, and Google Calendar.
- `GET /memory/search` uses Mem0 (`jarvis_mem0.search_memories`).
- `GET /brief` returns the last morning brief or Sunday weekly review saved by
  those jobs.
- `POST /spend` calls `live_spend.log_spend` (the Back Tap path is unchanged).
- `PATCH /mentor`, `PATCH /internships`, and `POST /internships` write
  `term_context.json` through `term_context.mutate_context`, the same locked
  atomic update the flags read on the next request.

## Auth

Every route, including `/health`, requires:

```
Authorization: Bearer $JARVIS_API_TOKEN
```

Set `JARVIS_API_TOKEN` in the VPS `.env` (the file is gitignored). Generate one with:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The process refuses to start if the variable is empty. Comparison is constant-time.
A missing or wrong token gets `401 {"error": "unauthorized"}`.

## Binding and how to expose it

`agent_api.py --serve` listens on **127.0.0.1:5557** only (`JARVIS_API_PORT` overrides
the port). Nothing on the public internet can reach it until you put TLS in front.

Pick one:

- **Cloudflare Tunnel.** Run `cloudflared` on the VPS and point the public
  hostname at `http://127.0.0.1:5557`. Cloudflare terminates TLS.
- **Tailscale Serve.** `tailscale serve` HTTPS to `http://127.0.0.1:5557`, so
  only your tailnet can connect.
- **Reverse proxy.** Caddy or nginx on 443 with a real certificate, proxying
  to `http://127.0.0.1:5557`.

Do not open TCP 5557 in the GCP firewall, and do not change the bind address
to a public interface. The token is a shared secret, not a substitute for TLS.

## Install on the VPS

```
sudo cp deploy/systemd/jarvis-agent-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-agent-api.service
```

Deploys restart the unit only after it is enabled. Confirm:

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" http://127.0.0.1:5557/health
```

Optional: after the service is enabled, add `jarvis-agent-api` to `ALWAYS_ON`
in `watchdog.py` so a dead process raises a Telegram alert.

## Endpoints

Replace `https://<your-host>` with the tunnel or proxy hostname.

### Health

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  https://<your-host>/health
```

`{"ok": true, "service": "jarvis-agent-api"}`

### Ask

One Claude call. Same cost band as a Telegram message. Do not poll.

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "What should I prioritise today?"}' \
  https://<your-host>/ask
```

`{"ok": true, "answer": "..."}`

### Flags

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  https://<your-host>/flags
```

`{"flags": [...], "academic_alerts": [...], "unavailable": []}`

### Deadlines, calendar, term context

`days` is optional, 1–120, default 21.

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  "https://<your-host>/context?days=21"
```

`term` is the term summary (subjects, assessments inside the summary's own
window, internships, mentor, portfolio). `deadlines` is every non-submitted
assessment due inside `days`. `calendar` is Google Calendar events from now
through `days`. If Calendar auth fails, `calendar` is `[]` and
`calendar_error` is `"calendar unavailable"`; term data is still returned.

### Memory search

One Mem0 / embedding call. `limit` is optional, 1–10, default 5.

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  "https://<your-host>/memory/search?q=mentor+follow+up&limit=5"
```

`{"query": "...", "limit": 5, "text": "..."}` — `text` is empty when nothing
relevant is stored.

### Latest brief

Populated the next time the morning brief or Sunday weekly review runs.
Until then this returns `404 {"error": "no brief saved yet"}`.

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  https://<your-host>/brief
```

`kind` is `morning` or `weekly`. `html` is the body that was emailed. `text`
is the same content with tags removed.

### Log a spend

Same categories and validation as the Back Tap logger.

```
curl -sS -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 14.50, "category": "Food & dining", "note": "coffee"}' \
  https://<your-host>/spend
```

`{"ok": true, "logged": "$14.50 Food & dining", "week_total": "$14.50", "entry": {...}}`

### Correct the mentor record

`PATCH /mentor`. Partial update. At least one of these fields is required:

| Field | Rule |
| --- | --- |
| `last_contact` | `YYYY-MM-DD`, not in the future (Australia/Sydney), year ≥ 2000 |
| `last_topic` | string, ≤ 300 characters |
| `awaiting_response` | JSON boolean |
| `next_action` | string, ≤ 300 characters |
| `notes` | string, ≤ 2000 characters |

Every write body also needs `actor` (1–40 characters: letters, digits, `.`, `_`, `-`). Optional `reason` (≤ 300 characters) is stored in the audit log only. Any other key is rejected. JSON `null` is rejected; send `""` to clear a text field.

`GET /flags` nags about the mentor only when `awaiting_response` is true and `last_contact` is 7 or more days ago. A check-in dated today clears that nag even if a reply is still outstanding.

```
curl -sS -X PATCH \
  -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "actor": "career",
    "reason": "Emailed on 7 Sep and sent a check-in on 28 Sep",
    "last_contact": "2026-09-28",
    "last_topic": "Check-in after the 7 Sep email",
    "awaiting_response": true,
    "next_action": "Wait for a reply to the 28 Sep check-in",
    "notes": "Original note 7 Sep; check-in sent 28 Sep"
  }' \
  https://<your-host>/mentor
```

`{"ok": true, "changed": true, "audit_logged": true, "changes": {"last_contact": {"old": "...", "new": "2026-09-28"}}, "mentor": {...}}`

`changed` is false when every supplied value already matches. That response is not audited. `name` and `email` on the mentor record are left as they are.

### Correct an internship

`PATCH /internships`. `company` is required (matched case-insensitively). `role` is required only when that company has more than one application. At least one of `status`, `last_update`, `next_action`, `notes`.

`status` must be exactly one of: `applied`, `OA_completed`, `interview`, `offer`, `rejected`, `withdrawn`.

`last_update` uses the same date rules as `last_contact`. The stored value is the date you send.

Stale nag: a row whose status is not `offer`, `rejected`, or `withdrawn`, and whose `last_update` is 14 or more days ago. `OA_completed` also nags on every brief until the status changes, including when `last_update` is recent. `offer`, `rejected`, and `withdrawn` stop the nag.

```
curl -sS -X PATCH \
  -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "actor": "career",
    "company": "Canva",
    "reason": "Application already handled",
    "status": "rejected",
    "last_update": "2026-09-28",
    "next_action": "No further action",
    "notes": "Closed out from email"
  }' \
  https://<your-host>/internships
```

`{"ok": true, "changed": true, "audit_logged": true, "changes": {...}, "internship": {...}}`

Unknown company is `404`. Two rows for the same company, with no `role`, is `409` and the file is not modified. Other fields already on the row (for example a careers URL) are preserved.

### Add an internship application

`POST /internships`. Required: `company`, `role`, `status`. Optional: `last_update` (defaults to today in Australia/Sydney), `next_action`, `notes`. Same `actor` / `reason` rules. A duplicate company and role (case-insensitive) is `409`.

```
curl -sS -X POST \
  -H "Authorization: Bearer $JARVIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "actor": "career",
    "company": "Atlassian",
    "role": "Software Engineering Intern",
    "status": "applied",
    "last_update": "2026-09-28",
    "next_action": "Wait for OA",
    "notes": "Applied via the careers site"
  }' \
  https://<your-host>/internships
```

`201 {"ok": true, "changed": true, "audit_logged": true, "internship": {...}}`

The new row is what `GET /flags` and `GET /context` read on the next request. No process restart.

### Why writes apply immediately

`term_updates.py` and `update_profile.py` queue overnight Claude suggestions until you approve them (`python term_updates.py`, or Telegram `/approve`). `proposal_trust.py` only counts those approve/reject decisions. Nothing reads the score to allow or block a write.

These routes are the external-agent equivalent of `/mentor` and `/internship`: the caller already holds `JARVIS_API_TOKEN`. They apply in the same locked `mutate_context` path, with a stricter allowlist than the Telegram command's free-form `key=value` pairs. A queued correction would leave `GET /flags` stale until a manual review, so the write hits `term_context.json` directly. The accountability record is the audit log below.

Bad input returns `400 {"error": "..."}` and leaves `term_context.json` unchanged. A failed audit append also leaves the context file unchanged.

### Audit log

Each real change appends one JSON line to `data/agent_api_audit.jsonl` on the VPS (the `data/` directory is gitignored). The line has `ts`, `actor`, `action` (`mentor_update`, `internship_update`, or `internship_add`), `target`, `reason`, `old`, and `new`. There is no HTTP route for the log.

## Laptop sync scripts

`deploy/sync-data-down.sh` and `deploy/sync-finance-up.sh` read
`JARVIS_VPS_USER` and `JARVIS_VPS_HOST` from the environment or the laptop
`.env`. Those names are listed in `.env.example`. Put the real values only
in `.env`.

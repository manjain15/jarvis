# Jarvis agent API

A small HTTP API so an external assistant can query Jarvis. It is a separate
process from the localhost dashboard (`dashboard.py`, port 5555, no auth) and
from the iPhone spend logger (`live_spend.py`, port 5556, its own token).

The agent API reuses existing code:

- `POST /ask` uses the Telegram conversational brain (`chat_with_claude`),
  without the tools that queue profile edits or start remote-work sessions.
- `GET /flags` uses `term_context.get_flags` and `study_tracker.get_academic_alerts`.
- `GET /context` uses the term summary, upcoming assessments, and Google Calendar.
- `GET /memory/search` uses Mem0 (`jarvis_mem0.search_memories`).
- `GET /brief` returns the last morning brief or Sunday weekly review saved by
  those jobs.
- `POST /spend` calls `live_spend.log_spend` (the Back Tap path is unchanged).

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

## Laptop sync scripts

`deploy/sync-data-down.sh` and `deploy/sync-finance-up.sh` read
`JARVIS_VPS_USER` and `JARVIS_VPS_HOST` from the environment or the laptop
`.env`. Those names are listed in `.env.example`. Put the real values only
in `.env`.

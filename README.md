# PhysioRehab ↔ Juvonno Webhook (Phase 2: live booking)

FastAPI server that sits between Retell AI (voice agent "Aria") and the
Juvonno REST API. Retell custom tools call these endpoints during a live
call; the server translates them into Juvonno API calls.

**The Juvonno API key lives only here** (env var). It is never sent to Retell,
never appears in the agent prompt, and never in tool schemas.

## ⚠️ Prerequisite: set a branch code in Juvonno

Juvonno's availability and appointment-list endpoints require a **branch
code** (`/api/appointments/availability/{branchCode}`,
`/api/appointments/list/{branchCode}`), and PhysioRehab's branch currently
has **no code** (returns "Invalid Branch Id or Num provided").

→ In Juvonno: **Setup > Branches > edit "PhysioRehab Center" > set Code to
`PRC`** (any short unique code works), then set `BRANCH_CODE=PRC` on this
server. Until then, availability/booking/reschedule/cancel return a clear
"not configured" error instead of guessing.

## Verified against live Juvonno (2026-09-24, prcenter.juvonno.com)

- `GET /api/staff` → `{"count","data"}`; practitioners have `num` (staff
  number) — appointment `attendants` takes **staff numbers**, not ids.
- `POST /api/appointments` body (`AppointmentPost`): `attendants: [staff_num]`,
  `customer_id` **or** `customer_num`, `date` (YYYY-MM-DD), `time` (military
  "9:00"/"16:30"), `duration` (minutes), one of `branch_id|branch_code|
  branch_name`, plus `schedule_type_id`, `memo`, `reason`, `status`.
- `GET /api/appointments/{id}`; `PUT /api/appointments/{id}` (same body —
  new date/time = reschedule).
- `GET /api/customers/list?page=N` → `{"total","page","count","list"}` (50/page);
  **no server-side phone search** — the webhook scans pages client-side across
  phone/cell/phone2.
- `POST /api/customers` **requires** `first_name`, `last_name`,
  `date_of_birth` (YYYY-MM-DD), `gender` — Aria must collect DOB for new
  patients before booking.
- `GET /api/schedule_types/list` → services; `GET
  /api/schedule_types/{id}/products` → service catalog with `duration`
  (minutes). Real durations are baked into `SERVICES` in `app.py`
  (e.g. Physio Treatment 45, Massage 60, MVA PhysioTreatment 45,
  Physio Assessment 60).
- Still VERIFY on live data: exact shape of the availability/list responses,
  the cancel `status` string, and whether `PUT /appointments/{id}` accepts a
  partial body (date/time/duration only).

## Endpoints (called by Retell custom tools)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check (no auth) |
| GET | `/tools/staff` | List practitioners (id + name) |
| GET | `/tools/services` | Service catalog with real Juvonno durations |
| POST | `/tools/check_availability` | Free slots for date/service/practitioner |
| POST | `/tools/book_appointment` | Find-or-create patient + create appointment |
| POST | `/tools/find_appointments` | Upcoming appointments by phone number |
| POST | `/tools/reschedule_appointment` | Move appointment to a new start time |
| POST | `/tools/cancel_appointment` | Cancel appointment |
| POST | `/retell/post-call` | Retell `call_analyzed` webhook → staff SMS + email when the caller left a message (see below) |
| POST | `/retell/daily-digest` | Compose + email the daily call digest (time, name, phone, summary per call) to the clinic inbox — trigger with a daily scheduler (see below) |

Tool schemas ready to register in Retell: [`retell_tools.json`](retell_tools.json)
(register with `PATCH /update-retell-llm/{llm_id}`, **appending** to the
existing `transfer_call` + `end_call` tools — do not replace them).

## Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `JUVONNO_API_KEY` | ✅ | — | Juvonno API key (from Juvonno profile settings) |
| `JUVONNO_TENANT` | ✅* | — | Tenant subdomain, e.g. `prcenter` → `https://prcenter.juvonno.com/api` |
| `JUVONNO_BASE_URL` | ✅* | built from tenant | Full override, e.g. `https://prcenter.juvonno.com/api` |
| `BRANCH_CODE` | ✅ | — | Branch code (see prerequisite above) — required for availability/booking/find/reschedule/cancel |
| `BRANCH_ID` | no | `1` | Branch id used in booking payloads |
| `WEBHOOK_TOKEN` | recommended | — | Shared secret; Retell tool URLs become `...?token=SECRET` (Retell doesn't sign tool requests) |
| `CLINIC_TIMEZONE` | no | `America/Toronto` | (informational; datetimes sent in clinic-local time) |
| `CLINIC_HOURS_JSON` | no | Mon–Fri 10–19, Sat 9–14, Sun closed | `{"0":[["10:00","19:00"]],...,"6":[]}` (0=Monday) |
| `SLOT_STEP_MINUTES` | no | `15` | Slot grid granularity |
| `MAX_SLOTS_RETURNED` | no | `8` | Cap per availability response (keeps voice replies short) |
| `LOOKAHEAD_DAYS` | no | `14` | (reserved for multi-day search) |
| `PORT` | no | `8000` | |
| `ALERT_SMS_TO` | no | `+16474904541` | Staff SMS alert destinations (comma-separated) |
| `ALERT_EMAIL_TO` | no | `info@physiorehabcenter.com` | Staff email alert destinations (comma-separated) |
| `TWILIO_ACCOUNT_SID` | for SMS | — | Twilio account SID (SMS sender) |
| `TWILIO_AUTH_TOKEN` | for SMS | — | Twilio auth token (SMS sender) |
| `TWILIO_FROM_NUMBER` | for SMS | — | Twilio SMS-capable number, E.164 (e.g. `+16475550100`) |
| `SMTP_HOST` | for email | — | SMTP host for alert emails |
| `SMTP_PORT` | no | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` | for email | — | SMTP username |
| `SMTP_PASS` | for email | — | SMTP password |
| `SMTP_FROM` | no | `Aria <info@physiorehabcenter.com>` | From header on alert emails |
| `RETELL_API_KEY` | recommended | — | Raw Retell API key — used ONLY to verify the `x-retell-signature` header on `/retell/post-call`. Never sent anywhere else. |
| `ALERT_AGENT_ID` | no | Aria's agent id | Only process post-call events for this agent (empty = any) |
| `CALLS_DB_PATH` | no | `calls.db` | SQLite log of analyzed calls (powers the digest) |
| `DIGEST_LOOKBACK_HOURS` | no | `24` | Lookback window for the digest email |

\* one of `JUVONNO_TENANT` / `JUVONNO_BASE_URL` must be set (plus the key).

## Staff message alerts (Retell post-call → SMS + email)

After every call Retell sends a `call_analyzed` webhook and the server
logs it for the end-of-day digest. Instant staff alerts (SMS via Twilio
+ immediate email) are **off by default** — Inder chose the digest only.
Set `ENABLE_MESSAGE_ALERTS=1` to turn them on; then, when the caller left
a message for staff, the server texts it to the clinic number
(`ALERT_SMS_TO`, default +16474904541) and emails the full message + call
summary to the clinic inbox (`ALERT_EMAIL_TO`, default
info@physiorehabcenter.com). SMS is truncated to 320 chars; the email
carries the full message, callback number, call time, summary and
recording link. Logging is redacted — only the call id and delivery
counts are logged, never names, numbers or message text. Retell retries
are deduplicated by `call_id`.

**Retell setup (one-time):**

1. LLM → Post-call analysis → add custom analysis data fields:
   - `message_for_staff` (string) — the message the caller left, empty when none.
     Prompt hint: *"If the caller asked you to pass a message to the clinic
     staff, put their exact message here; otherwise leave empty."*
   - `caller_name` (string), `callback_number` (string).
2. Agent/phone number → Webhooks → add event `call_analyzed` →
   `https://<your-app>/retell/post-call`.
3. Set `RETELL_API_KEY` on this server so the webhook signature is
   verified (HMAC-SHA256, 5-minute replay window).

Twilio: only needed if you later enable instant message alerts
(`ENABLE_MESSAGE_ALERTS=1`); create a Twilio account, buy a Canadian
SMS-capable number, and set the three `TWILIO_*` vars. SMTP: **required**
for the digest — use the clinic mailbox's SMTP details for the five
`SMTP_*` vars. If SMTP is unconfigured, the digest email cannot send
(check `/health`).

### Daily call digest (callback list)

Every analyzed call is logged to a local SQLite DB (`CALLS_DB_PATH`,
default `calls.db`). A daily scheduler triggers the digest, which emails
the clinic inbox one email per day with time, caller name, phone number
and summary for every call — so staff can call back:

```bash
curl -X POST https://<your-app>/retell/daily-digest \
  -H "x-webhook-token: <WEBHOOK_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"hours": 24}'
```

Example email:

```
Subject: [Aria] Call digest — Thursday, September 24, 2026 (2 calls)

1. 8:53 PM — Harpreet — +19055550100
   Asked about Saturday hours.
   *** Left a message — call back ***

2. 9:53 PM — Unknown caller — +19055550200
   Wanted a physio appointment next week.
```

Only calls newer than the last sent digest are included, so re-running
the scheduler never double-sends. When there are no new calls, no email
goes out. Recommended schedule: daily at 8:00 PM Toronto time.

## Run locally

```bash
cd juvonno-webhook
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export JUVONNO_API_KEY=... JUVONNO_TENANT=yourtenant WEBHOOK_TOKEN=s3cret
uvicorn app:app --port 8000
curl localhost:8000/health
```

## Deploy (pick one — ~5 minutes each)

All three give you a public HTTPS URL. Set the env vars in their dashboard,
then point the Retell tool URLs at `https://<your-app>/tools/...`.

**Render** (easiest)
1. Push this folder to a GitHub repo (private is fine).
2. Render → New → Web Service → connect repo. Build: `pip install -r requirements.txt`,
   Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
3. Add env vars under Environment. Deploy. Copy the `https://*.onrender.com` URL.
   (Free tier sleeps when idle — first call of the day takes ~30s. Paid $7/mo avoids it.)

**Railway**
1. `npm i -g @railway/cli && railway login && railway init` in this folder.
2. `railway variables set JUVONNO_API_KEY=...` (repeat per var), `railway up`.
3. Generate a domain under Settings → Networking.

**Fly.io**
1. `fly launch` (say no to Postgres/Redis), then `fly secrets set JUVONNO_API_KEY=...`.
2. `fly deploy`. App gets `https://<name>.fly.dev`.

## Verify the API mapping (do this once the key exists)

Juvonno's public docs only publish the **customers** and **branches**
schemas; appointment/staff field names are best-guess (see "Uncertainties").
Run the probe — it only reads, never writes:

```bash
export JUVONNO_API_KEY=... JUVONNO_TENANT=yourtenant
python verify_api.py
```

Compare its output with every `VERIFY:` comment in `juvonno_client.py` /
`app.py` and correct the names. Then re-run the curl tests below.

## Smoke tests

```bash
B=https://<your-app>; T=?token=s3cret   # omit T if WEBHOOK_TOKEN unset
curl "$B/tools/staff$T"
curl -X POST "$B/tools/check_availability$T" -H 'Content-Type: application/json' \
  -d '{"date":"2026-09-28","service_type":"physiotherapy"}'
curl -X POST "$B/tools/find_appointments$T" -H 'Content-Type: application/json' \
  -d '{"phone":"6474904541"}'
```

## API uncertainties — verify against the live API

What we **confirmed** from public docs: base URL + `api_key` query param,
plural REST resources (`/api/customers`, `/api/appointments`, `/api/staff`),
Juvonno-ID linking between resources, flexible `*_id`/`*_code`/`*_name`
params, customer create fields (`first_name`, `last_name`, `date_of_birth`,
`gender`), customer `custom_fields`, error code semantics, list envelope
`{"list": [...]}` (from branches).

What is **not published** and marked `VERIFY` in code:

1. **Appointment create fields** — we send `customer_id`, `staff_id`,
   `start`, `end`, `notes`, `status: "booked"` (`APPT_FIELDS` in
   `juvonno_client.py`). Real names/required set unknown; also whether
   `service_id`/`service_name` is needed to book a specific service.
2. **Datetime format** — we send `YYYY-MM-DD HH:MM:SS` (clinic-local).
   ISO-8601 is the fallback if the API rejects it.
3. **Appointment list filters** — we try `start_date/end_date`,
   `from/to`, `date_from/date_to` (+ `staff_id`); if all 400 we fall back
   to unfiltered list + client-side filtering (works, just heavier).
4. **Appointment status values** — we use `"booked"` on create and
   `"cancelled"` on cancel. Real enum unknown.
5. **Cancel mechanism** — implemented as `PUT {"status": "cancelled"}`
   (`CANCEL_MODE="put_status"`). If the API prefers `DELETE
   /api/appointments/{id}`, flip the constant.
6. **Customer search by phone** — no documented endpoint; we `GET
   /api/customers` and filter client-side by phone digits. If the list
   endpoint doesn't exist, `find_appointments` degrades gracefully
   (returns "no record found").
7. **Staff list fields** — we accept `full_name`/`name` or
   `first_name`+`last_name`; adjust `_staff_name()` to the real shape.
8. **Customer create without DOB** — docs mark `date_of_birth` + `gender`
   required; we send `gender: "unknown"` and omit DOB. If the API rejects
   it, the agent must ask for DOB (prompt change) or we generate a
   placeholder — decide after seeing the real validation error.
9. **Email/notes fields on customer** — `email`/`notes` names are guesses.

## Troubleshooting

- `401` from Juvonno → wrong `JUVONNO_API_KEY`.
- `403` → key lacks permission for that resource (check key scope in Juvonno).
- `400` with detailed message → **read it**: Juvonno validations are
  explicit and will name the exact bad/missing field — that's the fastest
  way to fix the `VERIFY` guesses.
- Retell tool timeouts → raise `timeout_ms` in `retell_tools.json`
  (default 60s) or reduce `MAX_SLOTS_RETURNED`.
- Double-booking → `book_appointment` re-checks the exact slot just before
  creating; concurrent bookings seconds apart are still theoretically
  possible (Juvonno-side validation is the final guard).

## Privacy

This server handles patient names, phone numbers and appointment details
(PHI). Host it on a reputable provider, keep the API key out of logs
(it's redacted from our debug logs), use `WEBHOOK_TOKEN`, and make sure
Retell's BAA covers the data flowing through the voice calls before
going live with real patients.

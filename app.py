"""
PhysioRehab <-> Juvonno webhook server.

Exposes POST endpoints that Retell AI custom tools call during a live call,
so the voice agent (Aria) can check real availability, book, reschedule and
cancel appointments in Juvonno.

Verified 2026-09-24 against the live tenant (prcenter.juvonno.com) and the
official OpenAPI spec (api.swaggerhub.com/apis/globaloffice/juvonno):

  - POST /api/appointments {attendants:[staff_num], customer_id|customer_num,
    date, time (military), duration (min), branch_id|branch_code|branch_name,
    schedule_type_id, memo, reason, status}
  - GET /api/appointments/{id} ; PUT /api/appointments/{id} (same body)
  - GET /api/appointments/list/{branchCode}?start_date&end_date&status
  - GET /api/appointments/availability/{branchCode}?start_date&end_date
  - GET /api/customers/list?page=N (50/page, no server-side search)
  - POST /api/customers requires first_name, last_name, date_of_birth, gender
  - GET /api/schedule_types/list ; GET /api/schedule_types/{id}/products

Security: the Juvonno API key lives ONLY here (env var), never in the Retell
prompt or tool schemas. Optionally gate the endpoints with WEBHOOK_TOKEN.

Env:
  JUVONNO_API_KEY      (required)
  JUVONNO_TENANT       (required, e.g. prcenter -> prcenter.juvonno.com)
  JUVONNO_BASE_URL     (optional override)
  BRANCH_CODE          (required for availability/appointment-list; the clinic
                        branch must have a code set in Juvonno Setup>Branches)
  BRANCH_ID            (default 1; used for booking payloads)
  WEBHOOK_TOKEN        (optional shared secret for the tool endpoints)
  CLINIC_HOURS_JSON    (optional override of default clinic hours)
  SLOT_STEP_MINUTES    (default 15)   MAX_SLOTS_RETURNED (default 8)

Staff message alerts (Retell post-call webhook -> SMS + email):
  ALERT_SMS_TO         (default +16474904541, comma-separated for more)
  ALERT_EMAIL_TO       (default info@physiorehabcenter.com, comma-separated)
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER  (SMS sender)
  SMTP_HOST / SMTP_PORT (default 587) / SMTP_USER / SMTP_PASS / SMTP_FROM
  RETELL_API_KEY       (only used to verify the x-retell-signature header)
  ALERT_AGENT_ID       (default Aria's agent id; empty = any agent)

Daily call digest (callback list for staff):
  CALLS_DB_PATH        (default calls.db — SQLite log of analyzed calls)
  DIGEST_LOOKBACK_HOURS (default 24; scheduler hits POST /retell/daily-digest)

Run:  uvicorn app:app --port 8000
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import ssl
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date as date_cls
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from juvonno_client import JuvonnoClient, JuvonnoError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")


# ---------------------------------------------------------------- config ---
def _getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _getenv_json(name: str, default: dict) -> dict:
    raw = _getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("env %s is not valid JSON, using default", name)
        return default


JUVONNO_API_KEY = _getenv("JUVONNO_API_KEY")
JUVONNO_TENANT = _getenv("JUVONNO_TENANT")
JUVONNO_BASE_URL = _getenv(
    "JUVONNO_BASE_URL",
    f"https://{JUVONNO_TENANT}.juvonno.com" if JUVONNO_TENANT else "",
)
BRANCH_CODE = _getenv("BRANCH_CODE")
BRANCH_ID = int(_getenv("BRANCH_ID", "1") or "1")
WEBHOOK_TOKEN = _getenv("WEBHOOK_TOKEN")
CLINIC_TIMEZONE = _getenv("CLINIC_TIMEZONE", "America/Toronto")
SLOT_STEP_MINUTES = int(_getenv("SLOT_STEP_MINUTES", "15"))
MAX_SLOTS_RETURNED = int(_getenv("MAX_SLOTS_RETURNED", "8"))
LOOKAHEAD_DAYS = int(_getenv("LOOKAHEAD_DAYS", "14"))

# ---- staff message alerts (Retell post-call webhook -> SMS + email) ----
# Destinations: SMS goes to the clinic number, email to the clinic inbox.
ALERT_SMS_TO = [n.strip() for n in
                _getenv("ALERT_SMS_TO", "+16474904541").split(",") if n.strip()]
ALERT_EMAIL_TO = [e.strip() for e in
                  _getenv("ALERT_EMAIL_TO",
                          "info@physiorehabcenter.com").split(",") if e.strip()]
TWILIO_ACCOUNT_SID = _getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = _getenv("TWILIO_FROM_NUMBER")
SMTP_HOST = _getenv("SMTP_HOST")
SMTP_PORT = int(_getenv("SMTP_PORT", "587") or "587")
SMTP_USER = _getenv("SMTP_USER")
SMTP_PASS = _getenv("SMTP_PASS")
SMTP_FROM = _getenv("SMTP_FROM", "Aria <info@physiorehabcenter.com>")
# Raw Retell API key: used ONLY to verify the x-retell-signature header on
# the post-call webhook. Never sent anywhere else.
RETELL_API_KEY = _getenv("RETELL_API_KEY")
# ---- instant message alerts (currently OFF — digest only) ----
# Set ENABLE_MESSAGE_ALERTS=1 to also text/email staff the moment a caller
# leaves a message. Default off: Inder chose the end-of-day digest only.
ENABLE_MESSAGE_ALERTS = _getenv("ENABLE_MESSAGE_ALERTS", "0") == "1"

# ---- instant booking-failure alerts (ON by default) ----
# A caller who wanted to book but was NOT booked in Juvonno must reach
# staff immediately (email to the clinic inbox, SMS if Twilio is set up),
# even in digest-only mode. Set ENABLE_BOOKING_FAILURE_ALERTS=0 to turn
# these off too.
ENABLE_BOOKING_FAILURE_ALERTS = _getenv(
    "ENABLE_BOOKING_FAILURE_ALERTS", "1") == "1"

# ---- instant booking-completed alerts (ON by default) ----
# When Aria successfully books an appointment, the clinic inbox gets an
# email with who/when/why so staff see every phone booking. Email only
# (no SMS) to avoid text noise. Set
# ENABLE_BOOKING_COMPLETED_ALERTS=0 to turn these off.
ENABLE_BOOKING_COMPLETED_ALERTS = _getenv(
    "ENABLE_BOOKING_COMPLETED_ALERTS", "1") == "1"

# Only process post-call events for Aria's agent (empty = accept any agent).
ALERT_AGENT_ID = _getenv("ALERT_AGENT_ID", "agent_bd831504019513a74e71f83d2d")

# ---- daily call digest (callback list for staff) ----
# Every call_analyzed event is logged to a local SQLite DB; a daily scheduler
# hits POST /retell/daily-digest, which emails the last 24h of calls
# (time, name, phone, summary) to the clinic inbox.
CALLS_DB_PATH = _getenv("CALLS_DB_PATH", "calls.db")
DIGEST_LOOKBACK_HOURS = int(_getenv("DIGEST_LOOKBACK_HOURS", "24") or "24")

# Clinic hours: weekday (0=Mon) -> list of [open, close] in HH:MM.
# PhysioRehab actual hours: Mon-Fri 10:00-19:00, Sat 09:00-14:00, Sun closed.
DEFAULT_HOURS = {
    "0": [["10:00", "19:00"]], "1": [["10:00", "19:00"]],
    "2": [["10:00", "19:00"]], "3": [["10:00", "19:00"]],
    "4": [["10:00", "19:00"]], "5": [["09:00", "14:00"]],
    "6": [],
}
CLINIC_HOURS = _getenv_json("CLINIC_HOURS_JSON", DEFAULT_HOURS)

# service key -> (schedule_type_id, default duration minutes)
# Durations verified live from /api/schedule_types/{id}/products.
SERVICES = {
    "physiotherapy":   {"schedule_type_id": 2, "duration": 45},  # Physio Treatment
    "physio_assessment": {"schedule_type_id": 9, "duration": 60},  # Physio Assessment
    "mva":             {"schedule_type_id": 2, "duration": 45},  # MVA PhysioTreatment
    "mva_assessment":  {"schedule_type_id": 9, "duration": 60},  # Motor Vehicle Accident Physio
    "mva_reassessment": {"schedule_type_id": 9, "duration": 30},  # MVA Physiotherapy Reassessment
    "whiplash":        {"schedule_type_id": 2, "duration": 45},
    "pelvic":          {"schedule_type_id": 2, "duration": 45},  # Pelvic Physio Treatment
    "pelvic_assessment": {"schedule_type_id": 9, "duration": 60},  # Pelvic Physio
    "massage":         {"schedule_type_id": 6, "duration": 60},  # 60 Mins Massage Therapy
    "massage_30":      {"schedule_type_id": 6, "duration": 30},
    "massage_45":      {"schedule_type_id": 6, "duration": 45},
    "massage_90":      {"schedule_type_id": 6, "duration": 90},
    "prenatal_massage": {"schedule_type_id": 8, "duration": 60},
    "postnatal_massage": {"schedule_type_id": 5, "duration": 60},
    "wsib":            {"schedule_type_id": 2, "duration": 30},  # WSIB Treatment
    "follow_up":       {"schedule_type_id": 2, "duration": 45},
}

app = FastAPI(title="PhysioRehab Juvonno webhook")


@app.on_event("startup")
def _warm_customer_index() -> None:
    """Build the customer phone index in the background on boot so the
    first find_appointments call doesn't pay the full-scan cost."""
    try:
        client = _client()
    except Exception as e:
        log.warning("startup: skipping customer index warm (%s)", e)
        return
    _kv_set("customer_index_built_at",
            datetime.now().isoformat(timespec="seconds"))  # claim: no dupes
    threading.Thread(target=_rebuild_customer_index, args=(client,),
                     daemon=True).start()


def _client() -> JuvonnoClient:
    if not JUVONNO_API_KEY or not JUVONNO_BASE_URL:
        raise HTTPException(
            500, "Server misconfigured: JUVONNO_API_KEY and "
                 "JUVONNO_TENANT (or JUVONNO_BASE_URL) must be set.")
    return JuvonnoClient(JUVONNO_BASE_URL, JUVONNO_API_KEY)


def _need_branch_code() -> str:
    if not BRANCH_CODE:
        raise HTTPException(
            500, "Server misconfigured: BRANCH_CODE is not set. The clinic "
                 "branch needs a code in Juvonno (Setup > Branches) for the "
                 "availability and appointment-list endpoints.")
    return BRANCH_CODE


# ---------------------------------------------------------------- helpers --
def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _same_phone(a: str, b: str) -> bool:
    da, db = _digits(a), _digits(b)
    return bool(da and db) and da[-10:] == db[-10:]


def _parse_dt(value: str) -> datetime:
    return date_parser.parse(value)


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").lstrip("0")


def _staff_name(s: dict) -> str:
    first = s.get("first_name", "")
    last = s.get("last_name", "")
    return f"{first} {last}".strip() or f"staff #{s.get('id')}"


def _staff_num(s: dict) -> str:
    return str(s.get("num") or "").strip()


def _appt_datetime(a: dict) -> tuple[datetime | None, datetime | None]:
    """Extract (start, end) from an appointment dict.

    Verified live shape (2026-09-24):
      {"date": {"start": "2026-09-28T16:00:00-04:00",
                "end": "2026-09-28T17:00:00-04:00"}, "duration": 60, ...}
    Falls back to older-guessed shapes for safety."""
    date_field = a.get("date")
    if isinstance(date_field, dict):
        try:
            start = date_parser.parse(str(date_field["start"]))
        except Exception:
            start = None
        try:
            end = date_parser.parse(str(date_field["end"]))
        except Exception:
            end = None
        if start:
            if not end and a.get("duration"):
                try:
                    end = start + timedelta(minutes=int(float(a["duration"])))
                except Exception:
                    end = None
            return start, end
    start = None
    for key in ("start", "start_time", "start_datetime", "datetime",
                "appointment_start", "slot_start"):
        if a.get(key):
            try:
                start = date_parser.parse(str(a[key]))
                break
            except Exception:
                continue
    # date + time pair (Juvonno AppointmentPost style)
    if not start and a.get("date") and a.get("time"):
        try:
            start = date_parser.parse(f"{a['date']} {a['time']}")
        except Exception:
            pass
    if not start:
        return None, None
    end = None
    for key in ("end", "end_time", "end_datetime", "appointment_end"):
        if a.get(key):
            try:
                end = date_parser.parse(str(a[key]))
                break
            except Exception:
                continue
    if not end:
        for key in ("duration", "duration_minutes", "length"):
            if a.get(key):
                try:
                    end = start + timedelta(minutes=int(float(a[key])))
                    break
                except Exception:
                    continue
    return start, end


def _appt_customer_id(a: dict) -> int | None:
    cust = a.get("customer") or a.get("patient") or a.get("client")
    for v in (a.get("customer_id"), a.get("patient_id"),
              cust.get("id") if isinstance(cust, dict) else None,
              (cust or {}).get("id") if isinstance(cust, dict) else None):
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


_CANCELLED_STATUSES = {"cancelled", "cancel", "cancelled_by_patient",
                       "canceled", "cancelled_by_clinic"}


def _practitioner_busy(client: JuvonnoClient, branch_code: str,
                       day: date_cls) -> dict[str, list[tuple[datetime, datetime]]]:
    """Real busy intervals per practitioner for one day, from the
    appointments list — NOT the availability grid.

    Verified 2026-10-17: the grid still offered 11:00/11:15/11:30/11:45
    after the 11:00-11:45 booking existed, so the grid cannot be trusted
    for overlap detection. Times are normalized to naive America/Toronto
    wall time to match the grid's naive datetimes. Cancelled
    appointments do not block. Returns {staff_num: [(start, end)]}."""
    tz = ZoneInfo("America/Toronto")
    start = day.strftime("%Y-%m-%d")
    end = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    busy: dict[str, list[tuple[datetime, datetime]]] = {}
    for a in _list_appts_in_range(client, branch_code, start, end):
        status = str(a.get("status") or "").strip().lower()
        if status in _CANCELLED_STATUSES:
            continue
        s, e = _appt_datetime(a)
        if not s or not e:
            continue
        if s.tzinfo is not None:
            s = s.astimezone(tz).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(tz).replace(tzinfo=None)
        attendants = a.get("attendants") or []
        nums = {str(x.get("num") or "") for x in attendants
                if isinstance(x, dict) and x.get("num") is not None}
        if not nums:
            continue
        for num in nums:
            busy.setdefault(num, []).append((s, e))
    return busy


def _availability_slots(client: JuvonnoClient, branch_code: str,
                        day: date_cls, schedule_type_id: int,
                        practitioner_num: str | None = None,
                        duration_min: int = 15,
                        now: datetime | None = None) -> list[dict]:
    """Real per-staff slot grid from
    GET /api/appointments/availability/{branchCode}?start_date&end_date.

    Verified live shape (2026-09-24):
      {"list": [{"staff": {"id","num","first_name","last_name"},
                 "slots": {"YYYY-MM-DD": [{"available": bool,
                    "time": {"time": "10:00", "meridiem": "am", "interval": 15},
                    "schedule_type": {"id","code","name"}}]}}],
       "status": 200}
    Only start times whose schedule_type matches the requested service AND
    which have enough contiguous available intervals to cover the service's
    full duration (same staff, same schedule type) are returned, so Aria
    never offers a start whose tail end is unavailable. Caller must still
    double-check at booking time."""
    now = now or datetime.now()
    raw = client.get_availability(branch_code,
                                  start_date=day.strftime("%Y-%m-%d"),
                                  end_date=day.strftime("%Y-%m-%d"))
    entries = raw.get("list") if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        entries = []
    day_key = day.strftime("%Y-%m-%d")
    slots: list[dict] = []
    # Overlap guard: the grid does not reflect real bookings, so filter
    # every candidate start against the practitioner's real appointments.
    # On lookup failure, fall back to grid-only rather than failing the
    # whole availability check; _slot_free re-checks at booking time.
    try:
        busy_by_staff = _practitioner_busy(client, branch_code, day)
    except Exception as e:
        log.warning("busy-interval lookup failed, using grid only: %s", e)
        busy_by_staff = {}
    for entry in entries:
        staff = entry.get("staff") or {}
        if practitioner_num and str(staff.get("num") or "") != practitioner_num:
            continue
        name = f"{staff.get('first_name', '')} {staff.get('last_name', '')}".strip()
        staff_busy = busy_by_staff.get(str(staff.get("num") or ""), [])
        # Sorted per-staff grid points: (start, step_min, schedule_type_id,
        # available, schedule_type_name)
        points: list[tuple[datetime, int, int, bool, str]] = []
        for s in (entry.get("slots") or {}).get(day_key, []):
            t = s.get("time") or {}
            try:
                step = int(t.get("interval") or 15)
            except (TypeError, ValueError):
                step = 15
            try:
                start = date_parser.parse(
                    f"{day_key} {t.get('time', '')} {t.get('meridiem', '')}")
            except Exception:
                continue
            st = s.get("schedule_type") or {}
            try:
                st_id = int(st.get("id") or 0)
            except (TypeError, ValueError):
                st_id = 0
            points.append((start, step, st_id, bool(s.get("available")),
                           str(st.get("name") or "")))
        points.sort(key=lambda p: p[0])
        for i, (start, step, st_id, available, st_name) in enumerate(points):
            if not available or st_id != schedule_type_id or start <= now:
                continue
            needed = max(1, -(-duration_min // step))  # ceil div
            ok = True
            for j in range(1, needed):
                if i + j >= len(points):
                    ok = False
                    break
                nxt_start, nxt_step, nxt_st, nxt_avail, _ = points[i + j]
                if (not nxt_avail or nxt_st != schedule_type_id
                        or nxt_start != start + timedelta(minutes=j * step)):
                    ok = False
                    break
            if not ok:
                continue
            end = start + timedelta(minutes=duration_min)
            # Real-appointment overlap: never offer a start whose
            # [start, end) overlaps this practitioner's existing
            # booking. Back-to-back is fine (10-11 then 11-12).
            if any(_overlaps(start, end, b[0], b[1])
                   for b in staff_busy):
                continue
            slots.append({
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "start_display": (start.strftime("%A, %B %-d at ")
                                  + _fmt_time(start)),
                "practitioner": name,
                "practitioner_id": staff.get("id"),
                "practitioner_num": staff.get("num"),
                "schedule_type": st_name,
            })
    slots.sort(key=lambda x: x["start"])
    return slots


def _overlaps(s1: datetime, e1: datetime, s2: datetime, s3: datetime) -> bool:
    return s1 < s3 and s2 < e1


def _resolve_staff(client: JuvonnoClient,
                   practitioner: str | None) -> tuple[dict | None, list[dict]]:
    """Match practitioner by name, staff num, or id. Returns (match, all)."""
    staff = [s for s in client.list_staff()
             if s.get("staff_type") == "practitioner"
             and _staff_num(s).lower() != "inactive"]
    if not practitioner:
        return None, staff
    p = practitioner.strip().lower()
    if p.isdigit():
        for s in staff:
            if str(s.get("id")) == p:
                return s, staff
    for s in staff:  # exact staff-num match (e.g. "NavdeepBajaj")
        if _staff_num(s).lower() == p:
            return s, staff
    matches = [s for s in staff if p in _staff_name(s).lower()]
    if len(matches) == 1:
        return matches[0], staff
    return None, staff  # 0 or 2+ -> caller clarifies


def _find_customer_by_phone(client: JuvonnoClient, phone: str,
                            name: str | None = None) -> dict | None:
    """customers/list has no server-side search. Fast path: local phone
    index (verified against Juvonno). Fallback: scan pages client-side,
    matching across phone/cell/phone2/default_phone_value."""
    norm = _phone_norm(phone)
    if norm:
        _index_rebuild_if_stale(client)
        cid = _index_lookup(norm)
        if cid:
            try:
                c = client.get_customer(cid)
                fields = [c.get("phone"), c.get("cell"), c.get("phone2"),
                          c.get("default_phone_value")]
                if any(_same_phone(str(f or ""), phone) for f in fields):
                    return c
                log.info("stale customer index entry for %s; rescanning",
                         phone)
            except JuvonnoError as e:
                log.warning("indexed customer fetch failed: %s", e)
    cands: list[dict] = []
    try:
        for c in client.iter_all_customers():
            fields = [c.get("phone"), c.get("cell"), c.get("phone2"),
                      c.get("default_phone_value")]
            if any(_same_phone(str(f or ""), phone) for f in fields):
                cands.append(c)
    except JuvonnoError as e:
        log.warning("customer scan failed: %s", e)
        return None
    if name and len(cands) > 1:
        first = name.strip().split()[0].lower()
        cands = [c for c in cands
                 if str(c.get("first_name", "")).lower() == first]
    if cands:
        _index_customer_phones(cands[0])
    return cands[0] if cands else None


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "Unknown", "Unknown"
    if len(parts) == 1:
        return parts[0], "-"
    return parts[0], " ".join(parts[1:])


def _list_appts_in_range(client: JuvonnoClient, branch_code: str,
                         start_date: str, end_date: str,
                         status: str | None = None) -> list[dict]:
    """GET /api/appointments/list/{branchCode}; unwrap liberally. Pages
    through start/results so growing clinics never silently miss items."""
    out: list[dict] = []
    start, page = 0, 200
    while True:
        raw = client.list_appointments(branch_code, start_date=start_date,
                                       end_date=end_date, status=status,
                                       start=start, results=page)
        items: list[dict] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            for key in ("list", "appointments", "data"):
                if isinstance(raw.get(key), list):
                    items = raw[key]
                    break
        out.extend(items)
        if len(items) < page:
            break
        start += page
    return out


def _busy_intervals(client: JuvonnoClient, branch_code: str,
                    day: date_cls) -> list[tuple[datetime, datetime]]:
    """Busy blocks for the whole clinic on one day (all practitioners)."""
    start = day.strftime("%Y-%m-%d")
    end = (day + timedelta(days=1)).strftime("%Y-%m-%d")
    busy: list[tuple[datetime, datetime]] = []
    for a in _list_appts_in_range(client, branch_code, start, end):
        s, e = _appt_datetime(a)
        if s and e:
            busy.append((s, e))
    return busy


def _compute_slots(day: date_cls, staff_member: dict,
                   busy: list[tuple[datetime, datetime]],
                   duration_min: int, now: datetime) -> list[dict]:
    """Free start times for one practitioner on one day from clinic hours
    minus busy blocks."""
    hours = CLINIC_HOURS.get(str(day.weekday()), [])
    slots: list[dict] = []
    for open_s, close_s in hours:
        oh, om = map(int, open_s.split(":"))
        ch, cm = map(int, close_s.split(":"))
        cursor = datetime(day.year, day.month, day.day, oh, om)
        close = datetime(day.year, day.month, day.day, ch, cm)
        while cursor + timedelta(minutes=duration_min) <= close:
            end = cursor + timedelta(minutes=duration_min)
            if cursor > now and not any(
                    _overlaps(cursor, end, b[0], b[1]) for b in busy):
                slots.append({
                    "start": cursor.strftime("%Y-%m-%d %H:%M"),
                    "start_display": (cursor.strftime("%A, %B %-d at ")
                                      + _fmt_time(cursor)),
                    "end": end.strftime("%Y-%m-%d %H:%M"),
                    "practitioner": _staff_name(staff_member),
                    "practitioner_id": staff_member.get("id"),
                    "practitioner_num": _staff_num(staff_member),
                    "duration_minutes": duration_min,
                })
            cursor += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def _slot_free(client: JuvonnoClient, branch_code: str,
               start_dt: datetime, duration_min: int,
               staff_num: str, schedule_type_id: int,
               exclude_appt_id: int | None = None) -> bool:
    """Double-booking guard: re-verify the exact slot against Juvonno's live
    availability grid just before booking."""
    slots = _availability_slots(client, branch_code, start_dt.date(),
                                schedule_type_id, staff_num, duration_min,
                                now=datetime.now())
    target = start_dt.strftime("%Y-%m-%d %H:%M")
    return any(s["start"] == target for s in slots)


# ---------------------------------------------------------------- models ---
class AvailabilityIn(BaseModel):
    date: str = Field(...,
        description="Desired date: YYYY-MM-DD (agent converts spoken dates)")
    service_type: str = Field("physiotherapy",
        description="|".join(sorted(SERVICES)))
    practitioner: str | None = Field(None,
        description="Practitioner name; omit for any practitioner")
    time_preference: str | None = Field(None,
        description="morning|afternoon|evening")


class BookIn(BaseModel):
    name: str
    phone: str
    email: str | None = None
    date_of_birth: str | None = Field(None,
        description="YYYY-MM-DD; REQUIRED when the patient is not already "
                    "in Juvonno (the API rejects new patients without it)")
    gender: str = Field("unknown",
        description="male|female|private|unknown")
    service_type: str = "physiotherapy"
    practitioner: str | None = None
    start: str = Field(...,
        description="Chosen slot start: 'YYYY-MM-DD HH:MM' (24h)")
    reason: str | None = None
    accident_date: str | None = None
    claim_number: str | None = None
    is_new_patient: bool = True


class FindIn(BaseModel):
    name: str | None = None
    phone: str = Field(..., description="Patient's phone number")


class RescheduleIn(BaseModel):
    appointment_id: int
    new_start: str = Field(..., description="'YYYY-MM-DD HH:MM' (24h)")


class CancelIn(BaseModel):
    appointment_id: int


def _ok(data: dict) -> dict:
    return {"success": True, **data}


def _fail(message: str) -> dict:
    return {"success": False, "error": message}


async def _tool_body(request: Request, model_cls):
    """Parse a custom-tool call body.

    Retell nests tool arguments under a wrapper key instead of sending
    them as the top-level JSON object; direct API callers send them
    top-level. Observed Retell shapes: {"args": {...}} and
    {"call": {...}}. Accept all of them, including nested combinations
    (e.g. {"args": {"call": {...}}}), and don't depend on the
    Content-Type header being set correctly.

    Strategy: collect every dict reachable by recursively unwrapping
    known wrapper keys (plus top-level+inner merges), then return the
    first candidate that validates against the model. If none
    validates, report the error from the best-overlap candidate.
    """
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    fields = set(getattr(model_cls, "model_fields", {}) or {})
    candidates: list[dict] = []
    seen: set[str] = set()
    queue: list[dict] = [data]
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, dict):
            continue
        fingerprint = json.dumps(cur, sort_keys=True, default=str)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(cur)
        for wrapper in ("args", "arguments", "parameters", "input", "call"):
            inner = cur.get(wrapper)
            if isinstance(inner, dict):
                queue.append(inner)
                # Old merge behaviour: fields split across levels.
                queue.append({**cur, **inner})
    candidates.sort(key=lambda c: len(fields & set(c.keys())),
                    reverse=True)
    last_err: Exception | None = None
    for cand in candidates:
        try:
            return model_cls(**cand)
        except Exception as e:
            last_err = e
    raise ValueError(f"invalid parameters: {last_err}")


def _check_token(token: str | None, header_token: str | None):
    if WEBHOOK_TOKEN and (token or header_token) != WEBHOOK_TOKEN:
        raise HTTPException(401, "Invalid webhook token")


# ------------------------------------------------- staff message alerts --
# Retell sends a `call_analyzed` webhook after each call. When the caller
# left a message for staff, Aria's custom analysis data carries it and we
# forward it instantly: SMS via Twilio to the clinic number + email to the
# clinic inbox. Names, numbers and message text are NEVER written to logs.

_processed_calls: set[str] = set()  # idempotency: Retell retries webhooks


def _mask(value: str) -> str:
    """Last-4 masking for any phone/address that must appear in logs."""
    d = _digits(value)
    return f"***{d[-4:]}" if len(d) >= 4 else "***"


def _verify_retell_signature(raw: bytes, header: str | None) -> bool:
    """Verify Retell's x-retell-signature header.

    Format: v={timestamp_ms},d={hex_hmac_sha256}. The digest is computed
    over raw_body + timestamp with the Retell API key. Rejects replays
    older than 5 minutes."""
    if not RETELL_API_KEY:
        log.warning("RETELL_API_KEY not set; accepting webhook unverified")
        return True
    if not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, digest = parts["v"], parts["d"]
    except Exception:
        return False
    try:
        age_ms = abs(int(time.time() * 1000) - int(ts))
    except (TypeError, ValueError):
        return False
    if age_ms > 5 * 60 * 1000:
        return False
    expected = hmac.new(RETELL_API_KEY.encode("utf-8"),
                        raw + ts.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


def _send_sms_twilio(to_numbers: list[str], body: str) -> int:
    """Send an SMS via Twilio. Returns count accepted by Twilio."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        log.warning("Twilio not configured; SMS alert skipped")
        return 0
    creds = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{TWILIO_ACCOUNT_SID}/Messages.json")
    sent = 0
    for to in to_numbers:
        data = urllib.parse.urlencode(
            {"To": to, "From": TWILIO_FROM_NUMBER, "Body": body}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Basic {creds}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    sent += 1
                else:
                    log.warning("Twilio SMS to %s failed: HTTP %s",
                                _mask(to), resp.status)
        except Exception as e:
            log.warning("Twilio SMS to %s failed: %s", _mask(to), e)
    return sent


def _send_email_smtp(to_addrs: list[str], subject: str, body: str) -> int:
    """Send a plain-text email via SMTP. Returns count of recipients."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        log.warning("SMTP not configured; email alert skipped")
        return 0
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)
    stage = "init"
    try:
        if SMTP_PORT == 465:
            # Implicit TLS from the first byte (e.g. Gmail port 465).
            stage = "connect_ssl"
            context = ssl.create_default_context()
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20,
                                 context=context)
        else:
            # Plain SMTP upgraded with STARTTLS (e.g. Gmail port 587).
            stage = "connect"
            s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        with s:
            log.info("SMTP %s:%d stage=%s ok (user=%s)",
                     SMTP_HOST, SMTP_PORT, stage, SMTP_USER)
            if SMTP_PORT != 465:
                stage = "starttls"
                s.starttls()
                log.info("SMTP %s:%d stage=%s ok", SMTP_HOST, SMTP_PORT, stage)
            stage = "login"
            s.login(SMTP_USER, SMTP_PASS)
            log.info("SMTP %s:%d stage=%s ok (user=%s)",
                     SMTP_HOST, SMTP_PORT, stage, SMTP_USER)
            stage = "send_message"
            s.send_message(msg)
            log.info("SMTP %s:%d stage=%s ok to %d recipient(s)",
                     SMTP_HOST, SMTP_PORT, stage, len(to_addrs))
    except Exception as e:
        log.warning("SMTP email to %s failed at stage=%s: %s %s",
                    ", ".join(_mask(a) for a in to_addrs), stage,
                    type(e).__name__, e)
        return 0
    return len(to_addrs)


def _extract_message_alert(call: dict) -> dict | None:
    """Pull the staff message out of a call_analyzed payload.

    Aria's post-call custom analysis data must define:
      message_for_staff (string, empty when none), caller_name,
      callback_number. Returns None when no message was left."""
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    message = str(custom.get("message_for_staff") or "").strip()
    if not message:
        return None
    caller_name = str(custom.get("caller_name") or "").strip() or "Unknown"
    callback = str(custom.get("callback_number")
                   or call.get("from_number") or "").strip()
    summary = str(analysis.get("call_summary") or "").strip()
    start_ts = call.get("start_timestamp")
    try:
        call_time = (datetime.fromtimestamp(int(start_ts) / 1000)
                     .strftime("%Y-%m-%d %I:%M %p")) if start_ts else "unknown"
    except (TypeError, ValueError):
        call_time = "unknown"
    sms_text = f"PhysioRehab msg from {caller_name}"
    if callback:
        sms_text += f" ({callback})"
    sms_text += f": {message}"
    if len(sms_text) > 320:
        sms_text = sms_text[:317] + "..."
    lines = [
        "A caller left a message for the clinic staff.",
        "",
        f"From: {caller_name}",
        f"Callback: {callback or 'not provided'}",
        f"Call time: {call_time}",
        "",
        "Message:",
        message,
    ]
    if summary:
        lines += ["", "Call summary:", summary]
    recording = call.get("recording_url")
    if recording:
        lines += ["", f"Recording: {recording}"]
    return {
        "sms_text": sms_text,
        "email_subject": f"[Aria] Caller message — {caller_name}",
        "email_body": "\n".join(lines),
    }


def _truthy(value) -> bool:
    """Coerce Retell post-call analysis values to bool.

    The analysis model may emit real booleans, or strings like
    "true"/"yes"/"1" — treat all truthy spellings as True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "y", "t")
    return bool(value)


def _extract_booking_failure_alert(call: dict) -> dict | None:
    """Detect 'wanted to book, Juvonno did not book it' from a
    call_analyzed payload.

    Aria's post-call custom analysis data must define:
      booking_requested (bool), booking_completed (bool),
      caller_name, callback_number, reason_for_call.
    Returns None when no booking was requested, or the booking
    completed. The alert carries the caller's name, callback number
    and reason for calling so staff can call back and book manually."""
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    if not _truthy(custom.get("booking_requested")):
        return None
    if _truthy(custom.get("booking_completed")):
        return None
    caller_name = str(custom.get("caller_name") or "").strip() or "Unknown"
    callback = str(custom.get("callback_number")
                   or call.get("from_number") or "").strip()
    reason = str(custom.get("reason_for_call") or "").strip()
    summary = str(analysis.get("call_summary") or "").strip()
    start_ts = call.get("start_timestamp")
    try:
        call_time = (datetime.fromtimestamp(int(start_ts) / 1000)
                     .strftime("%Y-%m-%d %I:%M %p")) if start_ts else "unknown"
    except (TypeError, ValueError):
        call_time = "unknown"
    sms_text = f"PhysioRehab BOOKING NOT DONE: {caller_name}"
    if callback:
        sms_text += f" ({callback})"
    if reason:
        sms_text += f" — {reason}"
    sms_text += " — please call back to book."
    if len(sms_text) > 320:
        sms_text = sms_text[:317] + "..."
    lines = [
        "A caller wanted to book an appointment but it was NOT booked "
        "in Juvonno.",
        "",
        f"From: {caller_name}",
        f"Callback: {callback or 'not provided'}",
        f"Call time: {call_time}",
        f"Reason for calling: {reason or 'not recorded'}",
        "",
        "Please call them back to complete the booking.",
    ]
    if summary:
        lines += ["", "Call summary:", summary]
    recording = call.get("recording_url")
    if recording:
        lines += ["", f"Recording: {recording}"]
    return {
        "sms_text": sms_text,
        "email_subject": f"[Aria] Booking NOT completed — {caller_name}",
        "email_body": "\n".join(lines),
    }


def _extract_booking_completed_alert(call: dict) -> dict | None:
    """Build the 'appointment booked' staff email from a call_analyzed
    payload. Fires when the post-call analysis says a booking was
    requested and completed. Carries caller name, callback number,
    reason for calling and the call summary (which holds the booked
    day/time/practitioner) so staff can see the booking at a glance.
    Email only — no SMS for completed bookings."""
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    if not _truthy(custom.get("booking_requested")):
        return None
    if not _truthy(custom.get("booking_completed")):
        return None
    caller_name = str(custom.get("caller_name") or "").strip() or "Unknown"
    callback = str(custom.get("callback_number")
                   or call.get("from_number") or "").strip()
    reason = str(custom.get("reason_for_call") or "").strip()
    summary = str(analysis.get("call_summary") or "").strip()
    start_ts = call.get("start_timestamp")
    try:
        call_time = (datetime.fromtimestamp(int(start_ts) / 1000)
                     .strftime("%Y-%m-%d %I:%M %p")) if start_ts else "unknown"
    except (TypeError, ValueError):
        call_time = "unknown"
    lines = [
        "Aria booked an appointment in Juvonno.",
        "",
        f"From: {caller_name}",
        f"Callback: {callback or 'not provided'}",
        f"Call time: {call_time}",
        f"Reason for calling: {reason or 'not recorded'}",
    ]
    if summary:
        lines += ["", "Booking:", summary]
    recording = call.get("recording_url")
    if recording:
        lines += ["", f"Recording: {recording}"]
    return {
        "email_subject": f"[Aria] Appointment booked — {caller_name}",
        "email_body": "\n".join(lines),
    }


# ------------------------------------------------- call log + digest --
# Every analyzed call is logged to SQLite so the daily digest can list all
# calls received (time, caller name, phone, summary) as a callback list
# for staff. A scheduler (daily cron) triggers POST /retell/daily-digest.


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(CALLS_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS calls (
            call_id TEXT PRIMARY KEY, agent_id TEXT,
            from_number TEXT, to_number TEXT,
            start_ts INTEGER, end_ts INTEGER,
            caller_name TEXT, callback_number TEXT,
            summary TEXT, sentiment TEXT, successful INTEGER,
            message_left INTEGER, recording_url TEXT)""")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS customer_phones (
            phone_norm TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            name TEXT,
            updated_at TEXT)""")
    conn.commit()
    return conn


def _kv_get(key: str) -> str:
    try:
        conn = _db()
        row = conn.execute("SELECT value FROM kv WHERE key=?",
                           (key,)).fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _kv_set(key: str, value: str) -> None:
    try:
        conn = _db()
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("kv write failed: %s", e)


# --------------------------------- customer phone index --------------------
# Juvonno's customers/list has no server-side phone search, so a cold lookup
# scans every page (~25s for this clinic) — unusable mid-call. We keep a
# local phone -> customer_id index in SQLite: instant on hit, with a full
# scan as fallback that also refreshes the index. A background thread keeps
# it warm (startup + at most once every 24h).

def _phone_norm(phone: str) -> str:
    d = _digits(phone)
    return d[-10:] if d else ""


def _index_customer_phones(customer: dict) -> None:
    """Upsert every phone field of one customer record into the index."""
    try:
        cid = customer.get("id")
        if not cid:
            return
        name = (f"{customer.get('first_name', '')} "
                f"{customer.get('last_name', '')}").strip()
        now = datetime.now().isoformat(timespec="seconds")
        conn = _db()
        for f in (customer.get("phone"), customer.get("cell"),
                  customer.get("phone2"),
                  customer.get("default_phone_value")):
            norm = _phone_norm(str(f or ""))
            if norm:
                conn.execute(
                    "INSERT OR REPLACE INTO customer_phones "
                    "(phone_norm, customer_id, name, updated_at) "
                    "VALUES (?,?,?,?)", (norm, cid, name, now))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("customer index upsert failed: %s", e)


def _index_lookup(phone_norm: str) -> int | None:
    try:
        conn = _db()
        row = conn.execute(
            "SELECT customer_id FROM customer_phones WHERE phone_norm=?",
            (phone_norm,)).fetchone()
        conn.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def _rebuild_customer_index(client) -> None:
    """Full re-scan of Juvonno customers into the phone index. Slow (~25s);
    always runs in a background thread, never on a call path."""
    try:
        n = 0
        for c in client.iter_all_customers():
            _index_customer_phones(c)
            n += 1
        _kv_set("customer_index_built_at",
                datetime.now().isoformat(timespec="seconds"))
        log.info("customer phone index rebuilt: %d customers", n)
    except Exception as e:
        log.warning("customer index rebuild failed: %s", e)
        _kv_set("customer_index_built_at", "")  # allow a retry later


def _index_rebuild_if_stale(client) -> None:
    try:
        last = _kv_get("customer_index_built_at")
        age = (datetime.now() - datetime.fromisoformat(last)
               ).total_seconds() if last else float("inf")
    except Exception:
        age = float("inf")
    if age < 24 * 3600:
        return
    _kv_set("customer_index_built_at",
            datetime.now().isoformat(timespec="seconds"))  # claim: no stampede
    threading.Thread(target=_rebuild_customer_index, args=(client,),
                     daemon=True).start()


def _to_secs(ts) -> int:
    """Retell timestamps are milliseconds; store seconds."""
    try:
        v = int(ts)
        return v // 1000 if v > 10_000_000_000 else v
    except (TypeError, ValueError):
        return 0


def _record_call(call: dict, message_left: bool) -> None:
    """Persist one analyzed call. Never raises (logging must not break
    alerts). Phone numbers are stored because the digest is a callback
    list — this DB never leaves the server."""
    try:
        analysis = call.get("call_analysis") or {}
        custom = analysis.get("custom_analysis_data") or {}
        conn = _db()
        conn.execute(
            """INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(call.get("call_id") or ""),
             str(call.get("agent_id") or ""),
             str(call.get("from_number") or ""),
             str(call.get("to_number") or ""),
             _to_secs(call.get("start_timestamp")),
             _to_secs(call.get("end_timestamp")),
             str(custom.get("caller_name") or "").strip(),
             str(custom.get("callback_number")
                 or call.get("from_number") or "").strip(),
             str(analysis.get("call_summary") or "").strip(),
             str(analysis.get("user_sentiment") or "").strip(),
             1 if analysis.get("call_successful") else 0,
             1 if message_left else 0,
             str(call.get("recording_url") or "")))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("call log write failed: %s", e)


def _compose_digest(rows: list[tuple], day_label: str) -> tuple[str, str]:
    """Build the callback-list email: time, name, phone prominent per call."""
    tz = ZoneInfo(CLINIC_TIMEZONE)
    lines = [f"PhysioRehab call digest — {day_label}",
             f"{len(rows)} call(s) received. Call back as needed.", ""]
    for i, r in enumerate(rows, 1):
        start_ts, name, callback, from_number, summary, message_left = r
        try:
            t = datetime.fromtimestamp(start_ts, tz).strftime("%-I:%M %p")
        except Exception:
            t = "?"
        phone = callback or from_number or "number unknown"
        lines.append(f"{i}. {t} — {name or 'Unknown caller'} — {phone}")
        if summary:
            lines.append(f"   {summary}")
        if message_left:
            lines.append("   *** Left a message — call back ***")
        lines.append("")
    lines.append("— Aria, PhysioRehab Center")
    subject = f"[Aria] Call digest — {day_label} ({len(rows)} calls)"
    return subject, "\n".join(lines)


class DigestIn(BaseModel):
    hours: int = Field(DIGEST_LOOKBACK_HOURS,
                       description="Lookback window in hours")


# --------------------------------------------------------------- endpoints --
@app.post("/retell/post-call")
async def retell_post_call(request: Request,
                           x_retell_signature: str | None = Header(None)):
    """Retell `call_analyzed` webhook -> staff SMS + email when the caller
    left a message, plus an instant booking-failure alert when the
    caller wanted to book but no booking was completed.

    Retell setup (dashboard -> phone number / agent -> Webhooks):
      event: call_analyzed
      url:   https://<host>/retell/post-call
    LLM custom analysis data fields (Retell dashboard -> LLM ->
    Post-call analysis): message_for_staff, caller_name,
    callback_number, booking_requested, booking_completed,
    reason_for_call.

    Logging is redacted: only the call id and delivery counts are logged,
    never names, numbers or message text."""
    raw = await request.body()
    if not _verify_retell_signature(raw, x_retell_signature):
        raise HTTPException(401, "Invalid Retell webhook signature")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")
    if payload.get("event") != "call_analyzed":
        return {"ok": True, "ignored": True, "event": payload.get("event")}
    call = payload.get("call") or {}
    call_id = str(call.get("call_id") or "")
    if (ALERT_AGENT_ID and call.get("agent_id")
            and call.get("agent_id") != ALERT_AGENT_ID):
        return {"ok": True, "ignored": True, "reason": "different agent"}
    if call_id and call_id in _processed_calls:
        return {"ok": True, "duplicate": True}
    alert = _extract_message_alert(call)
    if call_id:
        _processed_calls.add(call_id)
        if len(_processed_calls) > 5000:  # bounded memory
            _processed_calls.clear()
    # Log every analyzed call for the daily callback digest, whether or
    # not a message was left.
    _record_call(call, alert is not None)
    # Booking-failure alert: always instant (separate flag, default ON).
    # A caller who wanted to book but was not booked in Juvonno must
    # reach staff immediately, even in digest-only mode.
    booking_alert = _extract_booking_failure_alert(call)
    booking_sent = False
    if booking_alert and ENABLE_BOOKING_FAILURE_ALERTS:
        sms_n = _send_sms_twilio(ALERT_SMS_TO, booking_alert["sms_text"])
        email_n = _send_email_smtp(ALERT_EMAIL_TO,
                                   booking_alert["email_subject"],
                                   booking_alert["email_body"])
        booking_sent = sms_n > 0 or email_n > 0
        log.info("post-call %s: booking-failure alert sms=%d email=%d",
                 call_id or "?", sms_n, email_n)
    # Booking-completed alert: email-only, same instant path.
    completed_alert = _extract_booking_completed_alert(call)
    completed_sent = False
    if completed_alert and ENABLE_BOOKING_COMPLETED_ALERTS:
        email_n = _send_email_smtp(ALERT_EMAIL_TO,
                                   completed_alert["email_subject"],
                                   completed_alert["email_body"])
        completed_sent = email_n > 0
        log.info("post-call %s: booking-completed alert email=%d",
                 call_id or "?", email_n)
    if not ENABLE_MESSAGE_ALERTS:
        # Digest-only mode: log the call, skip instant message alerts.
        # (Booking alerts above still fire — separate flags.)
        return {"ok": True, "alert_sent": False,
                "booking_alert_sent": booking_sent,
                "booking_completed_alert_sent": completed_sent,
                "reason": "message alerts disabled (digest only)"}
    if not alert:
        log.info("post-call %s: no message left, no alert", call_id or "?")
        return {"ok": True, "alert_sent": False,
                "reason": "no message left by caller"}
    sms_sent = _send_sms_twilio(ALERT_SMS_TO, alert["sms_text"])
    email_sent = _send_email_smtp(ALERT_EMAIL_TO, alert["email_subject"],
                                  alert["email_body"])
    log.info("post-call %s: message alert sms=%d email=%d",
             call_id or "?", sms_sent, email_sent)
    return {"ok": True, "alert_sent": True,
            "sms_sent": sms_sent, "email_sent": email_sent}


@app.post("/retell/daily-digest")
def retell_daily_digest(body: DigestIn,
                        token: str | None = None,
                        x_webhook_token: str | None = Header(None)):
    """Email the daily call digest to the clinic inbox: one line per call
    with time, caller name, phone number and summary, so staff can call
    back. Meant to be triggered by a daily scheduler (cron). Only calls
    newer than the last sent digest are included, so re-runs never
    double-send."""
    _check_token(token, x_webhook_token)
    hours = max(1, min(int(body.hours or DIGEST_LOOKBACK_HOURS), 72))
    now = int(time.time())
    since = now - hours * 3600
    last = _kv_get("last_digest_ts")
    last_ts = int(last) if last.isdigit() else 0
    try:
        conn = _db()
        rows = conn.execute(
            """SELECT start_ts, caller_name, callback_number, from_number,
                      summary, message_left FROM calls
               WHERE start_ts >= ? AND start_ts > ?
               ORDER BY start_ts""",
            (since, last_ts)).fetchall()
        conn.close()
    except Exception as e:
        return _fail(f"Could not read call log: {e}")
    if not rows:
        return {"ok": True, "sent": False, "reason": "no new calls"}
    tz = ZoneInfo(CLINIC_TIMEZONE)
    day_label = datetime.fromtimestamp(now, tz).strftime("%A, %B %-d, %Y")
    subject, email_body = _compose_digest(rows, day_label)
    sent = _send_email_smtp(ALERT_EMAIL_TO, subject, email_body)
    if sent:
        _kv_set("last_digest_ts", str(now))
    # Redacted: counts only, no names/numbers in logs.
    log.info("daily digest: %d call(s), email to %d recipient(s)",
             len(rows), sent)
    return {"ok": True, "sent": bool(sent), "count": len(rows)}


@app.get("/health")
def health():
    try:
        conn = _db()
        call_count = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        conn.close()
    except Exception:
        call_count = -1
    return {"ok": True,
            "version": "2026-10-18b",
            "juvonno_configured": bool(JUVONNO_API_KEY and JUVONNO_BASE_URL),
            "branch_code_configured": bool(BRANCH_CODE),
            "calls_logged": call_count,
            "alerts_configured": {
                "sms_destinations": len(ALERT_SMS_TO),
                "email_destinations": len(ALERT_EMAIL_TO),
                "twilio": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
                               and TWILIO_FROM_NUMBER),
                "smtp": bool(SMTP_HOST and SMTP_USER and SMTP_PASS),
            }}


@app.get("/tools/staff")
def tool_list_staff(token: str | None = None,
                    x_webhook_token: str | None = Header(None)):
    """Real practitioner names for the agent."""
    _check_token(token, x_webhook_token)
    client = _client()
    try:
        staff = client.list_staff()
    except JuvonnoError as e:
        return _fail(str(e))
    return _ok({"practitioners": [
        {"id": s.get("id"), "name": _staff_name(s)}
        for s in staff
        if s.get("staff_type") == "practitioner"
        and _staff_num(s).lower() != "inactive"
    ]})


@app.get("/tools/services")
def tool_list_services(token: str | None = None,
                       x_webhook_token: str | None = Header(None)):
    """Service catalog with real Juvonno durations."""
    _check_token(token, x_webhook_token)
    return _ok({"services": [
        {"key": k, "schedule_type_id": v["schedule_type_id"],
         "duration_minutes": v["duration"]}
        for k, v in SERVICES.items()
    ]})


@app.post("/tools/check_availability")
async def tool_check_availability(request: Request,
                                  token: str | None = None,
                                  x_webhook_token: str | None = Header(None)):
    _check_token(token, x_webhook_token)
    try:
        body = await _tool_body(request, AvailabilityIn)
    except ValueError as e:
        return _fail(str(e))
    branch_code = _need_branch_code()
    client = _client()
    try:
        day = _parse_dt(body.date).date()
    except Exception:
        return _fail(f"Could not understand date '{body.date}'. "
                     "Use YYYY-MM-DD.")
    if day < datetime.now().date():
        return _fail("That date is in the past. Please pick a future date.")
    svc = SERVICES.get(body.service_type, SERVICES["physiotherapy"])
    schedule_type_id = svc["schedule_type_id"]
    duration = svc["duration"]
    matched, all_staff = _resolve_staff(client, body.practitioner)
    if body.practitioner and not matched:
        names = ", ".join(_staff_name(s) for s in all_staff) or "none listed"
        return _fail(
            f"Couldn't identify practitioner '{body.practitioner}'. "
            f"Available practitioners: {names}.")
    practitioner_num = _staff_num(matched) if matched else None
    try:
        slots = _availability_slots(client, branch_code, day,
                                    schedule_type_id, practitioner_num,
                                    duration)
    except JuvonnoError as e:
        return _fail(f"Juvonno error while checking availability: {e}")
    pref = (body.time_preference or "").lower()
    pref_matched = True
    if pref in ("morning", "afternoon", "evening"):
        def _tod(slot):
            h = int(slot["start"][11:13])
            return ("morning" if h < 12 else
                    "afternoon" if h < 17 else "evening")
        preferred = [s for s in slots if _tod(s) == pref]
        if preferred:
            slots = preferred
        else:
            pref_matched = False  # no slots at preferred time; agent should
            # say so and offer the returned alternatives instead
    if not slots:
        return _ok({"date": day.isoformat(), "slots": [],
                    "message": "No free slots that day for that service. "
                               "Juvonno reports no open slots."})
    return _ok({"date": day.isoformat(),
                "service_type": body.service_type,
                "duration_minutes": duration,
                "time_preference": body.time_preference,
                "time_preference_matched": pref_matched,
                "slots": slots[:MAX_SLOTS_RETURNED],
                "message": f"Found {len(slots)} free slot(s)."})


@app.post("/tools/book_appointment")
async def tool_book_appointment(request: Request,
                                token: str | None = None,
                                x_webhook_token: str | None = Header(None)):
    _check_token(token, x_webhook_token)
    try:
        body = await _tool_body(request, BookIn)
    except ValueError as e:
        return _fail(str(e))
    branch_code = _need_branch_code()
    client = _client()
    try:
        start_dt = _parse_dt(body.start)
    except Exception:
        return _fail(f"Could not understand start time '{body.start}'. "
                     "Use 'YYYY-MM-DD HH:MM'.")
    if start_dt <= datetime.now():
        return _fail("That time is in the past. Please pick a future time.")
    svc = SERVICES.get(body.service_type, SERVICES["physiotherapy"])
    duration = svc["duration"]
    try:
        matched, all_staff = _resolve_staff(client, body.practitioner)
        if body.practitioner and not matched:
            names = ", ".join(_staff_name(s) for s in all_staff)
            return _fail(f"Couldn't identify practitioner "
                         f"'{body.practitioner}'. Available: {names}.")
        if not matched:
            if len(all_staff) == 1:
                matched = all_staff[0]
            else:
                return _fail("Which practitioner? Available: " + ", ".join(
                    _staff_name(s) for s in all_staff))
        staff_num = _staff_num(matched)
        if not staff_num:
            return _fail("That practitioner has no staff number in Juvonno; "
                         "please have staff book this manually.")

        # Re-verify the exact slot is still free (avoid double-booking).
        if not _slot_free(client, branch_code, start_dt, duration,
                          staff_num, svc["schedule_type_id"]):
            return _fail("That slot was just taken. Please pick another time.")

        # Find or create the patient.
        customer = _find_customer_by_phone(client, body.phone, body.name)
        if not customer:
            if not body.date_of_birth:
                return _fail(
                    "This looks like a new patient, and Juvonno requires "
                    "their date of birth to create a record. Please ask for "
                    "the date of birth (YYYY-MM-DD) and try again.")
            first, last = _split_name(body.name)
            try:
                customer = client.create_customer(
                    first, last, body.date_of_birth, body.gender,
                    phone=body.phone, email=body.email)
            except JuvonnoError as e:
                return _fail(
                    "Couldn't create the patient record in Juvonno: "
                    f"{e}. Please have staff book this manually.")
        customer_id = customer.get("id")
        if not customer_id:
            return _fail("Couldn't identify the patient record in Juvonno.")
        _index_customer_phones(customer)  # keep the phone index warm

        memo_parts = [f"Booked by voice agent Aria "
                      f"({'new' if body.is_new_patient else 'returning'} patient)."]
        if body.reason:
            memo_parts.append(f"Reason: {body.reason}")
        if body.accident_date or body.claim_number:
            memo_parts.append(
                "MVA details: accident date "
                f"{body.accident_date or 'not provided'}, claim "
                f"{body.claim_number or 'not provided'}.")
        appt = client.create_appointment(
            attendants=[staff_num],
            date=start_dt.strftime("%Y-%m-%d"),
            time=start_dt.strftime("%H:%M"),
            duration=duration,
            branch_id=BRANCH_ID,
            customer_id=int(customer_id),
            schedule_type_id=svc["schedule_type_id"],
            memo=" ".join(memo_parts),
        )
    except JuvonnoError as e:
        return _fail(f"Juvonno error while booking: {e}")
    return _ok({
        "appointment_id": appt.get("id"),
        "patient": body.name,
        "practitioner": _staff_name(matched),
        "start": start_dt.strftime("%A, %B %-d at ") + _fmt_time(start_dt),
        "duration_minutes": duration,
        "message": "Appointment booked in Juvonno.",
    })


@app.post("/tools/find_appointments")
async def tool_find_appointments(request: Request,
                                 token: str | None = None,
                                 x_webhook_token: str | None = Header(None)):
    _check_token(token, x_webhook_token)
    try:
        body = await _tool_body(request, FindIn)
    except ValueError as e:
        return _fail(str(e))
    branch_code = _need_branch_code()
    client = _client()
    customer = _find_customer_by_phone(client, body.phone, body.name)
    if not customer:
        return _ok({"appointments": [],
                    "message": "No patient record found for that number."})
    cid = customer.get("id")
    try:
        # Juvonno returns tz-aware ISO timestamps; compare against an
        # aware "now" (naive vs aware comparison raises TypeError -> 500).
        now = datetime.now().astimezone()
        upcoming = []
        appts = _list_appts_in_range(
            client, branch_code,
            now.strftime("%Y-%m-%d"),
            (now + timedelta(days=90)).strftime("%Y-%m-%d"))
        for a in appts:
            if _appt_customer_id(a) != cid:
                continue
            s, e = _appt_datetime(a)
            if not s:
                continue
            if s.tzinfo is None:
                s = s.replace(tzinfo=ZoneInfo("America/Toronto"))
            if s < now:
                continue
            attendants = a.get("attendants") or []
            practs = ", ".join(
                f"{x.get('first_name', '')} {x.get('last_name', '')}".strip()
                for x in attendants if isinstance(x, dict))
            upcoming.append({
                "appointment_id": a.get("id"),
                "start": s.strftime("%Y-%m-%d %H:%M"),
                "start_display": s.strftime("%A, %B %-d at ") + _fmt_time(s),
                "practitioner": practs or None,
                "service": ((a.get("schedule_type") or {}).get("name")
                            if isinstance(a.get("schedule_type"), dict)
                            else None),
                "status": a.get("status"),
            })
    except JuvonnoError as e:
        return _fail(f"Juvonno error while looking up appointments: {e}")
    upcoming.sort(key=lambda x: x["start"])
    return _ok({"patient": f"{customer.get('first_name', '')} "
                           f"{customer.get('last_name', '')}".strip(),
                "appointments": upcoming,
                "message": f"Found {len(upcoming)} upcoming appointment(s)."})


@app.post("/tools/reschedule_appointment")
async def tool_reschedule_appointment(request: Request,
                                      token: str | None = None,
                                      x_webhook_token: str | None = Header(None)):
    _check_token(token, x_webhook_token)
    try:
        body = await _tool_body(request, RescheduleIn)
    except ValueError as e:
        return _fail(str(e))
    branch_code = _need_branch_code()
    client = _client()
    try:
        new_start = _parse_dt(body.new_start)
    except Exception:
        return _fail(f"Could not understand new time '{body.new_start}'. "
                     "Use 'YYYY-MM-DD HH:MM'.")
    if new_start <= datetime.now():
        return _fail("That time is in the past. Please pick a future time.")
    try:
        appt = client.get_appointment(body.appointment_id)
    except JuvonnoError as e:
        return _fail(f"Couldn't find appointment "
                     f"#{body.appointment_id}: {e}")
    s_old, e_old = _appt_datetime(appt)
    duration = (int((e_old - s_old).total_seconds() // 60)
                if s_old and e_old else 45)
    attendants = appt.get("attendants") or []
    staff_num = (attendants[0].get("num")
                 if attendants and isinstance(attendants[0], dict) else None)
    st = appt.get("schedule_type") or {}
    schedule_type_id = int(st.get("id") or 0) if isinstance(st, dict) else 0
    try:
        if staff_num and schedule_type_id:
            slot_ok = _slot_free(client, branch_code, new_start, duration,
                                 staff_num, schedule_type_id)
        else:
            slot_ok = True  # can't verify; proceed with caution
        if not slot_ok:
            return _fail("That new time is not free. "
                         "Please pick another time.")
        # PUT accepts the AppointmentPost shape. VERIFY on live data that a
        # partial update (date/time/duration only) is accepted.
        updated = client.update_appointment(body.appointment_id, {
            "date": new_start.strftime("%Y-%m-%d"),
            "time": new_start.strftime("%H:%M"),
            "duration": duration,
        })
    except JuvonnoError as e:
        return _fail(f"Juvonno error while rescheduling: {e}")
    return _ok({
        "appointment_id": body.appointment_id,
        "new_start": new_start.strftime("%A, %B %-d at ")
        + _fmt_time(new_start),
        "message": "Appointment rescheduled in Juvonno.",
    })


@app.post("/tools/cancel_appointment")
async def tool_cancel_appointment(request: Request,
                                  token: str | None = None,
                                  x_webhook_token: str | None = Header(None)):
    _check_token(token, x_webhook_token)
    try:
        body = await _tool_body(request, CancelIn)
    except ValueError as e:
        return _fail(str(e))
    client = _client()
    # The exact "cancel" status string is not in Juvonno's public docs
    # (active appointments show status "open"). Juvonno validates strictly
    # and rejects unknown statuses with a 400 that changes nothing, so we
    # can safely try the likely values in order.
    errors: list[str] = []
    for status_value in ("cancelled", "cancel", "cancelled_by_patient"):
        try:
            client.update_appointment(body.appointment_id,
                                      {"status": status_value})
            return _ok({"appointment_id": body.appointment_id,
                        "cancel_status": status_value,
                        "message": "Appointment cancelled in Juvonno."})
        except JuvonnoError as e:
            errors.append(f"{status_value}: {e}")
    log.error("cancel failed for appt %s: %s", body.appointment_id, errors)
    return _fail("Juvonno did not accept any cancel status "
                 f"({'; '.join(errors)}). Please have staff cancel this "
                 "appointment manually.")


@app.exception_handler(JuvonnoError)
async def _juvonno_error_handler(request: Request, exc: JuvonnoError):
    return JSONResponse(status_code=502,
                        content=_fail(f"Juvonno API error: {exc}"))

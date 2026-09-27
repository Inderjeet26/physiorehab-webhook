"""
Thin REST wrapper around the Juvonno API.

Verified against the live tenant (prcenter.juvonno.com) 2026-09-24 and the
official OpenAPI spec (api.swaggerhub.com/apis/globaloffice/juvonno):

  - Base: https://{tenant}.juvonno.com/api ; api_key ALWAYS a query param.
  - GET /api/staff                    -> {"count": N, "data": [...]}
      practitioner entries have first_name/last_name and "num" (staff number;
      this is what appointment "attendants" expects, NOT the staff id).
  - GET /api/customers/list?page=N    -> {"total","page","count","list":[...]}
      50 per page. No server-side phone/name search: filter client-side.
      Single: GET /api/customers/{id} or /api/customers/chart/{chartNumber}.
  - POST /api/customers requires: first_name, last_name, date_of_birth
      (YYYY-MM-DD), gender (male|female|private|unknown). Optional: phone,
      email, chart_num, custom_fields [{custom_field_id, value}].
  - GET /api/schedule_types/list      -> {"list":[{id,code,name,...}]}
      (services: Physiotherapy id 2, Massage id 6, Pre/Post Natal Massage,
       PT Assessments, ...)
  - GET /api/schedule_types/{id}/products
      -> {"schedule_type":{...},"list":[{id,itemnum,name,duration,...}]}
      duration is in minutes.
  - POST /api/appointments (AppointmentPost):
      attendants: [staff_num, ...]   (REQUIRED, array of staff NUMBERS)
      customer_id | customer_num      (REQUIRED, one of them)
      date                         (REQUIRED, YYYY-MM-DD)
      time                         (REQUIRED, military "9:00" / "16:30")
      duration                     (REQUIRED, minutes)
      branch_id | branch_code | branch_name  (one REQUIRED)
      schedule_type_id, reason, memo, status, items, complaint_id (optional)
    NOTE: the spec's "required" list literally names 'attentdants',
    'customer_num', 'date', 'time', 'duration', 'branch_num' — in practice
    branch_id works (flexible *_id/*_code/*_name params, priority id>code>name).
  - GET /api/appointments/{id} ; PUT /api/appointments/{id} (same body as POST)
      PUT with a new date/time = reschedule.
      VERIFY: exact cancel status string; currently trying {"status": "cancelled"}.
  - GET /api/appointments/list/{branchCode}?start_date&end_date&status&start&results
      status defaults to "open". VERIFY response shape on live data.
  - GET /api/appointments/availability/{branchCode}?start_date&end_date
      -> {"list": ...}  VERIFY response shape on live data.
  - {branchCode} is the BRANCH CODE. PhysioRehab's branch currently has NO
    code set, so these two endpoints 400 until a code is configured in
    Juvonno (Setup > Branches) and passed as BRANCH_CODE.

Everything marked VERIFY is still unconfirmed against live data.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("juvonno")

# VERIFY: exact status string Juvonno uses for a cancelled appointment.
CANCELLED_STATUS = "cancelled"


class JuvonnoError(Exception):
    def __init__(self, message: str, status_code: int | None = None,
                 payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class JuvonnoClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(timeout=timeout)

    # -- low-level ---------------------------------------------------------
    def _request(self, method: str, path: str,
                 params: dict | None = None,
                 body: dict | None = None) -> Any:
        q = {"api_key": self.api_key}
        if params:
            q.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{path}"
        log.debug("%s %s params=%s", method, path,
                  {k: v for k, v in q.items() if k != "api_key"})
        resp = self._http.request(method, url, params=q,
                                  json=body if body is not None else None)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise JuvonnoError(
                f"Juvonno API error {resp.status_code} on {method} {path}: "
                f"{detail}",
                status_code=resp.status_code, payload=detail)
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    # -- staff -------------------------------------------------------------
    def list_staff(self) -> list[dict]:
        """GET /api/staff -> {"count","data"}. Verified live."""
        data = self._request("GET", "/api/staff")
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    # -- branches ----------------------------------------------------------
    def list_branches(self) -> list[dict]:
        data = self._request("GET", "/api/branches")
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
        return []

    # -- customers ---------------------------------------------------------
    def list_customers(self, page: int = 1) -> dict:
        """GET /api/customers/list?page=N -> {"total","page","count","list"}.
        Verified live. No server-side search: filter client-side."""
        return self._request("GET", "/api/customers/list",
                             params={"page": page})

    def iter_all_customers(self) -> Any:
        """Yield every customer across all pages."""
        page = 1
        while True:
            data = self.list_customers(page=page)
            items = data.get("list", []) if isinstance(data, dict) else []
            if not items:
                break
            yield from items
            total = data.get("total", 0)
            if page * data.get("count", 50) >= total:
                break
            page += 1

    def get_customer(self, customer_id: int) -> dict:
        data = self._request("GET", f"/api/customers/{customer_id}")
        return data.get("customer", data) if isinstance(data, dict) else {}

    def get_customer_by_chart(self, chart_num: str) -> dict:
        data = self._request("GET", f"/api/customers/chart/{chart_num}")
        return data.get("customer", data) if isinstance(data, dict) else {}

    def create_customer(self, first_name: str, last_name: str,
                        date_of_birth: str, gender: str = "unknown",
                        phone: str | None = None,
                        email: str | None = None,
                        chart_num: str | None = None) -> dict:
        """POST /api/customers. first_name, last_name, date_of_birth
        (YYYY-MM-DD) and gender are REQUIRED by the API — a voice agent
        must collect DOB before creating a new patient."""
        body: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "date_of_birth": date_of_birth,
            "gender": gender,
        }
        if phone:
            body["phone"] = phone
        if email:
            body["email"] = email
        if chart_num:
            body["chart_num"] = chart_num
        data = self._request("POST", "/api/customers", body=body)
        return data.get("customer", data) if isinstance(data, dict) else {}

    # -- services (schedule types + products) ------------------------------
    def list_schedule_types(self) -> list[dict]:
        """GET /api/schedule_types/list -> {"list":[{id,code,name,...}]}."""
        data = self._request("GET", "/api/schedule_types/list")
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
        return []

    def list_products(self, schedule_type_id: int) -> list[dict]:
        """GET /api/schedule_types/{id}/products -> service catalog with
        per-service duration in minutes."""
        data = self._request(
            "GET", f"/api/schedule_types/{schedule_type_id}/products")
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
        return []

    # -- appointments ------------------------------------------------------
    def list_appointments(self, branch_code: str,
                          start_date: str | None = None,
                          end_date: str | None = None,
                          status: str | None = None,
                          start: int = 0,
                          results: int = 50) -> Any:
        """GET /api/appointments/list/{branchCode}. VERIFY response shape
        against live data once BRANCH_CODE is configured."""
        params: dict[str, Any] = {"start": start, "results": results}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if status:
            params["status"] = status
        return self._request("GET",
                             f"/api/appointments/list/{branch_code}",
                             params=params)

    def get_availability(self, branch_code: str,
                         start_date: str | None = None,
                         end_date: str | None = None) -> Any:
        """GET /api/appointments/availability/{branchCode}. VERIFY response
        shape against live data once BRANCH_CODE is configured."""
        params: dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self._request(
            "GET", f"/api/appointments/availability/{branch_code}",
            params=params or None)

    def get_appointment(self, appointment_id: int) -> dict:
        data = self._request("GET", f"/api/appointments/{appointment_id}")
        return data.get("appointment", data) if isinstance(data, dict) else {}

    def create_appointment(self, attendants: list[str], date: str,
                           time: str, duration: int,
                           branch_id: int | None = None,
                           branch_code: str | None = None,
                           branch_name: str | None = None,
                           customer_id: int | None = None,
                           customer_num: str | None = None,
                           schedule_type_id: int | None = None,
                           reason: str | None = None,
                           memo: str | None = None,
                           status: str | None = None) -> dict:
        """POST /api/appointments. attendants = array of STAFF NUMBERS
        (e.g. "NavdeepBajaj"), NOT staff ids. time is military "9:00"/"16:30",
        duration in minutes."""
        body: dict[str, Any] = {
            "attendants": attendants,
            "date": date,
            "time": time,
            "duration": duration,
        }
        if branch_id is not None:
            body["branch_id"] = branch_id
        elif branch_code:
            body["branch_code"] = branch_code
        elif branch_name:
            body["branch_name"] = branch_name
        if customer_id is not None:
            body["customer_id"] = customer_id
        elif customer_num:
            body["customer_num"] = customer_num
        if schedule_type_id is not None:
            body["schedule_type_id"] = schedule_type_id
        if reason:
            body["reason"] = reason
        if memo:
            body["memo"] = memo
        if status:
            body["status"] = status
        data = self._request("POST", "/api/appointments", body=body)
        return data.get("appointment", data) if isinstance(data, dict) else {}

    def update_appointment(self, appointment_id: int,
                           payload: dict) -> dict:
        """PUT /api/appointments/{id} with AppointmentPost fields.
        New date/time = reschedule; {"status": ...} = status change."""
        data = self._request("PUT",
                             f"/api/appointments/{appointment_id}",
                             body=payload)
        return data.get("appointment", data) if isinstance(data, dict) else {}

    def cancel_appointment(self, appointment_id: int) -> dict:
        """Cancel via PUT {"status": "cancelled"}.
        VERIFY: exact status string on live data."""
        return self.update_appointment(appointment_id,
                                       {"status": CANCELLED_STATUS})

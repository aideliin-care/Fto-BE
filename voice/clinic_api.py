"""HTTP client for the appointment API defined in CONTRACT.md.

Stdlib only — no install. The service requires Content-Type: application/json
or it returns BAD_REQUEST, so that header is set here once and never forgotten
at a call site.
"""

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get(
    "CLINIC_API", f"http://127.0.0.1:{os.environ.get('PORT', '8787')}"
)
TIMEOUT = 8


class ApiError(Exception):
    """A structured error from the appointment service.

    `code` is the contract's machine-readable code — SLOT_TAKEN is the one the
    conversation actually recovers from, so it must survive as a code rather
    than being flattened into a message string.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            error = json.loads(raw)["error"]
        except (ValueError, KeyError, TypeError):
            # A 500 with no JSON body still has to become something the agent
            # can branch on, rather than an opaque crash mid-call.
            raise ApiError(exc.code, "SERVER_ERROR", raw.decode(errors="replace")[:200]) from exc
        raise ApiError(exc.code, error.get("code", "UNKNOWN"), error.get("message", "")) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, "UNREACHABLE", f"appointment service unreachable: {exc.reason}") from exc


def lookup_patient(phone: str) -> dict:
    return _post("/lookup_patient", {"phone": phone})


def find_slots(**kwargs) -> dict:
    payload = {k: v for k, v in kwargs.items() if v is not None}
    payload.setdefault("limit", 5)
    return _post("/find_slots", payload)


def book(call_id: str, slot_id: str, patient_name: str, phone: str) -> dict:
    return _post("/book", {
        "call_id": call_id,
        "slot_id": slot_id,
        "patient_name": patient_name,
        "phone": phone,
    })


def reschedule(call_id: str, appointment_id: str, new_slot_id: str) -> dict:
    return _post("/reschedule", {
        "call_id": call_id,
        "appointment_id": appointment_id,
        "new_slot_id": new_slot_id,
    })


def cancel(call_id: str, appointment_id: str) -> dict:
    return _post("/cancel", {"call_id": call_id, "appointment_id": appointment_id})


def health() -> dict:
    with urllib.request.urlopen(f"{BASE_URL}/health", timeout=TIMEOUT) as response:
        return json.loads(response.read())

"""Appointment service — workstream B of CONTRACT.md.

Implements the five tool endpoints the voice agent calls, on 127.0.0.1:8787.

Storage is SQLite (see schema.sql). Slots are generated from doctor_hours
before anyone books, so contention is a single indexed row and the database
arbitrates it rather than the agent prompt.

Response shapes match api/stub_server.py exactly, including UUID-string ids,
so swapping this in for the stub is invisible to the voice workstream.

    python3 seed.py --reset
    python3 -m uvicorn main:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

DB_PATH = Path(__file__).parent / "clinic.db"
TZ = ZoneInfo("Asia/Taipei")

app = FastAPI(title="Appointment Service", version="1.0")


# ---------------------------------------------------------------- plumbing

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx():
    """A write transaction. BEGIN IMMEDIATE takes the write lock up front, so
    two callers racing for one slot serialise here instead of one of them
    failing late with 'database is locked'."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status_code=status, detail={"code": code, "message": message})


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) and "code" in detail \
        else {"code": "ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": body})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    """Match the stub's error codes: BAD_JSON for unparseable bodies,
    MISSING_FIELD for absent or empty required fields."""
    first = exc.errors()[0]
    kind = first.get("type", "")
    if kind == "json_invalid":
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "BAD_JSON",
                               "message": "Body is not valid JSON."}},
        )
    field = ".".join(str(p) for p in first["loc"][1:]) or "body"
    if kind in ("missing", "string_too_short"):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "MISSING_FIELD",
                               "message": f"Field required: {field}"}},
        )
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "BAD_REQUEST",
                           "message": f"{field}: {first['msg']}"}},
    )


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    """Every response the agent can receive is parseable JSON in the contract's
    error shape. A bodiless 500 gives the voice layer nothing to recover from."""
    logging.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL",
                           "message": "Unexpected server error."}},
    )


# ---------------------------------------------------------------- schemas

class LookupIn(BaseModel):
    phone: str = Field(min_length=1)


class Upcoming(BaseModel):
    appointment_id: str
    starts_at: str
    doctor_name: str


class LookupOut(BaseModel):
    found: bool
    patient_id: Optional[str] = None
    name: Optional[str] = None
    upcoming: list[Upcoming] = []


class FindSlotsIn(BaseModel):
    department: Optional[str] = None
    doctor_name: Optional[str] = None
    earliest: Optional[str] = None
    latest: Optional[str] = None
    # No upper bound rejected: the stub accepted any limit, so a large one
    # must not 400. It is clamped below instead, keeping responses bounded.
    limit: Optional[int] = Field(default=5, ge=1)


class SlotOut(BaseModel):
    slot_id: str
    doctor_name: str
    department: str
    starts_at: str
    ends_at: str


class FindSlotsOut(BaseModel):
    slots: list[SlotOut]


class BookIn(BaseModel):
    call_id: str = Field(min_length=1)
    slot_id: str = Field(min_length=1)
    patient_name: str = Field(min_length=1)
    phone: str = Field(min_length=1)


class RescheduleIn(BaseModel):
    call_id: str = Field(min_length=1)
    appointment_id: str = Field(min_length=1)
    new_slot_id: str = Field(min_length=1)


class CancelIn(BaseModel):
    call_id: str = Field(min_length=1)
    appointment_id: str = Field(min_length=1)


class BookingOut(BaseModel):
    appointment_id: str
    starts_at: str
    doctor_name: str
    status: str


class CancelOut(BaseModel):
    appointment_id: str
    status: str


# ---------------------------------------------------------------- helpers

BOOKING_SELECT = """
SELECT a.id AS appointment_id, s.starts_at, d.name AS doctor_name, a.status
FROM appointment a
JOIN slot   s ON s.id = a.slot_id
JOIN doctor d ON d.id = s.doctor_id
WHERE a.id = ?
"""


def booking_payload(conn: sqlite3.Connection, appointment_id: str) -> dict:
    return dict(conn.execute(BOOKING_SELECT, (appointment_id,)).fetchone())


def require_slot(conn: sqlite3.Connection, slot_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT s.id, s.starts_at FROM slot s "
        "JOIN doctor d ON d.id = s.doctor_id AND d.active = 1 "
        "WHERE s.id = ?", (slot_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "SLOT_NOT_FOUND", "No such slot.")
    if row["starts_at"] <= now_iso():
        raise ApiError(409, "SLOT_IN_PAST", "That time has already passed.")
    return row


SLOT_TAKEN_MSG = "That time was just taken."
MAX_SLOTS = 50   # ceiling on find_slots, applied by clamping not rejecting


def resolve_conflict(conn: sqlite3.Connection,
                     exc: sqlite3.IntegrityError,
                     slot_id: str) -> None:
    """Turn an IntegrityError into the right API error.

    SQLite reports 'UNIQUE constraint failed: appointment.slot_id' and never
    names the index that was violated, so matching on 'one_booking_per_slot'
    silently never fires and the caller gets a 500 instead of a 409. Decide
    from the actual state instead, which cannot drift with the message text.
    """
    live = conn.execute(
        "SELECT 1 FROM appointment WHERE slot_id = ? AND status = 'BOOKED'",
        (slot_id,),
    ).fetchone()
    if live is not None:
        raise ApiError(409, "SLOT_TAKEN", SLOT_TAKEN_MSG) from exc
    raise exc


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        slots = conn.execute("SELECT count(*) FROM slot").fetchone()[0]
        free = conn.execute(
            "SELECT count(*) FROM slot s WHERE s.starts_at > ? AND NOT EXISTS "
            "(SELECT 1 FROM appointment a WHERE a.slot_id = s.id "
            " AND a.status = 'BOOKED')", (now_iso(),)
        ).fetchone()[0]
        return {"ok": True, "now": now_iso(), "slots": slots, "free": free}
    finally:
        conn.close()


@app.post("/lookup_patient", response_model=LookupOut)
def lookup_patient(body: LookupIn) -> LookupOut:
    """Called first on every inbound call, with the caller ID. A miss is a
    normal outcome, not an error: most callers have no row until they book."""
    conn = connect()
    try:
        patient = conn.execute(
            "SELECT id, name FROM patient WHERE phone = ?", (body.phone,)
        ).fetchone()
        if patient is None:
            return LookupOut(found=False, upcoming=[])

        rows = conn.execute(
            """
            SELECT a.id AS appointment_id, s.starts_at, d.name AS doctor_name
            FROM appointment a
            JOIN slot   s ON s.id = a.slot_id
            JOIN doctor d ON d.id = s.doctor_id
            WHERE a.patient_id = ? AND a.status = 'BOOKED' AND s.starts_at > ?
            ORDER BY s.starts_at
            """,
            (patient["id"], now_iso()),
        ).fetchall()
        return LookupOut(
            found=True,
            patient_id=patient["id"],
            name=patient["name"],
            upcoming=[Upcoming(**dict(r)) for r in rows],
        )
    finally:
        conn.close()


@app.post("/find_slots", response_model=FindSlotsOut)
def find_slots(body: FindSlotsIn) -> FindSlotsOut:
    """Free, future slots only. doctor_name is matched as a substring, which
    is a superset of the stub's exact match: anything that worked there still
    works, plus a partial name from speech."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.id AS slot_id, d.name AS doctor_name, d.department,
                   s.starts_at, s.ends_at
            FROM slot s
            JOIN doctor d ON d.id = s.doctor_id AND d.active = 1
            WHERE s.starts_at > :now
              AND NOT EXISTS (SELECT 1 FROM appointment a
                              WHERE a.slot_id = s.id AND a.status = 'BOOKED')
              AND (:department  IS NULL OR d.department = :department)
              AND (:doctor_name IS NULL OR d.name LIKE '%' || :doctor_name || '%')
              AND (:earliest    IS NULL OR s.starts_at >= :earliest)
              AND (:latest      IS NULL OR s.starts_at <= :latest)
            ORDER BY s.starts_at
            LIMIT :limit
            """,
            {
                "now": now_iso(),
                "department": body.department or None,
                "doctor_name": body.doctor_name or None,
                "earliest": body.earliest or None,
                "latest": body.latest or None,
                "limit": min(body.limit or 5, MAX_SLOTS),
            },
        ).fetchall()
        return FindSlotsOut(slots=[SlotOut(**dict(r)) for r in rows])
    finally:
        conn.close()


@app.post("/book", response_model=BookingOut)
def book(body: BookIn) -> BookingOut:
    """Idempotent on call_id: a dropped call that redials with the same
    call_id gets the first booking back rather than a second one."""
    with tx() as conn:
        prior = conn.execute(
            "SELECT id FROM appointment WHERE call_id = ?", (body.call_id,)
        ).fetchone()
        if prior is not None:
            return BookingOut(**booking_payload(conn, prior["id"]))

        require_slot(conn, body.slot_id)

        # The patient usually does not exist yet. Creating them is part of
        # booking, not a prerequisite the agent has to remember.
        patient = conn.execute(
            "SELECT id FROM patient WHERE phone = ?", (body.phone,)
        ).fetchone()
        if patient is None:
            patient_id = new_id()
            conn.execute(
                "INSERT INTO patient (id, name, phone) VALUES (?, ?, ?)",
                (patient_id, body.patient_name, body.phone),
            )
        else:
            patient_id = patient["id"]

        appointment_id = new_id()
        try:
            conn.execute(
                "INSERT INTO appointment (id, slot_id, patient_id, call_id) "
                "VALUES (?, ?, ?, ?)",
                (appointment_id, body.slot_id, patient_id, body.call_id),
            )
        except sqlite3.IntegrityError as exc:
            # Two callers can reach the INSERT with the same call_id if the
            # redial lands while the first is still in flight. The loser
            # returns the winner's booking rather than an error.
            prior = conn.execute(
                "SELECT id FROM appointment WHERE call_id = ?", (body.call_id,)
            ).fetchone()
            if prior is not None:
                return BookingOut(**booking_payload(conn, prior["id"]))
            resolve_conflict(conn, exc, body.slot_id)

        return BookingOut(**booking_payload(conn, appointment_id))


@app.post("/reschedule", response_model=BookingOut)
def reschedule(body: RescheduleIn) -> BookingOut:
    """Moving to the slot it already holds is a no-op, not an error.

    Matching the stub, rescheduling a cancelled appointment revives it onto
    the new slot rather than refusing. The partial unique index still stops
    it landing on a slot someone else has taken.
    """
    with tx() as conn:
        appt = conn.execute(
            "SELECT id, slot_id, status FROM appointment WHERE id = ?",
            (body.appointment_id,),
        ).fetchone()
        if appt is None:
            raise ApiError(404, "APPOINTMENT_NOT_FOUND", "No such appointment.")

        if appt["slot_id"] == body.new_slot_id and appt["status"] == "BOOKED":
            return BookingOut(**booking_payload(conn, appt["id"]))

        require_slot(conn, body.new_slot_id)

        try:
            conn.execute(
                "UPDATE appointment SET slot_id = ?, status = 'BOOKED', "
                "updated_at = ? WHERE id = ?",
                (body.new_slot_id, now_iso(), appt["id"]),
            )
        except sqlite3.IntegrityError as exc:
            resolve_conflict(conn, exc, body.new_slot_id)

        return BookingOut(**booking_payload(conn, appt["id"]))


@app.post("/cancel", response_model=CancelOut)
def cancel(body: CancelIn) -> CancelOut:
    """Cancelling twice is not an error. The row is kept as history and the
    slot reopens because it drops out of the partial unique index."""
    with tx() as conn:
        appt = conn.execute(
            "SELECT id, status FROM appointment WHERE id = ?",
            (body.appointment_id,),
        ).fetchone()
        if appt is None:
            raise ApiError(404, "APPOINTMENT_NOT_FOUND", "No such appointment.")
        if appt["status"] == "CANCELLED":
            return CancelOut(appointment_id=appt["id"], status="CANCELLED")

        conn.execute(
            "UPDATE appointment SET status = 'CANCELLED', updated_at = ? "
            "WHERE id = ?",
            (now_iso(), appt["id"]),
        )
        return CancelOut(appointment_id=appt["id"], status="CANCELLED")

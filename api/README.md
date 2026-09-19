# Appointment service — workstream B

Implements the five tool endpoints in `../CONTRACT.md` §3 on `127.0.0.1:8787`.
Owns everything behind the seam: the schema, slot generation, and notifications.
Knows nothing about phones, speech, or the agent.

## Run

```bash
python3 seed.py --reset          # build clinic.db and generate 30 days of slots
python3 -m uvicorn main:app --host 127.0.0.1 --port 8787
./test_api.sh                    # 24 assertions, exits non-zero on failure
```

No install step. FastAPI, uvicorn and pydantic were already present; storage is
SQLite from the standard library, so nothing is added to disk beyond `clinic.db`
(about 140 KB for a month of slots).

## Files

| File | |
|---|---|
| `schema.sql` | Six tables, two unique indexes, three triggers |
| `seed.py` | Doctors, weekly hours, and slot generation |
| `main.py` | The five endpoints plus `/health` |
| `test_api.sh` | End-to-end smoke test over curl |
| `clinic.db` | Generated, not source. Delete it freely |

## The design in one paragraph

A bookable time is a **row**, not two columns on a booking. `doctor_hours`
holds the weekly pattern, `seed.py` materialises it into concrete `slot` rows
before anyone calls, and an `appointment` points at one slot. That is what
makes contention a single indexed row the database can arbitrate, rather than
something the agent prompt has to reason about.

## The three hard cases, and where each is handled

**Racing callers.** A partial unique index, not application logic:

```sql
CREATE UNIQUE INDEX one_booking_per_slot
  ON appointment (slot_id) WHERE status = 'BOOKED';
```

Writes go through `BEGIN IMMEDIATE`, so concurrent callers serialise on the
write lock instead of failing late. Verified with twenty simultaneous bookings
on one slot: one `200`, nineteen `409 SLOT_TAKEN`, one row in the database.

**Redial.** `appointment.call_id` is unique. A repeat `book` with the same
`call_id` returns the first appointment rather than creating a second. Verified
with twenty concurrent redials: one appointment, one row.

**The patient does not exist yet.** Caller identity is a phone number, so
creating the patient is part of `book`, not a prerequisite. `lookup_patient`
returning `found: false` is a normal outcome, not an error.

## Notes for integration

- IDs are UUID strings, matching the shapes the voice agent was coded against
  in the original `stub_server.py`.
- Times are RFC3339 with the `+08:00` offset. One clinic, one timezone, so
  lexical ordering equals chronological ordering. Serving a second timezone
  means moving to UTC storage and a timezone column.
- `doctor_name` in `find_slots` matches as a substring, a superset of the
  stub's exact match, so a partial spoken name still resolves.
- `limit` is clamped to 50 rather than rejected, so a large value never 400s.
- Every response is JSON in the contract's shape, including unexpected errors,
  which return `500` with code `INTERNAL` rather than an empty body.
- One error code is not in the contract: `409 SLOT_IN_PAST`, when a stale
  `slot_id` refers to a time that has already passed.

## Not built

Reminders before the appointment need no schema change: insert a `notification`
row with a future `send_at`. There is no worker that drains that queue yet, so
rows accumulate unsent. Several patients per slot, and doctors editing their own
hours through a UI, are both unbuilt and neither is needed for the demo.

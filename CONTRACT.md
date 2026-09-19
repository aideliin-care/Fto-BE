# Voice Appointment System — Interface Contract

Owner: SA/PM session (`fto-hacking-3b`). This file is the seam between the two
workstreams. Both sides build against it and do not need to wait for each other.

Scope: everything lives under `Fto-hacking/`. Nothing outside is touched.

## 1. What we are building

A caller phones a number, talks to an AI, and walks away with a booked
appointment. One inbound call, one outcome: booked, rescheduled, cancelled, or
handed to a human.

```
caller ──PSTN──> phone number ──> realtime voice agent ──tool calls──> appointment API ──> DB
                                         │
                                         └── fallback: transfer to human
```

## 2. Hard constraints

These are not preferences. They come from the machine and the domain.

| Constraint | Consequence |
|---|---|
| **292 MB disk free** | No Docker. No heavy images. Watch `node_modules`/venv size. Prefer a file-backed DB over installing a server. |
| Caller identity is a phone number | A patient row may not exist when the call starts. Phone is the natural key for lookup. |
| Speech is revisable mid-sentence | Never write a partial booking. Only commit on explicit caller confirmation. |
| Calls drop and get retried | Every write is idempotent on `call_id`. A redial must not double-book. |
| A slot holds at most one live booking | Enforced in the DB, not in the agent prompt. |

## 3. The seam: tool contract

The voice agent calls these. The appointment service implements them. Both
sides may develop against a stub of the other.

Transport: HTTP+JSON on `http://127.0.0.1:8787`. All responses are JSON.
Errors return `{"error": {"code": "...", "message": "..."}}` with a 4xx/5xx.

### `lookup_patient`
`POST /lookup_patient` → `{phone}`
→ `{found: bool, patient_id?, name?, upcoming: [{appointment_id, starts_at, doctor_name}]}`

Called first on every inbound call, with the caller ID. Drives whether the
agent greets by name and whether "cancel my appointment" has a referent.

### `find_slots`
`POST /find_slots` → `{department?, doctor_name?, earliest?, latest?, limit?}`
→ `{slots: [{slot_id, doctor_name, department, starts_at, ends_at}]}`

`starts_at`/`ends_at` are RFC3339 with offset. Returns only free, future slots.
`limit` defaults to 5 — the agent must be able to read them aloud.

### `book`
`POST /book` → `{call_id, slot_id, patient_name, phone}`
→ `{appointment_id, starts_at, doctor_name, status: "BOOKED"}`

Idempotent on `call_id`: the same `call_id` twice returns the first result
rather than booking twice. If the slot was taken in the meantime, returns
`409` with code `SLOT_TAKEN` — the agent must offer alternatives, not fail.

A stale `slot_id` pointing at a time that has already passed returns `409`
`SLOT_IN_PAST`. This is deliberately **not** folded into `SLOT_TAKEN`: both
recover the same way, by re-querying and offering alternatives, but the agent
must not tell a caller their slot was booked by someone else when it merely
expired. Saying something false to the caller is worse than an extra code.

### `reschedule`
`POST /reschedule` → `{call_id, appointment_id, new_slot_id}`
→ `{appointment_id, starts_at, doctor_name, status: "BOOKED"}`

### `cancel`
`POST /cancel` → `{call_id, appointment_id}`
→ `{appointment_id, status: "CANCELLED"}`

Cancelled rows are kept as history; they are never deleted.

## 4. Workstreams

**A — Voice layer** (`voice/`): phone number, inbound call handling, realtime
speech, the agent prompt, tool-call dispatch to the API above, human fallback.
Owns nothing behind the seam.

**B — Appointment service** (`api/`): the five endpoints, the schema, slot
generation from doctor hours, notification rows on create/update/cancel.
Owns nothing in front of the seam.

Integration is owned by the SA/PM session.

## 5. Definition of done for the demo

A real phone call that books a real row, read back to the caller with the
doctor's name and time, and visible in the DB afterwards. One happy path
end to end beats four half-built features.

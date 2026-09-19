-- Appointment service schema (workstream B).
-- SQLite: file-backed, zero install. See CONTRACT.md section 2 for why.
--
-- IDs are UUID strings, not rowids, to match the shapes the voice agent was
-- coded against in stub_server.py. They are generated in Python.
--
-- Times are RFC3339 strings with a fixed offset ('2026-09-21T09:00:00+08:00').
-- One clinic, one timezone, so lexical ordering equals chronological ordering.
-- If this ever serves two timezones, move to UTC storage + a tz column.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS doctor (
  id          TEXT    PRIMARY KEY,
  name        TEXT    NOT NULL UNIQUE,
  department  TEXT    NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);

-- The weekly pattern. weekday: 0 = Sunday .. 6 = Saturday.
-- slot_minutes is per block, so a 15-min morning and a 30-min afternoon coexist.
CREATE TABLE IF NOT EXISTS doctor_hours (
  doctor_id     TEXT    NOT NULL REFERENCES doctor(id) ON DELETE CASCADE,
  weekday       INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  start_time    TEXT    NOT NULL,                  -- 'HH:MM'
  end_time      TEXT    NOT NULL,
  slot_minutes  INTEGER NOT NULL DEFAULT 30 CHECK (slot_minutes > 0),
  PRIMARY KEY (doctor_id, weekday, start_time),
  CHECK (end_time > start_time)
);

CREATE TABLE IF NOT EXISTS patient (
  id              TEXT NOT NULL PRIMARY KEY,
  name            TEXT NOT NULL,
  phone           TEXT NOT NULL UNIQUE,            -- the natural key from caller ID
  preferred_time  TEXT NOT NULL DEFAULT '{}',      -- json: {"weekdays":[1,3],"from":"09:00"}
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per bookable time, generated from doctor_hours before anyone books.
-- Generation steps by slot_minutes inside each block, so slots cannot overlap
-- by construction. That is what replaces Postgres' EXCLUDE USING gist here.
CREATE TABLE IF NOT EXISTS slot (
  id         TEXT NOT NULL PRIMARY KEY,
  doctor_id  TEXT NOT NULL REFERENCES doctor(id) ON DELETE CASCADE,
  starts_at  TEXT NOT NULL,
  ends_at    TEXT NOT NULL,
  UNIQUE (doctor_id, starts_at),
  CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS slot_lookup ON slot (starts_at);

CREATE TABLE IF NOT EXISTS appointment (
  id          TEXT NOT NULL PRIMARY KEY,
  slot_id     TEXT NOT NULL REFERENCES slot(id),
  patient_id  TEXT NOT NULL REFERENCES patient(id),
  -- idempotency key from the phone call. A redial reuses it and must not
  -- produce a second appointment. NULL allowed for non-call bookings.
  call_id     TEXT UNIQUE,
  status      TEXT NOT NULL DEFAULT 'BOOKED'
                   CHECK (status IN ('BOOKED','CANCELLED')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- At most one live booking per slot. Racing callers are resolved here, in the
-- database, not in the agent prompt. Cancelled rows drop out and free the slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_slot
  ON appointment (slot_id) WHERE status = 'BOOKED';

CREATE INDEX IF NOT EXISTS appointment_by_patient
  ON appointment (patient_id) WHERE status = 'BOOKED';

CREATE TABLE IF NOT EXISTS notification (
  id              INTEGER PRIMARY KEY,             -- internal, never returned
  appointment_id  TEXT    NOT NULL REFERENCES appointment(id),
  event           TEXT    NOT NULL
                          CHECK (event IN ('CREATED','UPDATED','CANCELLED')),
  -- the slot as it stood at the moment of the event, so a queued message
  -- still reads correctly after a later reschedule
  slot_id         TEXT    NOT NULL REFERENCES slot(id),
  prev_slot_id    TEXT    REFERENCES slot(id),
  send_at         TEXT    NOT NULL DEFAULT (datetime('now')),
  sent_at         TEXT,
  attempts        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS notification_due
  ON notification (send_at) WHERE sent_at IS NULL;

-- Notifications are written by the database so no code path can forget them.

CREATE TRIGGER IF NOT EXISTS appointment_created
AFTER INSERT ON appointment
BEGIN
  INSERT INTO notification (appointment_id, event, slot_id)
  VALUES (NEW.id, 'CREATED', NEW.slot_id);
END;

CREATE TRIGGER IF NOT EXISTS appointment_moved
AFTER UPDATE OF slot_id ON appointment
WHEN NEW.slot_id <> OLD.slot_id AND NEW.status = 'BOOKED'
BEGIN
  INSERT INTO notification (appointment_id, event, slot_id, prev_slot_id)
  VALUES (NEW.id, 'UPDATED', NEW.slot_id, OLD.slot_id);
END;

CREATE TRIGGER IF NOT EXISTS appointment_cancelled
AFTER UPDATE OF status ON appointment
WHEN NEW.status = 'CANCELLED' AND OLD.status <> 'CANCELLED'
BEGIN
  INSERT INTO notification (appointment_id, event, slot_id)
  VALUES (NEW.id, 'CANCELLED', NEW.slot_id);
END;

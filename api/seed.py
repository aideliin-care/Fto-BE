"""Create clinic.db, load the schema, seed doctors, and generate slots.

    python3 seed.py            # 30 days of slots from today
    python3 seed.py --days 60
    python3 seed.py --reset    # delete the database first

Slot generation walks each doctor_hours block in slot_minutes steps, so slots
cannot overlap by construction. That is what stands in for the Postgres
exclusion constraint on this SQLite deployment.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("CLINIC_DB_PATH", HERE / "clinic.db"))
SCHEMA = HERE / "schema.sql"
TZ = ZoneInfo("Asia/Taipei")

# name, department, [(weekday, start, end, slot_minutes)]
# weekday: 0 = Sunday .. 6 = Saturday
# Names and departments match stub_server.py so the swap is invisible to the
# voice workstream. Hours are richer than the stub's to exercise per-block
# slot_minutes; the stub used a flat 30 minutes everywhere.
DOCTORS = [
    ("林怡君", "小兒科", [
        (1, "09:00", "12:00", 30),
        (2, "09:00", "12:00", 30),
        (3, "09:00", "12:00", 30),
        (4, "09:00", "12:00", 30),
        (5, "09:00", "12:00", 30),
    ]),
    ("陳柏宏", "家醫科", [
        (1, "09:00", "12:00", 15),
        (2, "09:00", "12:00", 15),
        (2, "14:00", "17:00", 30),
        (3, "09:00", "12:00", 15),
        (4, "14:00", "17:00", 30),
        (5, "09:00", "12:00", 15),
    ]),
    ("黃雅琳", "皮膚科", [
        (1, "14:00", "17:00", 20),
        (3, "14:00", "17:00", 20),
        (5, "14:00", "17:00", 20),
        (6, "09:00", "12:00", 30),
    ]),
]


def generate_slots(conn: sqlite3.Connection, days: int) -> int:
    """Materialise slots for the next `days` days. Safe to rerun: existing
    slots collide on UNIQUE(doctor_id, starts_at) and are skipped."""
    hours = conn.execute(
        "SELECT h.doctor_id, h.weekday, h.start_time, h.end_time, h.slot_minutes "
        "FROM doctor_hours h JOIN doctor d ON d.id = h.doctor_id AND d.active = 1"
    ).fetchall()

    today = datetime.now(TZ).date()
    rows = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        # Python: Monday=0..Sunday=6. Ours: Sunday=0..Saturday=6.
        weekday = (day.weekday() + 1) % 7
        for h in hours:
            if h["weekday"] != weekday:
                continue
            step = timedelta(minutes=h["slot_minutes"])
            sh, sm = (int(x) for x in h["start_time"].split(":"))
            eh, em = (int(x) for x in h["end_time"].split(":"))
            cursor = datetime(day.year, day.month, day.day, sh, sm, tzinfo=TZ)
            block_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=TZ)
            while cursor + step <= block_end:
                rows.append((
                    str(uuid.uuid4()),
                    h["doctor_id"],
                    cursor.isoformat(timespec="seconds"),
                    (cursor + step).isoformat(timespec="seconds"),
                ))
                cursor += step

    before = conn.execute("SELECT count(*) FROM slot").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO slot (id, doctor_id, starts_at, ends_at) "
        "VALUES (?,?,?,?)",
        rows,
    )
    after = conn.execute("SELECT count(*) FROM slot").fetchone()[0]
    return after - before


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        print(f"removed {DB_PATH.name}")

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    conn.execute("PRAGMA foreign_keys = ON")

    for name, department, blocks in DOCTORS:
        row = conn.execute("SELECT id FROM doctor WHERE name = ?", (name,)).fetchone()
        if row is None:
            doctor_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO doctor (id, name, department) VALUES (?, ?, ?)",
                (doctor_id, name, department),
            )
        else:
            doctor_id = row["id"]
        conn.executemany(
            "INSERT OR IGNORE INTO doctor_hours "
            "(doctor_id, weekday, start_time, end_time, slot_minutes) "
            "VALUES (?,?,?,?,?)",
            [(doctor_id, *b) for b in blocks],
        )

    added = generate_slots(conn, args.days)
    total = conn.execute("SELECT count(*) FROM slot").fetchone()[0]
    free = conn.execute(
        "SELECT count(*) FROM slot s WHERE NOT EXISTS "
        "(SELECT 1 FROM appointment a WHERE a.slot_id = s.id AND a.status='BOOKED')"
    ).fetchone()[0]
    conn.close()

    print(f"doctors: {len(DOCTORS)}")
    print(f"slots added: {added}   total: {total}   free: {free}")
    print(f"database: {DB_PATH}")


if __name__ == "__main__":
    main()

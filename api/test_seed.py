import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import seed


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "clinic.db"
        # A database seeded with the previous clinic, as production is.
        conn = sqlite3.connect(self.db_path)
        conn.executescript((HERE / "schema.sql").read_text())
        conn.executescript("""
            INSERT INTO doctor VALUES ('old-1', '林怡君', '復健科', 1);
            INSERT INTO doctor_hours VALUES ('old-1', 1, '08:30', '12:00', 15);
        """)
        conn.close()
        self.old_db_path = seed.DB_PATH
        seed.DB_PATH = self.db_path

    def tearDown(self):
        seed.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def run_seed(self):
        with mock.patch.object(sys, "argv", ["seed.py", "--days", "14"]):
            seed.main()

    def query(self, sql, *args):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def starts(self, name):
        rows = self.query(
            "SELECT s.starts_at FROM slot s JOIN doctor d ON d.id = s.doctor_id "
            "WHERE d.name = ?", name)
        return [datetime.fromisoformat(r[0]) for r in rows]

    def test_retires_doctors_dropped_from_the_roster(self):
        self.run_seed()
        active = dict(self.query("SELECT name, active FROM doctor"))
        self.assertEqual(active, {"林怡君": 0, "林志豪": 1, "陳瑋庭": 1})
        self.assertEqual(self.starts("林怡君"), [])

    def test_slots_follow_the_little_star_schedule(self):
        self.run_seed()
        # isoweekday: Monday = 1 .. Sunday = 7
        lin = self.starts("林志豪")
        self.assertEqual({s.isoweekday() for s in lin}, {1, 2, 3, 5, 6})
        mondays = sorted(s.strftime("%H:%M") for s in lin if s.isoweekday() == 1)
        self.assertEqual(mondays[0], "09:00")
        # Registration closes at 11:30 and 21:00; no slot starts at or after.
        self.assertIn("11:15", mondays)
        self.assertNotIn("11:30", mondays)
        self.assertIn("20:45", mondays)
        self.assertNotIn("21:00", mondays)
        self.assertNotIn("15:00", mondays)  # Monday afternoon is Dr. Chen's

        chen = self.starts("陳瑋庭")
        self.assertEqual({s.isoweekday() for s in chen}, {1, 2, 3, 5, 6})
        self.assertNotIn(4, {s.isoweekday() for s in lin + chen})  # closed Thursday
        self.assertNotIn(7, {s.isoweekday() for s in lin + chen})  # closed Sunday
        saturdays = {s.hour for s in chen if s.isoweekday() == 6}
        self.assertEqual(saturdays, {9, 10, 11})  # mornings only

    def test_rerun_adds_nothing(self):
        self.run_seed()
        before = self.query("SELECT count(*) FROM slot")[0][0]
        self.run_seed()
        self.assertEqual(self.query("SELECT count(*) FROM slot")[0][0], before)


if __name__ == "__main__":
    unittest.main()

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import main
import seed

KEY = {"X-Dashboard-Key": "test-key"}


class VisitDetailsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "clinic.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript((HERE / "schema.sql").read_text())
        conn.executescript("""
            INSERT INTO doctor VALUES ('doctor-1', '林志豪', '小兒科', 1);
            INSERT INTO patient (id, name, phone) VALUES ('patient-1', '王大明', '0912345678');
            INSERT INTO slot VALUES ('slot-1', 'doctor-1', '2099-01-01T09:00:00+08:00', '2099-01-01T09:15:00+08:00');
            INSERT INTO slot VALUES ('slot-2', 'doctor-1', '2099-01-01T09:15:00+08:00', '2099-01-01T09:30:00+08:00');
        """)
        conn.close()
        self.old_path = main.DB_PATH
        self.old_key = main.DASHBOARD_API_KEY
        main.DB_PATH = self.db_path
        main.DASHBOARD_API_KEY = "test-key"
        self.client = TestClient(main.app)

    def tearDown(self):
        main.DB_PATH = self.old_path
        main.DASHBOARD_API_KEY = self.old_key
        self.tempdir.cleanup()

    def _book(self, **extra):
        body = {
            "call_id": "call-1",
            "slot_id": "slot-1",
            "patient_name": "王小明",
            "phone": "0912345678",
            **extra,
        }
        return self.client.post("/book", json=body)

    def _reservation(self):
        response = self.client.get("/dashboard/reservations", headers=KEY)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        return items[0]

    def test_a_booking_keeps_who_the_visit_is_for(self):
        response = self._book(patient_birth_date="2016-03-05")

        self.assertEqual(response.status_code, 200, response.text)
        patient = self._reservation()["patient"]
        self.assertEqual(patient["name"], "王小明")
        self.assertEqual(patient["birth_date"], "2016-03-05")
        self.assertEqual(patient["id"], "patient-1")

    def test_a_second_child_on_one_phone_keeps_their_own_name(self):
        self._book()
        self._book(call_id="call-2", slot_id="slot-2", patient_name="王小華")

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT visit_name FROM appointment ORDER BY call_id"
        ).fetchall()
        patients = conn.execute("SELECT count(*) FROM patient").fetchone()[0]
        conn.close()
        self.assertEqual([row[0] for row in rows], ["王小明", "王小華"])
        self.assertEqual(patients, 1)

    def test_a_booking_without_a_birth_date_still_works(self):
        response = self._book()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self._reservation()["patient"]["birth_date"])

    def test_an_impossible_birth_date_is_refused(self):
        response = self._book(patient_birth_date="2016-13-45")

        self.assertEqual(response.status_code, 400)

    def test_a_row_from_before_these_columns_shows_the_patients_own_name(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO appointment (id, slot_id, patient_id, call_id) "
            "VALUES ('appt-old', 'slot-1', 'patient-1', 'old')"
        )
        conn.commit()
        conn.close()

        patient = self._reservation()["patient"]

        self.assertEqual(patient["name"], "王大明")
        self.assertIsNone(patient["birth_date"])

    def test_the_booking_log_names_the_visit(self):
        self._book()

        logs = self.client.get("/dashboard/logs", headers=KEY).json()["items"]

        self.assertEqual(logs[0]["patient_name"], "王小明")


class ExistingDatabaseTest(unittest.TestCase):
    def test_a_deployed_table_gains_the_columns_once_and_keeps_its_rows(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE appointment (
              id TEXT PRIMARY KEY, slot_id TEXT, patient_id TEXT, call_id TEXT UNIQUE,
              status TEXT NOT NULL DEFAULT 'BOOKED'
            );
            INSERT INTO appointment (id, slot_id, patient_id) VALUES ('a', 's', 'p');
        """)

        seed.add_missing_columns(conn)
        seed.add_missing_columns(conn)

        row = conn.execute("SELECT * FROM appointment").fetchone()
        self.assertEqual(row["id"], "a")
        self.assertIsNone(row["visit_name"])
        self.assertIsNone(row["visit_birth_date"])


if __name__ == "__main__":
    unittest.main()

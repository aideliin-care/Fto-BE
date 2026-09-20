import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import main


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "clinic.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript((HERE / "schema.sql").read_text())
        conn.executescript("""
            INSERT INTO doctor VALUES ('doctor-1', '陳柏宏', '皮膚科', 1);
            INSERT INTO patient (id, name, phone) VALUES ('patient-1', '王小明', '0912345678');
            INSERT INTO slot VALUES ('slot-1', 'doctor-1', '2030-01-01T09:00:00+08:00', '2030-01-01T09:30:00+08:00');
            INSERT INTO slot VALUES ('slot-2', 'doctor-1', '2030-01-01T10:00:00+08:00', '2030-01-01T10:30:00+08:00');
            INSERT INTO slot VALUES ('slot-3', 'doctor-1', '2030-01-01T11:00:00+08:00', '2030-01-01T11:30:00+08:00');
            INSERT INTO appointment (id, slot_id, patient_id, call_id, status) VALUES ('appt-1', 'slot-1', 'patient-1', 'call-1', 'BOOKED');
            INSERT INTO appointment (id, slot_id, patient_id, call_id, status) VALUES ('appt-2', 'slot-2', 'patient-1', 'call-2', 'CANCELLED');
            INSERT INTO appointment (id, slot_id, patient_id, call_id, status) VALUES ('appt-3', 'slot-3', 'patient-1', 'call-3', 'BOOKED');
        """)
        conn.close()
        self.old_db_path, self.old_key = main.DB_PATH, main.DASHBOARD_API_KEY
        main.DB_PATH, main.DASHBOARD_API_KEY = self.db_path, "test-key"
        self.client = TestClient(main.app)

    def tearDown(self):
        main.DB_PATH, main.DASHBOARD_API_KEY = self.old_db_path, self.old_key
        self.tempdir.cleanup()

    def test_lists_filters_and_paginates_reservations(self):
        headers = {"X-Dashboard-Key": "test-key"}
        first = self.client.get(
            "/dashboard/reservations?status=BOOKED&limit=1", headers=headers
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["items"][0]["patient"]["phone"], "0912345678")
        cursor = first.json()["next_cursor"]
        self.assertIsNotNone(cursor)
        second = self.client.get(
            f"/dashboard/reservations?status=BOOKED&limit=1&cursor={cursor}",
            headers=headers,
        )
        self.assertEqual(second.json()["items"][0]["appointment_id"], "appt-3")
        self.assertIsNone(second.json()["next_cursor"])

        cancelled = self.client.get(
            "/dashboard/reservations?status=CANCELLED", headers=headers
        )
        self.assertEqual(cancelled.json()["items"][0]["appointment_id"], "appt-2")

        cors = self.client.options(
            "/dashboard/reservations",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(cors.headers["access-control-allow-origin"], "http://localhost:5173")


if __name__ == "__main__":
    unittest.main()

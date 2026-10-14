import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import main


class StaffDashboardTest(unittest.TestCase):
    """The staff frontend logs in here and reads with a short-lived token."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "clinic.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript((HERE / "schema.sql").read_text())
        conn.executescript("""
            INSERT INTO doctor VALUES ('doctor-1', '林怡君', '復健科', 1);
            INSERT INTO doctor VALUES ('doctor-2', '陳柏宏', '復健科', 1);
            INSERT INTO doctor_hours VALUES ('doctor-1', 1, '08:30', '12:00', 15);
            INSERT INTO doctor_hours VALUES ('doctor-1', 6, '08:30', '12:00', 15);
            INSERT INTO doctor_hours VALUES ('doctor-1', 3, '18:30', '21:00', 15);
            INSERT INTO doctor_hours VALUES ('doctor-1', 3, '08:30', '12:00', 15);
            INSERT INTO patient (id, name, phone) VALUES ('patient-1', '王小明', '0912345678');
            INSERT INTO patient (id, name, phone) VALUES ('patient-2', '李大華', '0922222222');
            INSERT INTO slot VALUES ('past-1', 'doctor-2', '2020-01-01T09:00:00+08:00', '2020-01-01T09:15:00+08:00');
            INSERT INTO slot VALUES ('past-2', 'doctor-2', '2020-02-01T09:00:00+08:00', '2020-02-01T09:15:00+08:00');
            INSERT INTO slot VALUES ('past-3', 'doctor-1', '2020-03-01T09:00:00+08:00', '2020-03-01T09:15:00+08:00');
            INSERT INTO slot VALUES ('future-1', 'doctor-1', '2030-01-01T09:00:00+08:00', '2030-01-01T09:15:00+08:00');
            INSERT INTO appointment (id, slot_id, patient_id, call_id) VALUES ('appt-1', 'past-1', 'patient-1', 'call-1');
            INSERT INTO appointment (id, slot_id, patient_id, call_id) VALUES ('appt-2', 'past-2', 'patient-1', 'call-2');
            INSERT INTO appointment (id, slot_id, patient_id, call_id) VALUES ('appt-3', 'past-3', 'patient-1', NULL);
            INSERT INTO appointment (id, slot_id, patient_id, call_id) VALUES ('appt-4', 'future-1', 'patient-1', 'call-4');
            UPDATE appointment SET status = 'CANCELLED' WHERE id = 'appt-3';
        """)
        conn.close()
        config = patch.multiple(
            main, DB_PATH=self.db_path, DASHBOARD_API_KEY="test-key",
            STAFF_ID="staff01", STAFF_PASSWORD="s3cret", STAFF_NAME="櫃檯 A",
        )
        config.start()
        self.addCleanup(config.stop)
        self.addCleanup(self.tempdir.cleanup)
        self.client = TestClient(main.app)

    def login(self, password="s3cret"):
        return self.client.post("/auth/login", json={"staff_id": "staff01", "password": password})

    def auth(self):
        return {"Authorization": f"Bearer {self.login().json()['token']}"}

    def test_login_returns_a_token_and_the_staff_user(self):
        body = self.login().json()
        self.assertTrue(body["token"])
        self.assertEqual(body["user"], {"id": "staff01", "name": "櫃檯 A", "role": "reception"})

    def test_wrong_password_is_rejected(self):
        response = self.login(password="nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_LOGIN")

    def test_login_is_disabled_until_staff_credentials_exist(self):
        with patch.object(main, "STAFF_PASSWORD", None):
            self.assertEqual(self.login().status_code, 503)

    def test_token_reads_reservations(self):
        response = self.client.get("/dashboard/reservations", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 4)

    def test_tampered_and_expired_tokens_are_refused(self):
        token = self.login().json()["token"]
        tampered = {"Authorization": f"Bearer {token[:-2]}xx"}
        self.assertEqual(self.client.get("/dashboard/reservations", headers=tampered).status_code, 403)
        with patch.object(main.time, "time", return_value=time.time() + main.TOKEN_TTL_SECONDS + 1):
            expired = self.client.get("/dashboard/reservations", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(expired.status_code, 403)

    def test_api_key_still_works_for_server_callers(self):
        response = self.client.get("/dashboard/doctors", headers={"X-Dashboard-Key": "test-key"})
        self.assertEqual(response.status_code, 200)

    def test_new_endpoints_need_credentials(self):
        for path in ("/dashboard/doctors", "/dashboard/patients", "/dashboard/logs"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_doctors_carry_their_working_days(self):
        doctors = self.client.get("/dashboard/doctors", headers=self.auth()).json()["items"]
        lin = next(d for d in doctors if d["id"] == "doctor-1")
        self.assertEqual(lin, {"id": "doctor-1", "name": "林怡君", "department": "復健科",
                               "working_days": ["mon", "wed", "sat"]})
        chen = next(d for d in doctors if d["id"] == "doctor-2")
        self.assertEqual(chen["working_days"], [])

    def test_patients_show_last_past_visit_and_usual_doctor(self):
        patients = self.client.get("/dashboard/patients", headers=self.auth()).json()["items"]
        wang = next(p for p in patients if p["id"] == "patient-1")
        # appt-3 (March) was cancelled and appt-4 is in the future: neither is a visit.
        self.assertEqual(wang["last_visit"], "2020-02-01")
        self.assertEqual(wang["primary_doctor_id"], "doctor-2")
        self.assertEqual(wang["phone"], "0912345678")
        lee = next(p for p in patients if p["id"] == "patient-2")
        self.assertIsNone(lee["last_visit"])
        self.assertIsNone(lee["primary_doctor_id"])

    def test_logs_list_booking_events_newest_first(self):
        logs = self.client.get("/dashboard/logs", headers=self.auth()).json()["items"]
        self.assertEqual(len(logs), 5)  # four CREATED + one CANCELLED
        self.assertEqual(logs[0]["action"], "CANCELLED")
        self.assertEqual(logs[0]["appointment_id"], "appt-3")
        self.assertEqual(logs[0]["patient_name"], "王小明")
        self.assertEqual(logs[0]["channel"], "staff")
        created = next(entry for entry in logs if entry["appointment_id"] == "appt-1")
        self.assertEqual(created["channel"], "agent")
        self.assertEqual(created["starts_at"], "2020-01-01T09:00:00+08:00")
        self.assertEqual(created["doctor_name"], "陳柏宏")
        self.assertIsNone(created["prev_starts_at"])

    def test_a_move_logs_both_slots_and_keeps_the_earlier_entry(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO slot VALUES ('future-2', 'doctor-1', "
                     "'2030-01-01T09:15:00+08:00', '2030-01-01T09:30:00+08:00')")
        conn.execute("UPDATE appointment SET slot_id = 'future-2' WHERE id = 'appt-2'")
        conn.commit()
        conn.close()
        logs = self.client.get("/dashboard/logs", headers=self.auth()).json()["items"]
        moved = logs[0]
        self.assertEqual(moved["action"], "UPDATED")
        self.assertEqual(moved["starts_at"], "2030-01-01T09:15:00+08:00")
        self.assertEqual(moved["doctor_name"], "林怡君")
        self.assertEqual(moved["prev_starts_at"], "2020-02-01T09:00:00+08:00")
        created = next(e for e in logs if e["appointment_id"] == "appt-2" and e["action"] == "CREATED")
        self.assertEqual(created["starts_at"], "2020-02-01T09:00:00+08:00")

    def test_cors_allows_the_authorization_header(self):
        response = self.client.options("/dashboard/patients", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()

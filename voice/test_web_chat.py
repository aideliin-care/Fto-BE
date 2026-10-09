import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import web_chat
from agent import State


class FakeDraft:
    appointment_id = None


class FakeAgent:
    def __init__(self, phone, driver):
        self.phone = phone
        self.state = State.INTENT
        self.draft = FakeDraft()

    def answer(self):
        return "您好，這裡是康寧診所。"

    def turn(self, said):
        if said == "轉真人":
            self.state = State.HANDOFF
            return "這部分我幫您轉接櫃檯人員，請稍等。"
        if said == "好":
            self.state = State.COMMITTED
            self.draft.appointment_id = 42
            return "好的，已經幫您約好了。"
        if said == "壞掉":
            raise RuntimeError("provider down")
        return "請問您想約哪一科？"


class WebChatTest(unittest.TestCase):
    def setUp(self):
        self.agent = patch.object(web_chat, "Agent", FakeAgent)
        self.driver = patch.object(web_chat, "make_driver", return_value=object())
        self.agent.start()
        self.driver.start()
        web_chat.sessions.clear()
        self.client = TestClient(web_chat.app)

    def tearDown(self):
        self.driver.stop()
        self.agent.stop()
        web_chat.sessions.clear()

    def start(self):
        response = self.client.post("/chat/start", json={"phone": "0987000111"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def say(self, session_id, text):
        return self.client.post("/chat/message", json={"session_id": session_id, "text": text})

    def test_start_greets_and_remembers_session(self):
        body = self.start()
        self.assertIn("康寧診所", body["reply"])
        self.assertEqual(body["state"], "INTENT")
        self.assertIn(body["session_id"], web_chat.sessions)

    def test_message_continues_conversation(self):
        session_id = self.start()["session_id"]
        body = self.say(session_id, "我要掛號").json()
        self.assertEqual(body["reply"], "請問您想約哪一科？")
        self.assertIn(session_id, web_chat.sessions)

    def test_booking_returns_appointment_and_ends_session(self):
        session_id = self.start()["session_id"]
        body = self.say(session_id, "好").json()
        self.assertEqual(body["state"], "COMMITTED")
        self.assertEqual(body["appointment_id"], 42)
        self.assertNotIn(session_id, web_chat.sessions)

    def test_handoff_ends_session(self):
        session_id = self.start()["session_id"]
        body = self.say(session_id, "轉真人").json()
        self.assertEqual(body["state"], "HANDOFF")
        self.assertNotIn(session_id, web_chat.sessions)

    def test_unknown_session_is_404(self):
        self.assertEqual(self.say("nope", "你好").status_code, 404)

    def test_agent_failure_is_502_and_ends_session(self):
        session_id = self.start()["session_id"]
        self.assertEqual(self.say(session_id, "壞掉").status_code, 502)
        self.assertNotIn(session_id, web_chat.sessions)

    def test_serves_chat_page(self):
        response = self.client.get("/chat")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()

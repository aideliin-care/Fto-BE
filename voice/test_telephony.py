import asyncio
import os
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from xml.etree import ElementTree as ET

from starlette.requests import Request

import telephony
from agent import State


class FakeAgent:
    def __init__(self, phone, driver, call_id):
        self.phone = phone
        self.call_id = call_id
        self.state = State.INTENT

    def answer(self):
        return "您好，這裡是康寧診所。"

    def turn(self, said):
        if said == "轉真人":
            self.state = State.HANDOFF
            return "這部分我幫您轉接櫃檯人員，請稍等。"
        self.state = State.COMMITTED
        return "好的，已經幫您約好了。"


class TelephonyTest(unittest.TestCase):
    def setUp(self):
        self.agent = patch.object(telephony, "Agent", FakeAgent)
        self.driver = patch.object(telephony, "make_driver", return_value=object())
        self.agent.start()
        self.driver.start()
        telephony.sessions.clear()

    def tearDown(self):
        self.driver.stop()
        self.agent.stop()
        telephony.sessions.clear()

    def test_starts_a_speech_gather_and_remembers_call(self):
        root = ET.fromstring(telephony.start_call({"CallSid": "CA1", "From": "+886900000000"}))
        self.assertEqual(root.find("Gather").attrib["action"], "/twilio/gather")
        self.assertIn("康寧診所", root.findtext("Gather/Say"))
        self.assertEqual(telephony.sessions["CA1"].call_id, "CA1")

    def test_completed_call_says_result_and_hangs_up(self):
        telephony.start_call({"CallSid": "CA2", "From": "+886900000000"})
        root = ET.fromstring(telephony.continue_call({"CallSid": "CA2", "SpeechResult": "好"}))
        self.assertEqual(root.findtext("Say"), "好的，已經幫您約好了。")
        self.assertIsNotNone(root.find("Hangup"))
        self.assertNotIn("CA2", telephony.sessions)

    def test_handoff_dials_configured_reception(self):
        telephony.start_call({"CallSid": "CA3", "From": "+886900000000"})
        with patch.dict(os.environ, {"HANDOFF_NUMBER": "+886212345678"}):
            root = ET.fromstring(telephony.continue_call({"CallSid": "CA3", "SpeechResult": "轉真人"}))
        self.assertEqual(root.findtext("Dial"), "+886212345678")

    def test_signature_rejects_changed_form(self):
        url = "https://example.test/twilio/voice"
        form = {"CallSid": "CA4", "From": "+886900000000"}
        signature = telephony.twilio_signature(url, form, "secret")
        self.assertTrue(telephony.valid_signature(url, form, signature, "secret"))
        self.assertFalse(telephony.valid_signature(url, {**form, "From": "changed"}, signature, "secret"))

    def test_signature_accepts_webhook_query_string(self):
        form = {"CallSid": "CA5", "From": "+886900000000"}
        url = "https://example.test/twilio/voice?source=phone"
        signature = telephony.twilio_signature(url, form, "secret")
        body = urlencode(form).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/twilio/voice",
                "query_string": b"source=phone",
                "headers": [(b"x-twilio-signature", signature.encode())],
            },
            receive,
        )
        with patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": "secret", "PUBLIC_BASE_URL": "https://example.test"}):
            self.assertEqual(asyncio.run(telephony._twilio_form(request)), form)


if __name__ == "__main__":
    unittest.main()

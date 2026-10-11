import unittest
from unittest.mock import patch

import clinic_api
from agent import Agent, ScriptedDriver, State


class SymptomHandoffTest(unittest.TestCase):
    """Naming a symptom is the reason for the visit, not a request for advice."""

    def setUp(self):
        stubs = [
            patch.object(clinic_api, "lookup_patient", return_value={"found": False, "upcoming": []}),
            patch.object(clinic_api, "find_slots", return_value={"slots": []}),
        ]
        for stub in stubs:
            stub.start()
            self.addCleanup(stub.stop)
        self.agent = Agent(phone="0900000000", driver=ScriptedDriver())
        self.agent.answer()

    def test_symptom_with_booking_intent_keeps_booking(self):
        self.agent.turn("我小孩發燒，想看醫生")
        self.assertIsNot(self.agent.state, State.HANDOFF)

    def test_asking_for_medical_advice_hands_off(self):
        self.agent.turn("發燒要吃什麼藥？")
        self.assertIs(self.agent.state, State.HANDOFF)

    def test_asking_for_a_human_hands_off(self):
        self.agent.turn("我要找真人")
        self.assertIs(self.agent.state, State.HANDOFF)


if __name__ == "__main__":
    unittest.main()

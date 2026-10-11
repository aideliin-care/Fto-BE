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
        self.agent.turn("我肩膀痛，想掛號復健")
        self.assertIsNot(self.agent.state, State.HANDOFF)

    def test_asking_for_medical_advice_hands_off(self):
        self.agent.turn("肩膀痛能不能熱敷？")
        self.assertIs(self.agent.state, State.HANDOFF)

    def test_asking_for_a_human_hands_off(self):
        self.agent.turn("我要找真人")
        self.assertIs(self.agent.state, State.HANDOFF)


class EmergencyDriver:
    def greet(self, found):
        return "您好，這裡是康寧復健科診所。"

    def respond(self, agent, said):
        agent.call_tool("handoff", {"reason": "emergency"})
        return "這聽起來很緊急，請立刻打 119 或到最近的急診。"


class EmergencyHandoffTest(unittest.TestCase):
    def test_emergency_advice_is_spoken_before_the_transfer(self):
        with patch.object(clinic_api, "lookup_patient", return_value={"found": False, "upcoming": []}):
            agent = Agent(phone="0900000000", driver=EmergencyDriver())
            agent.answer()
        reply = agent.turn("我爸突然半邊無力")
        self.assertIs(agent.state, State.HANDOFF)
        self.assertIn("119", reply)
        self.assertIn(Agent.HANDOFF_LINE, reply)


if __name__ == "__main__":
    unittest.main()

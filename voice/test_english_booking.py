import unittest
from unittest.mock import patch

import clinic_api
from agent import Agent, State, speak_time
from prompt import system_prompt
from test_llm_booking import SLOTS, ToolDriver


class EnglishBookingTest(unittest.TestCase):
    """The same gate as the Chinese call, spoken in English."""

    def setUp(self):
        stubs = [
            patch.object(clinic_api, "lookup_patient", return_value={"found": False, "upcoming": []}),
            patch.object(clinic_api, "find_slots", return_value={"slots": [dict(s) for s in SLOTS]}),
        ]
        for stub in stubs:
            stub.start()
            self.addCleanup(stub.stop)
        book = patch.object(clinic_api, "book", return_value={
            "appointment_id": "a1", "doctor_name": "林怡君", "starts_at": SLOTS[0]["starts_at"]})
        self.booked = book.start()
        self.addCleanup(book.stop)

    def run_booking(self, readback, yes="Yes, please."):
        driver = ToolDriver([
            ([("find_slots", {"department": "復健科"})], "Saturday at 8:30 or 8:45 AM. Which works?"),
            ([("propose", {"slot_id": "s1"})], readback),
            ([("confirm", {"quote": yes}),
              ("book", {"slot_id": "s1", "patient_name": "Amy Wang"})], "Booked."),
        ])
        agent = Agent(phone="0900000000", driver=driver, lang="en")
        agent.answer()
        for said in ["Saturday morning?", "The first one", yes]:
            agent.turn(said)
        return agent, driver

    def full_readback(self, driver):
        result = driver.results[-1]
        return f"That's {result['doctor_name']}, {result['spoken']}. Shall I book that for you?"

    def test_speak_time_in_english(self):
        self.assertEqual(speak_time(SLOTS[0]["starts_at"], "en"), "Saturday, October 17 at 8:30 AM")
        self.assertEqual(speak_time("2026-10-17T18:45:00+08:00", "en"), "Saturday, October 17 at 6:45 PM")

    def test_greeting_is_english(self):
        agent = Agent(phone="0900000000", driver=ToolDriver([]), lang="en")
        self.assertEqual(agent.answer(), "Hello, this is Kangning Rehabilitation Clinic. How can I help you today?")

    def test_propose_gives_english_name_and_time(self):
        _, driver = self.run_booking(self.full_readback)
        self.assertEqual(driver.results[1]["doctor_name"], "Dr. Lin Yi-Chun")
        self.assertEqual(driver.results[1]["spoken"], "Saturday, October 17 at 8:30 AM")

    def test_books_after_english_readback_and_yes(self):
        agent, _ = self.run_booking(self.full_readback)
        self.assertIs(agent.state, State.COMMITTED)
        self.assertEqual(agent.transcript[-1][1],
                         "All set. You're booked with Dr. Lin Yi-Chun on "
                         "Saturday, October 17 at 8:30 AM. Thank you, goodbye.")

    def test_surname_and_short_time_count_as_readback(self):
        agent, _ = self.run_booking(lambda d: "Dr. Lin, Saturday at 8:30 AM. Shall I book it?")
        self.assertIs(agent.state, State.COMMITTED)

    def test_readback_without_a_day_does_not_count(self):
        agent, _ = self.run_booking(lambda d: "Dr. Lin at 8:30. Shall I book it?")
        self.assertIsNot(agent.state, State.COMMITTED)
        self.booked.assert_not_called()

    def test_hedged_or_negative_answers_do_not_book(self):
        for said in ["Maybe?", "I can't make that one", "Hmm... okay", "No problem, but wait"]:
            with self.subTest(said=said):
                agent, _ = self.run_booking(self.full_readback, yes=said)
                self.assertIsNot(agent.state, State.COMMITTED)
        self.booked.assert_not_called()

    def test_handoff_line_is_english(self):
        driver = ToolDriver([([("handoff", {"reason": "human"})], "ok")])
        agent = Agent(phone="0900000000", driver=driver, lang="en")
        agent.answer()
        self.assertEqual(agent.turn("Can I talk to a person?"),
                         "Let me transfer you to our front desk. One moment, please.")

    def test_prompt_asks_for_english_with_an_english_calendar(self):
        prompt = system_prompt(lang="en")
        self.assertIn("Language: English", prompt)
        self.assertIn("Dr. Lin Yi-Chun", prompt)
        self.assertIn("(today)", prompt)
        self.assertNotIn("Language: English", system_prompt())


if __name__ == "__main__":
    unittest.main()

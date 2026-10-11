import unittest
from unittest.mock import patch

import clinic_api
from agent import Agent, State

SLOTS = [
    {"slot_id": "s1", "doctor_name": "林怡君", "department": "復健科",
     "starts_at": "2026-10-17T08:30:00+08:00", "ends_at": "2026-10-17T08:45:00+08:00"},
    {"slot_id": "s2", "doctor_name": "林怡君", "department": "復健科",
     "starts_at": "2026-10-17T08:45:00+08:00", "ends_at": "2026-10-17T09:00:00+08:00"},
]


class ToolDriver:
    """Plays the model: one scripted list of tool calls and a reply per turn."""

    def __init__(self, turns):
        self.turns = iter(turns)
        self.results = []

    def greet(self, found):
        return "您好，這裡是康寧復健科診所。"

    def respond(self, agent, said):
        calls, reply = next(self.turns)
        for name, args in calls:
            self.results.append(agent.call_tool(name, args))
        return reply(self) if callable(reply) else reply


class LlmBookingTest(unittest.TestCase):
    def setUp(self):
        stubs = [
            patch.object(clinic_api, "lookup_patient", return_value={"found": False, "upcoming": []}),
            patch.object(clinic_api, "find_slots", return_value={"slots": [dict(s) for s in SLOTS]}),
        ]
        for stub in stubs:
            stub.start()
            self.addCleanup(stub.stop)
        self.book = patch.object(clinic_api, "book", return_value={
            "appointment_id": "a1", "doctor_name": "林怡君", "starts_at": SLOTS[0]["starts_at"]})
        self.booked = self.book.start()
        self.addCleanup(self.book.stop)

    def run_booking(self, readback, book_slot="s1"):
        driver = ToolDriver([
            ([("find_slots", {"department": "復健科"})], "禮拜六上午有八點半、八點四十五分，您要哪一個？"),
            ([("propose", {"slot_id": "s1"})], readback),
            ([("confirm", {"quote": "好"}),
              ("book", {"slot_id": book_slot, "patient_name": "王小明"})], "好的，已經幫您約好了。"),
        ])
        agent = Agent(phone="0900000000", driver=driver)
        agent.answer()
        for said in ["禮拜六早上可以嗎", "第一個", "好"]:
            agent.turn(said)
        return agent, driver

    def test_model_can_book_after_propose_readback_and_yes(self):
        agent, driver = self.run_booking(lambda d: f"林怡君醫師，{d.results[-1]['spoken']}，這樣可以嗎？")
        self.assertIs(agent.state, State.COMMITTED)
        self.assertEqual(self.booked.call_args.args[1], "s1")

    def test_surname_readback_counts_when_unambiguous(self):
        agent, _ = self.run_booking(lambda d: f"林醫師，{d.results[-1]['spoken']}，這樣可以嗎？")
        self.assertIs(agent.state, State.COMMITTED)

    def test_closing_line_comes_from_the_booking_not_the_model(self):
        agent, _ = self.run_booking(lambda d: f"林怡君醫師，{d.results[-1]['spoken']}，這樣可以嗎？")
        self.assertEqual(agent.transcript[-1][1],
                         "好的，已經幫您約好了：林怡君醫師，10月17號 禮拜六 上午8點30分。謝謝您，再見。")

    def test_propose_spells_the_date_for_the_model(self):
        _, driver = self.run_booking(lambda d: f"林怡君醫師，{d.results[-1]['spoken']}，這樣可以嗎？")
        self.assertEqual(driver.results[1]["spoken"], "10月17號 禮拜六 上午8點30分")

    def test_book_refuses_a_slot_other_than_the_one_read_back(self):
        agent, driver = self.run_booking(
            lambda d: f"林怡君醫師，{d.results[-1]['spoken']}，這樣可以嗎？", book_slot="s2")
        self.assertIsNot(agent.state, State.COMMITTED)
        self.booked.assert_not_called()
        self.assertEqual(driver.results[-1]["error"], "SLOT_MISMATCH")

    def test_propose_rejects_a_slot_that_was_never_offered(self):
        agent = Agent(phone="0900000000", driver=ToolDriver([]))
        agent.answer()
        self.assertEqual(agent.call_tool("propose", {"slot_id": "nope"})["error"], "UNKNOWN_SLOT")


class CalendarTest(unittest.TestCase):
    def test_prompt_spells_out_weekdays_so_the_model_need_not_compute_them(self):
        from datetime import datetime
        from prompt import TZ, system_prompt
        text = system_prompt(datetime(2026, 10, 12, 9, 0, tzinfo=TZ))
        self.assertIn("2026-10-12 = 10月12號 禮拜一（今天）", text)
        self.assertIn("2026-10-17 = 10月17號 禮拜六", text)


if __name__ == "__main__":
    unittest.main()

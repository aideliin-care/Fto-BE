#!/usr/bin/env python3
"""Place a call to the agent from the terminal — the last mile without a phone.

    python3 voice/call.py                      # interactive, new caller
    python3 voice/call.py --phone 0912345678   # known patient
    python3 voice/call.py --demo               # scripted end-to-end booking

Uses a real LLM when OPENAI_API_KEY is set, a deterministic driver otherwise.
Either way the tool calls are real and the booking lands in clinic.db.
"""

import argparse
import sys

import clinic_api
from agent import Agent, make_driver

DEMO_TURNS = [
    "我要掛號，小兒科",
    "第一個",
    "好，麻煩你",
]


def banner(agent: Agent, driver) -> None:
    print(f"  driver   : {type(driver).__name__}")
    print(f"  caller   : {agent.phone}")
    print(f"  call_id  : {agent.call_id}")
    print(f"  api      : {clinic_api.BASE_URL}")
    print("-" * 60)


def run(phone: str, demo: bool) -> int:
    try:
        health = clinic_api.health()
    except Exception as exc:  # noqa: BLE001 - surface any startup failure plainly
        print(f"appointment service unreachable at {clinic_api.BASE_URL}: {exc}")
        print("start it first:  python3 -m uvicorn main:app --port 8787  (from api/)")
        return 1

    driver = make_driver()
    agent = Agent(phone=phone, driver=driver)
    banner(agent, driver)
    print(f"  {health['free']}/{health['slots']} slots free")
    print("-" * 60)

    print(f"診所: {agent.answer()}")

    turns = iter(DEMO_TURNS) if demo else None
    while True:
        if turns is not None:
            said = next(turns, None)
            if said is None:
                break
            print(f"來電者: {said}")
        else:
            try:
                said = input("來電者: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not said:
                continue
            if said in {"掛斷", "quit", "exit"}:
                break

        print(f"診所: {agent.turn(said)}")
        if agent.state.name in {"COMMITTED", "HANDOFF"} and turns is None:
            break

    print("-" * 60)
    print(f"  final state    : {agent.state.name}")
    print(f"  confirmed      : {agent.draft.confirmed}")
    print(f"  appointment_id : {agent.draft.appointment_id}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="0987000111")
    parser.add_argument("--demo", action="store_true", help="run a scripted booking")
    args = parser.parse_args()
    sys.exit(run(args.phone, args.demo))

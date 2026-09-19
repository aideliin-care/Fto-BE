"""Clinic reception agent: conversation state, tool dispatch, write gating.

State machine and dispatch rules are the spec authored by the `Voice` session.

The one design decision worth defending: the confirmation gate is enforced
here, in code, not in the prompt. A prompt rule is a request; this is a
guarantee. If the model tries to book before the caller has actually said yes,
the call is refused and the model is told to read back and ask. Same for
call_id — the agent injects it, so the model cannot omit it and silently break
redial idempotency.
"""

import json
import os
import re
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

import clinic_api
from prompt import TZ, TOOLS, system_prompt

WRITE_TOOLS = {"book", "reschedule", "cancel"}


class State(str, Enum):
    GREETING = "GREETING"
    INTENT = "INTENT"
    COLLECTING = "COLLECTING"
    CONFIRMING = "CONFIRMING"
    RECOVERING = "RECOVERING"
    COMMITTED = "COMMITTED"
    CLOSING = "CLOSING"
    HANDOFF = "HANDOFF"


@dataclass
class Draft:
    """One per call, freely overwritten. Revision replaces, never accumulates."""

    phone: str
    intent: str | None = None
    patient_id: str | None = None
    patient_name: str | None = None
    appointment_id: str | None = None
    slot_id: str | None = None
    doctor_name: str | None = None
    starts_at: str | None = None
    confirmed: bool = False
    offered: list[dict] = field(default_factory=list)
    last_query: dict = field(default_factory=dict)


def speak_time(iso: str) -> str:
    """RFC3339 -> something a person can hear. Never read codes aloud."""
    dt = datetime.fromisoformat(iso)
    weekday = "一二三四五六日"[dt.weekday()]
    hour, meridiem = dt.hour, "上午"
    if dt.hour >= 12:
        meridiem = "下午"
        hour = dt.hour - 12 if dt.hour > 12 else 12
    minute = f"{dt.minute}分" if dt.minute else "整"
    return f"{dt.month}月{dt.day}號 禮拜{weekday} {meridiem}{hour}點{minute}"


class Agent:
    """Drives one call. `turn()` takes what the caller said, returns what to say."""

    def __init__(self, phone: str, driver, call_id: str | None = None):
        self.phone = phone
        self.call_id = call_id or f"call-{uuid.uuid4()}"
        self.driver = driver
        self.draft = Draft(phone=phone)
        self.state = State.GREETING
        self.messages = [{"role": "system", "content": system_prompt()}]
        self.transcript: list[tuple[str, str]] = []

    # -- lifecycle ---------------------------------------------------------

    def answer(self) -> str:
        """Called the moment the line is picked up: caller ID lookup first."""
        try:
            found = clinic_api.lookup_patient(self.phone)
        except clinic_api.ApiError:
            found = {"found": False, "upcoming": []}

        if found.get("found"):
            self.draft.patient_id = found.get("patient_id")
            self.draft.patient_name = found.get("name")
            upcoming = found.get("upcoming") or []
            if upcoming:
                self.draft.appointment_id = upcoming[0]["appointment_id"]
        self._note_system(f"caller_id_lookup={json.dumps(found, ensure_ascii=False)}")
        self.state = State.INTENT
        return self._say(self.driver.greet(found))

    def turn(self, said: str) -> str:
        self.transcript.append(("caller", said))
        self.messages.append({"role": "user", "content": said})
        if self._is_affirmation(said) and self.state == State.CONFIRMING:
            self.draft.confirmed = True
        elif self._is_revision(said):
            self.draft.confirmed = False
        return self._say(self.driver.respond(self, said))

    # -- tool dispatch -----------------------------------------------------

    def call_tool(self, name: str, args: dict) -> dict:
        """Every tool call funnels through here, including the model's."""
        if name in WRITE_TOOLS and not self.draft.confirmed:
            # The gate. Refuse rather than write, and say why.
            return {
                "error": "NOT_CONFIRMED",
                "hint": "來電者還沒有明確確認。請先讀回醫師姓名和完整時間，問對方確認後再送出。",
            }

        try:
            if name == "find_slots":
                self.draft.last_query = dict(args)
                result = clinic_api.find_slots(**args)
                self.draft.offered = result.get("slots", [])
                self.state = State.COLLECTING
                return result

            if name == "book":
                result = clinic_api.book(
                    self.call_id,
                    args["slot_id"],
                    args.get("patient_name") or self.draft.patient_name or "來電民眾",
                    self.phone,
                )
            elif name == "reschedule":
                result = clinic_api.reschedule(
                    self.call_id, args["appointment_id"], args["new_slot_id"]
                )
            elif name == "cancel":
                result = clinic_api.cancel(self.call_id, args["appointment_id"])
            elif name == "handoff":
                self.state = State.HANDOFF
                return {"handoff": True, "reason": args.get("reason", "")}
            else:
                return {"error": "UNKNOWN_TOOL", "name": name}

        except clinic_api.ApiError as exc:
            if exc.code == "SLOT_TAKEN":
                return self._recover_slot_taken()
            if exc.code == "SLOT_IN_PAST":
                # Distinct from SLOT_TAKEN on purpose: nobody took it, our list
                # went stale. Recovery is the same, but we must not tell the
                # caller it was booked by someone else — that would be a lie.
                return self._recover_slot_taken(
                    hint="這個時段已經過了，不是被約走。請重新提供可選時段，不要說被約走。"
                )
            if exc.code in {"SERVER_ERROR", "UNREACHABLE", "INTERNAL"}:
                self.state = State.HANDOFF
                return {"error": exc.code, "handoff": True,
                        "hint": "系統有狀況，請向來電者致歉並轉接真人。"}
            return {"error": exc.code, "message": exc.message}

        self.draft.appointment_id = result.get("appointment_id")
        self.draft.doctor_name = result.get("doctor_name")
        self.draft.starts_at = result.get("starts_at")
        self.state = State.COMMITTED
        return result

    def _recover_slot_taken(self, hint: str | None = None) -> dict:
        """409 is never an error to the caller — it's a new set of options."""
        self.draft.confirmed = False
        self.draft.slot_id = None
        self.state = State.RECOVERING
        try:
            alternatives = clinic_api.find_slots(**self.draft.last_query).get("slots", [])
        except clinic_api.ApiError:
            alternatives = []
        self.draft.offered = alternatives
        return {
            "slot_taken": True,
            "alternatives": alternatives,
            "hint": hint or "那個時段剛被約走。請直接說明並馬上提兩個替代時段，不要只是道歉。",
        }

    # -- caller intent heuristics -----------------------------------------

    AFFIRM = re.compile(r"(^|[^不])(好|對|可以|沒問題|就這個|就那個|麻煩你|是的|ok|OK)")
    REVISE = re.compile(r"(不對|不要|改成|換成|等一下|等等|還是|另外|重新)")

    def _is_affirmation(self, said: str) -> bool:
        return bool(self.AFFIRM.search(said)) and not self.REVISE.search(said)

    def _is_revision(self, said: str) -> bool:
        return bool(self.REVISE.search(said))

    # -- bookkeeping -------------------------------------------------------

    def _say(self, text: str) -> str:
        self.transcript.append(("agent", text))
        self.messages.append({"role": "assistant", "content": text})
        return text

    def _note_system(self, note: str) -> None:
        self.messages.append({"role": "system", "content": note})


# -- drivers ---------------------------------------------------------------
# The driver is only the language model. Everything above it — state, gating,
# dispatch, recovery — is exercised identically whichever driver is in use, so
# the scripted driver genuinely tests the path that touches the database.


class OpenAIDriver:
    """Real LLM over plain HTTP. No SDK, so nothing to install."""

    def __init__(self, model: str | None = None):
        self.api_key = os.environ["OPENAI_API_KEY"]
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def greet(self, found: dict) -> str:
        if found.get("found"):
            name = found.get("name", "")
            upcoming = found.get("upcoming") or []
            if upcoming:
                when = speak_time(upcoming[0]["starts_at"])
                return (f"{name}您好，這裡是康寧診所。您目前有一筆 {when}、"
                        f"{upcoming[0]['doctor_name']}醫師的預約。請問有什麼可以幫您？")
            return f"{name}您好，這裡是康寧診所，請問有什麼可以幫您？"
        return "您好，這裡是康寧診所，請問有什麼可以幫您？"

    def respond(self, agent: "Agent", said: str) -> str:
        for _ in range(4):  # bounded tool-call loop
            reply = self._chat(agent.messages)
            calls = reply.get("tool_calls")
            if not calls:
                return reply.get("content") or "不好意思，可以再說一次嗎？"
            agent.messages.append(reply)
            for call in calls:
                args = json.loads(call["function"]["arguments"] or "{}")
                result = agent.call_tool(call["function"]["name"], args)
                agent.messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "不好意思，我幫您轉接櫃檯人員。"

    def _chat(self, messages: list) -> dict:
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "temperature": 0.3,
        }).encode()
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["choices"][0]["message"]


class ScriptedDriver:
    """Deterministic stand-in so the whole path runs with no API key.

    Not a toy: it drives the same Agent.call_tool path, so a booking made here
    is a real row in clinic.db. It understands little, on purpose — its job is
    to prove the wiring, not to hold a conversation.
    """

    DEPARTMENTS = ["小兒科", "家醫科", "皮膚科", "內科", "耳鼻喉科"]
    PICK = {"第一": 0, "一": 0, "第二": 1, "二": 1, "第三": 2, "三": 2}

    def greet(self, found: dict) -> str:
        return OpenAIDriver.greet(self, found)  # same wording, no key needed

    def respond(self, agent: "Agent", said: str) -> str:
        draft = agent.draft

        if re.search(r"(症狀|過敏|吃藥|藥物|痛|發燒|真人|專員|櫃檯)", said):
            agent.call_tool("handoff", {"reason": "clinical or human request"})
            return "這部分我幫您轉接櫃檯人員，請稍等。"

        if re.search(r"取消", said):
            draft.intent = "cancel"
            if not draft.appointment_id:
                return "請問您要取消哪一筆預約呢？我這邊查不到您名下的預約。"
            agent.state = State.CONFIRMING
            return f"要取消的是 {speak_time(draft.starts_at or '')} 這筆，確定要取消嗎？" \
                if draft.starts_at else "您名下有一筆預約，確定要取消嗎？"

        if re.search(r"(改期|改時間|換時間|改到)", said):
            draft.intent = "reschedule"

        if draft.intent is None:
            draft.intent = "book"

        for department in self.DEPARTMENTS:
            if department in said:
                draft.last_query = {"department": department}

        # Caller picked one of the options we read out.
        for token, index in self.PICK.items():
            if token in said and draft.offered and index < len(draft.offered):
                chosen = draft.offered[index]
                draft.slot_id = chosen["slot_id"]
                draft.doctor_name = chosen["doctor_name"]
                draft.starts_at = chosen["starts_at"]
                agent.state = State.CONFIRMING
                return (f"好的，幫您確認一下：{chosen['doctor_name']}醫師，"
                        f"{speak_time(chosen['starts_at'])}。這樣可以嗎？")

        if draft.confirmed and draft.slot_id and agent.state != State.COMMITTED:
            result = agent.call_tool("book", {
                "slot_id": draft.slot_id,
                "patient_name": draft.patient_name or self._name_from(said) or "來電民眾",
            })
            if result.get("slot_taken"):
                return self._offer(result["alternatives"], prefix="不好意思，那個時段剛剛被約走了。")
            if result.get("error"):
                return "不好意思，系統有點狀況，我幫您轉接櫃檯人員。"
            return (f"好的，已經幫您約好了：{result['doctor_name']}醫師，"
                    f"{speak_time(result['starts_at'])}。謝謝您，再見。")

        if draft.confirmed and draft.intent == "cancel" and draft.appointment_id:
            result = agent.call_tool("cancel", {"appointment_id": draft.appointment_id})
            if result.get("error"):
                return "不好意思，取消沒有成功，我幫您轉接櫃檯人員。"
            return "好的，已經幫您取消了。謝謝您，再見。"

        result = agent.call_tool("find_slots", {**draft.last_query, "limit": 5})
        slots = result.get("slots", [])
        if not slots:
            return "不好意思，最近沒有可以預約的時段了，我幫您轉接櫃檯人員。"
        return self._offer(slots)

    def _offer(self, slots: list, prefix: str = "") -> str:
        options = slots[:2]
        spoken = "，或是".join(
            f"{s['doctor_name']}醫師 {speak_time(s['starts_at'])}" for s in options
        )
        return f"{prefix}目前有 {spoken}。您方便哪一個？"

    def _name_from(self, said: str):
        match = re.search(r"我(?:姓|叫|是)\s*([一-龥]{2,4})", said)
        return match.group(1) if match else None


def make_driver():
    """Real model when a key exists, scripted otherwise. Phone is the last mile."""
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIDriver()
    return ScriptedDriver()

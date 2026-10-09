"""Browser chat adapter for the appointment agent — the phone call, typed.

Same shape as telephony.py: one Agent per live session, one HTTP request per
turn. Sessions live in memory; this is a demo surface, not a durable one.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent import Agent, State, make_driver

app = FastAPI(title="Clinic web chat")
router = APIRouter()
sessions: dict[str, Agent] = {}
PAGE = Path(__file__).with_name("web_chat.html")
ENDED = {State.COMMITTED, State.HANDOFF}


class StartIn(BaseModel):
    phone: str


class MessageIn(BaseModel):
    session_id: str
    text: str


def _reply(session_id: str, agent: Agent, reply: str) -> dict:
    if agent.state in ENDED:
        sessions.pop(session_id, None)
    return {
        "session_id": session_id,
        "reply": reply,
        "state": agent.state.name,
        "appointment_id": agent.draft.appointment_id if agent.state is State.COMMITTED else None,
    }


def start_chat(phone: str) -> dict:
    session_id = str(uuid.uuid4())
    agent = Agent(phone=phone, driver=make_driver())
    sessions[session_id] = agent
    return _reply(session_id, agent, agent.answer())


def continue_chat(session_id: str, text: str) -> dict:
    agent = sessions.get(session_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="對話已結束或不存在，請重新開始。")
    try:
        reply = agent.turn(text)
    except Exception as exc:  # noqa: BLE001 - never expose provider failures to the page
        logging.exception("web chat turn failed for %s", session_id)
        sessions.pop(session_id, None)
        raise HTTPException(status_code=502, detail="系統暫時無法服務，請稍後再試。") from exc
    return _reply(session_id, agent, reply)


@router.get("/chat", response_class=HTMLResponse)
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@router.post("/chat/start")
async def start(body: StartIn) -> dict:
    return await run_in_threadpool(start_chat, body.phone.strip())


@router.post("/chat/message")
async def message(body: MessageIn) -> dict:
    return await run_in_threadpool(continue_chat, body.session_id, body.text.strip())


app.include_router(router)

"""Minimal Twilio Voice adapter for the text appointment agent.

Twilio does speech recognition and playback with TwiML. This module only keeps
one Agent per live CallSid and translates each webhook into one agent turn.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from agent import Agent, State, make_driver

app = FastAPI(title="Clinic phone adapter")
sessions: dict[str, Agent] = {}


def twilio_signature(url: str, form: dict[str, str], token: str) -> str:
    """Create the signature Twilio sends for an urlencoded Voice webhook."""
    payload = url + "".join(key + form[key] for key in sorted(form))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(url: str, form: dict[str, str], signature: str, token: str) -> bool:
    return hmac.compare_digest(twilio_signature(url, form, token), signature)


def _xml(response: ET.Element) -> str:
    return ET.tostring(response, encoding="unicode")


def _listen(text: str) -> str:
    response = ET.Element("Response")
    gather = ET.SubElement(
        response,
        "Gather",
        input="speech",
        action="/twilio/gather",
        method="POST",
        language="zh-TW",
        speechTimeout="auto",
        actionOnEmptyResult="true",
    )
    ET.SubElement(gather, "Say", language="zh-TW").text = text
    return _xml(response)


def _end(text: str) -> str:
    response = ET.Element("Response")
    ET.SubElement(response, "Say", language="zh-TW").text = text
    ET.SubElement(response, "Hangup")
    return _xml(response)


def _transfer() -> str:
    number = os.environ.get("HANDOFF_NUMBER")
    if not number:
        return _end("不好意思，櫃檯目前無法轉接，請稍後再撥。")
    response = ET.Element("Response")
    ET.SubElement(response, "Dial").text = number
    return _xml(response)


def start_call(form: dict[str, str]) -> str:
    call_id, phone = form.get("CallSid"), form.get("From")
    if not call_id or not phone:
        raise ValueError("Twilio webhook is missing CallSid or From")
    agent = Agent(phone=phone, driver=make_driver(), call_id=call_id)
    sessions[call_id] = agent
    return _listen(agent.answer())


def continue_call(form: dict[str, str]) -> str:
    call_id = form.get("CallSid")
    agent = sessions.get(call_id or "")
    if agent is None:
        return _end("通話已逾時，請重新撥號，謝謝。")

    said = form.get("SpeechResult", "").strip()
    if not said:
        return _listen("不好意思，我沒有聽清楚，請再說一次。")

    try:
        reply = agent.turn(said)
    except Exception:  # noqa: BLE001 - never expose provider failures to callers
        logging.exception("voice turn failed for %s", call_id)
        sessions.pop(call_id, None)
        return _end("不好意思，系統暫時無法服務，請稍後再撥。")

    if agent.state is State.HANDOFF:
        sessions.pop(call_id, None)
        return _transfer()
    if agent.state is State.COMMITTED:
        sessions.pop(call_id, None)
        return _end(reply)
    return _listen(reply)


async def _twilio_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    form = {key: values[-1] for key, values in parsed.items()}

    token = os.environ.get("TWILIO_AUTH_TOKEN")
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{base_url}{request.url.path}"
    if not token or not base_url or not valid_signature(url, form, signature, token):
        raise HTTPException(status_code=403, detail="invalid Twilio webhook signature")
    return form


@app.get("/health")
def health() -> dict:
    return {"ok": True, "active_calls": len(sessions)}


@app.post("/twilio/voice", response_class=Response)
async def incoming_call(request: Request) -> Response:
    try:
        twiml = start_call(await _twilio_form(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(twiml, media_type="application/xml")


@app.post("/twilio/gather", response_class=Response)
async def gather(request: Request) -> Response:
    return Response(continue_call(await _twilio_form(request)), media_type="application/xml")

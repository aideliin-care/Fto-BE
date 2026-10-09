"""Railway entry point: one process serves appointments and phone webhooks."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path[:0] = [str(ROOT / "api"), str(ROOT / "voice")]

from main import app  # noqa: E402
from telephony import router  # noqa: E402
from web_chat import router as chat_router  # noqa: E402

app.include_router(router)
app.include_router(chat_router)

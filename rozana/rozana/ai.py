"""Optional AI helpers (Gemini). The app works without them.

1. rewrite(): when the rule parser doesn't understand a message, Gemini
   rewrites it into one of the simple formats the parser does understand.
   The AI never touches your plan directly; it only translates words.
2. read_timetable(): reads a timetable photo into lines like
   "Mon 10:00-11:30 Operations" for you to confirm before saving.
"""
from __future__ import annotations

import base64
import os

import httpx

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
KEY = os.getenv("GEMINI_API_KEY", "")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

REWRITE_PROMPT = """You convert a student's chat message to a planner bot into ONE canonical line.
Current local time: {now}. Allowed outputs (use 24h HH:MM times):
  up
  sleep HH:MM
  <title> HH:MM to HH:MM        (a new fixed event today, e.g. "Movie 20:00 to 23:00")
  cancel <class name>
  sick | travel | off day | back to normal
  did <activity> <minutes>      (e.g. "did gym 40")
  plan | week
  unknown
Message: {msg}
Reply with only the line."""

TIMETABLE_PROMPT = """This image is a weekly class timetable. List every class as one line:
<Mon|Tue|Wed|Thu|Fri|Sat|Sun> HH:MM-HH:MM <course name>
Use 24h time. Short course names. No other text. If unreadable, reply: NONE"""


def enabled() -> bool:
    return bool(KEY)


async def _call(parts: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(URL.format(model=MODEL), params={"key": KEY},
                              json={"contents": [{"parts": parts}],
                                    "generationConfig": {"temperature": 0}})
        r.raise_for_status()
        data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


async def rewrite(msg: str, now_str: str) -> str | None:
    if not enabled():
        return None
    try:
        out = await _call([{"text": REWRITE_PROMPT.format(now=now_str, msg=msg)}])
        line = out.strip().strip("`").splitlines()[0].strip()
        return None if line.lower() == "unknown" else line
    except Exception:
        return None


async def read_timetable(image: bytes, mime: str = "image/jpeg") -> list[str]:
    if not enabled():
        return []
    out = await _call([{"text": TIMETABLE_PROMPT},
                       {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}}])
    if out.strip().upper().startswith("NONE"):
        return []
    return [l.strip("-• ").strip() for l in out.splitlines() if l.strip()]

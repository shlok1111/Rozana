"""Turns everyday messages into actions, with plain rules (no AI).

Understands things like:
  up | up at 9:30 | woke up
  sleep at 1 | bed 12:30am
  movie 8 to 11 | football from 6-7pm | OB moved to 3 to 4 | dinner at 9 for 2h
  cancel OB | no marketing class today
  sick | travelling today | off day | back to normal
  did gym 40 | done interview prep 1h30 | finished reading 25m
  plan | week | goals | help
If none of these match, ai.py (optional) rewrites the message into one of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

TIME = r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)"
RANGE_SEP = r"\s*(?:to|till|until|upto|up to|-|–|—)\s*"
DUR = r"(\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours)(?:\s*\d+\s*(?:m|min|mins|minutes)?)?|\d+\s*(?:m|min|mins|minutes)?)"
EVENING_WORDS = re.compile(r"\b(movie|film|dinner|party|drinks|night|outing|concert|match|show)\b", re.I)


@dataclass
class Intent:
    kind: str                       # up | bed | event | cancel | mode | log | cmd | unknown
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    minutes: int = 0
    mode: str = ""
    cmd: str = ""
    arg: str = ""


def parse_minutes(s: str) -> int:
    s = s.strip().lower()
    h = re.match(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\s*(?:(\d+)\s*(?:m|min|mins|minutes)?)?$", s)
    if h:
        return int(round(float(h.group(1)) * 60)) + int(h.group(2) or 0)
    m = re.match(r"(\d+)\s*(?:m|min|mins|minutes)?$", s)
    return int(m.group(1)) if m else 0


def _clock(tok: str) -> tuple[int, int, str | None]:
    tok = tok.strip().lower().replace(".", ":").replace("a:m:", "am").replace("p:m:", "pm")
    ampm = None
    if tok.endswith("am") or tok.endswith("pm"):
        ampm, tok = tok[-2:], tok[:-2].strip()
    tok = tok.rstrip(":")
    if ":" in tok:
        h, m = tok.split(":")[:2]
    else:
        h, m = tok, "0"
    return int(h), int(m or 0), ampm


def candidates(tok: str, day_start: datetime) -> list[datetime]:
    """All sensible readings of a clock time across the logical day (+ after midnight)."""
    h, m, ampm = _clock(tok)
    if h > 23 or m > 59:
        return []
    hours: list[int]
    if ampm == "am":
        hours = [0 if h == 12 else h]
    elif ampm == "pm":
        hours = [12 if h == 12 else h + 12]
    elif h >= 13 or h == 0 or ":" in tok and len(tok.split(":")[0]) == 2 and tok.startswith("0"):
        hours = [h]                              # 24h style: 20:00, 08:30
    else:
        hours = [h % 12, h % 12 + 12]
    out = []
    for base in hours:
        for extra in (0, 24):                    # same night, after midnight
            out.append(day_start + timedelta(hours=base + extra, minutes=m))
    return sorted(set(out))


def resolve_range(t1: str, t2: str, now: datetime, day_start: datetime,
                  title: str = "") -> tuple[datetime, datetime] | None:
    starts = candidates(t1, day_start)
    if not starts:
        return None
    # "8 to 11pm": the end's am/pm applies to the start too
    _, _, a2 = _clock(t2)
    _, _, a1 = _clock(t1)
    if a1 is None and a2 is not None:
        starts = candidates(t1 + a2, day_start) or starts
    prefer_pm = bool(EVENING_WORDS.search(title))
    pick = None
    for s in starts:
        ends = [e for e in candidates(t2, day_start) if s < e <= s + timedelta(hours=14)]
        if not ends:
            continue
        e = ends[0]
        if e <= now:                             # already over
            continue
        if prefer_pm and s.hour < 12 and a1 is None and a2 is None and s < day_start + timedelta(hours=24):
            continue
        pick = (s, e)
        break
    return pick


def resolve_future(tok: str, now: datetime, day_start: datetime, min_ahead=timedelta(0)) -> datetime | None:
    for c in candidates(tok, day_start):
        if c > now + min_ahead:
            return c
    return None


def resolve_past(tok: str, now: datetime, day_start: datetime) -> datetime | None:
    past = [c for c in candidates(tok, day_start) if c <= now + timedelta(minutes=5)]
    return past[-1] if past else None


def _clean_title(t: str) -> str:
    t = re.sub(r"\b(today|tonight|now|is|has been|got|have|i|i'm|im|going|for|a|the|at|from|"
               r"moved|shifted|rescheduled|changed|postponed|preponed|plan|plans|planned|to)\b", " ", t, flags=re.I)
    t = re.sub(r"[<>&]", "", t)                  # keep Telegram HTML safe
    t = re.sub(r"\s+", " ", t).strip(" ,.-:")
    return t[:1].upper() + t[1:] if t else "Event"


def parse(text: str, now: datetime, day_start: datetime) -> Intent:
    t = text.strip()
    low = t.lower().strip()

    # slash commands and bare words
    if low.startswith("/"):
        cmd, _, arg = t[1:].partition(" ")
        return Intent("cmd", cmd=cmd.split("@")[0].lower(), arg=arg.strip())
    if low in {"plan", "today", "my plan", "replan", "week", "goals", "help", "timetable", "events"}:
        return Intent("cmd", cmd={"today": "plan", "my plan": "plan"}.get(low, low))

    # waking up
    m = re.match(r"^(?:i'?m\s+)?(?:up|woke(?:\s+up)?|awake|good morning|gm|morning)(?:\s+(?:at|since)\s+" + TIME + r")?[\s!.]*$", low)
    if m:
        w = resolve_past(m.group(1), now, day_start) if m.group(1) else now
        return Intent("up", start=w or now)

    # bedtime
    m = re.match(r"^(?:i'?ll\s+)?(?:sleep(?:ing)?|bed(?:time)?|going to (?:sleep|bed)|sleep by)\s*(?:at|by|around)?\s*" + TIME + r"[\s.!]*$", low)
    if m:
        cands = [c for c in candidates(m.group(1), day_start) if c > now + timedelta(minutes=10)]
        night = [c for c in cands if c.hour >= 20 or c.hour < 8]    # bedtimes are at night
        explicit = _clock(m.group(1))[2] is not None
        b = (cands if explicit else (night or cands) or [None])[0]
        if b:
            return Intent("bed", start=b)

    # modes
    if re.search(r"\b(back to normal|feeling better|normal day|not sick|reached|back now)\b", low):
        return Intent("mode", mode="normal")
    if re.search(r"\b(sick|ill|fever|unwell|not well|cold and)\b", low):
        return Intent("mode", mode="sick")
    if re.search(r"\b(travel|travell?ing|journey|on the road|flight|train today|bus today)\b", low):
        return Intent("mode", mode="travel")
    if re.search(r"\b(off day|day off|rest day|holiday mode)\b", low):
        return Intent("mode", mode="off")

    # logging: "did gym 40", "did 40 min gym", "done interview prep 1h"
    m = re.match(r"^(?:did|done|finished|completed|logged?)\s+(.+?)\s+(?:for\s+)?" + DUR + r"[\s.!]*$", low)
    if m and parse_minutes(m.group(2)):
        return Intent("log", title=m.group(1), minutes=parse_minutes(m.group(2)))
    m = re.match(r"^(?:did|done|finished|completed)\s+" + DUR + r"\s+(?:of\s+)?(.+?)[\s.!]*$", low)
    if m and parse_minutes(m.group(1)):
        return Intent("log", title=m.group(2), minutes=parse_minutes(m.group(1)))

    # cancellations: "cancel OB", "no marketing class today", "OB cancelled"
    m = re.match(r"^(?:cancel|cancelled|no)\s+(.+?)(?:\s+class)?(?:\s+today)?[\s.!]*$", low) or \
        re.match(r"^(.+?)(?:\s+class)?\s+(?:is\s+)?(?:cancelled|canceled|off)(?:\s+today)?[\s.!]*$", low)
    if m:
        return Intent("cancel", title=m.group(1).strip())

    # events with a range: "movie 8 to 11", "football from 6-7pm", "3 to 4 OB"
    m = re.match(r"^(.*?)\s*(?:from\s+|at\s+)?" + TIME + RANGE_SEP + TIME + r"\s*(.*?)[\s.!]*$", low)
    if m and (m.group(1) or m.group(4)):
        raw_title = (m.group(1) or m.group(4))
        title = _clean_title(raw_title)
        rng = resolve_range(m.group(2), m.group(3), now, day_start, raw_title)
        if rng:
            return Intent("event", title=title, start=rng[0], end=rng[1])

    # events with a duration: "dinner at 9 for 2h", "gym at 6pm for 45 min"
    m = re.match(r"^(.+?)\s+at\s+" + TIME + r"\s+for\s+" + DUR + r"[\s.!]*$", low)
    if m and parse_minutes(m.group(3)):
        raw = m.group(1)
        s = None
        for c in candidates(m.group(2), day_start):
            if c + timedelta(minutes=parse_minutes(m.group(3))) > now and \
                    not (EVENING_WORDS.search(raw) and c.hour < 12 and c < day_start + timedelta(hours=24)):
                s = c
                break
        if s:
            return Intent("event", title=_clean_title(raw), start=s,
                          end=s + timedelta(minutes=parse_minutes(m.group(3))))

    return Intent("unknown", title=t)

"""Simulated days. Run: python -m pytest -q"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rozana.core import App
from rozana.db import DB

TZ = ZoneInfo("Asia/Kolkata")


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def set(self, y, mo, d, h, mi=0): self.t = datetime(y, mo, d, h, mi, tzinfo=TZ)


def make():
    c = Clock(datetime(2026, 11, 23, 9, 0, tzinfo=TZ))   # Monday
    app = App(DB(":memory:"), clock=c)
    app.save_timetable_lines(["Tue 14:00-17:00 Operations Management", "Tue 10:00-11:00 Marketing"])
    return app, c


def strip(s): return re.sub(r"<[^>]+>", "", s)


def test_late_wake_class_and_movie():
    app, c = make()
    c.set(2026, 11, 24, 12, 0)                     # Tuesday, woke at noon
    r = app.handle("up")
    print(strip(r.text))
    assert "Interview prep" in r.text and "Operations" in r.text
    c.set(2026, 11, 24, 17, 30)
    r = app.handle("movie with friends 8 to 11")
    print(strip(r.text))
    assert "8:00pm–11:00pm" in r.text and "counts as Fun" in r.text
    assert "Dinner" not in r.text                  # movie replaced dinner slot
    assert "🎮" not in r.text                       # movie covers fun


def test_missed_morning_moves_to_evening():
    app, c = make()
    c.set(2026, 11, 23, 7, 0)
    app.handle("up")
    first = app.db.one("SELECT * FROM blocks ORDER BY start_utc")
    c.set(2026, 11, 23, 9, 0)
    app.checkin(first["id"])
    r = app.button(f"later:{first['id']}")
    print(strip(r.text))
    assert first["name"] in r.text


def test_tight_day_squeezes_fun_first():
    app, c = make()
    c.set(2026, 11, 23, 22, 30)
    r = app.handle("up")
    print(strip(r.text))
    assert "Interview prep" in r.text and "Exercise" in r.text and "🎮" not in r.text


def test_sick_day_no_debt_and_slack_creates_capped_debt():
    app, c = make()
    c.set(2026, 11, 23, 10, 0); app.handle("up"); app.handle("sick today")
    c.set(2026, 11, 24, 10, 0); app.handle("up")   # Tuesday: slack, log nothing
    c.set(2026, 11, 25, 10, 0)
    r = app.handle("up")                           # Wednesday
    print(strip(r.text))
    g = app.db.one("SELECT * FROM goals WHERE name='Interview prep'")
    assert app.carry(g, "2026-11-25") == 30        # 60 min missed Tue → 30 (Mon sick excused)
    assert "+30m catch-up" in r.text


def test_cancel_and_move_class():
    app, c = make()
    c.set(2026, 11, 24, 9, 0); app.handle("up")
    r = app.handle("cancel marketing"); print(strip(r.text))
    assert "Marketing cancelled" in r.text
    r = app.handle("OM moved to 3 to 4"); print(strip(r.text))
    assert "replaces the usual Operations Management" in r.text and "Interview prep" in r.text


def test_log_and_week_and_sprint():
    app, c = make()
    c.set(2026, 11, 23, 10, 0); app.handle("up")
    r = app.handle("did gym 40"); assert "40m of Exercise" in r.text
    r = app.command("sprint", "Test prep 180 until thu"); print(strip(r.text))
    assert "Test prep" in r.text
    print(strip(app.command("week", "").text))
    print(strip(app.wrapup()))


def test_time_parsing():
    from rozana.parse import parse
    ds = datetime(2026, 11, 24, tzinfo=TZ)
    now = ds + timedelta(hours=16)
    i = parse("sleep at 1", now, ds); assert i.start.hour == 1 and i.start.day == 25
    i = parse("dinner at 9 for 2h", now, ds); assert i.start.hour == 21
    i = parse("football 6-7pm", now, ds); assert (i.start.hour, i.end.hour) == (18, 19)
    i = parse("class 2 to 5", now, ds); assert i.start.hour == 14   # ongoing
    i = parse("movie 8 to 11", ds + timedelta(hours=9), ds); assert i.start.hour == 20
    i = parse("did 30 min reading", now, ds); assert i.kind == "log" and i.minutes == 30
    i = parse("done interview prep 1h30", now, ds); assert i.minutes == 90


def test_skip_bedtime_and_timetable_photo_flow():
    app, c = make()
    c.set(2026, 11, 23, 10, 0); app.handle("up")
    b = app.db.one("SELECT * FROM blocks WHERE name='Interview prep'")
    r = app.button(f"skip:{b['id']}")
    assert "Skipped Interview prep" in r.text and "🎯" in r.text
    assert "Interview prep" not in r.text.split("\n", 1)[1]   # not rescheduled today
    r = app.handle("sleep at 11"); assert "Bedtime set to 11:00pm" in r.text
    app.db.set("pending_timetable", "Mon 09:00-10:30 Finance\nWed 11:00-12:00 HRM\ngarbage line")
    r = app.button("tt_save"); assert "Saved 2 classes" in r.text
    assert "Finance" in app.command("timetable", "").text and "Lunch" in app.command("timetable", "").text

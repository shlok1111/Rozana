"""App logic: connects messages, storage and the planner. No Telegram code here,
so every behaviour can be tested by just calling functions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import parse as P
from .db import DB, parse_utc, utc
from .planner import Fixed, Goal, build_plan

DAY_CUTOFF_H = 4          # before 4am still counts as the previous day
DEBT_SHARE = 0.5          # half of a slacked minimum carries over
DEBT_CAP = 30             # ...but never more than +30 min per goal per day
DEBT_WINDOW = 7           # ...and only from the last 7 days
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EVERY_DAY = 7

DEFAULT_GOALS = [
    # name, priority, min, ideal, window, kind, keywords, max_chunk
    ("Interview prep", 1, 60, 90, "any", "core",
     "interview,mock,prep,guesstimate,case,star,hr,aptitude", 90),
    ("Exercise", 1, 30, 45, "evening", "core",
     "gym,workout,exercise,run,running,walk,football,cricket,badminton,yoga,sport", 60),
    ("Personal improvement", 2, 20, 45, "any", "core",
     "read,reading,english,learn,learning,course,skill,improvement,journal,book", 60),
    ("Fun", 3, 0, 120, "evening", "fun",
     "fun,movie,film,game,gaming,series,netflix,youtube,friends,chill,party,outing,hangout", 150),
]
DEFAULT_DAILY = [("13:00", "13:45", "Lunch"), ("20:30", "21:15", "Dinner")]


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    replanned: bool = False


def safe(s: str) -> str:
    return re.sub(r"[<>&]", "", s).strip()


def fmt(t: datetime) -> str:
    return t.strftime("%I:%M%p").lstrip("0").lower()


def hm(mins: int) -> str:
    h, m = divmod(max(0, int(mins)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class App:
    def __init__(self, db: DB, tz: str = "Asia/Kolkata", clock=None):
        self.db = db
        self.tz = ZoneInfo(db.get("tz", tz))
        self.clock = clock or (lambda: datetime.now(self.tz))
        if not db.q("SELECT 1 FROM goals LIMIT 1"):
            self._seed()

    # ---------- basics ----------
    def now(self) -> datetime:
        return self.clock().astimezone(self.tz)

    def day_of(self, t: datetime) -> str:
        return (t.astimezone(self.tz) - timedelta(hours=DAY_CUTOFF_H)).date().isoformat()

    def day_start(self, day: str) -> datetime:
        d = date.fromisoformat(day)
        return datetime(d.year, d.month, d.day, tzinfo=self.tz)

    def local(self, s: str) -> datetime:
        return parse_utc(s).astimezone(self.tz)

    def _seed(self):
        today = self.day_of(self.now())
        for name, pr, mn, idl, win, kind, kw, mc in DEFAULT_GOALS:
            self.db.run("INSERT INTO goals(name,priority,min_min,ideal_min,window,kind,keywords,max_chunk,start_day)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", (name, pr, mn, idl, win, kind, kw, mc, today))
        for s, e, t in DEFAULT_DAILY:
            self.db.run("INSERT INTO timetable(weekday,start_hm,end_hm,title) VALUES(?,?,?,?)",
                        (EVERY_DAY, s, e, t))
        self.db.set("first_day", today)
        self.db.set("default_bed", "01:00")

    def goals(self, day: str):
        return self.db.q("SELECT * FROM goals WHERE active=1 AND start_day<=? AND (end_day IS NULL OR end_day>=?)"
                         " ORDER BY priority, id", (day, day))

    def all_goals_on(self, day: str):
        return self.db.q("SELECT * FROM goals WHERE start_day<=? AND (end_day IS NULL OR end_day>=?)", (day, day))

    def match_goal(self, text: str, day: str):
        text = text.lower()
        words = set(re.findall(r"[a-z]+", text))
        for g in self.goals(day):
            if g["name"].lower() in text:
                return g
        for g in self.goals(day):
            kws = {k.strip() for k in g["keywords"].split(",") if k.strip()}
            if kws & words:
                return g
        return None

    def day_row(self, day: str):
        r = self.db.one("SELECT * FROM days WHERE day=?", (day,))
        if not r:
            self.db.run("INSERT INTO days(day) VALUES(?)", (day,))
            r = self.db.one("SELECT * FROM days WHERE day=?", (day,))
        return r

    def done_on(self, day: str) -> dict[int, int]:
        rows = self.db.q("SELECT goal_id, SUM(minutes) m FROM logs WHERE day=? GROUP BY goal_id", (day,))
        return {r["goal_id"]: r["m"] for r in rows}

    def bedtime(self, day: str) -> datetime:
        r = self.day_row(day)
        if r["bed_utc"]:
            return self.local(r["bed_utc"])
        h, m = map(int, self.db.get("default_bed", "01:00").split(":"))
        ds = self.day_start(day)
        return ds + timedelta(hours=h + (24 if h < 12 else 0), minutes=m)

    # ---------- fixed blocks ----------
    def fixed_for(self, day: str) -> list[Fixed]:
        ds = self.day_start(day)
        wd = date.fromisoformat(day).weekday()
        events = self.db.q("SELECT * FROM events WHERE day=?", (day,))
        cancelled = {e["cancels_timetable"] for e in events if e["cancels_timetable"]}
        out = []
        for r in self.db.q("SELECT * FROM timetable WHERE weekday IN (?,?)", (wd, EVERY_DAY)):
            if r["id"] in cancelled:
                continue
            sh, sm = map(int, r["start_hm"].split(":"))
            eh, em = map(int, r["end_hm"].split(":"))
            out.append(Fixed(ds + timedelta(hours=sh, minutes=sm), ds + timedelta(hours=eh, minutes=em),
                             r["title"]))
        for e in events:
            s, en = self.local(e["start_utc"]), self.local(e["end_utc"])
            if en > s:
                out.append(Fixed(s, en, e["title"], e["counts_for"]))
        # an event overlapping a meal replaces that meal (dinner out with friends)
        evs = [f for f in out if f.counts_for is not None or f.title not in {d[2] for d in DEFAULT_DAILY}]
        meals = [f for f in out if f not in evs]
        meals = [m for m in meals if not any(e.start < m.end and m.start < e.end for e in evs)]
        return sorted(evs + meals, key=lambda f: f.start)

    # ---------- debt + weekly ----------
    def carry(self, g, day: str) -> int:
        if g["kind"] == "fun":
            return 0
        first = self.db.get("first_day", day)
        d0 = date.fromisoformat(day)
        total = 0.0
        for i in range(DEBT_WINDOW, 0, -1):
            d = (d0 - timedelta(days=i)).isoformat()
            if d < first or d < g["start_day"]:
                continue
            row = self.db.one("SELECT mode FROM days WHERE day=?", (d,))
            if row and row["mode"] != "normal":
                continue                         # sick / travel / off: excused
            done = self.done_on(d).get(g["id"], 0)
            total += DEBT_SHARE * max(0, g["min_min"] - done) - max(0, done - g["min_min"])
            total = max(0.0, total)              # banked time can't go below zero debt
        return int(min(DEBT_CAP, round(total / 5) * 5))

    def week(self, day: str) -> str:
        d0 = date.fromisoformat(day)
        monday = d0 - timedelta(days=d0.weekday())
        first = self.db.get("first_day", day)
        lines = [f"<b>This week</b> (Mon {monday:%d %b} – Sun {(monday + timedelta(days=6)):%d %b})"]
        for g in self.goals(day):
            done = sum(self.done_on((monday + timedelta(days=i)).isoformat()).get(g["id"], 0)
                       for i in range(7))
            if g["kind"] == "fun":
                lines.append(f"🎮 {g['name']}: {hm(done)}")
                continue
            elig_past, elig_all = 0, 0
            for i in range(7):
                d = (monday + timedelta(days=i)).isoformat()
                if d < first or d < g["start_day"] or (g["end_day"] and d > g["end_day"]):
                    continue
                row = self.db.one("SELECT mode FROM days WHERE day=?", (d,))
                if row and row["mode"] != "normal":
                    continue
                elig_all += 1
                if d <= day:
                    elig_past += 1
            target_now, target_week = g["min_min"] * elig_past, g["min_min"] * elig_all
            gap = target_now - done
            status = "✅ on track" if gap <= 0 else f"⚠️ {hm(gap)} behind"
            bar = self._bar(done, target_week)
            lines.append(f"{bar} {g['name']}: {hm(done)} / {hm(target_week)} · {status}")
        unk = self.db.one("SELECT COUNT(*) c FROM blocks WHERE status='unknown' AND day>=?",
                          (monday.isoformat(),))["c"]
        if unk:
            lines.append(f"❔ {unk} block(s) never logged this week (count as not done)")
        return "\n".join(lines)

    @staticmethod
    def _bar(done: int, target: int, n: int = 6) -> str:
        if target <= 0:
            return "▫️"
        k = min(n, int(n * done / target))
        return "▰" * k + "▱" * (n - k)

    # ---------- planning ----------
    def replan(self, reason: str = "") -> Reply:
        now = self.now()
        day = self.day_of(now)
        dr = self.day_row(day)
        # past blocks nobody answered become 'unknown'; future planned blocks are rebuilt
        self.db.run("UPDATE blocks SET status='unknown' WHERE day=? AND status IN ('planned','asked') AND end_utc<=?",
                    (day, utc(now)))
        self.db.run("DELETE FROM blocks WHERE day=? AND status='planned' AND end_utc>?", (day, utc(now)))
        bed = self.bedtime(day)
        fixed = self.fixed_for(day)
        mode = dr["mode"]
        plan_goals, skipped = [], set(self._skipped(day))
        if mode == "normal":
            for g in self.goals(day):
                if g["id"] in skipped:
                    continue
                plan_goals.append(Goal(g["id"], g["name"], g["priority"], g["min_min"], g["ideal_min"],
                                       g["window"], g["kind"], max_chunk=g["max_chunk"]))
        carry = {g.id: self.carry(self.db.one("SELECT * FROM goals WHERE id=?", (g.id,)), day)
                 for g in plan_goals}
        # time already sitting in an in-progress block counts as booked
        busy = [Fixed(self.local(b["start_utc"]), self.local(b["end_utc"]), b["name"])
                for b in self.db.q("SELECT * FROM blocks WHERE day=? AND status='asked' AND end_utc>?",
                                   (day, utc(now)))]
        plan = build_plan(now, bed, fixed + busy, plan_goals, self.done_on(day), carry, self.day_start(day))
        for it in plan.items:
            if it.goal_id is not None:
                self.db.run("INSERT INTO blocks(day,goal_id,name,start_utc,end_utc) VALUES(?,?,?,?,?)",
                            (day, it.goal_id, it.name, utc(it.start), utc(it.end)))
        text = self.render_plan(day, plan, carry, reason)
        return Reply(text, replanned=True)

    def _skipped(self, day: str) -> list[int]:
        v = self.db.get(f"skip:{day}", "")
        return [int(x) for x in v.split(",") if x]

    def render_plan(self, day: str, plan, carry: dict[int, int], reason: str = "") -> str:
        now = self.now()
        mode = self.day_row(day)["mode"]
        head = f"{reason}\n" if reason else ""
        if mode != "normal":
            label = {"sick": "🤒 Sick day", "travel": "🚆 Travel day", "off": "🛋 Off day"}[mode]
            lines = [f"{head}{label}: no targets today, and no debt. Rest up."]
            for it in plan.items:
                lines.append(f"• {fmt(it.start)}–{fmt(it.end)}  {it.name}")
            return "\n".join(lines)
        lines = [f"{head}📋 <b>Plan</b> until {fmt(self.bedtime(day))}"]
        icon = {"fixed": "📌", "fun": "🎮", "core": "🎯", "sprint": "🔥"}
        for it in plan.items:
            if it.end <= now:
                continue
            extra = ""
            if it.goal_id in carry and carry[it.goal_id]:
                extra = f"  <i>(+{carry[it.goal_id]}m catch-up)</i>"
                carry[it.goal_id] = 0
            lines.append(f"{icon.get(it.kind, '•')} {fmt(it.start)}–{fmt(it.end)}  {it.name}{extra}")
        done = self.done_on(day)
        if done:
            names = {g["id"]: g["name"] for g in self.all_goals_on(day)}
            lines.append("✔️ Done today: " + ", ".join(f"{names.get(k, '?')} {hm(v)}" for k, v in done.items()))
        if plan.pushed:
            lines.append("⏭ Didn't fit today: " + ", ".join(f"{k} ({hm(v)})" for k, v in plan.pushed.items())
                         + ". It'll show up as catch-up.")
        if plan.free_minutes >= 30:
            lines.append(f"🕊 Free time left: {hm(plan.free_minutes)}")
        return "\n".join(lines)

    def current_plan_text(self) -> str:
        now = self.now()
        day = self.day_of(now)
        rows = self.db.q("SELECT * FROM blocks WHERE day=? AND end_utc>? ORDER BY start_utc", (day, utc(now)))
        if not self.day_row(day)["wake_utc"]:
            return "No plan yet today. Send <b>up</b> when you wake up."
        fixed = [f for f in self.fixed_for(day) if f.end > now]
        items = [("📌", f.start, f.end, f.title) for f in fixed] + \
                [("🎯", self.local(b["start_utc"]), self.local(b["end_utc"]), b["name"]) for b in rows
                 if b["status"] in ("planned", "asked")]
        items.sort(key=lambda x: x[1])
        if not items:
            return "Nothing left on today's plan. 🌙"
        return "📋 <b>Rest of today</b>\n" + "\n".join(f"{i} {fmt(s)}–{fmt(e)}  {n}" for i, s, e, n in items)

    # ---------- message handling ----------
    def handle(self, text: str) -> Reply:
        now = self.now()
        day = self.day_of(now)
        it = P.parse(text, now, self.day_start(day))
        return self.apply(it, text)

    def apply(self, it: P.Intent, raw: str = "") -> Reply:
        now = self.now()
        day = self.day_of(now)
        if it.kind == "up":
            self.day_row(day)
            if not self.db.get("started"):          # catch-up only counts from your first real day
                self.db.set("started", "1")
                self.db.set("first_day", day)
            self.db.run("UPDATE days SET wake_utc=? WHERE day=?", (utc(it.start or now), day))
            r = self.replan("☀️ Morning! Here's your day.")
            r.text += "\n\nSomething changes? Just tell me, like <i>movie 8 to 11</i> or <i>sick today</i>."
            return r
        if it.kind == "bed":
            self.day_row(day)
            self.db.run("UPDATE days SET bed_utc=? WHERE day=?", (utc(it.start), day))
            return self._replan_if_awake(day, f"🌙 Bedtime set to {fmt(it.start)}.")
        if it.kind == "mode":
            old = self.day_row(day)["mode"]
            self.db.run("UPDATE days SET mode=? WHERE day=?", (it.mode, day))
            r = self.replan({"normal": "Back to normal, plan rebuilt.", "sick": "Get well soon.",
                             "travel": "Safe travels.", "off": "Enjoy the break."}[it.mode])
            if it.mode != "normal":
                r.buttons = [[("↩️ Undo", f"mode:{old}")]]
            return r
        if it.kind == "event":
            g = self.match_goal(it.title, day)
            cancels = self._timetable_match(it.title, day)
            if cancels:
                it.title, g = cancels["title"], None
            eid = self.db.run("INSERT INTO events(day,start_utc,end_utc,title,counts_for,cancels_timetable)"
                              " VALUES(?,?,?,?,?,?)",
                              (day, utc(it.start), utc(it.end), it.title, g["id"] if g else None,
                               cancels["id"] if cancels else None))
            msg = f"📌 Added <b>{it.title}</b> {fmt(it.start)}–{fmt(it.end)}"
            if cancels:
                msg += f" (replaces the usual {cancels['title']} slot)"
            if g:
                msg += f", counts as {g['name']}"
            r = self._replan_if_awake(day, msg + ".")
            r.buttons = [[("↩️ Undo", f"undo:{eid}")]]
            return r
        if it.kind == "cancel":
            t = self._timetable_match(it.title, day)
            if not t:
                return Reply(f"I couldn't find a class called “{safe(it.title)}” today. Send /timetable to see them.")
            eid = self.db.run("INSERT INTO events(day,start_utc,end_utc,title,cancels_timetable) VALUES(?,?,?,?,?)",
                              (day, utc(now), utc(now), f"Cancelled {t['title']}", t["id"]))
            r = self._replan_if_awake(day, f"❌ {t['title']} cancelled today. Time freed up.")
            r.buttons = [[("↩️ Undo", f"undo:{eid}")]]
            return r
        if it.kind == "log":
            g = self.match_goal(it.title, day)
            if not g:
                names = ", ".join(x["name"] for x in self.goals(day))
                return Reply(f"Which goal is “{safe(it.title)}”? I know: {names}.")
            self.db.run("INSERT INTO logs(day,goal_id,minutes,note,at_utc) VALUES(?,?,?,?,?)",
                        (day, g["id"], it.minutes, raw, utc(now)))
            return self._replan_if_awake(day, f"✔️ Logged {hm(it.minutes)} of {g['name']}.")
        if it.kind == "cmd":
            return self.command(it.cmd, it.arg)
        return Reply("I didn't get that. Try: <i>up</i>, <i>movie 8 to 11</i>, <i>sleep at 1</i>, "
                     "<i>did gym 40</i>, <i>sick today</i>, or /help.")

    def _replan_if_awake(self, day: str, msg: str) -> Reply:
        if self.day_row(day)["wake_utc"]:
            return self.replan(msg)
        return Reply(msg + " Send <b>up</b> when you start your day and I'll plan around it.")

    def _timetable_match(self, title: str, day: str):
        wd = date.fromisoformat(day).weekday()
        t = title.lower().replace("class", "").strip()
        if not t:
            return None
        for r in self.db.q("SELECT * FROM timetable WHERE weekday=?", (wd,)):
            name = r["title"].lower()
            if t in name or name in t or t == "".join(w[0] for w in name.split()):
                return r
        return None

    # ---------- buttons ----------
    def button(self, data: str) -> Reply:
        now = self.now()
        action, _, arg = data.partition(":")
        if action in ("done", "half", "skip", "later"):
            b = self.db.one("SELECT * FROM blocks WHERE id=?", (int(arg),))
            if not b:
                return Reply("That block is gone (the plan changed).")
            mins = int((parse_utc(b["end_utc"]) - parse_utc(b["start_utc"])).total_seconds() // 60)
            day = b["day"]
            if b["status"] in ("done", "half", "skipped"):
                return Reply("Already logged 👍")
            if action in ("done", "half"):
                got = mins if action == "done" else mins // 2
                self.db.run("INSERT INTO logs(day,goal_id,minutes,note,block_id,at_utc) VALUES(?,?,?,?,?,?)",
                            (day, b["goal_id"], got, action, b["id"], utc(now)))
                self.db.run("UPDATE blocks SET status=? WHERE id=?", (action, b["id"]))
                cheer = "Nice work 💪" if action == "done" else "Half counts. The rest is rescheduled."
                return self._replan_if_awake(day, f"✔️ {b['name']} +{hm(got)}. {cheer}")
            if action == "skip":
                self.db.run("UPDATE blocks SET status='skipped' WHERE id=?", (b["id"],))
                sk = set(self._skipped(day)) | {b["goal_id"]}
                self.db.set(f"skip:{day}", ",".join(map(str, sk)))
                return self._replan_if_awake(day, f"⏭ Skipped {b['name']} for today. "
                                                  "Half of its minimum becomes catch-up later this week.")
            self.db.run("UPDATE blocks SET status='dropped' WHERE id=?", (b["id"],))
            return self._replan_if_awake(day, f"⏰ Moving {b['name']} later.")
        if action == "undo":
            e = self.db.one("SELECT * FROM events WHERE id=?", (int(arg),))
            if e:
                self.db.run("DELETE FROM events WHERE id=?", (e["id"],))
                return self._replan_if_awake(e["day"], f"↩️ Removed {e['title']}.")
            return Reply("Nothing to undo.")
        if action == "mode":
            day = self.day_of(now)
            self.db.run("UPDATE days SET mode=? WHERE day=?", (arg, day))
            return self.replan("↩️ Undone.")
        if action == "tt_save":
            lines = (self.db.get("pending_timetable") or "").split("\n")
            n = self.save_timetable_lines(lines, replace=True)
            self.db.set("pending_timetable", "")
            return Reply(f"✅ Saved {n} classes as your weekly timetable.")
        if action == "tt_cancel":
            self.db.set("pending_timetable", "")
            return Reply("Okay, not saved.")
        return Reply("?")

    # ---------- scheduled messages ----------
    def due_jobs(self) -> list[tuple[datetime, str, int]]:
        """(when, kind, block_id) for today's remaining reminders and check-ins."""
        now = self.now()
        day = self.day_of(now)
        jobs = []
        for b in self.db.q("SELECT * FROM blocks WHERE day=? AND status IN ('planned','asked') AND end_utc>?",
                           (day, utc(now - timedelta(minutes=1)))):
            s, e = self.local(b["start_utc"]), self.local(b["end_utc"])
            if b["status"] == "planned" and s - timedelta(minutes=5) > now:
                jobs.append((s - timedelta(minutes=5), "remind", b["id"]))
            jobs.append((e, "checkin", b["id"]))
        return jobs

    def remind_text(self, block_id: int) -> str | None:
        b = self.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
        if not b or b["status"] != "planned":
            return None
        return f"⏳ In 5 min: <b>{b['name']}</b> {fmt(self.local(b['start_utc']))}–{fmt(self.local(b['end_utc']))}"

    def checkin(self, block_id: int) -> Reply | None:
        b = self.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
        if not b or b["status"] not in ("planned",):
            return None
        self.db.run("UPDATE blocks SET status='asked' WHERE id=?", (block_id,))
        return Reply(f"Did you do <b>{b['name']}</b> ({fmt(self.local(b['start_utc']))}–"
                     f"{fmt(self.local(b['end_utc']))})?",
                     [[("✅ Done", f"done:{block_id}"), ("🌓 Half", f"half:{block_id}")],
                      [("⏰ Later", f"later:{block_id}"), ("⏭ Skip today", f"skip:{block_id}")]])

    def wrapup(self) -> str:
        now = self.now()
        day = self.day_of(now)
        self.db.run("UPDATE blocks SET status='unknown' WHERE day=? AND status IN ('planned','asked') AND end_utc<=?",
                    (day, utc(now)))
        done = self.done_on(day)
        mode = self.day_row(day)["mode"]
        lines = ["🌙 <b>Day wrap-up</b>"]
        if mode != "normal":
            lines.append("Excused day, nothing owed. Sleep well.")
        for g in self.goals(day):
            if mode != "normal":
                break
            d = done.get(g["id"], 0)
            if g["kind"] == "fun":
                continue
            mark = "✅" if d >= g["min_min"] else ("🌓" if d else "❌")
            lines.append(f"{mark} {g['name']}: {hm(d)} (min {hm(g['min_min'])})")
        lines.append("")
        lines.append(self.week(day))
        return "\n".join(lines)

    # ---------- commands ----------
    def command(self, cmd: str, arg: str) -> Reply:
        now = self.now()
        day = self.day_of(now)
        if cmd in ("start", "help"):
            return Reply(HELP)
        if cmd in ("plan", "today"):
            return Reply(self.current_plan_text())
        if cmd == "replan":
            return self.replan("🔄 Rebuilt.")
        if cmd == "week":
            return Reply(self.week(day))
        if cmd == "goals":
            lines = ["<b>Goals</b> (priority 0 = sprint, 1 high, 2 medium, 3 low)"]
            for g in self.goals(day):
                end = f", until {g['end_day']}" if g["end_day"] else ""
                lines.append(f"• {g['name']}: min {g['min_min']}m, ideal {g['ideal_min']}m, "
                             f"p{g['priority']}, {g['window']}{end}")
            lines.append("\nChange: <code>/goal Exercise min 40 ideal 60 prio 1 window morning</code>\n"
                         "Sprint: <code>/sprint Test prep 180 until thu</code>\n"
                         "Pause/remove: <code>/retire Fun</code>")
            return Reply("\n".join(lines))
        if cmd == "goal":
            return self._goal_cmd(arg, day)
        if cmd == "sprint":
            m = re.match(r"(.+?)\s+(\d+)\s*(?:m|min|mins)?(?:/day)?\s+until\s+(\S+)$", arg, re.I)
            if not m:
                return Reply("Format: <code>/sprint Test prep 180 until thu</code> (or a date like 2026-12-02)")
            end = self._parse_day(m.group(3), day)
            if not end:
                return Reply("Couldn't read that end day.")
            mins = int(m.group(2))
            self.db.run("INSERT INTO goals(name,priority,min_min,ideal_min,window,kind,keywords,max_chunk,start_day,end_day)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (safe(m.group(1)), 0, mins, mins, "any", "sprint",
                         ",".join(re.findall(r"[a-z]+", m.group(1).lower())), 90, day, end))
            return self._replan_if_awake(day, f"🔥 Sprint <b>{m.group(1).strip()}</b>: {hm(mins)}/day until {end}. "
                                              "Other goals get squeezed first when time is short.")
        if cmd == "retire":
            g = self.match_goal(arg, day)
            if not g:
                return Reply("Which goal? See /goals.")
            yday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            self.db.run("UPDATE goals SET end_day=? WHERE id=?", (yday, g["id"]))
            return self._replan_if_awake(day, f"🗃 {g['name']} retired (history kept).")
        if cmd == "bed":
            if not re.match(r"^\d{1,2}:\d{2}$", arg):
                return Reply("Format: <code>/bed 01:00</code> (your usual bedtime, 24h)")
            self.db.set("default_bed", arg)
            return Reply(f"Usual bedtime set to {arg}. For one night, just say <i>sleep at 12</i>.")
        if cmd == "timetable":
            rows = self.db.q("SELECT * FROM timetable ORDER BY weekday, start_hm")
            if not rows:
                return Reply("No classes saved. Add one: <code>/addclass mon 10:00 11:30 Operations</code>, "
                             "or send a photo of your timetable.")
            names = WEEKDAYS + ["daily"]
            return Reply("<b>Weekly timetable</b>\n" + "\n".join(
                f"#{r['id']} {names[r['weekday']].title()} {r['start_hm']}–{r['end_hm']} {r['title']}" for r in rows)
                + "\n\nRemove: <code>/delclass 3</code>")
        if cmd == "addclass":
            n = self.save_timetable_lines([arg])
            return Reply("Added." if n else "Format: <code>/addclass mon 10:00 11:30 Operations</code>")
        if cmd == "delclass":
            if not arg.isdigit():
                return Reply("Format: <code>/delclass 3</code> (number from /timetable)")
            self.db.run("DELETE FROM timetable WHERE id=?", (int(arg),))
            return self._replan_if_awake(day, "Removed.")
        if cmd == "events":
            rows = self.db.q("SELECT * FROM events WHERE day=? ORDER BY start_utc", (day,))
            if not rows:
                return Reply("No one-off events today.")
            return Reply("\n".join(f"#{e['id']} {fmt(self.local(e['start_utc']))}–{fmt(self.local(e['end_utc']))} "
                                   f"{e['title']}" for e in rows) + "\n\nRemove: <code>/del 4</code>")
        if cmd == "del":
            if arg.isdigit():
                return self.button(f"undo:{arg}")
            return Reply("Format: <code>/del 4</code>")
        return Reply("Unknown command. /help")

    def _goal_cmd(self, arg: str, day: str) -> Reply:
        m = re.match(r"(.+?)\s+((?:(?:min|ideal|prio|priority|window)\s+\S+\s*)+)$", arg, re.I)
        if not m:
            return Reply("Format: <code>/goal Exercise min 40 ideal 60 prio 1 window evening</code>\n"
                         "Windows: any, morning, afternoon, evening, night, day")
        name = safe(m.group(1))
        opts = dict(re.findall(r"(min|ideal|prio|priority|window)\s+(\S+)", m.group(2), re.I))
        opts = {k.lower().replace("priority", "prio"): v for k, v in opts.items()}
        g = self.db.one("SELECT * FROM goals WHERE lower(name)=lower(?) AND end_day IS NULL", (name,))
        if g:
            sets, vals = [], []
            for k, col in (("min", "min_min"), ("ideal", "ideal_min"), ("prio", "priority"), ("window", "window")):
                if k in opts:
                    sets.append(f"{col}=?")
                    vals.append(opts[k] if k == "window" else int(opts[k]))
            self.db.run(f"UPDATE goals SET {','.join(sets)} WHERE id=?", (*vals, g["id"]))
            msg = f"Updated {g['name']}."
        else:
            mn = int(opts.get("min", 30))
            self.db.run("INSERT INTO goals(name,priority,min_min,ideal_min,window,kind,keywords,start_day)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (name, int(opts.get("prio", 2)), mn, int(opts.get("ideal", mn)),
                         opts.get("window", "any"), "core", ",".join(re.findall(r"[a-z]+", name.lower())), day))
            msg = f"Added goal {name}."
        return self._replan_if_awake(day, msg)

    def _parse_day(self, s: str, today: str) -> str | None:
        s = s.lower()
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            pass
        d0 = date.fromisoformat(today)
        if s == "tomorrow":
            return (d0 + timedelta(days=1)).isoformat()
        for i, w in enumerate(WEEKDAYS):
            if s.startswith(w):
                ahead = (i - d0.weekday()) % 7 or 7
                return (d0 + timedelta(days=ahead)).isoformat()
        return None

    def save_timetable_lines(self, lines: list[str], replace: bool = False) -> int:
        parsed = []
        for line in lines:
            m = re.match(r"^\s*(mon|tue|wed|thu|fri|sat|sun)\w*\s+(\d{1,2}[:.]\d{2})\s*(?:-|–|to|\s)\s*"
                         r"(\d{1,2}[:.]\d{2})\s+(.+)$", line.strip(), re.I)
            if m:
                parsed.append((WEEKDAYS.index(m.group(1).lower()[:3]),
                               m.group(2).replace(".", ":").zfill(5), m.group(3).replace(".", ":").zfill(5),
                               safe(m.group(4))))
        if replace and parsed:
            self.db.run("DELETE FROM timetable WHERE weekday<?", (EVERY_DAY,))
        for p in parsed:
            self.db.run("INSERT INTO timetable(weekday,start_hm,end_hm,title) VALUES(?,?,?,?)", p)
        return len(parsed)


HELP = """<b>Rozana</b> plans your day around when you actually wake up.

<b>Daily</b>
• <i>up</i> → builds today's plan from now
• <i>movie 8 to 11</i>, <i>football 6-7pm</i>, <i>dinner at 9 for 2h</i> → adds it, re-plans the rest
• <i>OB moved to 3 to 4</i>, <i>cancel OB</i> → fixes today's classes
• <i>sleep at 12:30</i> → tonight's bedtime
• <i>did gym 40</i> → log something you did off-plan
• <i>sick today</i> / <i>travelling</i> / <i>off day</i> → excused, no debt
• Tap the buttons I send after each block

<b>Views</b>: /plan  /week  /goals  /timetable  /events
<b>Setup</b>: send a timetable photo, or /addclass mon 10:00 11:30 Operations
/goal …  /sprint …  /retire …  /bed 01:00

<b>Catch-up rule</b>: slack on a minimum and half of it comes back as catch-up (max +30 min/day, forgotten after 7 days). Sick/travel days cost nothing."""

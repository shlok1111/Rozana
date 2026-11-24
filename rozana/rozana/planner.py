"""The planning brain. Pure logic: no AI, no database, no Telegram.

Give it: the current time, bedtime, fixed blocks (classes, meals, a movie),
your goals with priority / minimum / ideal minutes, and what you've already
done today. It returns a plan for the rest of the day.

Rules, in order:
  1. Free time = now -> bedtime, minus fixed blocks (plus a short gap after each).
  2. Every goal gets its MINIMUM first, highest priority first.
  3. Leftover time grows goals toward their IDEAL, again by priority.
     (Amounts are decided first, then placed, so sessions stay in one piece.)
  4. Fun has the lowest priority, so it is the first thing squeezed.
  5. Whatever minimum still doesn't fit is reported as "pushed", not failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Preferred windows in local hours, measured from midnight of the logical day.
# Hours above 24 mean "after midnight" (you sleep late).
WINDOWS: dict[str, tuple[int, int] | None] = {
    "any": None,
    "morning": (5, 12),
    "afternoon": (12, 17),
    "evening": (17, 23),
    "night": (21, 27),
    "day": (8, 20),
}

GAP_AFTER_FIXED = timedelta(minutes=10)   # walk back from class, settle in
BREAK_AFTER_ITEM = timedelta(minutes=5)   # short breather between blocks


@dataclass
class Goal:
    id: int
    name: str
    priority: int            # 0 = sprint (top), 1 = high, 2 = medium, 3 = low
    min_min: int             # minutes that must happen
    ideal_min: int           # minutes you'd like
    window: str = "any"
    kind: str = "core"       # core | fun | sprint
    min_chunk: int = 20      # don't schedule slivers shorter than this
    max_chunk: int = 90      # split longer work into sessions


@dataclass
class Fixed:
    start: datetime
    end: datetime
    title: str
    counts_for: int | None = None   # e.g. a movie counts toward the Fun goal


@dataclass
class Item:
    goal_id: int | None
    name: str
    start: datetime
    end: datetime
    kind: str                # goal kind, or "fixed"

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)      # goal blocks + fixed, sorted
    pushed: dict[str, int] = field(default_factory=dict)  # goal name -> minutes that didn't fit
    free_minutes: int = 0


def _round_up(t: datetime, step: int = 5) -> datetime:
    t = t.replace(second=0, microsecond=0)
    extra = (-t.minute) % step
    return t + timedelta(minutes=extra)


def _mins(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _subtract(free: list[list[datetime]], s: datetime, e: datetime) -> list[list[datetime]]:
    out = []
    for fs, fe in free:
        if e <= fs or s >= fe:
            out.append([fs, fe])
            continue
        if s > fs:
            out.append([fs, s])
        if e < fe:
            out.append([e, fe])
    return out


def _window_bounds(goal: Goal, day_start: datetime) -> tuple[datetime, datetime] | None:
    w = WINDOWS.get(goal.window)
    if not w:
        return None
    return day_start + timedelta(hours=w[0]), day_start + timedelta(hours=w[1])


def _place(goal: Goal, minutes: int, free, items: list[Item], day_start: datetime,
           min_chunk: int) -> int:
    """Place up to `minutes` of a goal into free time. Returns minutes placed."""
    placed = 0
    win = _window_bounds(goal, day_start)
    while minutes - placed > 0:
        want = min(minutes - placed, goal.max_chunk)
        need = min(want, min_chunk)       # smallest chunk worth placing
        best = None                       # (score, start, available)
        for fs, fe in free:
            starts = [fs]
            if win and win[0] > fs and win[0] < fe:
                starts.append(win[0])     # jump to the start of the preferred window
            for st in starts:
                avail = _mins(st, fe)
                if avail < need:
                    continue
                chunk_end = st + timedelta(minutes=min(want, avail))
                overlap = 0
                if win:
                    overlap = max(0, _mins(max(st, win[0]), min(chunk_end, win[1])))
                # prefer: inside window, then fits whole chunk, then earliest
                score = (-overlap, -min(want, avail), st)
                if best is None or score < best[0]:
                    best = (score, st, avail)
        if best is None:
            break
        _, st, avail = best
        chunk = min(want, avail)
        en = st + timedelta(minutes=chunk)
        items.append(Item(goal.id, goal.name, st, en, goal.kind))
        free[:] = _subtract(free, st, en + BREAK_AFTER_ITEM)
        placed += chunk
    return placed


def build_plan(now: datetime, bedtime: datetime, fixed: list[Fixed], goals: list[Goal],
               done_today: dict[int, int] | None = None,
               carry: dict[int, int] | None = None,
               day_start: datetime | None = None) -> Plan:
    done_today = done_today or {}
    carry = carry or {}
    day_start = day_start or now.replace(hour=0, minute=0, second=0, microsecond=0)

    start = _round_up(now)
    plan = Plan()
    fixed_items = [Item(None, f.title, f.start, f.end, "fixed")
                   for f in fixed if f.end > start and f.start < bedtime]
    if start >= bedtime:
        plan.items = sorted(fixed_items, key=lambda i: i.start)
        return plan

    free = [[start, bedtime]]
    for f in fixed:
        free = _subtract(free, f.start, f.end + GAP_AFTER_FIXED)

    # Fixed blocks that count toward a goal (movie -> Fun) reduce what's needed.
    credit = dict(done_today)
    for f in fixed:
        if f.counts_for is not None:
            credit[f.counts_for] = credit.get(f.counts_for, 0) + _mins(f.start, f.end)

    order = sorted(goals, key=lambda g: (g.priority, g.kind == "fun", g.id))
    need_min, need_extra = {}, {}
    for g in order:
        done = credit.get(g.id, 0)
        c = carry.get(g.id, 0)
        need_min[g.id] = max(0, g.min_min + c - done)
        need_extra[g.id] = max(0, max(g.ideal_min, g.min_min) + c - done) - need_min[g.id]

    # Step A: decide HOW MUCH each goal gets (budgeting), before deciding WHEN.
    available = sum(_mins(s, e) for s, e in free)
    def breaks(mins: int, g: Goal) -> int:
        return 0 if mins <= 0 else (-(-mins // g.max_chunk)) * BREAK_AFTER_ITEM.seconds // 60
    budget = available - sum(need_min[g.id] + breaks(need_min[g.id], g) for g in order)
    target = {g.id: need_min[g.id] for g in order}
    for g in order:                       # extras go by priority, fun is last in line
        if budget <= 0:
            break
        add = min(need_extra[g.id], budget)
        if add < 15:
            continue                      # not worth a 10-minute sliver
        target[g.id] += add
        budget -= add + (breaks(target[g.id], g) - breaks(target[g.id] - add, g))

    # Step B: place each goal's total, highest priority first.
    items: list[Item] = []
    for g in order:
        n = target[g.id]
        if n <= 0:
            continue
        got = _place(g, n, free, items, day_start, min(g.min_chunk, n))
        if got < need_min[g.id]:
            plan.pushed[g.name] = need_min[g.id] - got

    plan.items = sorted(items + fixed_items, key=lambda i: i.start)
    plan.free_minutes = sum(_mins(s, e) for s, e in free)
    return plan

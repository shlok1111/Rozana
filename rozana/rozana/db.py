"""SQLite storage. One file, no server, nothing to disconnect.

Every moment in time is saved in UTC (ISO text). The app converts to your
local timezone only when showing it, so moving countries later is one setting.
Weekly class times are wall-clock times ("10:00 on Monday"), stored as HH:MM.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  priority INTEGER NOT NULL,          -- 0 sprint, 1 high, 2 medium, 3 low
  min_min INTEGER NOT NULL,
  ideal_min INTEGER NOT NULL,
  window TEXT NOT NULL DEFAULT 'any',
  kind TEXT NOT NULL DEFAULT 'core',  -- core | fun | sprint
  keywords TEXT NOT NULL DEFAULT '',  -- words that map messages to this goal
  max_chunk INTEGER NOT NULL DEFAULT 90,
  start_day TEXT NOT NULL,            -- logical day it became active
  end_day TEXT,                       -- sprints end; retired goals get one too
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS timetable (   -- recurring weekly fixed blocks
  id INTEGER PRIMARY KEY,
  weekday INTEGER NOT NULL,           -- 0 = Monday
  start_hm TEXT NOT NULL,
  end_hm TEXT NOT NULL,
  title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (      -- one-off fixed blocks (movie, extra class)
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  title TEXT NOT NULL,
  counts_for INTEGER,                 -- goal id it counts toward (movie -> Fun)
  cancels_timetable INTEGER           -- timetable id this replaces for the day
);

CREATE TABLE IF NOT EXISTS days (
  day TEXT PRIMARY KEY,
  wake_utc TEXT,
  bed_utc TEXT,
  mode TEXT NOT NULL DEFAULT 'normal' -- normal | sick | travel | off
);

CREATE TABLE IF NOT EXISTS blocks (      -- the current plan
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  goal_id INTEGER,
  name TEXT NOT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned' -- planned | done | half | skipped | unknown | dropped
);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  goal_id INTEGER NOT NULL,
  minutes INTEGER NOT NULL,
  note TEXT,
  block_id INTEGER,
  at_utc TEXT NOT NULL
);
"""


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


class DB:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        r = self.con.execute(sql, args).fetchone()
        return r

    def run(self, sql: str, args=()) -> int:
        cur = self.con.execute(sql, args)
        self.con.commit()
        return cur.lastrowid

    # settings
    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value) -> None:
        self.run("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

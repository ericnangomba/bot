import contextlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, goal TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, plan_attempts INTEGER NOT NULL DEFAULT 0,
    ready REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0, summary TEXT, paused INTEGER NOT NULL DEFAULT 0,
    planning INTEGER NOT NULL DEFAULT 0, finishing INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS steps (
    task TEXT NOT NULL REFERENCES tasks(id), id TEXT NOT NULL, capability TEXT NOT NULL,
    instruction TEXT NOT NULL, deps TEXT NOT NULL, review_of TEXT,
    state TEXT NOT NULL DEFAULT 'queued', bot TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    repairs INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
    ready REAL NOT NULL DEFAULT 0, action TEXT, effect INTEGER NOT NULL DEFAULT 0,
    observations TEXT NOT NULL DEFAULT '[]', result TEXT, feedback TEXT,
    PRIMARY KEY (task,id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, task TEXT NOT NULL, step TEXT, at REAL NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task,seq);
CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind,at);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, task TEXT NOT NULL, step TEXT NOT NULL, digest TEXT NOT NULL,
    action TEXT NOT NULL, state TEXT NOT NULL, response TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    seq INTEGER PRIMARY KEY, task TEXT NOT NULL, step TEXT NOT NULL, bot TEXT NOT NULL,
    at REAL NOT NULL, state TEXT NOT NULL, result TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls(at);
CREATE INDEX IF NOT EXISTS model_calls_provider_at ON model_calls(provider,at);
CREATE TABLE IF NOT EXISTS telegram_origins (
    task TEXT PRIMARY KEY REFERENCES tasks(id), update_id INTEGER NOT NULL UNIQUE,
    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, topic INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS telegram_origin_scope ON telegram_origins(user_id,chat_id,topic);
CREATE TABLE IF NOT EXISTS telegram_drafts (
    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, topic INTEGER NOT NULL,
    name TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY (user_id,chat_id,topic,name)
);
CREATE TABLE IF NOT EXISTS telegram_replies (
    user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, topic INTEGER NOT NULL,
    message_id INTEGER NOT NULL, approval TEXT NOT NULL,
    PRIMARY KEY (user_id,chat_id,topic,message_id)
);
CREATE TABLE IF NOT EXISTS telegram_voice (
    update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, at REAL NOT NULL, transcript TEXT
);
CREATE INDEX IF NOT EXISTS telegram_voice_user_at ON telegram_voice(user_id,at);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_submissions (
    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task TEXT NOT NULL REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, goal TEXT NOT NULL, mode TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL, next_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def event(db, task, kind, data=None, step=None):
    db.execute("INSERT INTO events(task,step,at,kind,data) VALUES(?,?,?,?,?)",
               (task, step, time.time(), kind, encode(data or {})))


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()

    @contextlib.contextmanager
    def tx(self, write=True):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

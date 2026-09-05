from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List, Optional

from .settings import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    skill_kind TEXT NOT NULL,
    repo_id INTEGER,
    working_directory TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pid INTEGER,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    stream TEXT NOT NULL,
    line TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_logs_run_id ON run_logs(run_id);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_kind TEXT NOT NULL,
    repo_id INTEGER,
    working_directory TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_fired_at TEXT,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    skill_kind TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    continue_on_error INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_steps_schedule ON agent_steps(schedule_id, position);
"""


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
    _migrate()


def _migrate() -> None:
    """Additive-only migrations. Safe to call every startup."""
    with conn() as c:
        def add_col(table: str, name: str, decl: str) -> None:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if name not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

        # Runs
        add_col("runs", "schedule_id", "INTEGER")
        add_col("runs", "total_cost_usd", "REAL")
        add_col("runs", "allowed_mcps", "TEXT")
        add_col("runs", "model", "TEXT")
        add_col("runs", "attempt_number", "INTEGER DEFAULT 1")
        add_col("runs", "timed_out", "INTEGER DEFAULT 0")
        add_col("runs", "parent_run_id", "INTEGER")
        add_col("runs", "step_position", "INTEGER DEFAULT 0")
        add_col("runs", "allowed_tools", "TEXT")

        # Schedules (aka agents)
        add_col("schedules", "allowed_mcps", "TEXT")
        add_col("schedules", "model", "TEXT")
        add_col("schedules", "max_cost_usd", "REAL")
        add_col("schedules", "max_turns", "INTEGER")
        add_col("schedules", "autopilot", "INTEGER DEFAULT 0")
        add_col("schedules", "notify_on_failure", "INTEGER DEFAULT 0")
        add_col("schedules", "notify_target", "TEXT")
        add_col("schedules", "timeout_seconds", "INTEGER")
        add_col("schedules", "retry_count", "INTEGER DEFAULT 0")
        add_col("schedules", "retry_delay_seconds", "INTEGER DEFAULT 60")
        add_col("schedules", "env_vars", "TEXT")
        add_col("schedules", "notes", "TEXT")
        add_col("schedules", "permission_mode", "TEXT")
        add_col("schedules", "extra_allowed_tools", "TEXT")


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with conn() as c:
        cur = c.execute(sql, args)
        return list(cur.fetchall())


def q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.fetchone()


def exec_(sql: str, args: tuple = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid or 0

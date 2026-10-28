"""SQLite storage: connections, schema and migrations.

SQLite is a deliberate choice: one file in the data volume, trivially backed
up, no separate database container for tired parents to keep alive.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .timeutil import now_db

MIGRATIONS: list[str] = [
    # --- v1: initial schema -------------------------------------------------
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE feedings (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at     TEXT NOT NULL,
        ended_at       TEXT,
        method         TEXT NOT NULL,
        side           TEXT,
        bottle_content TEXT,
        amount_ml      REAL,
        quality        INTEGER,
        issues         TEXT NOT NULL DEFAULT '',
        notes          TEXT NOT NULL DEFAULT '',
        logged_by      TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        deleted_at     TEXT
    );
    CREATE INDEX idx_feedings_started ON feedings(started_at);

    CREATE TABLE diapers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        kind        TEXT NOT NULL,
        color       TEXT,
        consistency TEXT,
        amount      TEXT,
        notes       TEXT NOT NULL DEFAULT '',
        logged_by   TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        deleted_at  TEXT
    );
    CREATE INDEX idx_diapers_occurred ON diapers(occurred_at);

    CREATE TABLE notes (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at      TEXT NOT NULL,
        body             TEXT NOT NULL,
        tags             TEXT NOT NULL DEFAULT '',
        for_pediatrician INTEGER NOT NULL DEFAULT 0,
        discussed        INTEGER NOT NULL DEFAULT 0,
        logged_by        TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        deleted_at       TEXT
    );
    CREATE INDEX idx_notes_occurred ON notes(occurred_at);

    CREATE TABLE vital_params (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        unit            TEXT NOT NULL DEFAULT '',
        decimals        INTEGER NOT NULL DEFAULT 1,
        expected_min    REAL,
        expected_max    REAL,
        alert_low       REAL,
        alert_high      REAL,
        notify_on_alert INTEGER NOT NULL DEFAULT 1,
        help_text       TEXT NOT NULL DEFAULT '',
        active          INTEGER NOT NULL DEFAULT 1,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE vital_checks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        measured_at TEXT NOT NULL,
        notes       TEXT NOT NULL DEFAULT '',
        logged_by   TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        deleted_at  TEXT
    );
    CREATE INDEX idx_vital_checks_measured ON vital_checks(measured_at);

    -- Limits are copied onto each reading so it is always judged against the
    -- limits that were in effect when it was taken.
    CREATE TABLE vital_readings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id     INTEGER NOT NULL REFERENCES vital_checks(id) ON DELETE CASCADE,
        param_id     INTEGER NOT NULL REFERENCES vital_params(id) ON DELETE RESTRICT,
        value        REAL NOT NULL,
        expected_min REAL,
        expected_max REAL,
        alert_low    REAL,
        alert_high   REAL,
        UNIQUE (check_id, param_id)
    );
    CREATE INDEX idx_vital_readings_param ON vital_readings(param_id);

    CREATE TABLE reminders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        kind            TEXT NOT NULL,
        vital_param_id  INTEGER REFERENCES vital_params(id) ON DELETE SET NULL,
        interval_minutes INTEGER NOT NULL,
        window_minutes  INTEGER NOT NULL DEFAULT 0,
        nag_minutes     INTEGER NOT NULL DEFAULT 0,
        max_nags        INTEGER NOT NULL DEFAULT 3,
        anchor          TEXT NOT NULL DEFAULT 'start',
        active_start    TEXT,
        active_end      TEXT,
        priority        INTEGER NOT NULL DEFAULT 3,
        notify_ntfy     INTEGER NOT NULL DEFAULT 1,
        notify_dash     INTEGER NOT NULL DEFAULT 1,
        enabled         INTEGER NOT NULL DEFAULT 1,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE reminder_state (
        reminder_id      INTEGER PRIMARY KEY REFERENCES reminders(id) ON DELETE CASCADE,
        anchor           TEXT,
        stage            TEXT NOT NULL DEFAULT 'ok',
        nag_count        INTEGER NOT NULL DEFAULT 0,
        last_notified_at TEXT,
        snoozed_until    TEXT
    );

    -- Completions of "custom routine" reminders (vitamin D, tummy time...).
    CREATE TABLE task_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER REFERENCES reminders(id) ON DELETE SET NULL,
        task_name   TEXT NOT NULL,
        done_at     TEXT NOT NULL,
        notes       TEXT NOT NULL DEFAULT '',
        logged_by   TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        deleted_at  TEXT
    );
    CREATE INDEX idx_task_logs_done ON task_logs(done_at);

    -- Every notification produced, whichever channels it went to.
    CREATE TABLE alerts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT NOT NULL,
        source          TEXT NOT NULL,
        reminder_id     INTEGER,
        stage           TEXT,
        title           TEXT NOT NULL,
        message         TEXT NOT NULL,
        priority        INTEGER NOT NULL DEFAULT 3,
        acknowledged_at TEXT,
        delivery        TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_alerts_created ON alerts(created_at);
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
        # executescript() commits implicitly, so wrap the script itself.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index};\nCOMMIT;")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
        _seed(conn)
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    """First-run examples. Only inserted into empty tables."""
    now = now_db()
    if conn.execute("SELECT COUNT(*) FROM vital_params").fetchone()[0] == 0:
        conn.execute(
            """INSERT INTO vital_params
               (name, unit, decimals, expected_min, expected_max, alert_low, alert_high,
                notify_on_alert, help_text, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Temperature", "°F", 1, 97.5, 100.3, 97.0, 100.4, 1,
                "Example limits only. Confirm the method (rectal, axillary...) "
                "and thresholds with your pediatrician.",
                1, now, now,
            ),
        )
        conn.execute(
            """INSERT INTO vital_params
               (name, unit, decimals, notify_on_alert, help_text, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("Weight", "lb", 2, 0, "", 2, now, now),
        )
    if conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0:
        rows = [
            # name, kind, interval, window, nag, max_nags, enabled, sort
            ("Feeding", "feeding", 180, 30, 15, 4, 1, 1),
            ("Diaper check", "diaper", 180, 0, 30, 2, 0, 2),
            ("Vitamin D drops", "custom", 1440, 60, 0, 0, 0, 3),
        ]
        for name, kind, interval, window, nag, max_nags, enabled, sort in rows:
            conn.execute(
                """INSERT INTO reminders
                   (name, kind, interval_minutes, window_minutes, nag_minutes, max_nags,
                    enabled, sort_order, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (name, kind, interval, window, nag, max_nags, enabled, sort, now, now),
            )

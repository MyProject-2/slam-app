"""
db.py — SQLite data layer for the SLAM (Starters, Leavers, And Movers) demo.

Uses Python's built-in sqlite3 module — a real relational database file
(slam.db) on disk, no external DB server or driver installation required.
"""

import sqlite3
import threading
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "slam.db")

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def init_db():
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                type TEXT NOT NULL,               -- starter | mover | leaver
                employee_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',  -- processing | complete
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE TABLE IF NOT EXISTS event_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                step_index INTEGER NOT NULL,
                label TEXT NOT NULL,
                system_key TEXT,                  -- which system this step touches, if any
                is_gdpr_flag INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                done_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_steps_event ON event_steps(event_id);
            """
        )
        _conn.commit()


def get_conn():
    return _conn


def get_lock():
    return _lock

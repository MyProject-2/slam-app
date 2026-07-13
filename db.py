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
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hris_id TEXT UNIQUE,              -- system-of-record ID; populated once the HRIS webhook lands
                full_name TEXT NOT NULL,
                country TEXT,                     -- ISO 3166-1 alpha-2, e.g. 'DK', 'GB' — drives country-specific GDPR/payroll rules
                personal_email TEXT,
                personal_phone TEXT,
                company_email TEXT,
                company_phone TEXT,
                bamboohr_id TEXT,                 -- BambooHR's own employee ID, set after the first successful Core HR sync
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                type TEXT NOT NULL,               -- starter | mover | leaver
                employee_name TEXT NOT NULL,
                employee_id INTEGER REFERENCES employees(id),
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
                is_welcome_message INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                done_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_steps_event ON event_steps(event_id);
            """
        )
        # Existing databases predate the employees table/employee_id column;
        # CREATE TABLE IF NOT EXISTS won't retrofit a column onto an events
        # table that already exists, so add it by hand when missing. This
        # must run before the employee_id index below, since that index
        # creation would otherwise fail against a pre-existing table.
        existing_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(events)")}
        if "employee_id" not in existing_columns:
            _conn.execute("ALTER TABLE events ADD COLUMN employee_id INTEGER REFERENCES employees(id)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_employee ON events(employee_id)")

        # Same story for employees.country, added after the table first shipped.
        existing_employee_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(employees)")}
        if "country" not in existing_employee_columns:
            _conn.execute("ALTER TABLE employees ADD COLUMN country TEXT")

        # ...and for event_steps.is_welcome_message.
        existing_step_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(event_steps)")}
        if "is_welcome_message" not in existing_step_columns:
            _conn.execute("ALTER TABLE event_steps ADD COLUMN is_welcome_message INTEGER NOT NULL DEFAULT 0")

        # ...and for employees.bamboohr_id.
        existing_employee_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(employees)")}
        if "bamboohr_id" not in existing_employee_columns:
            _conn.execute("ALTER TABLE employees ADD COLUMN bamboohr_id TEXT")
        _conn.commit()


def get_conn():
    return _conn


def get_lock():
    return _lock

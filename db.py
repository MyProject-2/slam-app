"""
db.py — Azure SQL Database data layer for the SLAM (Starters, Leavers, And
Movers) demo, via Microsoft's mssql-python driver (talks the TDS protocol
directly — no separate ODBC driver manager to install).

Moved off SQLite because Azure App Service Linux's persistent storage
(/home) is a network-mounted share, and SQLite's file-locking model isn't
reliable over network filesystems — Microsoft's own guidance for that
situation is to use a managed database service instead. Unlike the
Twilio/BambooHR/Entra/etc. integrations elsewhere in this app, there's no
env-var-gated simulated fallback here: the database isn't an optional
side effect, nothing works without one, so AZURE_SQL_SERVER/DATABASE/
USER/PASSWORD are required, not optional.
"""

import os
import threading
import time

from mssql_python import connect, IntegrityError, InterfaceError, OperationalError  # noqa: F401 (IntegrityError re-exported for api.py)

AZURE_SQL_SERVER = os.environ.get("AZURE_SQL_SERVER")
AZURE_SQL_DATABASE = os.environ.get("AZURE_SQL_DATABASE")
AZURE_SQL_USER = os.environ.get("AZURE_SQL_USER")
AZURE_SQL_PASSWORD = os.environ.get("AZURE_SQL_PASSWORD")

_lock = threading.Lock()

# Every timestamp column is a plain ISO-8601 string (not a native DATETIME2),
# matching api.py's now_iso() helper and its strptime() parsing elsewhere —
# this avoids a datetime<->string conversion layer throughout the app.
_ISO_DEFAULT = "DEFAULT (FORMAT(SYSUTCDATETIME(), 'yyyy-MM-ddTHH:mm:ss.fff') + 'Z')"


class Row:
    """Wraps an mssql-python result row together with its column names so
    it behaves like sqlite3.Row: dict(row) and row["column"] both work,
    matching every call site in api.py written against the old SQLite
    layer. Built locally rather than relying on mssql-python's own row
    type, whose dict/mapping support isn't documented."""
    __slots__ = ("_data",)

    def __init__(self, columns, values):
        self._data = dict(zip(columns, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def keys(self):
        return self._data.keys()


class _Cursor:
    """Wraps mssql-python's raw cursor so fetchone()/fetchall() return Row
    objects instead of its native row type."""
    def __init__(self, raw_cursor):
        self._raw = raw_cursor

    def _columns(self):
        return [col[0] for col in self._raw.description] if self._raw.description else []

    def fetchone(self):
        row = self._raw.fetchone()
        return Row(self._columns(), row) if row is not None else None

    def fetchall(self):
        columns = self._columns()
        return [Row(columns, row) for row in self._raw.fetchall()]


class _Conn:
    """Thin sqlite3-style convenience layer over mssql-python's Connection
    — exposes conn.execute(sql, params) directly (mssql-python only offers
    conn.cursor().execute(...)), so every call site in api.py written
    against sqlite3.Connection.execute() keeps working unchanged.

    Also reconnects transparently on a dropped connection: Azure SQL's
    serverless tier auto-pauses after an hour idle (see AZURE_SQL_DATABASE's
    "Auto-pause delay" setting), which kills this long-lived connection out
    from under the app between requests — the whole point of a persistent
    module-level connection is defeated if it can't recover from that.
    Resuming from auto-pause isn't instant, and observed to stay flaky
    (repeated "Communication link failure" errors) for a stretch after the
    first successful reconnect too — a single retry-once wasn't enough, so
    both connecting and querying retry in a loop with a short delay rather
    than giving up after one attempt."""
    def __init__(self):
        self._raw = self._connect_with_retry()

    def _connect_with_retry(self, attempts=4, delay_seconds=5):
        last_error = None
        for _ in range(attempts):
            try:
                return connect(_connection_string())
            except (OperationalError, InterfaceError) as e:
                last_error = e
                time.sleep(delay_seconds)
        raise last_error

    def execute(self, sql, params=(), attempts=5, delay_seconds=4):
        last_error = None
        for _ in range(attempts):
            try:
                cur = self._raw.cursor()
                cur.execute(sql, params)
                return _Cursor(cur)
            except (OperationalError, InterfaceError) as e:
                last_error = e
                time.sleep(delay_seconds)
                try:
                    self._raw = connect(_connection_string())
                except (OperationalError, InterfaceError):
                    pass  # still not ready — next loop iteration retries anyway
        raise last_error

    def commit(self):
        self._raw.commit()


def _connection_string():
    return (
        f"Server=tcp:{AZURE_SQL_SERVER},1433;Database={AZURE_SQL_DATABASE};"
        f"Uid={AZURE_SQL_USER};Pwd={AZURE_SQL_PASSWORD};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )


_conn = _Conn()


def init_db():
    with _lock:
        _conn.execute(f"""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'employees')
            CREATE TABLE employees (
                id INT IDENTITY(1,1) PRIMARY KEY,
                hris_id NVARCHAR(100) UNIQUE,          -- system-of-record ID; populated once the HRIS webhook lands
                full_name NVARCHAR(200) NOT NULL,
                country NVARCHAR(10),                  -- ISO 3166-1 alpha-2, e.g. 'DK', 'GB'
                department NVARCHAR(100),               -- e.g. 'Operations', 'People & Culture'
                personal_email NVARCHAR(200),
                personal_phone NVARCHAR(50),
                company_email NVARCHAR(200),
                company_phone NVARCHAR(50),
                bamboohr_id NVARCHAR(50),               -- BambooHR's own employee ID
                okta_id NVARCHAR(50),                   -- Okta's own user ID
                entra_id NVARCHAR(50),                  -- Microsoft Entra ID's own user object ID
                created_at NVARCHAR(30) NOT NULL {_ISO_DEFAULT}
            )
        """)
        _conn.execute(f"""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'events')
            CREATE TABLE events (
                id INT IDENTITY(1,1) PRIMARY KEY,
                ticket_id NVARCHAR(50) NOT NULL,
                type NVARCHAR(20) NOT NULL,             -- starter | mover | leaver
                employee_name NVARCHAR(200) NOT NULL,
                employee_id INT REFERENCES employees(id),
                status NVARCHAR(20) NOT NULL DEFAULT 'processing',  -- processing | complete
                created_at NVARCHAR(30) NOT NULL {_ISO_DEFAULT}
            )
        """)
        _conn.execute(f"""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'event_steps')
            CREATE TABLE event_steps (
                id INT IDENTITY(1,1) PRIMARY KEY,
                event_id INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                step_index INT NOT NULL,
                label NVARCHAR(200) NOT NULL,
                system_key NVARCHAR(50),                -- which system this step touches, if any
                is_gdpr_flag BIT NOT NULL DEFAULT 0,
                is_welcome_message BIT NOT NULL DEFAULT 0,
                done BIT NOT NULL DEFAULT 0,
                done_at NVARCHAR(30)
            )
        """)
        _conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_steps_event')
            CREATE INDEX idx_steps_event ON event_steps(event_id)
        """)
        _conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_events_employee')
            CREATE INDEX idx_events_employee ON events(employee_id)
        """)
        _conn.commit()


def get_conn():
    return _conn


def get_lock():
    return _lock

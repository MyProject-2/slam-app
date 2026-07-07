"""
server.py — SLAM demo application server.

A small REST API + static file server built entirely on Python's standard
library (http.server, sqlite3, threading) — no external dependencies to
install, so it runs anywhere Python 3 runs.

Run:
    python3 server.py
Then open:
    http://localhost:8000
"""

import json
import mimetypes
import os
import posixpath
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db
import pipelines

# Use an absolute path, resolved the same way regardless of OS.
# (A relative path here caused a Windows-only bug where the static-file
# safety check below would always incorrectly fail — see do_GET.)
PUBLIC_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))
PORT = int(os.environ.get("PORT", 8000))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_ticket_id(event_type: str) -> str:
    prefix = {"starter": "SL", "mover": "ML", "leaver": "XL"}.get(event_type, "EV")
    return f"{prefix}-{int(time.time() * 1000) % 100000}"


def process_event_in_background(event_id: int, steps_meta):
    """Marks each step 'done' one at a time, spaced out, so the frontend
    (which polls GET /api/events) sees the ticket progress live."""
    conn = db.get_conn()
    lock = db.get_lock()
    for step_index, _meta in enumerate(steps_meta):
        time.sleep(pipelines.STEP_DELAY_SECONDS)
        with lock:
            conn.execute(
                "UPDATE event_steps SET done = 1, done_at = ? WHERE event_id = ? AND step_index = ?",
                (now_iso(), event_id, step_index),
            )
            conn.commit()
    with lock:
        conn.execute("UPDATE events SET status = 'complete' WHERE id = ?", (event_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# API handlers — each returns (status_code, dict_or_list)
# ---------------------------------------------------------------------------

def api_list_events():
    conn = db.get_conn()
    with db.get_lock():
        events = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        result = []
        for ev in events:
            steps = conn.execute(
                "SELECT * FROM event_steps WHERE event_id = ? ORDER BY step_index",
                (ev["id"],),
            ).fetchall()
            result.append({
                "id": ev["id"],
                "ticket_id": ev["ticket_id"],
                "type": ev["type"],
                "employee_name": ev["employee_name"],
                "status": ev["status"],
                "created_at": ev["created_at"],
                "steps": [
                    {
                        "label": s["label"],
                        "system": s["system_key"],
                        "gdpr": bool(s["is_gdpr_flag"]),
                        "done": bool(s["done"]),
                        "done_at": s["done_at"],
                    }
                    for s in steps
                ],
            })
    return 200, {"events": result}


def api_create_event(body):
    event_type = (body or {}).get("type")
    employee_name = (body or {}).get("employee_name") or "Unnamed employee"

    if event_type not in pipelines.PIPELINES:
        return 400, {"error": f"Unknown event type '{event_type}'. Use starter, mover, or leaver."}

    steps_meta = pipelines.PIPELINES[event_type]
    ticket_id = make_ticket_id(event_type)
    conn = db.get_conn()

    with db.get_lock():
        cur = conn.execute(
            "INSERT INTO events (ticket_id, type, employee_name, status, created_at) VALUES (?, ?, ?, 'processing', ?)",
            (ticket_id, event_type, employee_name, now_iso()),
        )
        event_id = cur.lastrowid
        for idx, step in enumerate(steps_meta):
            conn.execute(
                "INSERT INTO event_steps (event_id, step_index, label, system_key, is_gdpr_flag, done) VALUES (?, ?, ?, ?, ?, 0)",
                (event_id, idx, step["label"], step.get("system"), int(step.get("gdpr", False))),
            )
        conn.commit()

    # Kick off background processing so the ticket "runs" without blocking the request
    threading.Thread(target=process_event_in_background, args=(event_id, steps_meta), daemon=True).start()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": event_type}


def api_clear_events():
    conn = db.get_conn()
    with db.get_lock():
        conn.execute("DELETE FROM event_steps")
        conn.execute("DELETE FROM events")
        conn.commit()
    return 200, {"cleared": True}


def api_systems_status():
    conn = db.get_conn()
    cutoff_ms = 1200  # a system stays "lit" for this many ms after a step touches it
    now = datetime.now(timezone.utc)
    with db.get_lock():
        recent = conn.execute(
            "SELECT system_key, done_at FROM event_steps WHERE done = 1 AND system_key IS NOT NULL"
        ).fetchall()

    lit_keys = set()
    for row in recent:
        if not row["done_at"]:
            continue
        try:
            done_time = datetime.strptime(row["done_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - done_time).total_seconds() * 1000 <= cutoff_ms:
            lit_keys.add(row["system_key"])

    systems = [
        {**sys, "lit": sys["key"] in lit_keys}
        for sys in pipelines.SYSTEMS
    ]
    return 200, {"systems": systems}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep console quiet; comment out to debug

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send_json(404, {"error": "Not found"})
            return
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/events":
            status, payload = api_list_events()
            self._send_json(status, payload)
            return
        if path == "/api/systems":
            status, payload = api_systems_status()
            self._send_json(status, payload)
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()})
            return

        # static file serving
        if path == "/":
            path = "/index.html"

        # Normalize using posixpath (URL paths always use forward slashes,
        # regardless of the OS the server runs on) to collapse any "..".
        normalized = posixpath.normpath(path).lstrip("/")
        parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]

        # Join onto PUBLIC_DIR component-by-component. Using os.path.join with
        # a single string here previously broke on Windows: a leading
        # backslash in the string made os.path.join discard PUBLIC_DIR
        # entirely and jump to the drive root, which then always failed the
        # containment check below and returned 403 for every request.
        full_path = os.path.abspath(os.path.join(PUBLIC_DIR, *parts)) if parts else PUBLIC_DIR

        if os.path.commonpath([full_path, PUBLIC_DIR]) != PUBLIC_DIR:
            self._send_json(403, {"error": "Forbidden"})
            return
        self._send_file(full_path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api_create_event(body)
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            status, payload = api_clear_events()
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": "Not found"})


def main():
    db.init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"SLAM demo running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

"""
api.py — SLAM API handlers.

Each function implements one API endpoint's business logic and returns
(status_code, payload_dict). Kept independent of the HTTP transport layer
(see server.py) so the handlers can be read, and called, without an
http.server request in play.
"""

import hashlib
import hmac
import os
import sqlite3
import time
from datetime import datetime, timezone

import db
import workflow

# Shared secret for verifying inbound HRIS webhook signatures (see
# handle_hris_webhook). Set a real value via the HRIS_WEBHOOK_SECRET env
# var in any environment that isn't just this local demo.
HRIS_WEBHOOK_SECRET = os.environ.get("HRIS_WEBHOOK_SECRET", "demo-insecure-secret-change-me")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_ticket_id(event_type: str) -> str:
    prefix = {"starter": "SL", "mover": "ML", "leaver": "XL"}.get(event_type, "EV")
    return f"{prefix}-{int(time.time() * 1000) % 100000}"


def _insert_event(conn, event_type, employee_name, employee_id=None):
    """Shared by create_event() and handle_hris_webhook() — writes the
    events row plus one event_steps row per step in that type's pipeline."""
    steps_meta = workflow.PIPELINES[event_type]
    ticket_id = make_ticket_id(event_type)
    cur = conn.execute(
        "INSERT INTO events (ticket_id, type, employee_name, employee_id, status, created_at) "
        "VALUES (?, ?, ?, ?, 'processing', ?)",
        (ticket_id, event_type, employee_name, employee_id, now_iso()),
    )
    event_id = cur.lastrowid
    for idx, step in enumerate(steps_meta):
        conn.execute(
            "INSERT INTO event_steps (event_id, step_index, label, system_key, is_gdpr_flag, done) VALUES (?, ?, ?, ?, ?, 0)",
            (event_id, idx, step["label"], step.get("system"), int(step.get("gdpr", False))),
        )
    return event_id, ticket_id


def list_events():
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


def create_event(body):
    """Handles the manual 'Simulate new starter/mover/leaver' buttons.
    No dedup, no employee linkage — that's what handle_hris_webhook is for.
    Kept around as a demo/testing tool now that the HRIS webhook exists."""
    event_type = (body or {}).get("type")
    employee_name = (body or {}).get("employee_name") or "Unnamed employee"

    if event_type not in workflow.PIPELINES:
        return 400, {"error": f"Unknown event type '{event_type}'. Use starter, mover, or leaver."}

    conn = db.get_conn()
    with db.get_lock():
        event_id, ticket_id = _insert_event(conn, event_type, employee_name)
        conn.commit()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": event_type}


def clear_events():
    conn = db.get_conn()
    with db.get_lock():
        conn.execute("DELETE FROM event_steps")
        conn.execute("DELETE FROM events")
        conn.commit()
    return 200, {"cleared": True}


def list_active_events():
    """Events that still have at least one undone step — the "active users" queue."""
    conn = db.get_conn()
    with db.get_lock():
        events = conn.execute(
            "SELECT * FROM events WHERE status = 'processing' ORDER BY id ASC"
        ).fetchall()
        result = []
        for ev in events:
            steps = conn.execute(
                "SELECT * FROM event_steps WHERE event_id = ? ORDER BY step_index",
                (ev["id"],),
            ).fetchall()
            next_step = next((s for s in steps if not s["done"]), None)
            result.append({
                "id": ev["id"],
                "ticket_id": ev["ticket_id"],
                "type": ev["type"],
                "employee_name": ev["employee_name"],
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
                "next_step_label": next_step["label"] if next_step else None,
            })
    return 200, {"events": result}


def advance_step(event_id):
    conn = db.get_conn()
    with db.get_lock():
        ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if ev is None:
            return 404, {"error": "Event not found"}

        next_step = conn.execute(
            "SELECT * FROM event_steps WHERE event_id = ? AND done = 0 ORDER BY step_index LIMIT 1",
            (event_id,),
        ).fetchone()
        if next_step is None:
            return 400, {"error": "No pending steps for this event"}

        conn.execute(
            "UPDATE event_steps SET done = 1, done_at = ? WHERE id = ?",
            (now_iso(), next_step["id"]),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM event_steps WHERE event_id = ? AND done = 0",
            (event_id,),
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute("UPDATE events SET status = 'complete' WHERE id = ?", (event_id,))
        conn.commit()

    return 200, {"event_id": event_id, "advanced_step": next_step["label"], "remaining": remaining}


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verifies an 'sha256=<hex>' HMAC signature header (the same scheme
    GitHub/Stripe use), computed over the raw request body with the shared
    HRIS_WEBHOOK_SECRET. This is what a native HRIS webhook subscription
    (or an iPaaS layer sitting in front of one) would sign requests with."""
    if not signature_header:
        return False
    expected = hmac.new(HRIS_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, provided)


def _find_employee(conn, hris_id=None, personal_email=None, company_email=None):
    """Looks up an existing employee by the most reliable identifier
    available, in order. This is the dedup check: if any of these match,
    the employee already exists in SLAM."""
    for column, value in (("hris_id", hris_id), ("personal_email", personal_email), ("company_email", company_email)):
        if not value:
            continue
        row = conn.execute(f"SELECT * FROM employees WHERE {column} = ?", (value,)).fetchone()
        if row:
            return row
    return None


def handle_hris_webhook(raw_body: bytes, signature_header: str, body: dict):
    """Receives lifecycle events pushed from the HRIS (system of record)
    instead of SLAM initiating them. Expected payload:
        {
          "event_type": "starter" | "mover" | "leaver",
          "employee": {
            "hris_id": "EMP-1234", "full_name": "...", "country": "DK",
            "personal_email": "...", "personal_phone": "...",
            "company_email": "...", "company_phone": "..."
          }
        }
    """
    if not verify_webhook_signature(raw_body, signature_header):
        return 401, {"error": "Invalid or missing webhook signature"}

    event_type = (body or {}).get("event_type")
    employee = (body or {}).get("employee") or {}

    if event_type not in workflow.PIPELINES:
        return 400, {"error": f"Unknown event_type '{event_type}'. Use starter, mover, or leaver."}

    full_name = employee.get("full_name")
    if not full_name:
        return 400, {"error": "employee.full_name is required"}

    hris_id = employee.get("hris_id")
    personal_email = employee.get("personal_email")
    company_email = employee.get("company_email")

    conn = db.get_conn()
    with db.get_lock():
        existing = _find_employee(conn, hris_id, personal_email, company_email)

        if event_type == "starter":
            if existing is not None:
                return 409, {
                    "error": "Employee already exists — a starter event was already processed for them. "
                             "Use a mover or leaver event instead.",
                    "employee_id": existing["id"],
                }
            cur = conn.execute(
                "INSERT INTO employees (hris_id, full_name, country, personal_email, personal_phone, company_email, company_phone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hris_id, full_name, employee.get("country"), personal_email,
                 employee.get("personal_phone"), company_email, employee.get("company_phone")),
            )
            employee_id = cur.lastrowid
        else:
            if existing is None:
                return 404, {
                    "error": f"No existing employee found for a {event_type} event. "
                             "The HRIS should send a starter event for this person first.",
                }
            employee_id = existing["id"]
            # Keep contact info/country current — the HRIS is the source of truth.
            conn.execute(
                "UPDATE employees SET full_name = ?, country = ?, personal_email = ?, personal_phone = ?, "
                "company_email = ?, company_phone = ? WHERE id = ?",
                (full_name, employee.get("country"), personal_email, employee.get("personal_phone"),
                 company_email, employee.get("company_phone"), employee_id),
            )

        event_id, ticket_id = _insert_event(conn, event_type, full_name, employee_id=employee_id)
        conn.commit()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": event_type, "employee_id": employee_id}


_EMPLOYEE_FIELDS = ("hris_id", "full_name", "country", "personal_email", "personal_phone", "company_email", "company_phone")


def list_employees():
    conn = db.get_conn()
    with db.get_lock():
        rows = conn.execute("SELECT * FROM employees ORDER BY full_name ASC").fetchall()
    return 200, {"employees": [dict(row) for row in rows]}


def create_employee(body):
    body = body or {}
    full_name = (body.get("full_name") or "").strip()
    if not full_name:
        return 400, {"error": "full_name is required"}

    values = [body.get(f) or None for f in _EMPLOYEE_FIELDS]
    values[_EMPLOYEE_FIELDS.index("full_name")] = full_name

    conn = db.get_conn()
    with db.get_lock():
        try:
            cur = conn.execute(
                f"INSERT INTO employees ({', '.join(_EMPLOYEE_FIELDS)}) VALUES ({', '.join('?' * len(_EMPLOYEE_FIELDS))})",
                values,
            )
        except sqlite3.IntegrityError:
            return 409, {"error": f"An employee with hris_id '{body.get('hris_id')}' already exists"}
        conn.commit()
        row = conn.execute("SELECT * FROM employees WHERE id = ?", (cur.lastrowid,)).fetchone()

    return 201, {"employee": dict(row)}


def update_employee(employee_id, body):
    body = body or {}
    full_name = (body.get("full_name") or "").strip()
    if not full_name:
        return 400, {"error": "full_name is required"}

    values = [body.get(f) or None for f in _EMPLOYEE_FIELDS]
    values[_EMPLOYEE_FIELDS.index("full_name")] = full_name

    conn = db.get_conn()
    with db.get_lock():
        existing = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if existing is None:
            return 404, {"error": "Employee not found"}
        try:
            conn.execute(
                f"UPDATE employees SET {', '.join(f + ' = ?' for f in _EMPLOYEE_FIELDS)} WHERE id = ?",
                values + [employee_id],
            )
        except sqlite3.IntegrityError:
            return 409, {"error": f"An employee with hris_id '{body.get('hris_id')}' already exists"}
        conn.commit()
        row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()

    return 200, {"employee": dict(row)}


def delete_employee(employee_id):
    conn = db.get_conn()
    with db.get_lock():
        existing = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if existing is None:
            return 404, {"error": "Employee not found"}
        linked = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE employee_id = ?", (employee_id,)
        ).fetchone()["c"]
        if linked > 0:
            return 400, {"error": f"Cannot delete — {linked} event(s) reference this employee"}
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        conn.commit()

    return 200, {"deleted": True}


def systems_status():
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
        for sys in workflow.SYSTEMS
    ]
    return 200, {"systems": systems}

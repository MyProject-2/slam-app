"""
api.py — SLAM API handlers.

Each function implements one API endpoint's business logic and returns
(status_code, payload_dict). Kept independent of the HTTP transport layer
(see server.py) so the handlers can be read, and called, without an
http.server request in play.
"""

import time
from datetime import datetime, timezone

import db
import workflow


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_ticket_id(event_type: str) -> str:
    prefix = {"starter": "SL", "mover": "ML", "leaver": "XL"}.get(event_type, "EV")
    return f"{prefix}-{int(time.time() * 1000) % 100000}"


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
    event_type = (body or {}).get("type")
    employee_name = (body or {}).get("employee_name") or "Unnamed employee"

    if event_type not in workflow.PIPELINES:
        return 400, {"error": f"Unknown event type '{event_type}'. Use starter, mover, or leaver."}

    steps_meta = workflow.PIPELINES[event_type]
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

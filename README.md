# SLAM — Starters, Leavers, And Movers

A small, real, running full-stack app that demonstrates a People Tech
automation pipeline: a **database**, a **REST API**, and a **web frontend**
that talks to it live.

Built entirely on Python's standard library — no `pip install` required,
so it runs anywhere Python 3 is installed.

## What's inside

| Layer      | Tech                          | File(s)          |
|------------|--------------------------------|-------------------|
| Database   | SQLite (real `.db` file)      | `db.py`           |
| Pipeline logic | Plain Python                | `pipelines.py`    |
| API + server | `http.server` (built-in)    | `server.py`       |
| Frontend   | HTML/CSS/JS, polls the API    | `public/index.html` |

## Run it

```bash
python3 server.py
```

Then open **http://localhost:8000** in your browser.

Open the same URL in two browser tabs — fire an event in one, watch it
appear in the other. Both are reading the same SQLite database through
the API, not local browser state.

## API reference

| Method | Path            | Description                                      |
|--------|-----------------|---------------------------------------------------|
| GET    | `/api/events`   | List the last 50 lifecycle events with their steps |
| POST   | `/api/events`   | Create an event. Body: `{"type": "starter\|mover\|leaver", "employee_name": "..."}` |
| DELETE | `/api/events`   | Clear all events (reset the demo)                 |
| GET    | `/api/systems`  | Current "lit" status of each downstream system     |
| GET    | `/api/health`   | Health check                                       |

## How it works

1. A button click on the page sends `POST /api/events` to the API.
2. The API writes a new row into the `events` table and one row per
   pipeline step into `event_steps` — all in SQLite (`slam.db`, created
   automatically on first run).
3. A background thread advances the steps one at a time (marking `done`
   and stamping `done_at`), simulating each system integration firing in
   sequence.
4. The frontend polls `GET /api/events` and `GET /api/systems` roughly
   once a second and re-renders from what's actually in the database.

## Extending it

- Swap `db.py` for Postgres/MySQL by replacing the `sqlite3` calls —
  the rest of the app doesn't care what's underneath.
- Add real integrations by having a step's completion also call an
  actual HRIS/IAM/payroll API instead of just sleeping.
- Add authentication, audit logging, or a "who changed what" trail on
  top of `event_steps` for a closer match to real GDPR/audit requirements.

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
| Workflow logic | Plain Python                | `workflow.py`    |
| API handlers | Plain Python                | `api.py`          |
| HTTP routing / server | `http.server` (built-in) | `server.py` |
| Frontend   | HTML/CSS/JS, polls the API    | `public/index.html`, `public/manage.html`, `public/employees.html` |

## Run it

```bash
python3 server.py
```

Then open **http://localhost:8000** in your browser.

Open the same URL in two browser tabs — fire an event in one, watch it
appear in the other. Both are reading the same SQLite database through
the API, not local browser state.

### Optional environment variables

| Variable | Purpose |
|---|---|
| `PORT` | Server port (default `8000`) |
| `HRIS_WEBHOOK_SECRET` | Shared secret for verifying `X-HRIS-Signature` on inbound webhook calls. Falls back to an insecure demo default with a startup warning if unset. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Real Twilio credentials for the welcome-message SMS step. If any are unset, sends are simulated (logged, not actually sent) so the demo works without a Twilio account. |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE` | Real Snowflake credentials for the "Synced to Snowflake" step (`SNOWFLAKE_ACCOUNT` is the `<organization>-<account>` identifier, e.g. `myorg-myaccount`; `SNOWFLAKE_PAT` is a [Programmatic Access Token](https://docs.snowflake.com/en/user-guide/programmatic-access-tokens)). If any are unset, syncs are simulated. Optional: `SNOWFLAKE_ROLE`, `SNOWFLAKE_TABLE` (default `SLAM_EVENTS`). |
| `BAMBOOHR_SUBDOMAIN`, `BAMBOOHR_API_KEY` | Real BambooHR credentials for the Core HR steps ("Core HR record created/updated", "Last day confirmed") — `BAMBOOHR_SUBDOMAIN` is the `xyz` in `xyz.bamboohr.com`. If either is unset, syncs are simulated. |
| `OKTA_ORG_URL`, `OKTA_API_TOKEN` | Real Okta credentials for `sync_to_okta()` (see below) — `OKTA_ORG_URL` is your org's base URL, e.g. `https://dev-12345.okta.com`. Built and tested, but not currently wired into `advance_step()` — see `ENTRA_*` below. |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_DOMAIN` | Real Microsoft Entra ID (Azure AD) credentials for the Access/IT steps ("Access + accounts provisioned"/"re-scoped", "Access revoked on schedule") — this is the integration `advance_step()` actually calls. `ENTRA_DOMAIN` is a domain verified on your tenant, used to build new users' sign-in address (e.g. `<org>.onmicrosoft.com`). If any are unset, syncs are simulated. |

## How it works

SLAM models the **Starter / Mover / Leaver** employee lifecycle as tickets
that move through a pipeline of steps — nothing advances automatically;
every step is either advanced by hand or triggered by a real event.

1. **The HRIS is the system of record.** `POST /api/webhooks/hris`
   receives lifecycle events pushed from the HRIS, authenticated via an
   HMAC-SHA256 signature (`X-HRIS-Signature: sha256=<hex>`, checked
   against `HRIS_WEBHOOK_SECRET`). A `starter` event creates an
   `employees` row; `mover`/`leaver` events require one to already exist
   (a `starter` for an existing employee is rejected with `409`; a
   `mover`/`leaver` for an unknown one is rejected with `404`).
2. The homepage's "Simulate new starter/mover/leaver" buttons
   (`POST /api/dev/simulate-hris`) are a demo/testing stand-in for a real
   HRIS: they build a realistic webhook payload from just a name, sign
   it themselves, and feed it through the exact same webhook logic above.
3. Each event gets one `event_steps` row per pipeline step (defined in
   `workflow.py`), all starting `done = 0`. Visit **Manage active users**
   (`/manage.html`) to step a ticket through its pipeline by hand via
   `POST /api/events/<id>/advance` — always in order, one step at a time.
4. The starter pipeline's **"Welcome message sent"** step sends a real
   SMS via Twilio (or simulates one — see env vars above), using
   `resolve_contact_channel()` to decide personal vs. company contact
   info: personal before Access is provisioned, company after (if the
   employee has one), otherwise personal permanently.
5. Every pipeline's final **"Synced to Snowflake"** step pushes a row
   (event, ticket, and employee identity) into a real Snowflake table via
   the SQL API (or simulates it — see env vars above).
6. Every pipeline's **Core HR step** ("Core HR record created"/"updated",
   "Last day confirmed") creates or updates the employee's real record in
   BambooHR (or simulates it — see env vars above). A `starter` creates
   the BambooHR record; `mover`/`leaver` update that same record (tracked
   via `employees.bamboohr_id`) rather than creating duplicates; a
   `leaver` also sets `employmentHistoryStatus` to `Terminated`.
7. Every pipeline's **Access/IT step** ("Access + accounts provisioned"/
   "re-scoped", "Access revoked on schedule") creates, updates, or
   deactivates the employee's real Microsoft Entra ID account (or
   simulates it — see env vars above). A `starter` creates the account
   (Graph requires a throwaway password at creation, unlike Okta —
   `forceChangePasswordNextSignIn` is set so it's never actually usable);
   `mover` updates that same account (tracked via `employees.entra_id`);
   `leaver` deactivates it. `sync_to_okta()` is also fully built and
   tested (`employees.okta_id`) as an alternative identity provider, just
   not the one currently wired up — swap which function `advance_step()`
   calls for the `"accessit"` system if you'd rather use Okta.
8. **Employees** (`/employees.html`) lets you view, add, edit, or delete
   employee records directly — useful for demos/testing without going
   through the webhook.
9. The frontend polls `GET /api/events`, `GET /api/events/active`, and
   `GET /api/systems` roughly once a second and re-renders from what's
   actually in the database — this browser tab and any other tab open to
   this server see the exact same data.

## API reference

| Method | Path            | Description                                      |
|--------|-----------------|---------------------------------------------------|
| GET    | `/api/events`   | List the last 50 lifecycle events with their steps |
| GET    | `/api/events/active` | Events with at least one step still pending, plus each one's next step |
| POST   | `/api/events`   | Create an event directly, no employee linkage or dedup. Demo/testing only. Body: `{"type": "starter\|mover\|leaver", "employee_name": "..."}` |
| POST   | `/api/events/<id>/advance` | Mark that event's next pending step done, in order |
| DELETE | `/api/events`   | Clear all events (reset the demo)                 |
| POST   | `/api/webhooks/hris` | Real HRIS webhook receiver — signed, deduped, creates/updates employees |
| POST   | `/api/dev/simulate-hris` | What the homepage buttons call — builds and self-signs a webhook payload from a bare name |
| GET    | `/api/employees` | List all employees |
| POST   | `/api/employees` | Create an employee directly |
| PUT    | `/api/employees/<id>` | Update an employee |
| DELETE | `/api/employees/<id>` | Delete an employee (blocked if events still reference it) |
| GET    | `/api/systems`  | Current "lit" status of each downstream system     |
| GET    | `/api/health`   | Health check                                       |

## Extending it

- Swap `db.py` for Postgres/MySQL by replacing the `sqlite3` calls —
  the rest of the app doesn't care what's underneath.
- Add real integrations by having a step's completion also call an
  actual HRIS/IAM/payroll API, the way the Core HR steps already call
  BambooHR, the Access/IT steps call Microsoft Entra ID (or Okta), the
  welcome-message step calls Twilio, and the Snowflake step calls
  Snowflake's SQL API.
- Add authentication, audit logging, or a "who changed what" trail on
  top of `event_steps` for a closer match to real GDPR/audit requirements.

# SLAM — Starters, Leavers, And Movers

A small, real, running full-stack app that demonstrates a People Tech
automation pipeline: a **database**, a **REST API**, and a **web frontend**
that talks to it live — plus seven real, working (or honestly-documented)
third-party integrations.

Live at **https://slam.asamkhan.dk** (also reachable at the raw
`azurewebsites.net` URL — see [Azure deployment](https://github.com/MyProject-2/slam-app/wiki/Azure-Deployment)
in the wiki). Access is gated (see [Login](#login) below); ask for a
demo-login-eligible email or a Microsoft SSO invite.

## What's inside

| Layer      | Tech                          | File(s)          |
|------------|--------------------------------|-------------------|
| Database   | Azure SQL Database, via Microsoft's `mssql-python` driver | `db.py`           |
| Workflow logic | Plain Python                | `workflow.py`    |
| API handlers | Plain Python                | `api.py`          |
| HTTP routing / server | `http.server` (built-in) | `server.py` |
| Frontend   | HTML/CSS/JS, polls the API    | `public/index.html`, `public/manage.html`, `public/employees.html`, `public/simulate.html`, `public/login.html` |

Everything else is Python standard library — `mssql-python` is SLAM's one
`pip install` dependency, added when the database moved off SQLite (see
"Why Azure SQL" below). Everything else (HTTP server, webhook signing,
Twilio/Snowflake/BambooHR/Okta/Entra/Teams REST calls, SMTP) is still
stdlib-only (`http.server`, `urllib`, `hmac`, `smtplib`) — no other SDK is
installed just to talk to a vendor API.

## Run it

```bash
pip install -r requirements.txt
python3 server.py
```

Then open **http://localhost:8000** in your browser.

You'll need a real (or free-tier) Azure SQL Database and its connection
details set as environment variables (see below) — unlike every vendor
integration in this app, the database has no simulated fallback; nothing
runs without one. Locally, the login gate (see below) stays off regardless,
so no Microsoft sign-in is needed to use `localhost:8000`.

Open the same URL in two browser tabs — fire an event in one, watch it
appear in the other. Both are reading the same Azure SQL database through
the API, not local browser state.

### Required environment variables

| Variable | Purpose |
|---|---|
| `AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`, `AZURE_SQL_USER`, `AZURE_SQL_PASSWORD` | Azure SQL Database connection details. Required — there's no SQLite/local-file fallback. `db.py`'s connection auto-reconnects (with retries) if the database drops, which happens routinely on Azure SQL's free serverless tier after an hour idle (auto-pause). |

### Optional environment variables

| Variable | Purpose |
|---|---|
| `PORT` | Server port (default `8000`) |
| `HRIS_WEBHOOK_SECRET` | Shared secret for verifying `X-HRIS-Signature` on inbound generic-HRIS webhook calls (`POST /api/webhooks/hris`). Falls back to an insecure demo default with a startup warning if unset. |
| `BAMBOOHR_WEBHOOK_SECRET` | Shared secret for verifying BambooHR's own event-based webhook (`POST /api/webhooks/bamboohr`) — a *different* signature scheme and a *different* secret from `HRIS_WEBHOOK_SECRET` above (BambooHR HMACs body+timestamp, not just body). Set in BambooHR under Account Settings → Webhooks when the webhook is created. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Real Twilio credentials for the welcome-message SMS step. If any are unset, sends are simulated (logged, not actually sent) so the demo works without a Twilio account. Note: a Twilio **trial** account can only send to phone numbers verified as a "Verified Caller ID" in the Twilio Console, and sending to **US** numbers additionally requires A2P 10DLC campaign registration (10-15 day review, not available to trial accounts at all) — sending to a **non-US** verified number (e.g. a Danish `+45` number) sidesteps the A2P 10DLC requirement entirely, since that's a US-carrier-specific compliance regime. This is exactly how SLAM's own live Twilio send is configured. |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE` | Real Snowflake credentials for the "Synced to Snowflake" step (`SNOWFLAKE_ACCOUNT` is the `<organization>-<account>` identifier, e.g. `myorg-myaccount`; `SNOWFLAKE_PAT` is a [Programmatic Access Token](https://docs.snowflake.com/en/user-guide/programmatic-access-tokens)). If any are unset, syncs are simulated. Optional: `SNOWFLAKE_ROLE`, `SNOWFLAKE_TABLE` (default `SLAM_EVENTS`). |
| `BAMBOOHR_SUBDOMAIN`, `BAMBOOHR_API_KEY` | Real BambooHR credentials for the Core HR steps ("Core HR record created/updated", "Last day confirmed") — `BAMBOOHR_SUBDOMAIN` is the `xyz` in `xyz.bamboohr.com`. If either is unset, syncs are simulated. |
| `OKTA_ORG_URL`, `OKTA_API_TOKEN` | Real Okta credentials for `sync_to_okta()` (see below) — `OKTA_ORG_URL` is your org's base URL, e.g. `https://dev-12345.okta.com`. Built and tested, but not currently wired into `advance_step()` — see `ENTRA_*` below. |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_DOMAIN` | Real Microsoft Entra ID (Azure AD) credentials for the Access/IT steps ("Access + accounts provisioned"/"re-scoped", "Access revoked on schedule") — this is the integration `advance_step()` actually calls. `ENTRA_DOMAIN` is a domain verified on your tenant, used to build new users' sign-in address (e.g. `<org>.onmicrosoft.com`). Also powers the Teams sync below, since it reuses the same app registration/token. If any are unset, syncs are simulated. |
| `TEAMS_GROUP_ID` | The Microsoft 365 Group ID backing the Teams the Comms step adds/removes people from. Reuses the Entra credentials above for auth (just needs one more Graph permission, `GroupMember.ReadWrite.All`, granted to that same app registration) — no separate credential set. If unset (or Entra isn't configured), syncs are simulated. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Real SMTP credentials for the two welcome-email sends (see "Two welcome emails" below). `SMTP_PORT` defaults to `465` (implicit TLS / `SMTP_SSL`, matching one.com's `send.one.com:465` setup, which is what this app's own live send uses). `SMTP_FROM` defaults to `SMTP_USERNAME` if unset. If `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` aren't all set, sends are simulated. |
| `DEMO_LOGIN_SECRET` | Signs the "demo login" cookie (see "Login" below). Falls back to an insecure demo default — set a real value in any environment that isn't just local demo use. |

## How it works

SLAM models the **Starter / Mover / Leaver** employee lifecycle as tickets
that move through a pipeline of steps — nothing advances automatically;
every step is either advanced by hand or triggered by a real event.

1. **The HRIS is the system of record**, and there are two ways it can
   reach SLAM:
   - `POST /api/webhooks/hris` — a generic webhook receiver for any HRIS,
     authenticated via an HMAC-SHA256 signature (`X-HRIS-Signature:
     sha256=<hex>`, checked against `HRIS_WEBHOOK_SECRET`).
   - `POST /api/webhooks/bamboohr` — BambooHR's own event-based webhook
     format (a *different* signature scheme — see `BAMBOOHR_WEBHOOK_SECRET`
     above), verified live end-to-end against a real BambooHR account:
     since BambooHR's `employee.created` payload carries only an ID, this
     handler reads the full employee record back from BambooHR's API
     before creating the ticket. Because the employee already exists in
     BambooHR by the time this fires, the ticket's "Core HR record
     created" step is auto-marked done at creation — the one deliberate
     exception to "nothing advances automatically," scoped to this one
     event source only.

   Both entry points share one core (`_ingest_lifecycle_event()`): a
   `starter` event creates an `employees` row; `mover`/`leaver` events
   require one to already exist (a `starter` for an existing employee is
   rejected with `409`; a `mover`/`leaver` for an unknown one is rejected
   with `404`). Employee dedup checks `hris_id`, then `personal_email` and
   `company_email` — against **both** email columns, not just the
   same-named one, since different sources (e.g. a manually-simulated
   starter vs. a BambooHR-originated one) can put the same real address in
   different columns.
2. The **Simulate SLAM** page (`/simulate.html`, linked from the homepage)
   is a demo/testing stand-in for a real HRIS. Its Starter station
   (`POST /api/dev/simulate-hris`) builds a realistic webhook payload from
   just a typed name, signs it itself, and feeds it through the exact same
   webhook logic above. Its Mover and Leaver stations work differently —
   they search real, already-existing employee records live (client-side
   filtering of `/api/employees`, no separate search endpoint) rather than
   deriving a fake identity from typed text: Mover (`POST
   /api/dev/simulate-mover`) opens a move-department modal and updates the
   selected employee's `department` directly; Leaver (`POST
   /api/dev/simulate-leaver`) opens a leaver ticket for the selected
   employee without deleting their record (a real HR system retains
   departed employees' records for compliance/audit — actual deletion
   stays a separate, explicit action via `DELETE /api/employees/<id>`).
3. Each event gets one `event_steps` row per pipeline step (defined in
   `workflow.py`), all starting `done = 0`. Visit **Manage active users**
   (`/manage.html`) to step a ticket through its pipeline by hand via
   `POST /api/events/<id>/advance` — always in order, one step at a time.
   Both the homepage's live ticket rail and Manage active users group
   tickets by employee, since one person can have more than one ticket
   in flight at once (e.g. a mover fired while their onboarding is still
   in progress).
4. The starter pipeline's **"Welcome message sent"** step fires two things
   at once: a real SMS via Twilio (`send_sms()` — or simulates one, see
   env vars above), using `resolve_contact_channel()` to decide personal
   vs. company contact info (personal before Access is provisioned,
   company after if the employee has one, otherwise personal
   permanently); and a plain welcome email to the employee's **personal**
   address (`send_generic_welcome_email()` — just a greeting, no
   credentials, since no account exists yet). See "Two welcome emails"
   below for how this differs from the Access-step email.
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
   `forceChangePasswordNextSignIn` is set so it's never actually usable),
   and immediately sends a **second** welcome email — `send_welcome_email()`
   — containing the new hire's sign-in address and that one-time password
   (see "Two welcome emails" below); `mover` updates that same account
   (tracked via `employees.entra_id`); `leaver` deactivates it.
   `sync_to_okta()` is also fully built and tested (`employees.okta_id`)
   as an alternative identity provider, just not the one currently wired
   up — swap which function `advance_step()` calls for the `"accessit"`
   system if you'd rather use Okta.
8. The starter's and leaver's **Comms step** ("Comms groups added"/
   "Comms access removed") adds or removes the employee from a Microsoft
   Teams / Microsoft 365 Group (`sync_to_teams()`) — reusing the same
   Entra app registration/OAuth token as step 7 rather than a separate
   credential set (see `TEAMS_GROUP_ID` above).
9. **Employees** (`/employees.html`) lets you view, add, edit, or delete
   employee records directly — useful for demos/testing without going
   through a webhook. Includes a `department` field alongside contact
   info and country.
10. The frontend polls `GET /api/events`, `GET /api/events/active`, and
    `GET /api/systems` roughly once a second and re-renders from what's
    actually in the database — this browser tab and any other tab open to
    this server see the exact same data.

### Two welcome emails — don't conflate them

SLAM sends two genuinely different emails, at two different pipeline
steps, for two different reasons:

| | `send_generic_welcome_email()` | `send_welcome_email()` |
|---|---|---|
| **Fires at** | "Welcome message sent" step (early — right after Core HR) | "Access + accounts provisioned" step (later — once an account exists) |
| **Sent to** | Employee's **personal** email | Employee's **work** email |
| **Contains** | A plain greeting, nothing else | Sign-in address (UPN) + one-time temporary Entra password |
| **Why the split** | No account exists yet at this point in the pipeline — there's nothing to give sign-in instructions for | This is the one legitimate place the Entra-generated temp password goes; it's popped out of the sync result immediately after and never logged or returned in an API response |

Both simulate (rather than fail) when SMTP isn't configured, and both use
the same plain `smtplib`/SMTP_SSL send over one.com's mail setup.

### Login

The live Azure deployment is gated. There are two ways in, both handled by
`/login.html`:

- **"Sign in with Microsoft"** — real SSO via Azure App Service's built-in
  Authentication ("Easy Auth"), backed by a dedicated Entra app
  registration restricted to one specific assigned user (not the same app
  registration `sync_to_entra()`/`sync_to_teams()` use — kept separate on
  purpose, so a login-only registration carries no Graph application
  permissions).
- **Demo login** — a lightweight second door for reviewers: type any email
  ending in `@joejuice.com` and get in. This is **intentionally weak and
  disclosed as such**: nothing verifies the person submitting the email
  actually owns it (no confirmation email is sent). It exists purely to
  hand out low-friction access to a portfolio-demo audience without
  provisioning a real Entra guest account per reviewer — not a real
  identity check, and not meant to be one.

Enforcement lives in `server.py` (`_is_authenticated()`), not in Easy
Auth's own platform-level gate — Easy Auth has no concept of a custom
"check an email suffix" identity provider, so its global enforcement was
turned off (`requireAuthentication: false`) in favor of the app checking
either (a) the `X-MS-CLIENT-PRINCIPAL-NAME` header Easy Auth still injects
for a real SSO session (unforgeable — Azure's front end strips any
client-supplied copy before adding its own), or (b) a signed
`slam_demo_login` cookie from `POST /api/demo-login`. This only activates
when running on Azure (`WEBSITE_SITE_NAME` is set); local dev stays open.
The two webhook endpoints (`/api/webhooks/hris`, `/api/webhooks/bamboohr`)
and the login page/endpoint itself stay reachable pre-login, since a real
HRIS obviously can't complete a browser SSO flow. See the
[wiki's Login page](https://github.com/MyProject-2/slam-app/wiki/Login)
for the full setup, including the Easy Auth configuration gotchas hit
along the way.

### Why Azure SQL

`db.py` used to be plain `sqlite3` against a local `.db` file. That broke
on Azure App Service Linux, whose persistent storage (`/home`) is a
network-mounted share — SQLite's file-locking model isn't reliable over
network filesystems, and Microsoft's own guidance is to use a managed
database service instead. Azure SQL Database's free offer (100,000
vCore-seconds + 32GB/month, free for the subscription's lifetime, not a
time-limited trial) was chosen over Postgres Flexible Server, which has no
free tier at all. `db.py` talks to it via Microsoft's `mssql-python`
driver — SLAM's first and only pip dependency — behind a thin
sqlite3-compatible shim (`Row`/`_Cursor`/`_Conn`) so the rest of the app's
`conn.execute(sql, params)` / `row["col"]` calls didn't need to change.
See the [wiki's Azure Deployment page](https://github.com/MyProject-2/slam-app/wiki/Azure-Deployment)
for the T-SQL differences, the auto-pause reconnect handling, and the App
Service deployment itself (GitHub Actions CI/CD, the custom domain, and a
couple of real, non-obvious deployment gotchas).

## API reference

| Method | Path            | Description                                      |
|--------|-----------------|---------------------------------------------------|
| GET    | `/api/events`   | List the last 50 lifecycle events with their steps |
| GET    | `/api/events/active` | Events with at least one step still pending, plus each one's next step |
| POST   | `/api/events`   | Create an event directly, no employee linkage or dedup. Demo/testing only. Body: `{"type": "starter\|mover\|leaver", "employee_name": "..."}` |
| POST   | `/api/events/<id>/advance` | Mark that event's next pending step done, in order |
| DELETE | `/api/events`   | Clear all events (reset the demo)                 |
| POST   | `/api/webhooks/hris` | Generic HRIS webhook receiver — signed, deduped, creates/updates employees |
| POST   | `/api/webhooks/bamboohr` | BambooHR's own event-based webhook receiver — different signature scheme, reads the employee back from BambooHR's API before creating the ticket |
| POST   | `/api/dev/simulate-hris` | What Simulate SLAM's Starter station calls — builds and self-signs a webhook payload from a bare name |
| POST   | `/api/dev/simulate-mover` | What Simulate SLAM's Mover station calls — updates a real employee's department and opens a mover ticket for them |
| POST   | `/api/dev/simulate-leaver` | What Simulate SLAM's Leaver station calls — opens a leaver ticket for a real employee without deleting their record |
| GET    | `/api/employees` | List all employees |
| POST   | `/api/employees` | Create an employee directly |
| PUT    | `/api/employees/<id>` | Update an employee |
| DELETE | `/api/employees/<id>` | Delete an employee (blocked if events still reference it) |
| GET    | `/api/systems`  | Current "lit" status of each downstream system     |
| GET    | `/api/health`   | Health check                                       |
| POST   | `/api/demo-login` | Backs `/login.html`'s demo-login form — issues a signed cookie if the submitted email ends in `@joejuice.com` |

## Extending it

- Swap `db.py` for another database by replacing the calls inside its
  sqlite3-compatible shim — the rest of the app doesn't care what's
  underneath, as proven by the SQLite → Azure SQL migration itself.
- Add real integrations by having a step's completion also call an
  actual HRIS/IAM/payroll API, the way the Core HR steps already call
  BambooHR, the Access/IT steps call Microsoft Entra ID (or Okta), the
  Comms steps call Microsoft Teams, the welcome-message step calls
  Twilio and SMTP, and the Snowflake step calls Snowflake's SQL API.
- Add authentication, audit logging, or a "who changed what" trail on
  top of `event_steps` for a closer match to real GDPR/audit requirements.

## More documentation

The [GitHub Wiki](https://github.com/MyProject-2/slam-app/wiki) goes
deeper on architecture, the data model, and each integration — including
vendor-specific gotchas and exact request/response shapes — than this
README does.

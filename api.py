"""
api.py — SLAM API handlers.

Each function implements one API endpoint's business logic and returns
(status_code, payload_dict). Kept independent of the HTTP transport layer
(see server.py) so the handlers can be read, and called, without an
http.server request in play.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db
import workflow

# Shared secret for verifying inbound HRIS webhook signatures (see
# handle_hris_webhook). Set a real value via the HRIS_WEBHOOK_SECRET env
# var in any environment that isn't just this local demo.
HRIS_WEBHOOK_SECRET = os.environ.get("HRIS_WEBHOOK_SECRET", "demo-insecure-secret-change-me")

# Signs the "demo login" cookie (see demo_login/verify_demo_login_cookie) —
# a second, much weaker way into the deployed app alongside real Microsoft
# SSO, offered on /login.html for anyone who can claim a @joejuice.com
# email. Deliberately not a real identity check: nothing confirms the
# person submitting the email actually owns it (no verification email
# sent) — accepted as good enough for a portfolio demo, not a production
# access control. Set via DEMO_LOGIN_SECRET outside local dev.
DEMO_LOGIN_SECRET = os.environ.get("DEMO_LOGIN_SECRET", "demo-insecure-secret-change-me")
DEMO_LOGIN_DOMAIN = "joejuice.com"
DEMO_LOGIN_COOKIE_NAME = "slam_demo_login"
DEMO_LOGIN_TTL_SECONDS = 60 * 60 * 12  # 12 hours

# Twilio credentials for the welcome-message SMS step (see send_sms). All
# three must be set for real sends; otherwise send_sms simulates instead
# of failing, so the demo works without a Twilio account.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")

# Snowflake credentials for the "Synced to Snowflake" step (see
# sync_to_snowflake). All five must be set for a real sync; otherwise it
# simulates instead of failing, so the demo works without a Snowflake
# account. SNOWFLAKE_ACCOUNT is the "<organization>-<account>" identifier
# used in the account URL, e.g. "myorg-myaccount".
SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_PAT = os.environ.get("SNOWFLAKE_PAT")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE")
SNOWFLAKE_SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE")  # optional
SNOWFLAKE_TABLE = os.environ.get("SNOWFLAKE_TABLE", "SLAM_EVENTS")

# BambooHR credentials for the Core HR steps (see sync_to_bamboohr). Both
# must be set for a real sync; otherwise it simulates instead of failing.
BAMBOOHR_SUBDOMAIN = os.environ.get("BAMBOOHR_SUBDOMAIN")
BAMBOOHR_API_KEY = os.environ.get("BAMBOOHR_API_KEY")

# Per-webhook secret shown once in BambooHR under Account Settings >
# Webhooks when the event-based webhook is created — distinct from
# BAMBOOHR_API_KEY, used only to verify handle_bamboohr_webhook's inbound
# signature (see verify_bamboohr_webhook_signature).
BAMBOOHR_WEBHOOK_SECRET = os.environ.get("BAMBOOHR_WEBHOOK_SECRET")

# Okta credentials for the Access/IT steps (see sync_to_okta). Both must
# be set for a real sync; otherwise it simulates instead of failing.
# OKTA_ORG_URL is the full base URL, e.g. "https://dev-12345.okta.com".
OKTA_ORG_URL = os.environ.get("OKTA_ORG_URL")
OKTA_API_TOKEN = os.environ.get("OKTA_API_TOKEN")

# Microsoft Entra ID (Azure AD) credentials — an alternative Access/IT
# integration to Okta (see sync_to_entra). JOE & THE JUICE's own domain
# resolves to Microsoft 365 (confirmed via MX record lookup: mail routes
# to *.mail.protection.outlook.com), making Entra the more realistic
# identity provider for that specific company, which is why this exists
# alongside Okta rather than replacing it outright — both are real,
# tested integrations; advance_step() below is wired to call Entra.
# ENTRA_DOMAIN is a verified domain on the tenant (e.g. the auto-assigned
# "<name>.onmicrosoft.com"), used to build new users' sign-in address.
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID")
ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID")
ENTRA_CLIENT_SECRET = os.environ.get("ENTRA_CLIENT_SECRET")
ENTRA_DOMAIN = os.environ.get("ENTRA_DOMAIN")

# Microsoft Teams / Microsoft 365 Group membership for the Comms steps
# (see sync_to_teams). Reuses the same Entra app registration/OAuth
# token as sync_to_entra — just needs one more Graph permission granted
# to it (GroupMember.ReadWrite.All) plus the target Team's underlying
# Microsoft 365 Group ID.
TEAMS_GROUP_ID = os.environ.get("TEAMS_GROUP_ID")

# SMTP credentials for the welcome email with sign-in instructions (see
# send_welcome_email). All three must be set for a real send; otherwise
# it simulates instead of failing. Uses Python's stdlib smtplib/email
# modules directly — no dependency — over implicit TLS (SMTPS), matching
# one.com's mail setup (send.one.com:465).
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USERNAME


def _sign_demo_login_cookie(email: str) -> str:
    expiry = int(time.time()) + DEMO_LOGIN_TTL_SECONDS
    payload = f"{email}:{expiry}"
    sig = hmac.new(DEMO_LOGIN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_demo_login_cookie(value: str) -> bool:
    """Checked by server.py on every request once app-level auth is live
    — a valid, unexpired, correctly-signed cookie is treated the same as
    a real Microsoft SSO session. See DEMO_LOGIN_SECRET's module comment
    for why this is intentionally a weak check."""
    if not value:
        return False
    parts = value.split(":")
    if len(parts) != 3:
        return False
    email, expiry, sig = parts
    expected = hmac.new(DEMO_LOGIN_SECRET.encode("utf-8"), f"{email}:{expiry}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        return int(expiry) >= int(time.time())
    except ValueError:
        return False


def demo_login(body):
    """Backs /login.html's 'Demo login' option. The only check is whether
    the submitted email ends in @joejuice.com — self-reported, never
    verified (no confirmation email, nothing proves ownership). Issues a
    signed cookie server.py sets on the response; see verify_demo_login_cookie's
    docstring for why this is an accepted, deliberate weak point rather
    than a bug."""
    email = ((body or {}).get("email") or "").strip().lower()
    if not email.endswith(f"@{DEMO_LOGIN_DOMAIN}"):
        return 403, {"error": f"Only @{DEMO_LOGIN_DOMAIN} email addresses can use demo login."}
    return 200, {"ok": True, "cookie_value": _sign_demo_login_cookie(email), "max_age": DEMO_LOGIN_TTL_SECONDS}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_ticket_id(event_type: str) -> str:
    prefix = {"starter": "SL", "mover": "ML", "leaver": "XL"}.get(event_type, "EV")
    return f"{prefix}-{int(time.time() * 1000) % 100000}"


def send_sms(to_number: str, body: str) -> dict:
    """Sends an SMS via Twilio's plain REST API (urllib only — no `twilio`
    package, to keep this app dependency-free). Falls back to a simulated
    send when TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER
    aren't all set, so the welcome-message step still works in a demo
    without a real Twilio account."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return {
            "sent": False, "simulated": True, "to": to_number, "body": body,
            "note": "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER not set — simulated only",
        }

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = urllib.parse.urlencode({"To": to_number, "From": TWILIO_FROM_NUMBER, "Body": body}).encode("utf-8")
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {"sent": True, "simulated": False, "to": to_number, "sid": payload.get("sid")}
    except urllib.error.HTTPError as e:
        return {"sent": False, "simulated": False, "to": to_number, "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"sent": False, "simulated": False, "to": to_number, "error": str(e)}


def sync_to_snowflake(record: dict) -> dict:
    """Pushes a row for this event to Snowflake via the SQL API
    (https://<account>.snowflakecomputing.com/api/v2/statements, plain
    REST + a Programmatic Access Token — no snowflake-connector package,
    keeping this app dependency-free). Falls back to a simulated sync
    when SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT/SNOWFLAKE_DATABASE/
    SNOWFLAKE_SCHEMA/SNOWFLAKE_WAREHOUSE aren't all set. Expects the
    target table (SNOWFLAKE_TABLE, default SLAM_EVENTS) to already exist
    with columns EVENT_ID, TICKET_ID, EVENT_TYPE, EMPLOYEE_ID, HRIS_ID,
    EMPLOYEE_NAME, COUNTRY, SYNCED_AT."""
    required = (SNOWFLAKE_ACCOUNT, SNOWFLAKE_PAT, SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_WAREHOUSE)
    if not all(required):
        return {
            "synced": False, "simulated": True, "record": record,
            "note": "SNOWFLAKE_ACCOUNT/SNOWFLAKE_PAT/SNOWFLAKE_DATABASE/SNOWFLAKE_SCHEMA/SNOWFLAKE_WAREHOUSE not set — simulated only",
        }

    def binding(value, sql_type="TEXT"):
        return {"type": sql_type, "value": "" if value is None else str(value)}

    statement = (
        f"INSERT INTO {SNOWFLAKE_TABLE} "
        "(EVENT_ID, TICKET_ID, EVENT_TYPE, EMPLOYEE_ID, HRIS_ID, EMPLOYEE_NAME, COUNTRY, SYNCED_AT) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP())"
    )
    body = {
        "statement": statement,
        "database": SNOWFLAKE_DATABASE,
        "schema": SNOWFLAKE_SCHEMA,
        "warehouse": SNOWFLAKE_WAREHOUSE,
        "timeout": 30,
        "bindings": {
            "1": binding(record.get("event_id"), "FIXED"),
            "2": binding(record.get("ticket_id")),
            "3": binding(record.get("event_type")),
            "4": binding(record.get("employee_id"), "FIXED"),
            "5": binding(record.get("hris_id")),
            "6": binding(record.get("employee_name")),
            "7": binding(record.get("country")),
        },
    }
    if SNOWFLAKE_ROLE:
        body["role"] = SNOWFLAKE_ROLE

    url = f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com/api/v2/statements"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {SNOWFLAKE_PAT}",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "SLAM/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {"synced": True, "simulated": False, "record": record, "statement_handle": payload.get("statementHandle")}
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "record": record, "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"synced": False, "simulated": False, "record": record, "error": str(e)}


def sync_to_bamboohr(full_name: str, work_email, event_type: str, existing_bamboohr_id):
    """Creates or updates this employee's record in BambooHR — the real
    action behind every pipeline's Core HR step (system_key == "corehr":
    "Core HR record created"/"updated", "Last day confirmed"). Plain REST
    via urllib (no SDK). Falls back to a simulated sync when
    BAMBOOHR_SUBDOMAIN/BAMBOOHR_API_KEY aren't set.

    BambooHR uses POST for both create (/v1/employees) and update
    (/v1/employees/<id>) — not PUT/PATCH, which is non-standard REST but
    is how their API actually works. When existing_bamboohr_id is None
    this creates a new employee and returns their new BambooHR id (parsed
    from the response's Location header) so the caller can persist it;
    otherwise it updates the existing record in place. A "leaver" event
    also sets employmentHistoryStatus to Terminated with today's date."""
    if not (BAMBOOHR_SUBDOMAIN and BAMBOOHR_API_KEY):
        return {
            "synced": False, "simulated": True, "bamboohr_id": existing_bamboohr_id,
            "note": "BAMBOOHR_SUBDOMAIN/BAMBOOHR_API_KEY not set — simulated only",
        }

    name_parts = full_name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else "(unknown)"

    body = {"firstName": first_name, "lastName": last_name}
    if work_email:
        body["workEmail"] = work_email
    if event_type == "leaver":
        body["employmentHistoryStatus"] = "Terminated"
        body["terminationDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    elif existing_bamboohr_id is None:
        body["hireDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if existing_bamboohr_id is None:
        url = f"https://{BAMBOOHR_SUBDOMAIN}.bamboohr.com/api/v1/employees"
    else:
        url = f"https://{BAMBOOHR_SUBDOMAIN}.bamboohr.com/api/v1/employees/{existing_bamboohr_id}"

    auth = base64.b64encode(f"{BAMBOOHR_API_KEY}:x".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            bamboohr_id = existing_bamboohr_id
            if existing_bamboohr_id is None:
                location = resp.headers.get("Location", "")
                bamboohr_id = location.rstrip("/").rsplit("/", 1)[-1] or None
        return {"synced": True, "simulated": False, "bamboohr_id": bamboohr_id}
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "bamboohr_id": existing_bamboohr_id,
                "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"synced": False, "simulated": False, "bamboohr_id": existing_bamboohr_id, "error": str(e)}


def sync_to_okta(full_name: str, email, event_type: str, existing_okta_id):
    """Creates, updates, or deactivates this employee's Okta user account —
    the real action behind every pipeline's Access/IT step (system_key ==
    "accessit": "Access + accounts provisioned"/"re-scoped"/"revoked on
    schedule"). Plain REST via urllib (no SDK). Falls back to a simulated
    sync when OKTA_ORG_URL/OKTA_API_TOKEN aren't set.

    starter (existing_okta_id is None) -> POST /api/v1/users?activate=true,
    creating the account without setting a password (Okta handles
    activation/invite) — returns the new user's id from the response body.
    mover (existing_okta_id set) -> POST /api/v1/users/<id>, a partial
    profile update (Okta merges — unspecified fields are left alone).
    leaver -> POST /api/v1/users/<id>/lifecycle/deactivate, no body.

    Okta requires a valid email for both the profile.email and
    profile.login fields, so this is skipped (not simulated) when no
    email is on file for the employee."""
    if not (OKTA_ORG_URL and OKTA_API_TOKEN):
        return {
            "synced": False, "simulated": True, "okta_id": existing_okta_id,
            "note": "OKTA_ORG_URL/OKTA_API_TOKEN not set — simulated only",
        }
    if not email:
        return {"synced": False, "simulated": False, "okta_id": existing_okta_id,
                "note": "No email on file for this employee — Okta requires one, skipped."}

    name_parts = full_name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else "(unknown)"

    headers = {
        "Authorization": f"SSWS {OKTA_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    org = OKTA_ORG_URL.rstrip("/")

    if event_type == "leaver" and existing_okta_id:
        url = f"{org}/api/v1/users/{existing_okta_id}/lifecycle/deactivate"
        req = urllib.request.Request(url, data=b"", method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            return {"synced": True, "simulated": False, "okta_id": existing_okta_id, "status": "deactivated"}
        except urllib.error.HTTPError as e:
            return {"synced": False, "simulated": False, "okta_id": existing_okta_id,
                    "error": e.read().decode("utf-8", errors="replace")}
        except Exception as e:
            return {"synced": False, "simulated": False, "okta_id": existing_okta_id, "error": str(e)}

    profile = {"firstName": first_name, "lastName": last_name, "email": email}
    if existing_okta_id is None:
        profile["login"] = email
        url = f"{org}/api/v1/users?activate=true"
        body = {"profile": profile}
    else:
        url = f"{org}/api/v1/users/{existing_okta_id}"
        body = {"profile": profile}

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        okta_id = payload.get("id", existing_okta_id)
        return {"synced": True, "simulated": False, "okta_id": okta_id}
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "okta_id": existing_okta_id,
                "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"synced": False, "simulated": False, "okta_id": existing_okta_id, "error": str(e)}


def _get_entra_token() -> str:
    """Requests a Microsoft Graph access token via the OAuth 2.0
    client-credentials flow. Fetched fresh on every call rather than
    cached — sync calls here are infrequent (one per manual pipeline-step
    advance), so token caching/refresh isn't worth the added complexity
    for this demo. Raises on failure; callers catch it."""
    url = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": ENTRA_CLIENT_ID,
        "client_secret": ENTRA_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["access_token"]


def _generate_entra_password() -> str:
    """A random password satisfying Entra's default policy (>=8 chars,
    at least 3 of 4 character classes: upper/lower/digit/symbol) —
    generated fresh per new hire. Exists because Graph requires *some*
    password at account-creation time, unlike Okta's invite-only flow.
    Never stored or logged: sync_to_entra() hands it back to
    advance_step() exactly once (via the "_temp_password" key, popped
    immediately) so send_welcome_email() can deliver it to the new
    hire — the account's forceChangePasswordNextSignIn flag means this
    value only works for that one first sign-in anyway."""
    classes = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    chars = [secrets.choice(c) for c in classes]
    chars += [secrets.choice(string.ascii_letters + string.digits) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def sync_to_entra(full_name: str, email, event_type: str, existing_entra_id):
    """Creates, updates, or deactivates this employee's Microsoft Entra ID
    account — the real action behind every pipeline's Access/IT step when
    Entra is configured (see the module-level comment on ENTRA_TENANT_ID
    for why this exists alongside sync_to_okta). Plain REST via urllib:
    an OAuth 2.0 client-credentials token from login.microsoftonline.com,
    then calls against Microsoft Graph (graph.microsoft.com/v1.0/users).
    Falls back to a simulated sync when ENTRA_TENANT_ID/ENTRA_CLIENT_ID/
    ENTRA_CLIENT_SECRET/ENTRA_DOMAIN aren't all set.

    Unlike Okta, Graph requires a password at creation (see
    _generate_entra_password) and requires the sign-in address
    (userPrincipalName) to be on a domain the tenant has verified
    (ENTRA_DOMAIN) — the employee's real email goes in the separate
    "mail" field instead. Create returns 201 with the new user object;
    update/deactivate (PATCH) return 204 with no body."""
    required = (ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, ENTRA_DOMAIN)
    if not all(required):
        return {
            "synced": False, "simulated": True, "entra_id": existing_entra_id,
            "note": "ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET/ENTRA_DOMAIN not set — simulated only",
        }
    if not email:
        return {"synced": False, "simulated": False, "entra_id": existing_entra_id,
                "note": "No email on file for this employee — Entra requires one, skipped."}

    try:
        token = _get_entra_token()
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "entra_id": existing_entra_id,
                "error": f"token request failed: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"synced": False, "simulated": False, "entra_id": existing_entra_id, "error": f"token request failed: {e}"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    mail_nickname = re.sub(r"[^a-zA-Z0-9.]+", ".", full_name.strip().lower()).strip(".") or "employee"

    if event_type == "leaver" and existing_entra_id:
        url = f"https://graph.microsoft.com/v1.0/users/{existing_entra_id}"
        req = urllib.request.Request(url, data=json.dumps({"accountEnabled": False}).encode("utf-8"),
                                      method="PATCH", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            return {"synced": True, "simulated": False, "entra_id": existing_entra_id, "status": "deactivated"}
        except urllib.error.HTTPError as e:
            return {"synced": False, "simulated": False, "entra_id": existing_entra_id,
                    "error": e.read().decode("utf-8", errors="replace")}
        except Exception as e:
            return {"synced": False, "simulated": False, "entra_id": existing_entra_id, "error": str(e)}

    new_upn = f"{mail_nickname}@{ENTRA_DOMAIN}"
    generated_password = None

    if existing_entra_id is None:
        url = "https://graph.microsoft.com/v1.0/users"
        method = "POST"
        generated_password = _generate_entra_password()
        body = {
            "accountEnabled": True,
            "displayName": full_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": new_upn,
            "mail": email,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": generated_password,
            },
        }
    else:
        url = f"https://graph.microsoft.com/v1.0/users/{existing_entra_id}"
        method = "PATCH"
        body = {"displayName": full_name, "mail": email}

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            entra_id = json.loads(raw).get("id", existing_entra_id) if raw else existing_entra_id
        result = {"synced": True, "simulated": False, "entra_id": entra_id}
        # Only present on a fresh create — advance_step() pops these two
        # out immediately to hand off to send_welcome_email() and never
        # lets them reach a log line or an HTTP response. See
        # _generate_entra_password's docstring for why this is safe: the
        # password is otherwise never persisted anywhere.
        if generated_password:
            result["_temp_password"] = generated_password
            result["_upn"] = new_upn
        return result
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "entra_id": existing_entra_id,
                "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"synced": False, "simulated": False, "entra_id": existing_entra_id, "error": str(e)}


def sync_to_teams(entra_id, event_type):
    """Adds or removes this employee from a Microsoft Teams / Microsoft
    365 Group — the real action behind the Comms steps ("Comms groups
    added" on starter, "Comms access removed" on leaver; there's no
    Comms step for mover). Reuses the Entra app registration's OAuth
    token (_get_entra_token) rather than a separate credential set — it
    only needs one more Graph permission (GroupMember.ReadWrite.All)
    granted to that same app. Falls back to a simulated sync when Entra
    credentials or TEAMS_GROUP_ID aren't all set, or skips (not
    simulated) when the employee has no entra_id yet to add/remove.

    The remove call deliberately keeps the '/$ref' suffix on the URL:
    per Microsoft's own docs, an app with both GroupMember.ReadWrite.All
    and User.ReadWrite.All (which this one has, from sync_to_entra)
    would delete the *user* outright if that suffix were left off,
    instead of just removing them from the group."""
    if not (ENTRA_TENANT_ID and ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET and TEAMS_GROUP_ID):
        return {
            "synced": False, "simulated": True,
            "note": "ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET/TEAMS_GROUP_ID not set — simulated only",
        }
    if not entra_id:
        return {"synced": False, "simulated": False,
                "note": "No Entra ID account for this employee yet — nothing to add/remove."}

    try:
        token = _get_entra_token()
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False,
                "error": f"token request failed: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"synced": False, "simulated": False, "error": f"token request failed: {e}"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    if event_type == "leaver":
        url = f"https://graph.microsoft.com/v1.0/groups/{TEAMS_GROUP_ID}/members/{entra_id}/$ref"
        req = urllib.request.Request(url, method="DELETE", headers=headers)
        action = "removed"
    else:
        url = f"https://graph.microsoft.com/v1.0/groups/{TEAMS_GROUP_ID}/members/$ref"
        body = {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{entra_id}"}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        action = "added"

    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        return {"synced": True, "simulated": False, "status": action}
    except urllib.error.HTTPError as e:
        return {"synced": False, "simulated": False, "error": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"synced": False, "simulated": False, "error": str(e)}


def send_welcome_email(to_email: str, full_name: str, upn: str, temp_password: str) -> dict:
    """Sends a real welcome email with sign-in instructions and a
    one-time temporary password, via plain SMTP (Python's stdlib
    smtplib/email — no dependency) over implicit TLS. This is the
    delivery mechanism the Entra integration was otherwise missing:
    sync_to_entra() generates a random password so Graph will create
    the account, but that value is useless to the actual new hire
    unless it reaches them somehow — this is the one legitimate place
    it's used, sent directly to them and nowhere else (never logged,
    never included in advance_step()'s HTTP response).

    Falls back to a simulated send when SMTP_HOST/SMTP_USERNAME/
    SMTP_PASSWORD aren't all set, so the demo works without a real
    mailbox. Uses SMTP_SSL (implicit TLS on connect) rather than
    STARTTLS, matching one.com's mail setup (send.one.com:465)."""
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        return {
            "sent": False, "simulated": True, "to": to_email,
            "note": "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD not set — simulated only",
        }
    if not to_email:
        return {"sent": False, "simulated": False, "to": to_email,
                "note": "No email on file for this employee — skipped."}

    msg = MIMEMultipart()
    msg["Subject"] = "Welcome to JOE & THE JUICE — set up your account"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(
        f"Hi {full_name},\n\n"
        f"Welcome to JOE & THE JUICE! Your account has been created.\n\n"
        f"Sign in at https://www.office.com using:\n"
        f"  Username: {upn}\n"
        f"  Temporary password: {temp_password}\n\n"
        f"You'll be asked to choose your own password the first time you sign in.\n\n"
        f"— SLAM (automated)",
        "plain",
    ))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return {"sent": True, "simulated": False, "to": to_email}
    except Exception as e:
        return {"sent": False, "simulated": False, "to": to_email, "error": str(e)}


def send_generic_welcome_email(to_email: str, full_name: str) -> dict:
    """Sends a plain welcome note to the new hire's personal email — the
    real action behind the starter pipeline's "Welcome message sent" step,
    alongside the Twilio SMS (see send_sms). Distinct from
    send_welcome_email(): this fires much earlier in the pipeline, before
    Access has provisioned any account, so there's no sign-in info to
    include yet — just a greeting. Falls back to a simulated send when
    SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD aren't all set."""
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD):
        return {
            "sent": False, "simulated": True, "to": to_email,
            "note": "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD not set — simulated only",
        }
    if not to_email:
        return {"sent": False, "simulated": False, "to": to_email,
                "note": "No personal email on file for this employee — skipped."}

    msg = MIMEMultipart()
    msg["Subject"] = "Welcome to JOE & THE JUICE!"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(
        f"Hi {full_name},\n\n"
        f"Welcome to JOE & THE JUICE! We're excited to have you join us — your onboarding is underway.\n\n"
        f"— SLAM (automated)",
        "plain",
    ))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return {"sent": True, "simulated": False, "to": to_email}
    except Exception as e:
        return {"sent": False, "simulated": False, "to": to_email, "error": str(e)}


def resolve_contact_channel(conn, employee_id):
    """Personal contact info before Access is provisioned; company contact
    info after, if the employee has one — otherwise stays on personal
    permanently for staff who never get a company account. Looks at
    whether this employee's most recent 'accessit' step is done, since
    that's the real-world moment company accounts start existing."""
    employee = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if employee is None:
        return None

    access_step = conn.execute(
        "SELECT TOP 1 es.done FROM event_steps es JOIN events e ON es.event_id = e.id "
        "WHERE e.employee_id = ? AND es.system_key = 'accessit' ORDER BY es.id DESC",
        (employee_id,),
    ).fetchone()
    access_provisioned = bool(access_step and access_step["done"])

    if access_provisioned and employee["company_phone"]:
        return {"channel": "company", "phone": employee["company_phone"], "email": employee["company_email"]}
    return {"channel": "personal", "phone": employee["personal_phone"], "email": employee["personal_email"]}


def _insert_event(conn, event_type, employee_name, employee_id=None):
    """Shared by create_event() and handle_hris_webhook() — writes the
    events row plus one event_steps row per step in that type's pipeline."""
    steps_meta = workflow.PIPELINES[event_type]
    ticket_id = make_ticket_id(event_type)
    cur = conn.execute(
        "INSERT INTO events (ticket_id, type, employee_name, employee_id, status, created_at) "
        "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, 'processing', ?)",
        (ticket_id, event_type, employee_name, employee_id, now_iso()),
    )
    event_id = cur.fetchone()[0]
    for idx, step in enumerate(steps_meta):
        conn.execute(
            "INSERT INTO event_steps (event_id, step_index, label, system_key, is_gdpr_flag, is_welcome_message, done) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (event_id, idx, step["label"], step.get("system"),
             int(step.get("gdpr", False)), int(step.get("welcome_message", False))),
        )
    return event_id, ticket_id


def list_events():
    conn = db.get_conn()
    with db.get_lock():
        events = conn.execute(
            "SELECT TOP 50 * FROM events ORDER BY id DESC"
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
                "employee_id": ev["employee_id"],
                "employee_name": ev["employee_name"],
                "status": ev["status"],
                "created_at": ev["created_at"],
                "steps": [
                    {
                        "label": s["label"],
                        "system": s["system_key"],
                        "gdpr": bool(s["is_gdpr_flag"]),
                        "welcome_message": bool(s["is_welcome_message"]),
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
                "employee_id": ev["employee_id"],
                "employee_name": ev["employee_name"],
                "created_at": ev["created_at"],
                "steps": [
                    {
                        "label": s["label"],
                        "system": s["system_key"],
                        "gdpr": bool(s["is_gdpr_flag"]),
                        "welcome_message": bool(s["is_welcome_message"]),
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
    contact = None
    employee_for_welcome_email = None
    employee_for_sync = None
    employee_for_bamboohr = None
    employee_for_access = None
    employee_for_comms = None
    with db.get_lock():
        ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if ev is None:
            return 404, {"error": "Event not found"}

        next_step = conn.execute(
            "SELECT TOP 1 * FROM event_steps WHERE event_id = ? AND done = 0 ORDER BY step_index",
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

        if next_step["is_welcome_message"] and ev["employee_id"] is not None:
            contact = resolve_contact_channel(conn, ev["employee_id"])
            employee_for_welcome_email = conn.execute(
                "SELECT personal_email FROM employees WHERE id = ?", (ev["employee_id"],)
            ).fetchone()

        if next_step["system_key"] == "snowflake" and ev["employee_id"] is not None:
            employee_for_sync = conn.execute(
                "SELECT hris_id, country FROM employees WHERE id = ?", (ev["employee_id"],)
            ).fetchone()

        if next_step["system_key"] == "corehr" and ev["employee_id"] is not None:
            employee_for_bamboohr = conn.execute(
                "SELECT company_email, personal_email, bamboohr_id FROM employees WHERE id = ?",
                (ev["employee_id"],),
            ).fetchone()

        if next_step["system_key"] == "accessit" and ev["employee_id"] is not None:
            employee_for_access = conn.execute(
                "SELECT company_email, personal_email, entra_id FROM employees WHERE id = ?",
                (ev["employee_id"],),
            ).fetchone()

        if next_step["system_key"] == "comms" and ev["employee_id"] is not None:
            employee_for_comms = conn.execute(
                "SELECT entra_id FROM employees WHERE id = ?", (ev["employee_id"],)
            ).fetchone()

        conn.commit()

    response = {"event_id": event_id, "advanced_step": next_step["label"], "remaining": remaining}

    # Sending the SMS / syncing to Snowflake are network calls — do them
    # after releasing the lock so a slow/unreachable service doesn't stall
    # every other request.
    if next_step["is_welcome_message"]:
        if ev["employee_id"] is None:
            response["welcome_message"] = {"sent": False, "note": "No employee record linked to this ticket — nothing to message."}
        elif not contact or not contact["phone"]:
            response["welcome_message"] = {"sent": False, "note": "No phone number on file for this employee — skipped."}
        else:
            result = send_sms(contact["phone"], f"Welcome to JOE & THE JUICE, {ev['employee_name']}! Your onboarding is underway.")
            result["channel"] = contact["channel"]
            response["welcome_message"] = result
            print(f"[welcome-message] {result}")

        if ev["employee_id"] is not None:
            personal_email = employee_for_welcome_email["personal_email"] if employee_for_welcome_email else None
            email_result = send_generic_welcome_email(personal_email, ev["employee_name"])
            response["welcome_message_email"] = email_result
            print(f"[welcome-message-email] sent={email_result.get('sent')} simulated={email_result.get('simulated')} to={email_result.get('to')}")

    if next_step["system_key"] == "snowflake":
        record = {
            "event_id": ev["id"],
            "ticket_id": ev["ticket_id"],
            "event_type": ev["type"],
            "employee_id": ev["employee_id"],
            "hris_id": employee_for_sync["hris_id"] if employee_for_sync else None,
            "employee_name": ev["employee_name"],
            "country": employee_for_sync["country"] if employee_for_sync else None,
        }
        result = sync_to_snowflake(record)
        response["snowflake_sync"] = result
        print(f"[snowflake-sync] {result}")

    if next_step["system_key"] == "corehr":
        if ev["employee_id"] is None:
            response["bamboohr_sync"] = {"synced": False, "note": "No employee record linked to this ticket — nothing to sync."}
        else:
            work_email = (employee_for_bamboohr["company_email"] or employee_for_bamboohr["personal_email"]) if employee_for_bamboohr else None
            existing_bamboohr_id = employee_for_bamboohr["bamboohr_id"] if employee_for_bamboohr else None
            result = sync_to_bamboohr(ev["employee_name"], work_email, ev["type"], existing_bamboohr_id)
            if result.get("bamboohr_id") and result["bamboohr_id"] != existing_bamboohr_id:
                with db.get_lock():
                    conn.execute(
                        "UPDATE employees SET bamboohr_id = ? WHERE id = ?",
                        (result["bamboohr_id"], ev["employee_id"]),
                    )
                    conn.commit()
            response["bamboohr_sync"] = result
            print(f"[bamboohr-sync] {result}")

    if next_step["system_key"] == "accessit":
        if ev["employee_id"] is None:
            response["entra_sync"] = {"synced": False, "note": "No employee record linked to this ticket — nothing to sync."}
        else:
            work_email = (employee_for_access["company_email"] or employee_for_access["personal_email"]) if employee_for_access else None
            existing_entra_id = employee_for_access["entra_id"] if employee_for_access else None
            result = sync_to_entra(ev["employee_name"], work_email, ev["type"], existing_entra_id)
            # Pop the temp password/UPN out immediately — everything from
            # here on (the print below, response["entra_sync"]) must never
            # see them. See send_welcome_email()'s docstring.
            temp_password = result.pop("_temp_password", None)
            new_upn = result.pop("_upn", None)
            if result.get("entra_id") and result["entra_id"] != existing_entra_id:
                with db.get_lock():
                    conn.execute(
                        "UPDATE employees SET entra_id = ? WHERE id = ?",
                        (result["entra_id"], ev["employee_id"]),
                    )
                    conn.commit()
            response["entra_sync"] = result
            print(f"[entra-sync] {result}")

            if temp_password:
                email_result = send_welcome_email(work_email, ev["employee_name"], new_upn, temp_password)
                response["welcome_email"] = email_result
                print(f"[welcome-email] sent={email_result.get('sent')} simulated={email_result.get('simulated')} to={email_result.get('to')}")

    if next_step["system_key"] == "comms":
        if ev["employee_id"] is None:
            response["teams_sync"] = {"synced": False, "note": "No employee record linked to this ticket — nothing to sync."}
        else:
            existing_entra_id = employee_for_comms["entra_id"] if employee_for_comms else None
            result = sync_to_teams(existing_entra_id, ev["type"])
            response["teams_sync"] = result
            print(f"[teams-sync] {result}")

    return 200, response


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
    the employee already exists in SLAM.

    Each email value is checked against *both* email columns, not just
    its same-named one — different event sources put the same real
    address in different columns (e.g. the BambooHR webhook derives
    company_email from workEmail for a person another source already
    recorded under personal_email), and checking same-column-only missed
    that as a duplicate rather than an update (found via a real duplicate
    "Barack Obama" record created this way)."""
    if hris_id:
        row = conn.execute("SELECT * FROM employees WHERE hris_id = ?", (hris_id,)).fetchone()
        if row:
            return row
    for email in (personal_email, company_email):
        if not email:
            continue
        row = conn.execute(
            "SELECT * FROM employees WHERE personal_email = ? OR company_email = ?", (email, email)
        ).fetchone()
        if row:
            return row
    return None


def _ingest_lifecycle_event(event_type: str, employee: dict):
    """Shared core behind every lifecycle-event entry point — the generic
    HRIS webhook (handle_hris_webhook), the BambooHR-originated webhook
    (handle_bamboohr_webhook), and the demo simulate button
    (simulate_hris_event) all normalize into this same `employee` shape
    (hris_id, full_name, country, department, personal_email,
    personal_phone, company_email, company_phone) and hand off here to
    create/update the employees row and open a new ticket."""
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
                "INSERT INTO employees (hris_id, full_name, country, department, personal_email, personal_phone, company_email, company_phone) "
                "OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hris_id, full_name, employee.get("country"), employee.get("department"), personal_email,
                 employee.get("personal_phone"), company_email, employee.get("company_phone")),
            )
            employee_id = cur.fetchone()[0]
        else:
            if existing is None:
                return 404, {
                    "error": f"No existing employee found for a {event_type} event. "
                             "The HRIS should send a starter event for this person first.",
                }
            employee_id = existing["id"]
            # Keep contact info/country/department current — the HRIS is the source of truth.
            conn.execute(
                "UPDATE employees SET full_name = ?, country = ?, department = ?, personal_email = ?, personal_phone = ?, "
                "company_email = ?, company_phone = ? WHERE id = ?",
                (full_name, employee.get("country"), employee.get("department"), personal_email, employee.get("personal_phone"),
                 company_email, employee.get("company_phone"), employee_id),
            )

        event_id, ticket_id = _insert_event(conn, event_type, full_name, employee_id=employee_id)
        conn.commit()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": event_type, "employee_id": employee_id}


def handle_hris_webhook(raw_body: bytes, signature_header: str, body: dict):
    """Receives lifecycle events pushed from the HRIS (system of record)
    instead of SLAM initiating them. Expected payload:
        {
          "event_type": "starter" | "mover" | "leaver",
          "employee": {
            "hris_id": "EMP-1234", "full_name": "...", "country": "DK",
            "department": "Operations", "personal_email": "...",
            "personal_phone": "...", "company_email": "...", "company_phone": "..."
          }
        }
    """
    if not verify_webhook_signature(raw_body, signature_header):
        return 401, {"error": "Invalid or missing webhook signature"}

    event_type = (body or {}).get("event_type")
    employee = (body or {}).get("employee") or {}
    return _ingest_lifecycle_event(event_type, employee)


def verify_bamboohr_webhook_signature(raw_body: bytes, timestamp_header: str, signature_header: str) -> bool:
    """Verifies BambooHR's own webhook signature scheme — distinct from
    verify_webhook_signature's GitHub/Stripe-style 'sha256=<hex>' over the
    body alone. BambooHR HMACs the raw body concatenated with the
    X-BambooHR-Timestamp header value, using the per-webhook secret shown
    once in Account Settings > Webhooks at creation time
    (BAMBOOHR_WEBHOOK_SECRET) — not BAMBOOHR_API_KEY, which is a
    different credential for a different direction of traffic."""
    if not (timestamp_header and signature_header and BAMBOOHR_WEBHOOK_SECRET):
        return False
    expected = hmac.new(
        BAMBOOHR_WEBHOOK_SECRET.encode("utf-8"),
        raw_body + timestamp_header.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _fetch_bamboohr_employee(bamboohr_employee_id: str) -> dict:
    """Reads back an employee's profile fields from BambooHR's API — the
    follow-up call handle_bamboohr_webhook needs because BambooHR's
    event-based employee.created payload carries only the employeeId, no
    actual field data. Reuses the same Basic-auth scheme as
    sync_to_bamboohr."""
    fields = "firstName,lastName,workEmail,homeEmail,mobilePhone,department,country"
    url = f"https://{BAMBOOHR_SUBDOMAIN}.bamboohr.com/api/v1/employees/{bamboohr_employee_id}?fields={fields}"
    auth = base64.b64encode(f"{BAMBOOHR_API_KEY}:x".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle_bamboohr_webhook(raw_body: bytes, timestamp_header: str, signature_header: str, body: dict):
    """Receives BambooHR's own event-based webhook directly — BambooHR is
    the trigger here instead of a separate upstream HRIS pushing to
    handle_hris_webhook. Configured in BambooHR under Account Settings >
    Webhooks as an Event-Based webhook, JSON encoding, subscribed to
    employee.created, pointed at this endpoint's public URL.

    employee.created's payload only carries {"data": {"companyId",
    "employeeId"}} — no field data — so this reads the rest back via
    _fetch_bamboohr_employee before handing off to the same
    _ingest_lifecycle_event() core every other event source uses, always
    as a "starter". Only employee.created is handled; BambooHR's
    employee.updated/employee.deleted events aren't wired to mover/leaver
    (not built yet)."""
    if not verify_bamboohr_webhook_signature(raw_body, timestamp_header, signature_header):
        return 401, {"error": "Invalid or missing BambooHR webhook signature"}

    event_kind = (body or {}).get("type")
    if event_kind != "employee.created":
        return 200, {"ignored": True, "reason": f"Event type '{event_kind}' not handled — only employee.created is wired up."}

    bamboohr_employee_id = ((body or {}).get("data") or {}).get("employeeId")
    if not bamboohr_employee_id:
        return 400, {"error": "Missing data.employeeId in BambooHR webhook payload"}

    try:
        record = _fetch_bamboohr_employee(bamboohr_employee_id)
    except urllib.error.HTTPError as e:
        return 502, {"error": f"Could not read employee back from BambooHR: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return 502, {"error": f"Could not read employee back from BambooHR: {e}"}

    first = (record.get("firstName") or "").strip()
    last = (record.get("lastName") or "").strip()
    full_name = f"{first} {last}".strip() or None

    employee = {
        "hris_id": f"BAMBOO-{bamboohr_employee_id}",
        "full_name": full_name,
        "country": record.get("country") or None,
        "department": record.get("department") or None,
        "personal_email": record.get("homeEmail") or None,
        "personal_phone": record.get("mobilePhone") or None,
        "company_email": record.get("workEmail") or None,
        "company_phone": None,
    }

    status, payload = _ingest_lifecycle_event("starter", employee)
    if status == 201:
        payload["bamboohr_id"] = bamboohr_employee_id
        with db.get_lock():
            conn = db.get_conn()
            # This employee already exists in BambooHR — that's where the event
            # came from — so record bamboohr_id right away. Otherwise the Core HR
            # pipeline step would see existing_bamboohr_id=None and POST a
            # duplicate record instead of updating this one.
            conn.execute(
                "UPDATE employees SET bamboohr_id = ? WHERE id = ?",
                (bamboohr_employee_id, payload["employee_id"]),
            )
            # The "Core HR record created" step is auto-completed here — and
            # only here, not for any other event source — because BambooHR
            # itself is the origin of this ticket: the record this step exists
            # to confirm was already created before SLAM ever heard about it.
            # Every other step still requires a manual advance.
            conn.execute(
                "UPDATE event_steps SET done = 1, done_at = ? WHERE event_id = ? AND system_key = 'corehr'",
                (now_iso(), payload["id"]),
            )
            conn.commit()
    return status, payload


def _slugify_hris_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return f"SIM-{slug}" if slug else "SIM-unknown"


def simulate_hris_event(body):
    """Backs the homepage 'Simulate new starter/mover/leaver' buttons.
    Builds a realistic HRIS webhook payload from just a name + event type,
    signs it with our own HRIS_WEBHOOK_SECRET, and feeds it through
    handle_hris_webhook() — the exact same path a real HRIS integration
    would exercise (signature check, dedup, employee linkage), rather than
    a separate shortcut around it. The name is deterministically turned
    into a fake hris_id so firing 'starter' twice for the same name
    demonstrates the dedup rejection, and firing 'mover'/'leaver' for a
    name that never had a starter demonstrates the unknown-employee 404."""
    body = body or {}
    event_type = body.get("type")
    employee_name = (body.get("employee_name") or "").strip()

    if not employee_name:
        return 400, {"error": "employee_name is required"}
    if event_type not in workflow.PIPELINES:
        return 400, {"error": f"Unknown event type '{event_type}'. Use starter, mover, or leaver."}

    local_part = re.sub(r"[^a-z0-9.]+", ".", employee_name.strip().lower()).strip(".")
    payload = {
        "event_type": event_type,
        "employee": {
            "hris_id": _slugify_hris_id(employee_name),
            "full_name": employee_name,
            "personal_email": f"{local_part}@personal.example",
            # 555-01xx is reserved for fictional use (US) — never a real subscriber,
            # so the welcome-message SMS step has something to resolve/simulate against.
            "personal_phone": "+15555550100",
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(HRIS_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return handle_hris_webhook(raw_body, signature, payload)


def simulate_mover_event(body):
    """Backs the Simulate SLAM page's mover flow: unlike simulate_hris_event()
    (which derives a fake identity from a typed name), this targets a real,
    already-selected employee_id directly — no name-matching/dedup guessing
    involved, since the employee was picked from a live search of actual
    records. Updates that employee's department immediately and opens a new
    mover ticket for them, bypassing the HRIS-webhook simulation path
    entirely (there's no HRIS payload to simulate — this is SLAM-initiated,
    like the Employees CRUD page)."""
    body = body or {}
    employee_id = body.get("employee_id")
    new_department = (body.get("department") or "").strip()

    if not employee_id:
        return 400, {"error": "employee_id is required"}
    if not new_department:
        return 400, {"error": "department is required"}

    conn = db.get_conn()
    with db.get_lock():
        employee = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if employee is None:
            return 404, {"error": "Employee not found"}

        conn.execute("UPDATE employees SET department = ? WHERE id = ?", (new_department, employee_id))
        event_id, ticket_id = _insert_event(conn, "mover", employee["full_name"], employee_id=employee_id)
        conn.commit()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": "mover", "employee_id": employee_id}


def simulate_leaver_event(body):
    """Backs the Simulate SLAM page's leaver flow — same pattern as
    simulate_mover_event(): targets a real, already-selected employee_id
    directly (picked via live search), no name-derived identity guessing.
    Only opens a leaver ticket; does not delete the employee record —
    a real HR system keeps a departed employee's record for compliance/
    audit, it doesn't erase them (the existing DELETE /api/employees/<id>
    is the separate, explicit way to actually remove a record, and it's
    blocked while any events still reference that employee)."""
    body = body or {}
    employee_id = body.get("employee_id")
    if not employee_id:
        return 400, {"error": "employee_id is required"}

    conn = db.get_conn()
    with db.get_lock():
        employee = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if employee is None:
            return 404, {"error": "Employee not found"}

        event_id, ticket_id = _insert_event(conn, "leaver", employee["full_name"], employee_id=employee_id)
        conn.commit()

    return 201, {"id": event_id, "ticket_id": ticket_id, "type": "leaver", "employee_id": employee_id}


_EMPLOYEE_FIELDS = ("hris_id", "full_name", "country", "department", "personal_email", "personal_phone", "company_email", "company_phone")


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
                f"INSERT INTO employees ({', '.join(_EMPLOYEE_FIELDS)}) OUTPUT INSERTED.id VALUES ({', '.join('?' * len(_EMPLOYEE_FIELDS))})",
                values,
            )
            new_id = cur.fetchone()[0]
        except db.IntegrityError:
            return 409, {"error": f"An employee with hris_id '{body.get('hris_id')}' already exists"}
        conn.commit()
        row = conn.execute("SELECT * FROM employees WHERE id = ?", (new_id,)).fetchone()

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
        except db.IntegrityError:
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

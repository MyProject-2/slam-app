"""
server.py — SLAM demo application server.

A small REST API + static file server built entirely on Python's standard
library (http.server, sqlite3) — no external dependencies to install, so
it runs anywhere Python 3 runs. All API business logic lives in api.py;
this file is just HTTP routing, request parsing, and static file serving.

Run:
    python3 server.py
Then open:
    http://localhost:8000
"""

import http.cookies
import json
import mimetypes
import os
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import api
import db

# Use an absolute path, resolved the same way regardless of OS.
# (A relative path here caused a Windows-only bug where the static-file
# safety check below would always incorrectly fail — see do_GET.)
PUBLIC_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))
PORT = int(os.environ.get("PORT", 8000))

ADVANCE_RE = re.compile(r"^/api/events/(\d+)/advance$")
EMPLOYEE_RE = re.compile(r"^/api/employees/(\d+)$")

# Only enforce the app-level login gate when actually running on Azure App
# Service (which always sets this) — local dev (`python server.py`) stays
# open, same as it's always been, since Easy Auth doesn't exist locally
# to have redirected anyone there in the first place.
ENFORCE_AUTH = bool(os.environ.get("WEBSITE_SITE_NAME"))

# Reachable without being logged in: the login page itself, the demo-login
# endpoint it calls, and the two real webhook receivers (BambooHR/HRIS),
# which authenticate their callers via their own signature checks instead
# — a real HRIS/BambooHR obviously can't complete a browser SSO flow.
PUBLIC_PATHS = {"/login.html", "/api/demo-login", "/api/webhooks/hris", "/api/webhooks/bamboohr"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep console quiet; comment out to debug

    def _is_authenticated(self):
        # Real Microsoft SSO: when Easy Auth's own global enforcement is
        # off (see authsettingsV2's globalValidation.requireAuthentication),
        # it still validates and injects this header for anyone who
        # already has a valid session from going through /.auth/login/aad
        # — and the App Service platform strips any client-supplied copy
        # of this header before adding its own, so it can't be spoofed by
        # a request from outside Azure's own front end.
        if self.headers.get("X-MS-CLIENT-PRINCIPAL-NAME"):
            return True
        # Demo login: a signed cookie from POST /api/demo-login.
        cookies = http.cookies.SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        demo_cookie = cookies.get(api.DEMO_LOGIN_COOKIE_NAME)
        return bool(demo_cookie and api.verify_demo_login_cookie(demo_cookie.value))

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

        if ENFORCE_AUTH and path not in PUBLIC_PATHS and not self._is_authenticated():
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.end_headers()
            return

        if path == "/api/events":
            status, payload = api.list_events()
            self._send_json(status, payload)
            return
        if path == "/api/events/active":
            status, payload = api.list_active_events()
            self._send_json(status, payload)
            return
        if path == "/api/systems":
            status, payload = api.systems_status()
            self._send_json(status, payload)
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True, "time": api.now_iso()})
            return
        if path == "/api/employees":
            status, payload = api.list_employees()
            self._send_json(status, payload)
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

        if parsed.path == "/api/demo-login":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.demo_login(body)
            if status != 200:
                self._send_json(status, payload)
                return
            cookie_value = payload.pop("cookie_value")
            max_age = payload.pop("max_age")
            secure_flag = "; Secure" if ENFORCE_AUTH else ""
            body_bytes = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header(
                "Set-Cookie",
                f"{api.DEMO_LOGIN_COOKIE_NAME}={cookie_value}; Path=/; Max-Age={max_age}; HttpOnly{secure_flag}; SameSite=Lax",
            )
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        if ENFORCE_AUTH and parsed.path not in PUBLIC_PATHS and not self._is_authenticated():
            self._send_json(401, {"error": "Not authenticated"})
            return

        if parsed.path == "/api/events":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.create_event(body)
            self._send_json(status, payload)
            return

        if parsed.path == "/api/dev/simulate-hris":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.simulate_hris_event(body)
            self._send_json(status, payload)
            return

        if parsed.path == "/api/dev/simulate-mover":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.simulate_mover_event(body)
            self._send_json(status, payload)
            return

        if parsed.path == "/api/dev/simulate-leaver":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.simulate_leaver_event(body)
            self._send_json(status, payload)
            return

        m = ADVANCE_RE.match(parsed.path)
        if m:
            status, payload = api.advance_step(int(m.group(1)))
            self._send_json(status, payload)
            return

        if parsed.path == "/api/webhooks/hris":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            signature = self.headers.get("X-HRIS-Signature", "")
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.handle_hris_webhook(raw, signature, body)
            self._send_json(status, payload)
            return

        if parsed.path == "/api/webhooks/bamboohr":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            timestamp = self.headers.get("X-BambooHR-Timestamp", "")
            signature = self.headers.get("X-BambooHR-Signature", "")
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.handle_bamboohr_webhook(raw, timestamp, signature, body)
            self._send_json(status, payload)
            return

        if parsed.path == "/api/employees":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.create_employee(body)
            self._send_json(status, payload)
            return

        self._send_json(404, {"error": "Not found"})

    def do_PUT(self):
        parsed = urlparse(self.path)
        if ENFORCE_AUTH and parsed.path not in PUBLIC_PATHS and not self._is_authenticated():
            self._send_json(401, {"error": "Not authenticated"})
            return
        m = EMPLOYEE_RE.match(parsed.path)
        if m:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            status, payload = api.update_employee(int(m.group(1)), body)
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if ENFORCE_AUTH and parsed.path not in PUBLIC_PATHS and not self._is_authenticated():
            self._send_json(401, {"error": "Not authenticated"})
            return
        if parsed.path == "/api/events":
            status, payload = api.clear_events()
            self._send_json(status, payload)
            return
        m = EMPLOYEE_RE.match(parsed.path)
        if m:
            status, payload = api.delete_employee(int(m.group(1)))
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": "Not found"})


def main():
    db.init_db()
    if api.HRIS_WEBHOOK_SECRET == "demo-insecure-secret-change-me":
        print("WARNING: HRIS_WEBHOOK_SECRET not set — using the insecure demo default. "
              "Set a real secret via the HRIS_WEBHOOK_SECRET env var outside local demo use.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"SLAM demo running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

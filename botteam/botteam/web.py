"""Private single-owner workbench. No bearer credential is sent to the browser."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).with_name("static")


class WebAccess:
    def __init__(self, password, origin="", secure=False, owner=None):
        self.password = password
        self.origin = origin.rstrip("/")
        self.secure = secure
        self.owner = owner
        self.sessions = {}
        self.failures = {}
        self.lock = threading.Lock()

    @classmethod
    def from_env(cls, telegram):
        password = os.environ.get("BOTTEAM_WEB_PASSWORD", "")
        if not password:
            return None
        if password == os.environ.get("BOTTEAM_TOKEN"):
            raise ValueError("Use a separate workbench password, not BOTTEAM_TOKEN")
        if len(password) < 24:
            raise ValueError("BOTTEAM_WEB_PASSWORD must contain at least 24 characters")
        origin = os.environ.get("BOTTEAM_WEB_ORIGIN", "")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("BOTTEAM_WEB_ORIGIN must be an HTTPS origin, without a path")
        owner = os.environ.get("BOTTEAM_TELEGRAM_OWNER_ID", "")
        if owner and (not telegram or not owner.isdigit() or int(owner) not in telegram.allowed):
            raise ValueError("Dashboard Telegram owner must be in the configured Telegram allowlist")
        return cls(password, origin, bool(origin), int(owner) if owner else None)

    def expected_origin(self, handler):
        port = handler.server.server_address[1]
        return self.origin or f"http://127.0.0.1:{port}"

    def login(self, address, password):
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now}
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
            count, expiry = self.failures.get(address, (0, now + 300))
            if count >= 8:
                return None
            if not isinstance(password, str) or not hmac.compare_digest(password.encode(), self.password.encode()):
                if len(self.failures) < 1000:
                    self.failures[address] = (count + 1, expiry)
                return None
            self.failures.pop(address, None)
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[session] = (now + 8 * 3600, csrf)
            return session

    def session(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
            key = cookie["botteam_session"].value
        except (KeyError, ValueError):
            return None, None
        with self.lock:
            item = self.sessions.get(key)
            if not item or item[0] < time.monotonic():
                self.sessions.pop(key, None)
                return None, None
        return key, item[1]


def headers(handler, status, content, kind, cookie=None):
    handler.send_response(status)
    for key, value in {
        "Content-Type": kind, "Content-Length": str(len(content)), "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        "X-Frame-Options": "DENY",
    }.items():
        handler.send_header(key, value)
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(content)


def dispatch_web(handler, parts, url):
    access = handler.server.web
    if access is None:
        raise KeyError("Workbench disabled")
    expected = access.expected_origin(handler)
    # Reject hostile Host headers even for login pages (DNS rebinding).
    if handler.headers.get("Host") != urlsplit(expected).netloc:
        handler.json(403, {"error": "Unexpected host"})
        return
    if handler.command == "GET" and parts in ([""], [], ["app.js"], ["style.css"]):
        name, kind = ("index.html", "text/html; charset=utf-8") if parts in ([], [""]) else (
            parts[0], "text/javascript; charset=utf-8" if parts[0] == "app.js" else "text/css; charset=utf-8")
        headers(handler, 200, (ASSETS / name).read_bytes(), kind)
        return
    if handler.command == "POST" and handler.headers.get("Origin") != expected:
        handler.json(403, {"error": "Same-origin request required"})
        return
    if parts == ["login"] and handler.command == "POST":
        session = access.login(handler.client_address[0], handler.body().get("password"))
        if not session:
            handler.json(401, {"error": "Sign-in failed or temporarily rate limited"})
            return
        cookie = f"botteam_session={session}; HttpOnly; SameSite=Strict; Path=/ui; Max-Age=28800"
        if access.secure:
            cookie += "; Secure"
        headers(handler, 200, b'{"ok":true}', "application/json", cookie)
        return
    session, csrf = access.session(handler)
    if not session:
        handler.json(401, {"error": "Sign in to the private workbench"})
        return
    if handler.command == "POST" and not hmac.compare_digest(handler.headers.get("X-CSRF-Token", ""), csrf):
        handler.json(403, {"error": "Invalid session request token"})
        return
    if parts == ["session"] and handler.command == "GET":
        engine = handler.server.engine
        handler.json(200, {"csrf": csrf, "offline": engine.model.offline,
                          "telegram_linked": bool(access.owner),
                          "bots": [{"id": b["id"], "capabilities": b["capabilities"]} for b in engine.bots.values()]})
    elif parts == ["logout"] and handler.command == "POST":
        with access.lock:
            access.sessions.pop(session, None)
        headers(handler, 200, b'{"ok":true}', "application/json",
                "botteam_session=; HttpOnly; SameSite=Strict; Path=/ui; Max-Age=0" + ("; Secure" if access.secure else ""))
    elif parts[:1] == ["api"]:
        route = parts[1:]
        if handler.command == "GET":
            # Reuse the Director's read routes and exact artifact path validation.
            handler.get_route(route, parse_qs(url.query))
        elif route == ["tasks"]:
            data = handler.body()
            key = handler.headers.get("Idempotency-Key", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", key):
                raise ValueError("A valid Idempotency-Key is required")
            from .store import encode
            fingerprint = hashlib.sha256(encode(data).encode()).hexdigest()
            scope = (access.owner, access.owner, 0) if access.owner else None
            mode = "demo" if handler.server.engine.model.offline else "live"
            task = handler.server.engine.submit(data.get("goal"), mode, data.get("attachments", {}),
                                               web_request=(key, fingerprint), telegram_scope=scope)
            handler.json(201, {"id": task})
        elif (len(route) == 3 and route[0] == "tasks" and route[2] in {"pause", "resume", "cancel", "retry"}) or (
                len(route) == 3 and route[0] == "approvals" and route[2] in {"approve", "reject"}):
            handler.post_route(route, handler.body())
        else:
            raise KeyError("Unknown workbench action")
    else:
        raise KeyError("Unknown workbench endpoint")

import hmac
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .actions import path_under, read_bytes
from .engine import Conflict, Engine
from .settings import read_config
from .store import encode
from .telegram import Telegram

LOG = logging.getLogger("botteam")
MAX_REQUEST = 3_500_000


class Handler(BaseHTTPRequestHandler):
    server_version = "BotTeam/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.client_address[0], fmt % args)

    def send_payload(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_type != "application/json":
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send_payload(status, encode(value).encode())

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_REQUEST:
            raise ValueError("Request exceeds size limit")
        value = json.loads(self.rfile.read(length)) if length else {}
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def do_GET(self):
        self.handle_route()

    def do_POST(self):
        self.handle_route()

    def handle_route(self):
        try:
            self.dispatch()
        except Conflict as exc:
            self.json(409, {"error": str(exc)})
        except (KeyError, FileNotFoundError):
            self.json(404, {"error": "Resource not found"})
        except (ValueError, TypeError) as exc:
            self.json(400, {"error": str(exc)[:500]})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            LOG.exception("API request failed")
            self.json(500, {"error": "Internal service error"})

    def dispatch(self):
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        engine = self.server.engine
        if self.command == "GET" and parts == ["healthz"]:
            live = bool(engine.thread and engine.thread.is_alive() and time.monotonic() - engine.heartbeat < 15)
            sandbox = engine.tools.sandbox_ready()
            model = engine.model.configured()
            ok = live and model and (sandbox or not engine.policy["sandbox_required"])
            gateway = self.server.telegram
            self.json(200 if ok else 503, {"ready": ok, "scheduler": live, "sandbox": sandbox,
                                           "model_configured": model,
                                           "telegram_configured": gateway is not None,
                                           "telegram_polling": bool(gateway and gateway.last_poll and
                                                                    time.monotonic() - gateway.last_poll < 45)})
            return
        expected = "Bearer " + self.server.token
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(expected.encode(), supplied.encode()):
            self.json(401, {"error": "Bearer token required"})
            return
        if self.command == "GET":
            self.get_route(parts, parse_qs(url.query))
        else:
            self.post_route(parts, self.body())

    def get_route(self, parts, query):
        engine = self.server.engine
        if parts == ["tasks"]:
            limit = max(1, min(100, int(query.get("limit", ["30"])[0])))
            with engine.store.tx(write=False) as db:
                rows = db.execute("SELECT id,goal,status,created,updated,summary FROM tasks ORDER BY created DESC LIMIT ?",
                                  (limit,)).fetchall()
            self.json(200, [dict(row) for row in rows])
        elif parts == ["schedules"]:
            with engine.store.tx(write=False) as db:
                rows = db.execute("SELECT * FROM schedules ORDER BY next_at").fetchall()
            self.json(200, [dict(row) for row in rows])
        elif parts == ["events"] or len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
            after = max(0, int(query.get("after", ["0"])[0]))
            with engine.store.tx(write=False) as db:
                sql = "SELECT * FROM events WHERE seq>?"
                parameters = [after]
                if parts[0] == "tasks":
                    engine.task_row(db, parts[1])
                    sql += " AND task=?"
                    parameters.append(parts[1])
                rows = db.execute(sql + " ORDER BY seq LIMIT 200", parameters).fetchall()
            self.json(200, [{**dict(row), "data": json.loads(row["data"])} for row in rows])
        elif len(parts) == 2 and parts[0] == "tasks":
            self.json(200, engine.task_view(parts[1]))
        elif len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "artifacts":
            with engine.store.tx(write=False) as db:
                engine.task_row(db, parts[1])
            file = path_under(engine.projects / parts[1], unquote("/".join(parts[3:])))
            if not file.is_file():
                raise FileNotFoundError()
            self.send_payload(200, read_bytes(file), "application/octet-stream")
        else:
            raise KeyError("Unknown endpoint")

    def post_route(self, parts, data):
        engine = self.server.engine
        if parts == ["tasks"]:
            task = engine.submit(data.get("goal"), data.get("mode", "live"), data.get("attachments", {}))
            self.json(201, {"id": task})
        elif parts == ["schedules"]:
            with engine.store.tx() as db:
                schedule = engine.schedule(db, data.get("goal"), data.get("interval_seconds"))
            self.json(201, {"id": schedule})
        elif len(parts) == 3 and parts[0] == "schedules" and parts[2] == "disable":
            with engine.store.tx() as db:
                result = db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (parts[1],))
                if not result.rowcount:
                    raise KeyError("Unknown schedule")
            self.json(200, {"id": parts[1], "enabled": False})
        elif len(parts) == 3 and parts[0] == "tasks":
            state = engine.control(parts[1], parts[2])
            self.json(200, {"id": parts[1], "status": state})
        elif len(parts) == 3 and parts[0] == "approvals":
            task = engine.approval(parts[1], parts[2], data.get("response", ""))
            self.json(200, {"id": parts[1], "task": task, "decision": parts[2]})
        else:
            raise KeyError("Unknown endpoint")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, token, engine, telegram=None):
        super().__init__(address, Handler)
        self.token = token
        self.engine = engine
        self.telegram = telegram


def serve(args):
    token = os.environ.get("BOTTEAM_TOKEN", "")
    if len(token) < 24:
        raise ValueError("Set BOTTEAM_TOKEN to a random value of at least 24 characters")
    config = read_config(args.config, args.offline)
    engine = Engine(config, args.data)
    telegram = Telegram.from_env(engine)
    with Server((args.host, args.port), token, engine, telegram) as server:
        engine.start()
        try:
            if telegram:
                telegram.start()
            exit_requested = threading.Event()
            def shutdown(*_):
                exit_requested.set()
                engine.stop.set()
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            server.timeout = 0.5
            LOG.info("Director listening on %s:%s (%s mode)", args.host, args.port, config["provider"])
            while not exit_requested.is_set():
                if not engine.thread.is_alive():
                    raise RuntimeError("Scheduler exited unexpectedly")
                server.handle_request()
        finally:
            if telegram:
                telegram.close()
            engine.close()

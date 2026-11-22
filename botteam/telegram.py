import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from .actions import HTTP, MAX_FILE, path_under, read_bytes
from .engine import Conflict
from .store import encode

LOG = logging.getLogger("botteam.telegram")
LONG_MESSAGE = 3500
MAX_VOICE = 12_000_000


class TransportError(Exception):
    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


def transaction(url, data=None, headers=None, timeout=25, limit=1_000_000):
    try:
        with HTTP.open(Request(url, data=data, headers=headers or {}), timeout=timeout) as response:
            raw = response.read(limit + 1)
    except HTTPError as exc:
        raise TransportError(f"HTTP {exc.code}", retry=exc.code in {408, 429} or exc.code >= 500) from None
    except (TimeoutError, URLError, OSError):
        raise TransportError("Transport unavailable") from None
    if len(raw) > limit:
        raise TransportError("Response exceeds size limit", retry=False)
    return raw


def multipart(fields, field, name, content, media_type):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.extend([f"--{boundary}\r\n", f'Content-Disposition: form-data; name="{key}"\r\n\r\n',
                      str(value), "\r\n"])
    parts.extend([f"--{boundary}\r\n",
                  f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n',
                  f"Content-Type: {media_type}\r\n\r\n"])
    data = "".join(parts).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return data, {"Content-Type": "multipart/form-data; boundary=" + boundary}


class TelegramClient:
    def __init__(self, token, transcription_model):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.files = f"https://api.telegram.org/file/bot{token}/"
        self.transcription_model = transcription_model

    def call(self, method, payload, upload=None, timeout=25):
        if upload:
            name, content = upload
            data, headers = multipart(payload, "document", name, content, "application/octet-stream")
        else:
            data, headers = encode(payload).encode(), {"Content-Type": "application/json"}
        response = json.loads(transaction(self.base + method, data, headers, timeout))
        if not response.get("ok"):
            raise TransportError("Telegram rejected " + method)
        return response["result"]

    def download(self, file_id, limit):
        result = self.call("getFile", {"file_id": file_id})
        if result.get("file_size", 0) > limit or not result.get("file_path"):
            raise ValueError(f"File exceeds the {limit:,}-byte download limit")
        path = result["file_path"].split("/")
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in path):
            raise ValueError("Telegram returned an invalid file path")
        try:
            return transaction(self.files + "/".join(quote(part, safe="") for part in path), limit=limit)
        except TransportError as exc:
            if not exc.retry:
                raise ValueError("Telegram file could not be downloaded") from None
            raise

    def transcribe(self, file_id):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Voice transcription needs OPENAI_API_KEY. You can send the goal as text.")
        voice = self.download(file_id, MAX_VOICE)
        data, headers = multipart({"model": self.transcription_model}, "file", "voice.ogg", voice, "audio/ogg")
        headers["Authorization"] = "Bearer " + api_key
        try:
            result = json.loads(transaction("https://api.openai.com/v1/audio/transcriptions",
                                            data, headers, timeout=60))
        except TransportError as exc:
            if exc.retry:
                raise
            raise ValueError("Voice transcription was rejected by the provider") from None
        return result.get("text", "").strip()


@dataclass(frozen=True)
class Scope:
    user: int
    chat: int
    topic: int = 0

    def keys(self):
        return self.user, self.chat, self.topic

    def destination(self):
        payload = {"chat_id": self.chat}
        if self.topic:
            payload["message_thread_id"] = self.topic
        return payload


class Telegram:
    def __init__(self, engine, client, allowed, forums=(), bot_topics=None, voice_per_hour=8, voice_seconds=180):
        self.engine = engine
        self.client = client
        self.allowed = set(allowed)
        self.forums = set(forums)
        self.bot_topics = bot_topics or {}
        self.voice_per_hour = voice_per_hour
        self.voice_seconds = voice_seconds
        self.stop = threading.Event()
        self.thread = None
        self.username = ""
        self.last_poll = 0.0

    @classmethod
    def from_env(cls, engine):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid TELEGRAM_BOT_TOKEN format")
        allowed = parse_ids("ALLOWED_USER_IDS")
        forums = parse_ids("TELEGRAM_FORUM_CHAT_IDS")
        if not allowed or any(item < 1 for item in allowed) or any(item >= 0 for item in forums):
            raise ValueError("Telegram requires positive ALLOWED_USER_IDS and negative forum chat IDs")
        bot_topics = json.loads(os.environ.get("TELEGRAM_BOT_TOPICS", "{}"))
        if not isinstance(bot_topics, dict):
            raise ValueError("TELEGRAM_BOT_TOPICS must map forum IDs to bot-topic mappings")
        for chat, topics in bot_topics.items():
            if (int(chat) not in forums or not isinstance(topics, dict)
                    or any(bot not in engine.bots or type(topic) is not int or topic < 1
                           for bot, topic in topics.items())):
                raise ValueError("Invalid TELEGRAM_BOT_TOPICS mapping")
        client = TelegramClient(token, os.environ.get("TELEGRAM_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"))
        voice_per_hour = int(os.environ.get("TELEGRAM_VOICE_PER_HOUR", "8"))
        voice_seconds = int(os.environ.get("TELEGRAM_VOICE_MAX_SECONDS", "180"))
        if not 0 <= voice_per_hour <= 100 or not 1 <= voice_seconds <= 3600:
            raise ValueError("Invalid Telegram voice limits")
        return cls(engine, client, allowed, forums, bot_topics, voice_per_hour, voice_seconds)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="telegram")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def value(self, key):
        with self.engine.store.tx(write=False) as db:
            row = db.execute("SELECT value FROM telegram_state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else 0

    def checkpoint(self, key, value):
        with self.engine.store.tx() as db:
            db.execute("INSERT INTO telegram_state VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                       "SET value=MAX(value,excluded.value)", (key, value))

    def run(self):
        failures = 0
        while not self.stop.is_set():
            try:
                if not self.username:
                    self.username = self.client.call("getMe", {})["username"].lower()
                self.deliver_events()
                updates = self.client.call("getUpdates", {"offset": self.value("update") + 1,
                    "timeout": 10, "limit": 20, "allowed_updates": ["message", "callback_query"]})
                self.last_poll = time.monotonic()
                for update in updates:
                    if self.stop.is_set():
                        return
                    try:
                        self.handle(update)
                    except TransportError as exc:
                        if exc.retry:
                            raise
                        LOG.warning("Telegram update %s delivery failed (%s)", update["update_id"], exc)
                    self.checkpoint("update", update["update_id"])
                failures = 0
            except Exception as exc:
                failures += 1
                LOG.warning("Telegram polling failed (%s)", type(exc).__name__)
                self.stop.wait(min(60, 2 ** min(failures, 6)))

    def scope(self, sender, message):
        user, chat = sender.get("id"), message.get("chat", {})
        chat_id = chat.get("id")
        if sender.get("is_bot") or user not in self.allowed:
            return None
        if chat.get("type") == "private" and chat_id == user:
            return Scope(user, chat_id)
        if chat.get("is_forum") and chat_id in self.forums:
            return Scope(user, chat_id, message.get("message_thread_id", 0))
        return None

    def send(self, scope, text, keyboard=None, reply_markup=None):
        payload = {**scope.destination(), "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.client.call("sendMessage", payload)

    def document(self, scope, name, content):
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:96] or "output.txt"
        return self.client.call("sendDocument", scope.destination(), upload=(safe_name, content))

    def output(self, scope, text, file_name="output.txt", keyboard=None):
        if len(text.encode("utf-16-le")) // 2 <= LONG_MESSAGE:
            self.send(scope, text, keyboard)
        else:
            self.send(scope, text[:700].rstrip() + "\n\nFull output is attached.", keyboard)
            self.document(scope, file_name, text.encode())

    def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        scope = self.scope(sender, message)
        if scope is None:
            return
        try:
            if callback:
                self.handle_callback(scope, callback)
            else:
                self.handle_message(scope, update["update_id"], message)
        except (Conflict, KeyError, ValueError) as exc:
            self.send(scope, (str(exc) if not isinstance(exc, KeyError) else "Task or action not found.")[:500])

    def handle_message(self, scope, update_id, message):
        text = message.get("text", "").strip()
        if text.startswith("/"):
            self.command(scope, text)
            return
        if message.get("voice"):
            voice = message["voice"]
            text = self.transcribe(scope, update_id, voice)
        replying = message.get("reply_to_message", {}).get("message_id")
        if replying and text:
            pending = self.reply_lookup(scope, replying)
            if pending:
                self.apply_answer(scope, pending, text)
                return
        files = {}
        if message.get("document") or message.get("photo"):
            photo = bool(message.get("photo") and not message.get("document"))
            file = message["photo"][-1] if photo else message["document"]
            name = (f"photo-{message['message_id']}.jpg" if photo else file.get("file_name")) or "attachment.bin"
            path_under(self.engine.root, name)
            if "/" in name or file.get("file_size", 0) > MAX_FILE:
                raise ValueError("Use a filename and a file under 200 KB")
            content = self.client.download(file["file_id"], MAX_FILE)
            files[name] = base64.b64encode(content).decode()
            text = message.get("caption", "").strip()
            if not text:
                self.stage(scope, name, files[name])
                self.send(scope, f"Attached {name}. Send your next goal to submit it with this file.")
                return
        if not text:
            self.send(scope, "Send a goal as text or a voice note, attach a file, or use /help.")
            return
        task = self.engine.submit(text, "demo" if self.engine.model.offline else "live", files,
                                  telegram_origin=(update_id, *scope.keys()))
        self.send(scope, f"Queued {task[:8]}. Use /status {task[:8]} to inspect it.")

    def transcribe(self, scope, update_id, voice):
        if voice.get("file_size", 0) > MAX_VOICE or voice.get("duration", 0) > self.voice_seconds:
            raise ValueError(f"Voice notes must be under 12 MB and {self.voice_seconds} seconds")
        with self.engine.store.tx() as db:
            existing = db.execute("SELECT transcript FROM telegram_voice WHERE update_id=?",
                                  (update_id,)).fetchone()
            if existing:
                if existing["transcript"]:
                    return existing["transcript"]
                raise ValueError("The previous transcription did not complete. Please resend it or use text.")
            now = time.time()
            calls = db.execute("SELECT COUNT(*) FROM telegram_voice WHERE user_id=? AND at>?",
                               (scope.user, now - 3600)).fetchone()[0]
            if calls >= self.voice_per_hour:
                raise ValueError("The voice transcription limit has been reached. You can still send text.")
            db.execute("INSERT INTO telegram_voice VALUES(?,?,?,NULL)", (update_id, scope.user, now))
        try:
            transcript = self.client.transcribe(voice["file_id"])
        except TransportError:
            raise ValueError("Voice transcription is unavailable. Please resend it or use text.") from None
        if not transcript:
            raise ValueError("No speech was transcribed")
        with self.engine.store.tx() as db:
            db.execute("UPDATE telegram_voice SET transcript=? WHERE update_id=?", (transcript, update_id))
        return transcript

    def stage(self, scope, name, content):
        with self.engine.store.tx() as db:
            existing = db.execute("SELECT name FROM telegram_drafts WHERE user_id=? AND chat_id=? AND topic=?",
                                  scope.keys()).fetchall()
            if len(existing) >= 12 and name not in {row["name"] for row in existing}:
                raise ValueError("This draft already has 12 files")
            db.execute("INSERT INTO telegram_drafts VALUES(?,?,?,?,?) ON CONFLICT(user_id,chat_id,topic,name) "
                       "DO UPDATE SET content=excluded.content", (*scope.keys(), name, content))

    def reply_lookup(self, scope, message):
        with self.engine.store.tx(write=False) as db:
            row = db.execute("SELECT approval FROM telegram_replies WHERE user_id=? AND chat_id=? AND topic=? "
                             "AND message_id=?", (*scope.keys(), message)).fetchone()
            return row["approval"] if row else None

    def apply_answer(self, scope, approval, response):
        self.owned_approval(scope, approval)
        self.engine.approval(approval, "approve", response)
        with self.engine.store.tx() as db:
            db.execute("DELETE FROM telegram_replies WHERE approval=?", (approval,))
        self.send(scope, "Answer recorded.")

    def lookup(self, scope, prefix="", active=False):
        if prefix and not re.fullmatch(r"[a-f0-9]{6,32}", prefix):
            raise ValueError("Use a full task ID or its six-or-more-character prefix")
        with self.engine.store.tx(write=False) as db:
            sql = "SELECT t.id,t.goal,t.status,t.summary FROM tasks t JOIN telegram_origins o ON o.task=t.id "
            sql += "WHERE o.user_id=? AND o.chat_id=? AND o.topic=? AND t.id LIKE ?"
            scope_keys = (*scope.keys(), prefix + "%")
            if active:
                sql += " AND t.status NOT IN ('completed','failed','cancelled')"
            rows = db.execute(sql + " ORDER BY t.created DESC LIMIT 12", scope_keys).fetchall()
        if prefix and len(rows) > 1:
            raise ValueError("Task ID prefix is ambiguous")
        return rows

    def owned_approval(self, scope, approval):
        with self.engine.store.tx(write=False) as db:
            row = db.execute("SELECT a.* FROM approvals a JOIN telegram_origins o ON o.task=a.task "
                             "WHERE a.id=? AND o.user_id=? AND o.chat_id=? AND o.topic=?",
                             (approval, *scope.keys())).fetchone()
        if row is None:
            raise KeyError("Unknown approval")
        return row

    @staticmethod
    def buttons(task, approval=None):
        rows = []
        if approval:
            rows.append([{"text": "Approve", "callback_data": "a:" + approval},
                         {"text": "Reject", "callback_data": "r:" + approval}])
        rows.append([{"text": "Retry", "callback_data": "t:" + task},
                     {"text": "View Logs", "callback_data": "l:" + task}])
        return rows

    def handle_callback(self, scope, callback):
        data = callback.get("data", "")
        try:
            if not re.fullmatch(r"[artl]:[a-f0-9]{32}", data):
                raise ValueError("This button is invalid")
            kind, target = data.split(":")
            if kind in {"a", "r"}:
                row = self.owned_approval(scope, target)
                if row["state"] != "pending":
                    raise Conflict("This approval is no longer pending")
                action = json.loads(row["action"])
                if kind == "a" and action["tool"] == "clarify":
                    prompt = self.send(scope, action["args"]["question"], reply_markup={
                        "force_reply": True, "selective": True
                    })
                    with self.engine.store.tx() as db:
                        db.execute("INSERT OR IGNORE INTO telegram_replies VALUES(?,?,?,?,?)",
                                   (*scope.keys(), prompt["message_id"], target))
                else:
                    self.engine.approval(target, "approve" if kind == "a" else "reject", "")
                    self.send(scope, "Approved." if kind == "a" else "Rejected.")
            else:
                if not self.lookup(scope, target):
                    raise KeyError("Unknown task")
                if kind == "l":
                    self.logs(scope, target)
                else:
                    self.engine.control(target, "retry")
                    self.send(scope, "Retry queued.")
        finally:
            try:
                self.client.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            except TransportError:
                pass

    def command(self, scope, message):
        command, _, remainder = message.partition(" ")
        command, _, mention = command.lower().partition("@")
        remainder = remainder.strip()
        if mention and mention != self.username:
            return
        if command in {"/start", "/help"}:
            self.send(scope, "Send a goal as text or a voice note. Send a file with a caption, or stage files "
                      "and send the goal next.\n\n"
                      "/status [id] · /tasks · /bots · /logs [id]\n"
                      "/pause [id] · /resume [id] · /cancel <id> · /retry <id>\n"
                      "/answer <approval_id> <answer> · /artifact <id> <path> · /clearfiles")
        elif command == "/bots":
            overview = ["Director — planning, scheduling, and final review"]
            overview.extend(f"{bot['id']} — {', '.join(bot['capabilities'])} · {' → '.join(bot['route'])}"
                            for bot in self.engine.bots.values())
            self.output(scope, "\n".join(overview), "bots.txt")
        elif command == "/tasks":
            tasks = self.lookup(scope)
            self.send(scope, "\n".join(f"{row['id'][:8]} · {row['status']} · {row['goal'][:120]}"
                                      for row in tasks) or "No tasks in this chat or topic.")
        elif command == "/clearfiles":
            with self.engine.store.tx() as db:
                db.execute("DELETE FROM telegram_drafts WHERE user_id=? AND chat_id=? AND topic=?", scope.keys())
            self.send(scope, "Staged files cleared.")
        elif command == "/answer":
            approval, _, answer = remainder.partition(" ")
            self.apply_answer(scope, approval, answer)
        elif command in {"/status", "/logs", "/pause", "/resume", "/cancel", "/retry", "/artifact"}:
            prefix, _, detail = remainder.partition(" ")
            if not prefix and command in {"/cancel", "/retry", "/artifact"}:
                raise ValueError(f"Use {command} with a task ID")
            matches = self.lookup(scope, prefix, active=not prefix and command in {"/pause", "/resume"})
            if not matches:
                raise ValueError("No matching task in this chat or topic")
            task = matches[0]["id"]
            if command == "/status":
                self.status(scope, task)
            elif command == "/logs":
                self.logs(scope, task)
            elif command == "/artifact":
                file = path_under(self.engine.projects / task, detail)
                if not file.is_file():
                    raise KeyError("Unknown artifact")
                name = file.name if file.suffix in {".md", ".txt"} else file.name + ".txt"
                self.document(scope, name, read_bytes(file))
            else:
                state = self.engine.control(task, command[1:])
                self.send(scope, f"{task[:8]} · {state}")
        else:
            self.send(scope, "Unknown command. Use /help.")

    def status(self, scope, task):
        view = self.engine.task_view(task)
        lines = [f"{task[:8]} · {view['status']}", view["goal"][:500]]
        if view["summary"]:
            lines.append(view["summary"])
        lines.extend(f"{step['id']} · {step['bot'] or 'unassigned'} · {step['state']}"
                     for step in view["steps"])
        if view["artifacts"]:
            lines += ["", "Artifacts:", *view["artifacts"]]
        self.output(scope, "\n".join(lines), f"{task[:8]}-status.md", self.buttons(task))
        for approval in view["approvals"]:
            if approval["state"] == "pending":
                self.send_approval(scope, task, approval["step"], approval["id"], approval["action"])

    def logs(self, scope, task):
        with self.engine.store.tx(write=False) as db:
            rows = db.execute("SELECT seq,step,kind,data FROM events WHERE task=? ORDER BY seq DESC LIMIT 40",
                              (task,)).fetchall()
        lines = [f"Latest events for {task[:8]}"]
        lines.extend(f"{row['seq']} {row['step'] or 'director'} {row['kind']} {row['data']}"
                     for row in reversed(rows))
        self.output(scope, "\n".join(lines), f"{task[:8]}-logs.txt")

    def send_approval(self, scope, task, step, approval, action):
        details = json.dumps({key: value for key, value in action.items() if key != "nonce"},
                             ensure_ascii=False, indent=2)
        text = f"{task[:8]} · {step} · {action['tool']} needs input\nApproval ID {approval}\n\n{details}"
        self.output(scope, text, f"{task[:8]}-approval.txt", self.buttons(task, approval))

    def deliver_events(self):
        with self.engine.store.tx(write=False) as db:
            rows = db.execute("SELECT e.*,o.user_id,o.chat_id,o.topic FROM events e JOIN telegram_origins o "
                              "ON e.task=o.task WHERE e.seq>? ORDER BY e.seq LIMIT 100",
                              (self.value("event"),)).fetchall()
        for row in rows:
            if self.stop.is_set():
                return
            scope = Scope(row["user_id"], row["chat_id"], row["topic"])
            if scope.user in self.allowed and (scope.chat == scope.user or scope.chat in self.forums):
                try:
                    self.deliver(scope, row)
                except TransportError as exc:
                    if exc.retry:
                        raise
                    LOG.warning("Telegram event %s delivery failed (%s)", row["seq"], exc)
            self.checkpoint("event", row["seq"])

    def deliver(self, scope, row):
        task, kind = row["task"], row["kind"]
        data = json.loads(row["data"])
        if kind == "approval_requested":
            approval = self.owned_approval(scope, data["approval"])
            if approval["state"] == "pending":
                self.send_approval(scope, task, row["step"], approval["id"], json.loads(approval["action"]))
        elif kind == "step_completed":
            bot = data["bot"]
            topic = self.bot_topics.get(str(scope.chat), {}).get(bot, scope.topic)
            preview = data["result"]["summary"][:450]
            self.send(Scope(scope.user, scope.chat, topic), f"{task[:8]} · {bot} finished {row['step']}\n{preview}")
        elif kind in {"task_completed", "task_failed", "task_cancelled"}:
            label = kind.removeprefix("task_")
            view = self.engine.task_view(task)
            # Historical terminal events remain in SQLite after an operator initiates a retry.
            if view["status"] != label:
                return
            summary = view["summary"] or label
            self.output(scope, f"{task[:8]} · {label}\n\n{summary}", f"{task[:8]}-summary.md",
                        self.buttons(task))
            if label == "completed":
                self.send_artifacts(scope, task, view)

    def send_artifacts(self, scope, task, view):
        seen = set()
        for path in view["artifacts"]:
            if path.startswith("attachments/"):
                continue
            file = path_under(self.engine.projects / task, path)
            try:
                content = read_bytes(file)
            except (FileNotFoundError, ValueError):
                continue
            if file.suffix not in {".md", ".txt", ".patch", ".diff"} and len(content) <= LONG_MESSAGE:
                continue
            identity = (file.name, content)
            if identity in seen:
                continue
            seen.add(identity)
            basename = file.name if file.suffix in {".md", ".txt"} else file.name + ".txt"
            self.document(scope, basename, content)


def parse_ids(name):
    return {int(value.strip()) for value in os.environ.get(name, "").split(",") if value.strip()}

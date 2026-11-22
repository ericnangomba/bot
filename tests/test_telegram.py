import json
import tempfile
import unittest
from pathlib import Path

from botteam.engine import Engine
from botteam.settings import read_config
from botteam.store import encode, event
from botteam.telegram import Scope, Telegram

ROOT = Path(__file__).resolve().parents[1]


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.transcriptions = 0

    def call(self, method, payload, upload=None, timeout=25):
        self.messages.append((method, payload, upload))
        return {"message_id": len(self.messages)}

    def download(self, file_id, limit):
        return (ROOT / "examples" / "orders.csv").read_bytes()

    def transcribe(self, file_id):
        self.transcriptions += 1
        return "Calculate the uploaded order total"


class TestTelegram(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        self.engine = Engine(read_config(ROOT / "bots.yaml", offline=True), self.data.name)
        self.client = FakeTelegram()
        self.gateway = Telegram(self.engine, self.client, [123, 456], [-100123],
                                {"-100123": {"builder": 90}})

    def send(self, update, user, chat=None, **fields):
        self.gateway.handle({"update_id": update, "message": {
            "from": {"id": user}, "chat": chat or {"id": user, "type": "private"},
            "message_id": update, **fields
        }})

    def callback(self, user, data, chat=None, **fields):
        self.gateway.handle({"update_id": 100, "callback_query": {"id": "cb", "from": {"id": user},
                             "data": data, "message": {"chat": chat or {"id": user, "type": "private"}, **fields}}})

    def test_strict_sender_allowlist_and_voice_deduplication(self):
        self.send(1, 999, text="submit a goal")
        self.callback(999, "l:" + "a" * 32)
        self.send(2, 123, chat={"id": -100999, "type": "supergroup", "is_forum": True}, text="submit")
        self.assertFalse(self.client.messages)
        self.send(3, 123, document={"file_id": "csv", "file_name": "orders.csv"})
        self.send(4, 123, voice={"file_id": "audio", "duration": 5})
        self.send(4, 123, voice={"file_id": "audio", "duration": 5})
        self.assertEqual(self.client.transcriptions, 1)
        rows = self.gateway.lookup(Scope(123, 123))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.engine.files(rows[0]["id"]), ["attachments/orders.csv"])
        self.assertEqual(self.gateway.lookup(Scope(456, 456)), [])

    def test_buttons_are_scoped_and_clarification_resumes(self):
        self.send(1, 123, text="Clarify the task")
        task = self.gateway.lookup(Scope(123, 123))[0]["id"]
        approval = "a" * 32
        with self.engine.store.tx() as db:
            action = {"tool": "clarify", "args": {"question": "Which date should I use?"}}
            db.execute("INSERT INTO steps(task,id,capability,instruction,deps,state) VALUES(?,?,?,?,?,'blocked')",
                       (task, "inspect", "research", "Clarify", "[]"))
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,'pending',NULL,100)",
                       (approval, task, "inspect", "digest", encode(action)))
            event(db, task, "approval_requested", {"approval": approval}, "inspect")
        self.gateway.deliver_events()
        keyboard = self.client.messages[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([button["text"] for row in keyboard for button in row],
                         ["Approve", "Reject", "Retry", "View Logs"])
        self.callback(456, "r:" + approval)
        self.assertEqual(self.engine.task_view(task)["approvals"][0]["state"], "pending")
        self.callback(123, "a:" + approval)
        prompt_id = next(index for index, (_, payload, _) in enumerate(self.client.messages, 1)
                         if payload.get("reply_markup", {}).get("force_reply"))
        self.send(2, 123, text="Friday", reply_to_message={"message_id": prompt_id})
        current = self.engine.task_view(task)["approvals"][0]
        self.assertEqual((current["state"], current["response"]), ("approved", "Friday"))

    def test_forum_topic_routes_and_long_results_attach(self):
        chat = {"id": -100123, "type": "supergroup", "is_forum": True}
        self.send(1, 123, chat=chat, message_thread_id=7, text="Example project")
        scope = Scope(123, -100123, 7)
        task = self.gateway.lookup(scope)[0]["id"]
        with self.engine.store.tx() as db:
            event(db, task, "step_completed", {"bot": "builder", "result": {"summary": "Built it"}}, "test")
        self.gateway.deliver_events()
        self.assertEqual(self.client.messages[-1][1]["message_thread_id"], 90)
        checkpoint = self.gateway.value("event")
        self.gateway.deliver_events()
        self.assertEqual(self.gateway.value("event"), checkpoint)
        self.gateway.output(scope, "x" * 3501, "long-report.md")
        _, payload, upload = self.client.messages[-1]
        self.assertEqual(payload["message_thread_id"], 7)
        self.assertEqual(upload, ("long-report.md", b"x" * 3501))
        self.assertEqual(self.gateway.lookup(Scope(123, -100123, 90)), [])


if __name__ == "__main__":
    unittest.main()

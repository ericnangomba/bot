import base64
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botteam.actions import Transient
from botteam.engine import Conflict, Engine
from botteam.settings import read_config, validate_plan

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    offline = False

    def __init__(self):
        self.plans = 0

    def plan(self, task, context, capabilities):
        self.plans += 1
        if self.plans == 1:
            raise Transient("Test upstream failure")
        return {"steps": [
            {"id": "build", "capability": "build", "depends_on": [], "instruction": "Build"},
            {"id": "review", "capability": "review", "depends_on": ["build"],
             "review_of": "build", "instruction": "Review"}
        ]}

    def worker(self, task, profile, context):
        if context["step"] == "build":
            return {"summary": "Corrected" if context["review_feedback"] else "Broken"}
        passed = context["dependencies"]["build"]["summary"] == "Corrected"
        return {"summary": "Verified" if passed else "Fix the output",
                "verdict": "pass" if passed else "fail"}

    def finish(self, task, context):
        return {"summary": "Corrected and verified", "passed": True}


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.engine = Engine(read_config(ROOT / "bots.yaml"), self.state.name)

    def test_director_backs_off_then_routes_review_repair(self):
        self.engine.model = ScriptedModel()
        task = self.engine.submit("Build and review", "live", {})
        with patch.object(self.engine.tools, "sandbox_cleanup"):
            self.engine.start()
            self.addCleanup(self.engine.close)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            view = self.engine.task_view(task)
            if view["status"] in {"completed", "failed"}:
                break
            time.sleep(0.1)
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["summary"], "Corrected and verified")
        self.assertEqual(view["steps"][0]["feedback"], "Fix the output")
        self.assertEqual(view["steps"][1]["repairs"], 1)
        with self.engine.store.tx(write=False) as db:
            kinds = [row["kind"] for row in db.execute("SELECT kind FROM events WHERE task=?", (task,))]
        self.assertIn("retry_scheduled", kinds)
        self.assertIn("review_rejected", kinds)

    def test_crash_recovery_marks_external_effect_uncertain(self):
        task = self.engine.submit("External action", "live", {})
        with self.engine.store.tx() as db:
            db.execute("INSERT INTO steps(task,id,capability,instruction,deps,state,effect) "
                       "VALUES(?,'send','ops','Send','[]','in_progress',1)", (task,))
        with patch.object(self.engine.tools, "sandbox_cleanup"):
            self.engine.start()
            self.addCleanup(self.engine.close)
        view = self.engine.task_view(task)
        self.assertEqual(view["status"], "failed")
        self.assertIn("Check the destination", view["summary"])
        with self.assertRaises(Conflict):
            self.engine.control(task, "retry")

    def ops_task(self):
        draft = base64.b64encode(b"# Draft report\n").decode()
        task = self.engine.submit("Review the draft report", "live", {"report.md": draft})
        with self.engine.store.tx() as db:
            db.execute("INSERT INTO steps(task,id,capability,instruction,deps) VALUES(?,?,?,?,?)",
                       (task, "report", "ops", "Review and publish", "[]"))
        return task

    def step(self, task):
        with self.engine.store.tx(write=False) as db:
            return db.execute("SELECT * FROM steps WHERE task=? AND id='report'", (task,)).fetchone()

    def propose(self, task, tool, args):
        self.engine.propose(task, self.step(task), self.engine.bots["ops"], {"tool": tool, "args": args})

    def execute(self, task):
        self.engine.execute(task, self.step(task), self.engine.bots["ops"])

    def answer(self, task):
        self.propose(task, "clarify", {"question": "Which draft should I use?"})
        self.execute(task)
        details = self.engine.task_view(task)
        self.assertEqual(details["status"], "blocked")
        self.engine.approval(details["approvals"][0]["id"], "approve", "Use report.md")
        self.execute(task)

    def test_repeated_clarification_reuses_answer_and_keeps_publication_gate(self):
        task = self.ops_task()
        self.answer(task)
        for question in ("Should I use the report draft?", "Which report did you mean?"):
            self.propose(task, "clarify", {"question": question})
            self.execute(task)
            view = self.engine.task_view(task)
            self.assertEqual(view["status"], "in_progress")
            self.assertEqual([gate["state"] for gate in view["approvals"]], ["consumed"])
            observation = view["steps"][0]["observations"][-1]["result"]
            self.assertEqual(observation["answer"], "Use report.md")
            self.assertEqual(observation["status"], "already_answered")

        self.propose(task, "publish", {"path": "attachments/report.md"})
        self.execute(task)
        view = self.engine.task_view(task)
        self.assertEqual(view["status"], "needs_review")
        pending = view["approvals"][-1]
        self.assertEqual(pending["action"]["tool"], "publish")
        self.assertNotIn("published/report/report.md", view["artifacts"])
        self.engine.approval(pending["id"], "approve", "")
        self.execute(task)
        self.assertIn("published/report/report.md", self.engine.task_view(task)["artifacts"])

    def test_restart_recovers_old_repeat_and_keeps_unanswered_question(self):
        repeated, genuine = self.ops_task(), self.ops_task()
        self.answer(repeated)
        self.propose(repeated, "clarify", {"question": "Can you confirm the draft?"})
        self.propose(genuine, "clarify", {"question": "Which file belongs in this report?"})
        self.execute(genuine)
        with self.engine.store.tx() as db:
            action = db.execute("SELECT action FROM steps WHERE task=?", (repeated,)).fetchone()["action"]
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,'pending',NULL,?)",
                       ("old", repeated, "report", hashlib.sha256(action.encode()).hexdigest(), action, time.time()))
            db.execute("UPDATE steps SET state='blocked' WHERE task=?", (repeated,))
            self.engine.refresh(db, repeated)
        with patch.object(self.engine.tools, "sandbox_cleanup"), patch.object(self.engine, "run"):
            self.engine.start()
            self.addCleanup(self.engine.close)
        current = self.engine.task_view(repeated)
        self.assertEqual(current["steps"][0]["state"], "queued")
        self.assertEqual([gate["state"] for gate in current["approvals"]], ["consumed", "void"])
        self.assertEqual(self.engine.task_view(genuine)["approvals"][0]["state"], "pending")
        self.assertEqual(self.engine.task_view(genuine)["status"], "blocked")
        with self.assertRaises(Conflict):
            self.engine.approval("old", "approve", "Yes")

    def test_worker_can_ask_again_after_doing_work(self):
        task = self.ops_task()
        self.answer(task)
        self.propose(task, "read", {"path": "attachments/report.md"})
        self.execute(task)
        self.propose(task, "clarify", {"question": "Who is the audience for this report?"})
        self.execute(task)
        current = self.engine.task_view(task)
        self.assertEqual(current["status"], "blocked")
        self.assertEqual([gate["state"] for gate in current["approvals"]], ["consumed", "pending"])

    def test_invalid_live_plan_gets_one_feedback_attempt(self):
        from unittest.mock import Mock
        bad = {"steps": [{"id": "check", "capability": "review", "instruction": "Check", "depends_on": []}]}
        good = {"steps": [{"id": "draft", "capability": "write", "instruction": "Write", "depends_on": []}]}
        self.engine.model = Mock(offline=False)
        self.engine.model.plan.side_effect = [bad, good]
        task = self.engine.submit("Write welcome", "live", {})
        with self.engine.store.tx(write=False) as db:
            row = dict(self.engine.task_row(db, task))
        self.engine.plan(row)
        self.assertEqual(self.engine.task_view(task)["steps"], [])
        self.assertNotEqual(self.engine.task_view(task)["status"], "failed")
        self.engine.plan(row)
        self.assertEqual(self.engine.task_view(task)["steps"][0]["id"], "draft")
        self.assertIn("directly depend", self.engine.model.plan.call_args.args[1]["plan_feedback"])

    def test_second_invalid_plan_fails_without_weakening_validator(self):
        from unittest.mock import Mock
        bad = {"steps": [{"id": "check", "capability": "review", "instruction": "Check", "depends_on": []}]}
        self.engine.model = Mock(offline=False)
        self.engine.model.plan.return_value = bad
        task = self.engine.submit("Write welcome", "live", {})
        with self.engine.store.tx(write=False) as db:
            row = dict(self.engine.task_row(db, task))
        self.engine.plan(row)
        self.engine.plan(row)
        view = self.engine.task_view(task)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["steps"], [])
        self.assertIn("directly depend", view["summary"])

    def artifact_step(self):
        task = self.engine.submit("Create greeting.md", "live", {})
        with self.engine.store.tx() as db:
            db.execute("INSERT INTO steps(task,id,capability,instruction,deps) VALUES(?,?,?,?,?)",
                       (task, "report", "write", "Create greeting.md", "[]"))
        return task

    def worker_turn(self, task):
        with self.engine.store.tx(write=False) as db:
            row = dict(self.engine.task_row(db, task))
        self.engine.turn(row, self.step(task), self.engine.bots["writer"])

    def test_missing_artifact_is_returned_to_worker_and_written(self):
        task = self.artifact_step()
        path = "steps/report/greeting.md"
        completion = {"summary": "Wrote the greeting", "artifacts": [path]}
        self.engine.model = Mock(offline=False)
        self.engine.model.worker.side_effect = [
            completion, {"tool": "write", "args": {"path": "greeting.md", "content": "Hello!\n"}}, completion
        ]

        self.worker_turn(task)
        retry = self.engine.task_view(task)
        self.assertEqual(retry["steps"][0]["state"], "queued")
        self.assertEqual(retry["steps"][0]["turns"], 1)
        self.assertEqual(retry["artifacts"], [])

        self.worker_turn(task)
        feedback = self.engine.model.worker.call_args.args[2]["observations"][-1]
        self.assertEqual(feedback["tool"], "completion_validation")
        self.assertEqual(feedback["result"]["missing"], [path])
        self.worker_turn(task)  # Execute the requested write.
        self.worker_turn(task)  # Submit the grounded completion.

        view = self.engine.task_view(task)
        self.assertEqual(view["steps"][0]["state"], "completed")
        self.assertEqual(view["steps"][0]["result"]["artifacts"], [path])
        self.assertEqual((self.engine.projects / task / path).read_text(), "Hello!\n")

    def test_unchanged_missing_artifact_fails_after_one_correction(self):
        task = self.artifact_step()
        self.engine.model = Mock(offline=False)
        self.engine.model.worker.return_value = {"summary": "Wrote it", "artifacts": ["greeting.md"]}
        self.worker_turn(task)
        self.worker_turn(task)
        view = self.engine.task_view(task)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["steps"][0]["turns"], 1)
        self.assertIn("missing artifact: greeting.md", view["summary"])
        self.assertEqual(view["artifacts"], [])

    def test_worker_corrects_disallowed_tool_without_starting_it(self):
        task = self.artifact_step()
        self.engine.model = Mock(offline=False)
        self.engine.model.worker.side_effect = [
            {"tool": "publish", "args": {"path": "greeting.md"}},
            {"tool": "write", "args": {"path": "greeting.md", "content": "Hello!"}},
            {"summary": "Wrote the greeting", "artifacts": ["steps/report/greeting.md"]},
        ]
        self.worker_turn(task)
        view = self.engine.task_view(task)
        self.assertIsNone(view["steps"][0]["action"])
        self.assertEqual(view["approvals"], [])
        self.assertEqual(view["steps"][0]["state"], "queued")
        self.worker_turn(task)
        feedback = self.engine.model.worker.call_args.args[2]["observations"][-1]
        self.assertEqual(feedback["tool"], "proposal_validation")
        self.worker_turn(task)
        self.worker_turn(task)
        view = self.engine.task_view(task)
        self.assertEqual(view["steps"][0]["state"], "completed")
        self.assertEqual(view["artifacts"], ["steps/report/greeting.md"])

    def test_invalid_proposal_and_completion_share_the_correction_limit(self):
        task = self.artifact_step()
        self.engine.model = Mock(offline=False)
        self.engine.model.worker.side_effect = [{"tool": "write", "args": "invalid"}, {"summary": ""}]
        self.worker_turn(task)
        self.worker_turn(task)
        view = self.engine.task_view(task)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["steps"][0]["turns"], 1)
        self.assertEqual(view["artifacts"], [])

    def test_invalid_dag_is_rejected(self):
        plan = {"steps": [
            {"id": "a", "capability": "build", "depends_on": ["b"], "instruction": "Build"},
            {"id": "b", "capability": "write", "depends_on": ["a"], "instruction": "Write"}
        ]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(plan, self.engine.capabilities)


if __name__ == "__main__":
    unittest.main()

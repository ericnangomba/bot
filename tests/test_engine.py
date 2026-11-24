import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_invalid_dag_is_rejected(self):
        plan = {"steps": [
            {"id": "a", "capability": "build", "depends_on": ["b"], "instruction": "Build"},
            {"id": "b", "capability": "write", "depends_on": ["a"], "instruction": "Write"}
        ]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(plan, self.engine.capabilities)


if __name__ == "__main__":
    unittest.main()

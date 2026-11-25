import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botteam.actions import ProviderRequestError, Transient
from botteam.engine import Engine
from botteam.settings import read_config
from botteam.telegram import Scope, Telegram

ROOT = Path(__file__).resolve().parents[1]
BASE = "1" * 40
TREE = "2" * 40
SOURCE = {"app.py": b"def total():\n    return 1\n",
          "check_total.py": b"from app import total\nassert total() == 2\n"}


class FixtureGitHub:
    def __init__(self):
        self.writes = []
        self.base = BASE
        self.branch = None
        self.pull = None
        self.lose_pr_reply = False

    def __call__(self, endpoint, payload=None):
        files = list(SOURCE)
        if payload is not None:
            self.writes.append((endpoint, payload))
            if endpoint == "git/refs":
                self.branch = payload["sha"]
                return {}
            if endpoint == "pulls":
                self.pull = {"number": 7, "draft": True, "state": "open", "head": {"sha": self.branch}}
                if self.lose_pr_reply:
                    self.lose_pr_reply = False
                    raise Transient("GitHub connection lost after creating the draft")
                return self.pull
            return {"git/trees": {"sha": "6" * 40}, "git/commits": {"sha": "7" * 40}}[endpoint]
        if endpoint == "issues/1":
            return {"state": "open", "title": "Correct the total", "body": "Update total() to return 2."}
        if endpoint == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if endpoint.startswith("git/ref/heads/botteam/"):
            if self.branch is None:
                raise ProviderRequestError(404, {})
            return {"object": {"sha": self.branch}}
        if endpoint.startswith("pulls?"):
            return [self.pull] if self.pull else []
        if endpoint == f"git/commits/{BASE}":
            return {"tree": {"sha": TREE}}
        if endpoint == f"git/trees/{TREE}?recursive=1":
            return {"tree": [{"path": name, "type": "blob", "mode": "100644", "size": len(SOURCE[name]),
                              "sha": str(index + 3) * 40} for index, name in enumerate(files)]}
        if endpoint.startswith("git/blobs/"):
            index = int(endpoint[-1]) - 3
            return {"encoding": "base64", "content": base64.b64encode(SOURCE[files[index]]).decode()}
        raise AssertionError(f"Unexpected GitHub request: {endpoint}")


class FixtureModel:
    offline = False

    def worker(self, task, profile, context):
        if context["step"] == "review":
            assert context["observations"][-1]["result"]["exit_code"] == 0
            return {"summary": "The implementation matches the issue.", "verdict": "pass"}
        if not context["observations"]:
            value = 2 if context["review_feedback"] else 0
            return {"tool": "write", "args": {"path": "changes/app.py", "content": f"def total():\n    return {value}\n"}}
        return {"summary": "Saved the change.", "artifacts": ["steps/build/changes/app.py"]}


@unittest.skipUnless(os.environ.get("BOTTEAM_TEST_DOCKER") == "1", "set BOTTEAM_TEST_DOCKER=1 for the Docker E2E")
class GitHubWorkflowTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        config = read_config(ROOT / "bots.yaml")
        config["policy"]["github_repo"] = "example/project"
        config["github_workflow"] = {"base": "main", "paths": list(SOURCE), "editable_paths": ["app.py"],
                                     "test_paths": ["check_total.py"], "test_argv": ["python", "check_total.py"]}
        self.engine = Engine(config, state.name)
        self.engine.model = FixtureModel()
        self.remote = FixtureGitHub()
        env = patch.dict(os.environ, {"BOTTEAM_GITHUB_TOKEN": "fixture"})
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(self.engine.github, "get", self.remote)
        network.start()
        self.addCleanup(network.stop)
        self.engine.start()
        self.addCleanup(self.engine.close)

    def wait(self, task, status):
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            view = self.engine.task_view(task)
            if view["status"] == status:
                return view
            if view["status"] in {"failed", "completed"}:
                self.fail(view["summary"])
            time.sleep(0.1)
        self.fail(f"Timed out waiting for {status}")

    def approval(self, task):
        view = self.wait(task, "needs_review")
        pending = [gate for gate in view["approvals"] if gate["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"]["tool"], "github_draft")
        self.assertEqual(self.remote.writes, [])
        return pending[0]

    def test_git_issue_repairs_failed_tests_and_publishes_tested_patch(self):
        client = Mock()
        telegram = Telegram(self.engine, client, {123})
        scope = Scope(123, 123)
        message = {"text": "/issue 1", "chat": {"id": 123, "type": "private"}, "from": {"id": 123}}
        telegram.handle({"update_id": 42, "message": message})
        telegram.handle({"update_id": 42, "message": message})
        jobs = telegram.lookup(scope)
        self.assertEqual(len(jobs), 1)
        task = jobs[0]["id"]
        decision = self.approval(task)
        telegram.deliver_events()
        self.assertTrue(any(call.args[0] == "sendMessage" and
                            "github_draft" in call.args[1]["text"] for call in client.call.call_args_list))
        view = self.engine.task_view(task)
        review = next(step for step in view["steps"] if step["id"] == "review")
        self.assertEqual(review["repairs"], 1)
        self.assertEqual(decision["action"]["args"]["base_sha"], BASE)
        self.assertEqual(decision["action"]["args"]["files"], ["app.py"])
        self.engine.approval(decision["id"], "approve", "")
        finished = self.wait(task, "completed")
        self.assertIn("https://github.com/example/project/pull/7", finished["summary"])
        self.assertEqual([call[0] for call in self.remote.writes], ["git/trees", "git/commits", "git/refs", "pulls"])
        self.assertEqual(self.remote.writes[0][1]["tree"][0]["content"], "def total():\n    return 2\n")
        self.assertTrue(self.remote.writes[-1][1]["draft"])
        self.assertIn("steps/review/tests.txt", finished["artifacts"])
        telegram.deliver_events()
        self.assertTrue(any(call.args[0] == "sendMessage" and
                            "/pull/7" in call.args[1]["text"] for call in client.call.call_args_list))

    def test_changed_patch_is_rejected_after_approval(self):
        task = self.engine.submit("1", "github_issue", {})
        decision = self.approval(task)
        (self.engine.projects / task / "steps/build/changes/app.py").write_text("def total():\n    return 9\n")
        self.engine.approval(decision["id"], "approve", "")
        self.assertIn("current patch", self.wait(task, "failed")["summary"])
        self.assertEqual(self.remote.writes, [])

    def test_lost_pr_reply_recovers_with_one_approval_and_no_duplicate(self):
        task = self.engine.submit("1", "github_issue", {})
        decision = self.approval(task)
        self.remote.lose_pr_reply = True
        self.engine.approval(decision["id"], "approve", "")
        finished = self.wait(task, "completed")
        endpoints = [endpoint for endpoint, _ in self.remote.writes]
        self.assertEqual(endpoints.count("git/refs"), 1)
        self.assertEqual(endpoints.count("pulls"), 1)
        self.assertEqual(len(finished["approvals"]), 1)
        self.assertIn("/pull/7", finished["summary"])
        commits = [payload for endpoint, payload in self.remote.writes if endpoint == "git/commits"]
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0], commits[1])

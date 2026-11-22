import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("BOTTEAM_TEST_DOCKER") == "1", "set BOTTEAM_TEST_DOCKER=1 for the Docker E2E")
class DaemonE2E(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory(prefix="botteam-test-")
        self.addCleanup(self.state.cleanup)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.env = {**os.environ, "BOTTEAM_TOKEN": secrets.token_hex(24),
                    "BOTTEAM_URL": f"http://127.0.0.1:{self.port}"}
        self.process = None
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "botteam", "serve", "--offline", "--port", str(self.port),
             "--data", self.state.name], cwd=ROOT, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read())
            probe = self.cli("health", check=False)
            if probe.returncode == 0:
                self.assertTrue(json.loads(probe.stdout)["sandbox"])
                return
            time.sleep(0.1)
        self.fail("Service did not become ready: " + probe.stderr)

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stderr.close()
        self.process = None

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, "-m", "botteam", *args], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=20, check=check)

    def view(self, task):
        return json.loads(self.cli("show", task).stdout)

    def wait_status(self, task, expected):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            view = self.view(task)
            if view["status"] == expected:
                return view
            if view["status"] in {"failed", "cancelled", "completed"}:
                self.fail(json.dumps(view, indent=2))
            time.sleep(0.15)
        self.fail(f"Expected {expected}: {view['status']}")

    def submit(self):
        return json.loads(self.cli("submit", "Calculate and verify the total for the uploaded orders",
                                   "--demo", "--file", "examples/orders.csv").stdout)["id"]

    def test_restart_at_gate_then_complete(self):
        task = self.submit()
        view = self.wait_status(task, "needs_review")
        approval = view["approvals"][0]
        self.assertEqual(approval["action"]["tool"], "publish")
        self.assertIn("sha256", approval["action"])
        self.assertFalse(any(path.startswith("published/") for path in view["artifacts"]))
        self.stop()
        self.start()
        self.assertEqual(self.view(task)["approvals"][0]["id"], approval["id"])
        self.cli("pause", task)
        self.cli("approve", approval["id"])
        self.assertEqual(self.view(task)["status"], "paused")
        self.assertFalse(any(path.startswith("published/") for path in self.view(task)["artifacts"]))
        self.cli("resume", task)
        view = self.wait_status(task, "completed")
        self.assertEqual({"researcher", "builder", "reviewer", "ops", "writer"},
                         {run["bot"] for run in view["runs"]})
        self.assertIn("45.05", view["summary"])
        self.assertEqual("consumed", view["approvals"][0]["state"])
        report = self.cli("artifact", task, "steps/report/report.md").stdout
        self.assertIn("Independently verified 3 order rows totaling 45.05", report)
        self.assertIn("published/publish/total_orders.py", report)
        events = [json.loads(line) for line in self.cli("events", task).stdout.splitlines()]
        self.assertTrue(any(event["kind"] == "tool_finished" and event["data"]["tool"] == "exec"
                            and event["data"]["result"]["exit_code"] == 0 for event in events))
        self.assertNotEqual(0, self.cli("approve", approval["id"], check=False).returncode)

    def test_reject_prevents_publication(self):
        task = self.submit()
        view = self.wait_status(task, "needs_review")
        self.cli("reject", view["approvals"][0]["id"], "--response", "Demo rejection")
        view = self.wait_status(task, "failed")
        self.assertEqual("rejected", view["approvals"][0]["state"])
        self.assertFalse(any(path.startswith("published/") for path in view["artifacts"]))


if __name__ == "__main__":
    unittest.main()

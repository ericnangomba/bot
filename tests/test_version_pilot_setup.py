import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class VersionPilotSetupTests(unittest.TestCase):
    def setUp(self):
        self.script = runpy.run_path(str(ROOT / "deploy/configure-version-pilot.py"))
        self.main = self.script["main"]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config, self.environment = self.root / "bots.yaml", self.root / "botteam.env"
        self.config.write_text((ROOT / "bots.yaml").read_text())
        self.environment.write_text("UNRELATED_SETTING=keep-me\n")
        self.token = "github_pat_example_for_tests"
        original_mkdtemp = tempfile.mkdtemp

        def make_directory(*args, **kwargs):
            if kwargs.get("dir") == "/var/backups":
                kwargs["dir"] = self.root
            return original_mkdtemp(*args, **kwargs)

        for fixture in (
            patch.dict(self.main.__globals__, {"CONFIG": self.config, "ENV_FILE": self.environment}),
            patch("os.geteuid", return_value=0),
            patch("sys.stdin.isatty", return_value=True),
            patch("getpass.getpass", return_value=self.token),
            patch("tempfile.mkdtemp", side_effect=make_directory),
            patch.object(self.script["GitHubWorkflow"], "checkout"),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    def test_config_preserves_settings_and_is_idempotent(self):
        original = yaml.safe_load(self.config.read_text())
        with patch.dict(self.main.__globals__, {"check_ready": lambda: None}), patch("subprocess.run") as restart:
            self.main()
            self.main()
        self.assertEqual(restart.call_count, 1)
        updated = yaml.safe_load(self.config.read_text())
        original["policy"]["github_repo"] = "ericnangomba/bot"
        original["github_workflow"] = self.script["RECIPE"]
        self.assertEqual(updated, original)
        self.assertIn("UNRELATED_SETTING=keep-me", self.environment.read_text())
        self.assertIn("BOTTEAM_GITHUB_TOKEN=" + self.token, self.environment.read_text())
        self.assertEqual(self.environment.stat().st_mode & 0o777, 0o600)

    def test_readiness_failure_restores_both_files(self):
        before = self.config.read_bytes(), self.environment.read_bytes()

        def failed():
            raise RuntimeError("unready")

        with patch.dict(self.main.__globals__, {"check_ready": failed}), patch("subprocess.run") as restart:
            with self.assertRaisesRegex(RuntimeError, "unready"):
                self.main()
        self.assertEqual(restart.call_count, 2)
        self.assertEqual((self.config.read_bytes(), self.environment.read_bytes()), before)
        self.assertEqual(len(list(self.root.glob("botteam-pilot-*"))), 1)

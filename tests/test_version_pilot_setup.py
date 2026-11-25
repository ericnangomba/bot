import io
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class VersionPilotSetupTests(unittest.TestCase):
    def setUp(self):
        self.script = runpy.run_path(str(ROOT / "deploy/configure-version-pilot.py"))
        self.main = self.script["main"]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config, self.environment = self.root / "botteam-live.yaml", self.root / "botteam.env"
        self.stock = self.root / "bots.yaml"
        self.stock.write_text((ROOT / "bots.yaml").read_text())
        self.config.write_text(self.stock.read_text())
        self.process = self.root / "proc"
        self.process.mkdir()
        (self.process / "cmdline").write_bytes(
            b"python\0-m\0botteam\0serve\0--config\0" + str(self.config).encode() + b"\0"
        )
        self.environment.write_text("UNRELATED_SETTING=keep-me\n")
        self.token = "github_pat_example_for_tests"
        original_mkdtemp = tempfile.mkdtemp

        def make_directory(*args, **kwargs):
            if kwargs.get("dir") == "/var/backups":
                kwargs["dir"] = self.root
            return original_mkdtemp(*args, **kwargs)

        for fixture in (
            patch.dict(self.main.__globals__, {"ENV_FILE": self.environment,
                                               "service_process": lambda: self.process}),
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
        stock = self.stock.read_text()
        ready = Mock()
        with patch.dict(self.main.__globals__, {"check_ready": ready}), patch("subprocess.run") as restart:
            self.main()
            self.main()
        self.assertEqual(restart.call_count, 1)
        ready.assert_called_with(self.config, self.token)
        self.assertEqual(self.stock.read_text(), stock)
        updated = yaml.safe_load(self.config.read_text())
        original["policy"]["github_repo"] = "ericnangomba/bot"
        original["github_workflow"] = self.script["RECIPE"]
        self.assertEqual(updated, original)
        self.assertIn("UNRELATED_SETTING=keep-me", self.environment.read_text())
        self.assertIn("BOTTEAM_GITHUB_TOKEN=" + self.token, self.environment.read_text())
        self.assertEqual(self.environment.stat().st_mode & 0o777, 0o600)

    def test_readiness_failure_restores_both_files(self):
        before = self.config.read_bytes(), self.environment.read_bytes()

        def failed(*_):
            raise RuntimeError("unready")

        with patch.dict(self.main.__globals__, {"check_ready": failed}), patch("subprocess.run") as restart:
            with self.assertRaisesRegex(RuntimeError, "unready"):
                self.main()
        self.assertEqual(restart.call_count, 2)
        self.assertEqual((self.config.read_bytes(), self.environment.read_bytes()), before)
        self.assertEqual(len(list(self.root.glob("botteam-pilot-*"))), 1)

    def test_process_config_uses_working_directory_for_relative_paths(self):
        (self.process / "cwd").symlink_to(self.root, target_is_directory=True)
        (self.process / "cmdline").write_bytes(b"python\0-m\0botteam\0serve\0--config=botteam-live.yaml\0")
        self.assertEqual(self.script["process_config"](self.process), self.config)

    def test_ready_status_alone_does_not_confirm_the_token(self):
        health = b'{"ready":true,"telegram_configured":true,"telegram_polling":true}'
        environment = self.process / "environ"
        environment.write_bytes(b"BOTTEAM_GITHUB_TOKEN=old-token\0")
        ready = self.main.__globals__["check_ready"]
        with patch.dict(self.main.__globals__, {"urlopen": lambda *a, **kw: io.BytesIO(health)}), patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "pilot config and token"):
                ready(self.config, self.token)
            environment.write_bytes(f"BOTTEAM_GITHUB_TOKEN={self.token}\0".encode())
            ready(self.config, self.token)

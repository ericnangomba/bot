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

    def test_ollama_setup_probes_before_switching_all_bots(self):
        original = self.script["pilot_config"](self.config.read_text())
        self.config.write_text(original)
        environment = self.environment.read_bytes()
        setup = self.script["configure_ollama"]
        responses = [
            (200, '{"models":[{"name":"qwen2.5-coder:latest","size":4700000000}]}'),
            (200, '{"done":true,"message":{"content":"{\\"ok\\":true}"}}'),
            (200, '{"done":true,"message":{"content":"{\\"ok\\":true}"}}'),
        ]
        calls = []

        def request(url, timeout, **options):
            self.assertEqual(self.config.read_text(), original)
            self.assertTrue(options["local"])
            calls.append((url, timeout, options))
            return responses.pop(0)

        with patch.dict(setup.__globals__, {"request": request, "check_ready": Mock()}):
            with patch("subprocess.run") as restart:
                setup("qwen2.5-coder:latest", 11435)
        restart.assert_called_once()
        self.assertEqual([url for url, _, _ in calls], [
            "http://127.0.0.1:11435/api/tags", "http://127.0.0.1:11435/api/chat",
            "http://127.0.0.1:11435/api/chat",
        ])
        self.assertEqual([timeout for _, timeout, _ in calls], [5, 180, 180])
        config = yaml.safe_load(self.config.read_text())
        self.assertTrue(all(profile["primary"] == "ollama_local_only" and profile["fallbacks"] == []
                            for profile in [config["director"], *config["bots"]]))
        self.assertEqual(config["github_workflow"], self.script["RECIPE"])
        self.assertEqual(self.environment.read_bytes(), environment)

    def test_ollama_rejects_cloud_models_and_restores_config_if_restart_unready(self):
        before = self.config.read_bytes()
        setup = self.script["configure_ollama"]
        probe = Mock()
        with patch.dict(setup.__globals__, {"probe_ollama": probe,
                                            "check_ready": Mock(side_effect=RuntimeError("unready"))}):
            with patch("subprocess.run") as restart:
                with self.assertRaisesRegex(ValueError, "local model"):
                    setup("model:cloud", 11434)
                probe.assert_not_called()
                restart.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, "unready"):
                    setup("qwen2.5-coder:latest", 11434)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(self.config.read_bytes(), before)

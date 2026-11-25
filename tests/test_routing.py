import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from botteam.actions import ProviderRequestError, Transient, request
from botteam.engine import Engine
from botteam.models import Model
from botteam.routing import BudgetExceeded, RateLimited, Router
from botteam.settings import read_config

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Test goal"}]


def hosted_reply(result):
    return 200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})


def ollama_reply(result):
    return 200, json.dumps({"done": True, "message": {"content": json.dumps(result)}})


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.config = read_config(ROOT / "bots.yaml")
        self.profile = {**self.config["director"], "route": ["local_general", "gemini_free"]}

    def test_ollama_director_requests_distinct_review_and_work_step_shapes(self):
        model = Model(self.config, lambda *args: None)
        plan = {"steps": [{"id": "draft", "capability": "write", "depends_on": [],
                           "instruction": "Draft the receipt."}]}
        with patch("botteam.routing.request", return_value=ollama_reply(plan)) as call:
            self.assertEqual(model.plan("task", {"goal": "Draft a receipt"}, ["write", "review"]), plan)
        shape = call.call_args.args[3]["format"]
        work, review = shape["properties"]["steps"]["items"]["anyOf"]
        self.assertFalse(work["additionalProperties"])
        self.assertNotIn("review_of", work["properties"])
        self.assertEqual(work["properties"]["capability"]["enum"], ["write"])
        self.assertIn("review_of", review["required"])
        self.assertEqual(review["properties"]["capability"]["enum"], ["review"])

    def test_http_rate_limit_and_timeout_fall_back(self):
        for reason in ("HTTP 429", "HTTP transport failure (TimeoutError)", "HTTP 503"):
            with self.subTest(reason=reason), patch.dict(os.environ, {"GEMINI_API_KEY": "fixture"}):
                reserved = []
                router = Router(self.config, lambda *args: reserved.append(args))
                with patch("botteam.routing.request", side_effect=[Transient(reason), hosted_reply({"ok": True})]) as call:
                    self.assertEqual(router.call("task", self.profile, MESSAGES), {"ok": True})
                self.assertEqual([args[2] for args in reserved], ["local_general", "gemini_free"])
                local, cloud = [record.args for record in call.call_args_list]
                self.assertTrue(local[0].endswith("/api/chat"))
                self.assertEqual(local[3]["options"]["num_ctx"], 16384)
                self.assertEqual(local[3]["keep_alive"], "120s")
                self.assertTrue(cloud[0].endswith("/chat/completions"))
                self.assertEqual(cloud[3]["messages"], MESSAGES)

    def test_http_protocol_fallback_over_loopback(self):
        requests = []

        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body))
                if self.path == "/api/chat":
                    self.send_error(429)
                    return
                status, response = hosted_reply({"via": "llama.cpp"})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response.encode())

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
            poller = threading.Thread(target=server.serve_forever, daemon=True)
            poller.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}"
                self.config["providers"]["ollama"]["base_url"] = address
                self.config["providers"]["llamacpp"].update(enabled=True, base_url=address + "/v1")
                self.config["inference"]["local_max_loaded_models"] = 2
                profile = {**self.profile, "route": ["local_general", "local_gguf"]}
                router = Router(self.config, lambda *args: None)
                self.assertEqual(router.call("task", profile, MESSAGES), {"via": "llama.cpp"})
            finally:
                server.shutdown()
                poller.join()
        self.assertEqual([path for path, _ in requests], ["/api/chat", "/v1/chat/completions"])
        self.assertEqual(requests[0][1]["format"], "json")
        self.assertEqual(requests[1][1]["response_format"], {"type": "json_object"})

    def test_json_format_rejection_retries_once_without_response_format(self):
        reserved = []
        router = Router(self.config, lambda *args: reserved.append(args))
        profile = {**self.profile, "route": ["groq_free"]}
        failure = ProviderRequestError(400, {"code": "json_validate_failed"})
        with patch.dict(os.environ, {"GROQ_API_KEY": "fixture"}), patch(
                "botteam.routing.request", side_effect=[failure, hosted_reply({"ok": True})]) as call:
            self.assertEqual(router.call("task", profile, MESSAGES), {"ok": True})
        self.assertEqual([item[2] for item in reserved], ["groq_free", "groq_free"])
        first, second = [record.args[3] for record in call.call_args_list]
        self.assertEqual(first["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", second)
        self.assertEqual({key: val for key, val in first.items() if key != "response_format"}, second)

    def test_json_format_retry_obeys_remaining_budget(self):
        reserved = []

        def reserve(*args):
            if reserved:
                raise BudgetExceeded("Budget exhausted")
            reserved.append(args)

        router = Router(self.config, reserve)
        profile = {**self.profile, "route": ["groq_free"]}
        failure = ProviderRequestError(400, {"param": "response_format"})
        with patch.dict(os.environ, {"GROQ_API_KEY": "fixture"}), patch(
                "botteam.routing.request", side_effect=failure) as call:
            with self.assertRaises(BudgetExceeded):
                router.call("task", profile, MESSAGES)
        self.assertEqual(call.call_count, 1)

    def test_generic_groq_400_falls_back_after_the_rate_window(self):
        router = Router(self.config, lambda *args: None)
        profile = {**self.profile, "route": ["groq_free"]}
        failure = ProviderRequestError(400, {"type": "invalid_request_error"})
        with patch.dict(os.environ, {"GROQ_API_KEY": "fixture"}), patch.object(
                router, "reserve", side_effect=[None, RateLimited(60), None]) as reserved, patch(
                "botteam.routing.request", side_effect=[failure, hosted_reply({"ok": True})]) as call:
            with self.assertRaises(RateLimited):
                router.call("task", profile, MESSAGES)
            self.assertEqual(router.call("task", profile, MESSAGES), {"ok": True})
        self.assertEqual(reserved.call_count, 3)
        first, second = [record.args[3] for record in call.call_args_list]
        self.assertIn("response_format", first)
        self.assertNotIn("response_format", second)

    def test_generic_400_on_another_provider_does_not_retry_without_format(self):
        router = Router(self.config, lambda *args: None)
        profile = {**self.profile, "route": ["gemini_free"]}
        failure = ProviderRequestError(400, {})
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fixture"}), patch(
                "botteam.routing.request", side_effect=failure) as call:
            with self.assertRaisesRegex(ValueError, "gemini_free: HTTP 400"):
                router.call("task", profile, MESSAGES)
        self.assertEqual(call.call_count, 1)

    def test_other_400_preserves_safe_reason_without_json_format_retry(self):
        router = Router(self.config, lambda *args: None)
        profile = {**self.profile, "route": ["groq_free"]}
        failure = ProviderRequestError(400, {"code": "context_length_exceeded"})
        with patch.dict(os.environ, {"GROQ_API_KEY": "fixture"}), patch(
                "botteam.routing.request", side_effect=failure) as call:
            with self.assertRaisesRegex(ValueError, r"HTTP 400 \(context_length_exceeded\)"):
                router.call("task", profile, MESSAGES)
        self.assertEqual(call.call_count, 1)

    def test_http_error_keeps_only_allowlisted_metadata(self):
        url = "https://api.groq.com/openai/v1/chat/completions"
        response = {"error": {"code": "json_validate_failed", "message": "private prompt",
                              "failed_generation": "private output" * 2000, "param": "response_format"}}
        error = HTTPError(url, 400, "Bad Request", {}, io.BytesIO(json.dumps(response).encode()))
        with patch("botteam.actions.HTTP.open", side_effect=error):
            with self.assertRaises(ProviderRequestError) as raised:
                request(url, 1, payload={})
        self.assertTrue(raised.exception.json_mode_failure)
        self.assertEqual(str(raised.exception), "HTTP 400 (json_validate_failed)")

    def test_paid_route_does_not_call_provider_at_zero_budget(self):
        profile = {**self.profile, "route": ["openai_mini", "local_general"]}
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {"OPENAI_API_KEY": "fixture"}):
            engine = Engine(self.config, data)
            task = engine.submit("test", "live", {})
            with patch("botteam.routing.request", return_value=ollama_reply({"ok": True})) as call:
                self.assertEqual(engine.model.router.call(task, profile, MESSAGES), {"ok": True})
            self.assertEqual(call.call_count, 1)
            self.assertTrue(call.call_args.args[0].endswith("/api/chat"))
            self.assertEqual(engine.task_view(task)["cost"], 0)
            with self.assertRaises(BudgetExceeded):
                engine.model.router.call(task, {**profile, "route": ["openai_mini"]}, MESSAGES)

    def test_per_provider_rate_limit_is_persistent(self):
        with tempfile.TemporaryDirectory() as data:
            first = Engine(self.config, data)
            task = first.submit("test", "live", {})
            model = self.config["models"]["local_general"]
            first.reserve(task, model, "local_general", "ollama", 1, 100, 100)
            after_restart = Engine(self.config, data)
            with self.assertRaises(RateLimited):
                after_restart.reserve(task, model, "local_general", "ollama", 1, 100, 100)
            after_restart.reserve(task, model, "local_general", "other", 1, 100, 100)
            self.assertEqual(after_restart.task_view(task)["tokens"], 400)

    def test_ollama_idle_eviction_and_active_limit(self):
        models = self.config["models"]
        models["alternate"] = {**models["local_general"], "name": "alternate:7b"}
        router = Router(self.config, lambda *args: None)
        provider = self.config["providers"]["ollama"]
        with router.slot("first", "ollama", provider, models["local_general"], router.defaults):
            with self.assertRaises(RateLimited):
                with router.slot("second", "ollama", provider, models["alternate"], router.defaults):
                    self.fail("admitted concurrent model")
        with patch("botteam.routing.request", return_value=(200, "{}")) as unload:
            with router.slot("second", "ollama", provider, models["alternate"], router.defaults):
                self.assertEqual(set(router.ollama_residents), {("ollama", "alternate:7b")})
        self.assertTrue(unload.call_args.args[0].endswith("/api/generate"))
        self.assertEqual(unload.call_args.args[3]["keep_alive"], 0)

    def test_llamacpp_pins_residency_and_uses_server_context(self):
        self.config["providers"]["llamacpp"]["enabled"] = True
        profile = {**self.profile, "route": ["local_general", "local_gguf"],
                   "inference": {"context_length": 32768}}
        router = Router(self.config, lambda *args: None)
        with patch("botteam.routing.request", return_value=hosted_reply({"ok": True})) as call:
            self.assertEqual(router.call("task", profile, MESSAGES), {"ok": True})
        self.assertEqual(call.call_count, 1)
        self.assertIn(":8080/v1/chat/completions", call.call_args.args[0])
        self.assertTrue(call.call_args.kwargs["local"])


if __name__ == "__main__":
    unittest.main()

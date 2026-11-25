import collections
import contextlib
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlsplit

from .actions import ProviderRequestError, Transient, request
from .store import encode


class RateLimited(Transient):
    def __init__(self, delay):
        self.delay = delay
        super().__init__("Model route is busy, cooling down, or rate limited")


class BudgetExceeded(ValueError):
    pass


class Router:
    def __init__(self, config, reserve):
        self.providers = config["providers"]
        self.models = config["models"]
        self.defaults = config["inference"]
        self.policy = config["policy"]
        self.reserve = reserve
        self.lock = threading.Lock()
        self.provider_load = collections.Counter()
        self.role_load = collections.Counter()
        self.cooldown = {}
        self.pending_plain_json = collections.OrderedDict()
        self.ollama_residents = {}
        self.static_residents = len({
            model["provider"] for model in self.models.values()
            if self.providers[model["provider"]]["kind"] == "llama_cpp"
            and self.providers[model["provider"]].get("enabled", True)
        })
        self.local_active = 0

    def configured(self):
        return any(provider.get("enabled", True) and (not provider.get("key_env")
                   or os.environ.get(provider["key_env"])) for provider in self.providers.values())

    @contextlib.contextmanager
    def slot(self, role, name, provider, model, inference):
        kind = provider["kind"]
        local = kind != "openai_compatible"
        resident_key = (name, model["name"])
        keep_alive = min(inference["keep_alive"], self.defaults["local_idle_seconds"])
        with self.lock:
            now = time.monotonic()
            if self.cooldown.get(name, 0) > now:
                raise RateLimited(self.cooldown[name] - now)
            if (self.role_load[role] >= inference["concurrency"]
                    or self.provider_load[name] >= provider["concurrency"]
                    or (local and self.local_active >= self.defaults["local_max_parallel"])):
                raise RateLimited(1)
            if kind == "ollama":
                self.ollama_residents = {key: value for key, value in self.ollama_residents.items()
                                         if value[0] or value[1] > now}
                if resident_key not in self.ollama_residents:
                    self.make_room()
                active, expiry = self.ollama_residents.get(resident_key, (0, now))
                self.ollama_residents[resident_key] = (active + 1, expiry)
            self.provider_load[name] += 1
            self.role_load[role] += 1
            self.local_active += int(local)
        try:
            yield keep_alive
        finally:
            with self.lock:
                self.role_load[role] -= 1
                self.provider_load[name] -= 1
                self.local_active -= int(local)
                if kind == "ollama":
                    active, _ = self.ollama_residents[resident_key]
                    self.ollama_residents[resident_key] = (active - 1, time.monotonic() + keep_alive)

    def make_room(self):
        capacity = self.defaults["local_max_loaded_models"] - self.static_residents
        if capacity < 1:
            raise ValueError("Local residency budget is occupied by enabled llama.cpp instances")
        if len(self.ollama_residents) < capacity:
            return
        idle = [key for key, (active, _) in self.ollama_residents.items() if not active]
        if not idle:
            raise RateLimited(1)
        candidate = min(idle, key=lambda key: self.ollama_residents[key][1])
        provider = self.providers[candidate[0]]
        key = os.environ.get(provider.get("key_env", ""))
        request(self.url(provider, "api/generate"), 3, self.headers(key),
                {"model": candidate[1], "prompt": "", "keep_alive": 0, "stream": False}, local=True)
        del self.ollama_residents[candidate]

    @staticmethod
    def url(provider, endpoint):
        return provider["base_url"].rstrip("/") + "/" + endpoint

    @staticmethod
    def headers(key):
        return {"Authorization": f"Bearer {key}"} if key else {}

    def call(self, task, profile, messages):
        request_bytes = encode(messages).encode()
        estimated_input = len(request_bytes) + 128
        request_hash = hashlib.sha256(request_bytes).digest()
        if estimated_input > self.policy["max_input_tokens"]:
            raise ValueError("Prompt exceeds the configured input ceiling")
        inference = {**self.defaults, **profile.get("inference", {})}
        reasons = []
        busy = []
        retriable = False
        budget_unavailable = False
        for alias in profile["route"]:
            model = self.models[alias]
            name = model["provider"]
            provider = self.providers[name]
            key = os.environ.get(provider.get("key_env", ""))
            if not provider.get("enabled", True) or (provider.get("key_env") and not key):
                reasons.append(f"{alias}: disabled or missing credentials")
                continue
            context = min(inference["context_length"], provider.get("context_length", inference["context_length"]))
            output = min(self.policy["max_output_tokens"], context - estimated_input)
            if output < 256:
                reasons.append(f"{alias}: insufficient context")
                continue
            try:
                with self.slot(profile["id"], name, provider, model, inference) as keep_alive:
                    reservation = task, model, alias, name, provider["rpm"], estimated_input, output
                    memo = (task, profile["id"], alias, request_hash)
                    with self.lock:
                        needs_plain = memo in self.pending_plain_json
                    self.reserve(*reservation)
                    try:
                        result = self.invoke(provider, model, messages, inference, context, output, key, keep_alive,
                                             use_json_mode=not needs_plain)
                    except ProviderRequestError as exc:
                        groq = urlsplit(provider["base_url"]).hostname == "api.groq.com"
                        generic_groq = groq and exc.code in (None, "invalid_request_error")
                        if (needs_plain or exc.status != 400 or not (exc.json_mode_failure or generic_groq) or
                                provider["kind"] == "ollama" or not provider.get("json_mode", True)):
                            raise
                        # Keep the fallback when the next reservation must wait for the RPM window.
                        with self.lock:
                            self.pending_plain_json[memo] = None
                            if len(self.pending_plain_json) > 256:
                                self.pending_plain_json.popitem(last=False)
                        self.reserve(*reservation)
                        result = self.invoke(provider, model, messages, inference, context, output, key, keep_alive,
                                             use_json_mode=False)
                    with self.lock:
                        self.pending_plain_json.pop(memo, None)
                    return result
            except RateLimited as exc:
                busy.append(exc.delay)
                reasons.append(f"{alias}: busy or rate limited")
            except BudgetExceeded:
                budget_unavailable = True
                reasons.append(f"{alias}: task budget")
            except Transient as exc:
                retriable = True
                duration = 30 if str(exc) == "HTTP 429" else 5
                with self.lock:
                    self.cooldown[name] = time.monotonic() + duration
                reasons.append(f"{alias}: {exc}")
            except ProviderRequestError as exc:
                reasons.append(f"{alias}: {exc}")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                detail = str(exc) if type(exc) is ValueError and str(exc).startswith("Local residency") else ""
                reasons.append(f"{alias}: {detail or 'invalid or unavailable response'}")
        if retriable:
            raise Transient("No model route succeeded (" + "; ".join(reasons) + ")")
        if busy:
            raise RateLimited(min(busy))
        if budget_unavailable:
            raise BudgetExceeded("Task budget prevents the remaining model routes")
        raise ValueError("No usable model route (" + "; ".join(reasons) + ")")

    def invoke(self, provider, model, messages, inference, context, output, key, keep_alive, *, use_json_mode=True):
        kind = provider["kind"]
        timeout = provider.get("timeout_seconds", self.policy["model_timeout_seconds"])
        if kind == "ollama":
            url = self.url(provider, "api/chat")
            payload = {"model": model["name"], "messages": messages, "stream": False, "format": "json",
                       "keep_alive": f"{keep_alive}s",
                       "options": {"num_ctx": context, "num_predict": output, "temperature": inference["temperature"]}}
        else:
            url = self.url(provider, "chat/completions")
            payload = {"model": model["name"], "messages": messages, "temperature": inference["temperature"],
                       provider.get("output_parameter", "max_tokens"): output}
            if use_json_mode and provider.get("json_mode", True):
                payload["response_format"] = {"type": "json_object"}
        _, body = request(url, timeout, self.headers(key), payload, local=kind != "openai_compatible")
        reply = json.loads(body)
        if kind == "ollama":
            if not reply.get("done") or reply.get("done_reason") == "length":
                raise ValueError("Model output was incomplete")
            content = reply["message"]["content"]
        else:
            choice = reply["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Model output was incomplete")
            content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("A JSON response string is required")
        content = content.strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.partition("\n")[2][:-3]
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object from model")
        return result

import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

IDENT = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
TOOLS = {"read", "write", "search", "fetch", "exec", "patch", "publish",
         "integration", "pull_request", "schedule", "clarify"}
EFFECTS = {"integration", "pull_request"}
GATED = EFFECTS | {"publish", "schedule"}


def identifier(value):
    return isinstance(value, str) and bool(IDENT.fullmatch(value))


def positive(value):
    return type(value) is int and value > 0


def validate_inference(options):
    if not positive(options["context_length"]) or not positive(options["concurrency"]):
        raise ValueError("Inference context_length and concurrency must be positive integers")
    if type(options["keep_alive"]) is not int or options["keep_alive"] < 0:
        raise ValueError("Inference keep_alive must be nonnegative seconds")
    if not 0 <= options["temperature"] <= 2:
        raise ValueError("Inference temperature must be between 0 and 2")


def read_config(path, offline=False):
    config = yaml.safe_load(Path(path).read_text())
    if offline:
        config["provider"] = "offline"
    if config["provider"] not in {"offline", "live"}:
        raise ValueError("provider must be live or offline")
    policy = config["policy"]
    if policy["autonomy"] not in {"safe", "confirm_all", "read_only"}:
        raise ValueError("Unknown autonomy level")
    for key in ("max_parallel", "model_rpm", "model_timeout_seconds", "tool_timeout_seconds",
                "token_budget", "max_input_tokens", "max_output_tokens", "max_turns", "max_attempts"):
        if not positive(policy[key]):
            raise ValueError(f"Invalid policy setting {key}")
    if type(policy["max_repairs"]) is not int or policy["max_repairs"] < 0:
        raise ValueError("Invalid max_repairs")
    if not math.isfinite(policy["budget_usd"]) or policy["budget_usd"] < 0:
        raise ValueError("Invalid budget_usd")
    if not set(policy["disabled_tools"]) <= TOOLS:
        raise ValueError("Unknown disabled tool")

    defaults = config["inference"]
    validate_inference(defaults)
    for key in ("local_max_parallel", "local_max_loaded_models", "local_idle_seconds"):
        if not positive(defaults[key]):
            raise ValueError(f"Invalid inference setting {key}")
    for name, provider in config["providers"].items():
        kind = provider["kind"]
        url = urlsplit(provider["base_url"])
        schemes = {"https"} if kind == "openai_compatible" else {"http", "https"}
        if (not identifier(name) or kind not in {"openai_compatible", "ollama", "llama_cpp"}
                or url.scheme not in schemes or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError(f"Invalid provider {name}")
        if not positive(provider["rpm"]) or not positive(provider["concurrency"]):
            raise ValueError(f"Invalid provider limits for {name}")
        if not positive(provider.get("timeout_seconds", policy["model_timeout_seconds"])):
            raise ValueError(f"Invalid provider timeout for {name}")
        if kind == "llama_cpp" and not positive(provider.get("context_length")):
            raise ValueError(f"llama.cpp provider {name} requires its server context_length")
        if any(key in provider and type(provider[key]) is not bool for key in ("enabled", "json_mode")):
            raise ValueError(f"Invalid provider flags for {name}")
        if provider.get("output_parameter", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(f"Invalid output_parameter for {name}")
    models = config["models"]
    llama_names = {}
    for alias, model in models.items():
        if (not identifier(alias) or not isinstance(model["name"], str) or not model["name"]
                or model["provider"] not in config["providers"]
                or any(not math.isfinite(model[key]) or model[key] < 0 for key in ("input_usd_m", "output_usd_m"))):
            raise ValueError(f"Invalid model {alias}")
        provider = config["providers"][model["provider"]]
        if provider["kind"] == "llama_cpp":
            previous = llama_names.setdefault(model["provider"], model["name"])
            if previous != model["name"]:
                raise ValueError("A llama.cpp endpoint can serve only one configured model")
    static_models = sum(config["providers"][name].get("enabled", True) for name in llama_names)
    if static_models > defaults["local_max_loaded_models"]:
        raise ValueError("Enabled llama.cpp instances exceed the local model residency budget")

    config["director"]["id"] = "director"
    ids = set()
    override_keys = {"context_length", "keep_alive", "temperature", "concurrency"}
    for profile in [config["director"], *config["bots"]]:
        route = [profile["primary"], *profile.get("fallbacks", [])]
        if len(set(route)) != len(route) or any(alias not in models for alias in route):
            raise ValueError(f"Unknown or repeated model alias in route for {profile['id']}")
        overrides = profile.get("inference", {})
        if not set(overrides) <= override_keys:
            raise ValueError(f"Unknown inference override for {profile['id']}")
        validate_inference({**defaults, **overrides})
        profile["route"] = route
        profile["model"] = models[route[0]]["name"]
        profile["price"] = sum(models[route[0]][key] for key in ("input_usd_m", "output_usd_m"))
        if profile["id"] == "director":
            if profile is not config["director"]:
                raise ValueError("Reserved bot ID director")
            continue
        bot = profile
        if not identifier(bot["id"]) or bot["id"] in ids:
            raise ValueError("Invalid or duplicate bot ID")
        ids.add(bot["id"])
        if not positive(bot["slots"]) or not bot["capabilities"]:
            raise ValueError("Bot needs positive slots and at least one capability")
        if not all(identifier(cap) for cap in bot["capabilities"]) or not bot["persona"]:
            raise ValueError("Invalid capability or empty persona")
        if not set(bot["tools"]) <= TOOLS:
            raise ValueError("Unknown bot tool")
    return config


def validate_plan(plan, capabilities):
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError("A plan needs 1 to 32 steps")
    by_id = {}
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each step must be an object")
        name, capability = step.get("id"), step.get("capability")
        if not identifier(name) or name in by_id or capability not in capabilities:
            raise ValueError("Unexpected, unsupported, or duplicate step")
        deps = step.get("depends_on")
        if not isinstance(deps, list) or not all(identifier(dep) for dep in deps) or len(set(deps)) != len(deps):
            raise ValueError("depends_on must contain unique step IDs")
        instruction = step.get("instruction")
        if not isinstance(instruction, str) or not 1 <= len(instruction) <= 3000:
            raise ValueError("Invalid step instruction")
        target = step.get("review_of")
        if capability == "review" and (not identifier(target) or target not in deps):
            raise ValueError("A review must directly depend on its target")
        if capability != "review" and target is not None:
            raise ValueError("Only a review can name a review target")
        by_id[name] = step
    remaining = set(by_id)
    done = set()
    while remaining:
        ready = {name for name in remaining if set(by_id[name]["depends_on"]) <= done}
        if not ready:
            raise ValueError("Missing dependency or cycle in plan")
        done.update(ready)
        remaining -= ready
    def ancestors(name):
        result = set()
        pending = list(by_id[name]["depends_on"])
        while pending:
            node = pending.pop()
            if node not in result:
                result.add(node)
                pending.extend(by_id[node]["depends_on"])
        return result
    ancestors_by_id = {name: ancestors(name) for name in by_id}
    for name, step in by_id.items():
        if step["capability"] == "review":
            target = step["review_of"]
            if by_id[target]["capability"] in {"review", "ops"}:
                raise ValueError("A review target must be repeatable work")
            if any(target in parents and name not in parents and candidate != name
                   for candidate, parents in ancestors_by_id.items()):
                raise ValueError("Downstream work must depend on the review")
        if step["capability"] == "ops":
            unreviewed = [parent for parent in ancestors_by_id[name] if by_id[parent]["capability"] == "build"
                          and not any(by_id[review].get("review_of") == parent for review in ancestors_by_id[name])]
            if unreviewed:
                raise ValueError("Operational work requires review of upstream build steps")
    return steps

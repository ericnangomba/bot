"""Configure the GitHub pilot or switch Bot Team to an existing Ollama server."""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from botteam.actions import Toolbox, Transient, request  # noqa: E402
from botteam.github_workflow import GitHubWorkflow  # noqa: E402
from botteam.settings import read_config  # noqa: E402

REPO = "ericnangomba/bot"
ENV_FILE = Path("/etc/botteam.env")
TOKEN_KEY = "BOTTEAM_GITHUB_TOKEN"
TOKEN_LINE = re.compile(r"^[ \t]*BOTTEAM_GITHUB_TOKEN[ \t]*=(.*)$")
VERSION_TEST = '''import contextlib, io, os, runpy, sys, types
sys.modules.setdefault("yaml", types.ModuleType("yaml"))
def cli(arg):
    out = io.StringIO()
    sys.argv = ["botteam", arg]
    with contextlib.redirect_stdout(out):
        try:
            runpy.run_module("botteam", run_name="__main__")
        except SystemExit as exc:
            assert exc.code == 0
    return out.getvalue()
os.environ["BOTTEAM_VERSION"] = "0.1.0-test"
assert cli("--version").strip() == "botteam 0.1.0-test"
os.environ.pop("BOTTEAM_VERSION")
assert cli("--version").strip() == "botteam unknown"
assert "submit" in cli("--help")
'''
RECIPE = {
    "base": "main",
    "paths": ["botteam/" + filename for filename in (
        "__init__.py", "__main__.py", "actions.py", "api.py", "engine.py", "github_workflow.py",
        "models.py", "routing.py", "settings.py", "store.py", "telegram.py", "web.py",
    )],
    "editable_paths": ["botteam/__main__.py"],
    "test_paths": ["botteam/__init__.py"],
    "test_argv": ["python", "-c", VERSION_TEST],
}


def mapping_entry(node, name):
    if isinstance(node, yaml.MappingNode):
        return next((val for key, val in node.value if key.value == name), None)
    return None


def pilot_config(text):
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("policy"), dict):
        raise ValueError("The VM's bots.yaml is missing its policy section.")
    if data["policy"].get("github_repo") not in (None, "", REPO):
        raise ValueError("A different GitHub repository is already configured.")
    if data.get("github_workflow") not in (None, RECIPE):
        raise ValueError("A different GitHub workflow is already configured.")
    root = yaml.compose(text)
    repo = mapping_entry(mapping_entry(root, "policy"), "github_repo")
    workflow = mapping_entry(root, "github_workflow")
    if not isinstance(repo, yaml.ScalarNode):
        raise ValueError("Add a github_repo value under policy before running setup.")
    if workflow is not None and data.get("github_workflow") is None:
        raise ValueError("Remove the existing empty github_workflow entry before running setup.")
    if data["policy"].get("github_repo") != REPO:
        start, end = repo.start_mark.index, repo.end_mark.index
        prefix = " " if start == end and text[start - 1:start] == ":" else ""
        text = text[:start] + prefix + json.dumps(REPO) + text[end:]
    if workflow is None:
        text = text.rstrip("\n") + "\n\n" + yaml.safe_dump({"github_workflow": RECIPE}, sort_keys=False)
    data["policy"]["github_repo"] = REPO
    data["github_workflow"] = RECIPE
    if yaml.safe_load(text) != data:
        raise ValueError("Could not update only the GitHub pilot settings safely.")
    return text


def token_config(text, token):
    lines = text.splitlines(keepends=True)
    matches = [line for line in lines if TOKEN_LINE.fullmatch(line.rstrip("\r\n"))]
    if len(matches) > 1:
        raise ValueError("The environment file has duplicate GitHub token settings.")
    if matches:
        lines[lines.index(matches[0])] = f"{TOKEN_KEY}={token}\n"
        return "".join(lines)
    return text.rstrip("\n") + f"\n{TOKEN_KEY}={token}\n"


def write_config(path, content, mode=None):
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".botteam-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), mode if mode is not None else info.st_mode & 0o777)
            os.fchown(file.fileno(), info.st_uid, info.st_gid)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def service_process():
    result = subprocess.run(
        ["systemctl", "show", "botteam", "--property=MainPID", "--value"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    pid = result.stdout.strip()
    if not pid.isdecimal() or int(pid) == 0:
        raise ValueError("Start the Bot Team service before running setup.")
    return Path("/proc") / pid


def process_config(process):
    args = [os.fsdecode(arg) for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
    if "-m" not in args or args[args.index("-m") + 1:args.index("-m") + 3] != ["botteam", "serve"]:
        raise ValueError("The systemd process is not running the Bot Team CLI.")
    config = "bots.yaml"
    for index, arg in enumerate(args):
        if arg == "--config" and index + 1 < len(args):
            config = args[index + 1]
        elif arg.startswith("--config="):
            config = arg.partition("=")[2]
    path = Path(config)
    return (path if path.is_absolute() else (process / "cwd").resolve() / path).resolve()


def check_ready(config_path, token=None):
    for _ in range(30):
        try:
            with urlopen("http://127.0.0.1:8042/healthz", timeout=2) as response:
                health = json.load(response)
            if (health["ready"] and (token is not None or health.get("model_configured")) and
                    (not health["telegram_configured"] or health["telegram_polling"])):
                process = service_process()
                if process_config(process) == config_path:
                    if token is None:
                        return
                    entries = (process / "environ").read_bytes().split(b"\0")
                    if f"{TOKEN_KEY}={token}".encode() in entries:
                        return
        except (OSError, URLError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    target = "the pilot config and token" if token is not None else "the Ollama config"
    raise RuntimeError(f"Bot Team did not become ready with {target}.")


def ollama_config(text, model, port):
    config = yaml.safe_load(text)
    alias = "ollama_local_only"
    config["provider"] = "live"
    config["providers"][alias] = {
        "kind": "ollama", "base_url": f"http://127.0.0.1:{port}",
        "rpm": 60, "concurrency": 1, "timeout_seconds": 180,
    }
    config["models"][alias] = {"provider": alias, "name": model, "input_usd_m": 0, "output_usd_m": 0}
    config["inference"].update(keep_alive=600, local_idle_seconds=600, local_max_parallel=1)
    for profile in [config["director"], *config["bots"]]:
        profile.update(primary=alias, fallbacks=[])
        # Keep the context allocation and lifetime consistent when bots share one loaded model.
        overrides = profile.get("inference", {})
        overrides.pop("context_length", None)
        overrides.pop("keep_alive", None)
    config["policy"]["budget_usd"] = 0
    return yaml.safe_dump(config, sort_keys=False)


def probe_ollama(config):
    route = config["director"]["primary"]
    model = config["models"][route]["name"]
    provider = config["providers"][route]
    url = provider["base_url"]
    timeout = provider["timeout_seconds"]
    _, body = request(url + "/api/tags", 5, local=True)
    installed = json.loads(body)["models"]
    if not any(entry.get("name") == model and entry.get("size", 0) > 0
               and not entry.get("remote_host") and not entry.get("remote_model") for entry in installed):
        raise ValueError(f"{model} must be installed locally on the Ollama server. Use the full model tag.")
    payload = {
        "model": model, "format": "json", "stream": False, "keep_alive": "600s",
        "messages": [{"role": "user", "content": 'Return only the JSON object {"ok":true}.'}],
        "options": {"num_ctx": config["inference"]["context_length"], "num_predict": 256, "temperature": 0},
    }
    print(f"Checking {model} on VM loopback. Model loading may take a while.", flush=True)
    for label in ("First request", "Warm request"):
        started = time.monotonic()
        _, body = request(url + "/api/chat", timeout, payload=payload, local=True)
        reply = json.loads(body)
        if (not reply.get("done") or reply.get("done_reason") == "length" or
                json.loads(reply["message"]["content"]) != {"ok": True}):
            raise ValueError(f"{model} did not pass the structured response check.")
        print(f"{label}: {time.monotonic() - started:.1f}s", flush=True)


def configure_ollama(model, port):
    if os.geteuid() != 0:
        raise ValueError("Run this setup with sudo on the Bot Team VM.")
    if not 1 <= port <= 65535 or not model or model.lower().endswith((":cloud", "-cloud")):
        raise ValueError("Supply a local model tag and valid Ollama port.")
    path = process_config(service_process())
    original = path.read_text()
    updated = ollama_config(original, model, port)
    with tempfile.TemporaryDirectory(prefix="botteam-ollama-check-") as directory:
        candidate = Path(directory) / "bots.yaml"
        candidate.write_text(updated)
        config = read_config(candidate)
        probe_ollama(config)
    if updated != original:
        backup = Path(tempfile.mkdtemp(prefix="botteam-ollama-", dir="/var/backups")) / path.name
        backup.write_text(original)
        write_config(path, updated)
        try:
            subprocess.run(["systemctl", "restart", "botteam"], check=True, timeout=60)
            check_ready(path)
        except (Exception, KeyboardInterrupt):
            write_config(path, original)
            subprocess.run(["systemctl", "restart", "botteam"], check=True, timeout=60)
            raise
        print(f"Previous config saved at {backup}.")
    else:
        check_ready(path)
    print(f"Bot Team now uses {model} for all agent inference.")


def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise ValueError("Run this setup with sudo in your interactive VM terminal.")
    config_path = process_config(service_process())
    original_config, original_env = config_path.read_text(), ENV_FILE.read_text()
    updated_config = pilot_config(original_config)
    old_token = next((match.group(1).strip().strip("\"'") for line in original_env.splitlines()
                      if (match := TOKEN_LINE.fullmatch(line))), "")
    prompt = "GitHub token (Enter keeps the current token): " if old_token else "Paste your GitHub token: "
    token = getpass.getpass(prompt).strip() or old_token
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,250}", token):
        raise ValueError("Enter a valid repository-scoped GitHub token.")
    updated_env = token_config(original_env, token)

    with tempfile.TemporaryDirectory(prefix="botteam-pilot-check-") as temporary:
        root = Path(temporary)
        candidate = root / "bots.yaml"
        candidate.write_text(updated_config)
        config = read_config(candidate)
        workflow = GitHubWorkflow(config, Toolbox(root, config["policy"]))
        os.environ[TOKEN_KEY] = token
        try:
            workflow.checkout(root, 1)
        finally:
            os.environ.pop(TOKEN_KEY, None)

    if updated_config == original_config and updated_env == original_env:
        check_ready(config_path, token)
        print("GitHub pilot is already configured. Send /issue 1 in Telegram.")
        return
    backup = Path(tempfile.mkdtemp(prefix="botteam-pilot-", dir="/var/backups"))
    (backup / config_path.name).write_text(original_config)
    secret_backup = backup / ENV_FILE.name
    secret_backup.touch(mode=0o600)
    secret_backup.write_text(original_env)
    env_mode = ENV_FILE.stat().st_mode & 0o777
    try:
        write_config(ENV_FILE, updated_env, mode=0o600)
        write_config(config_path, updated_config)
        subprocess.run(["systemctl", "restart", "botteam"], check=True, timeout=60)
        check_ready(config_path, token)
    except (Exception, KeyboardInterrupt):
        write_config(config_path, original_config)
        write_config(ENV_FILE, original_env, mode=env_mode)
        subprocess.run(["systemctl", "restart", "botteam"], check=False, timeout=60)
        raise
    print(f"Pilot configured. Backup saved in {backup}.")
    print("Send /issue 1 in Telegram.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama", metavar="MODEL", help="Route every bot to this installed local Ollama model")
    parser.add_argument("--ollama-port", type=int, default=11434, help="Ollama port on VM loopback")
    args = parser.parse_args()
    try:
        if args.ollama is None:
            main()
        else:
            configure_ollama(args.ollama, args.ollama_port)
    except (OSError, KeyError, ValueError, RuntimeError, Transient, subprocess.SubprocessError) as error:
        print(f"Bot Team setup stopped: {error}", file=sys.stderr)
        sys.exit(1)

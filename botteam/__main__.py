import argparse
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .actions import read_bytes
from .api import serve


def api(path, payload=None, raw=False):
    base = os.environ.get("BOTTEAM_URL", "http://127.0.0.1:8042").rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ.get("BOTTEAM_TOKEN", "")}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(base + path, data=data, headers=headers), timeout=15) as response:
            result = response.read()
        return result if raw else json.loads(result)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(6000))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        raise ValueError(body.get("error", str(body) if body else f"HTTP {exc.code}")) from None
    except URLError as exc:
        raise ValueError(f"Could not reach Director at {base}: {exc.reason}") from None


def display(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def watch(args, task_path):
    after = args.after
    notices = {"task_completed", "task_failed", "task_cancelled", "approval_requested"}
    while True:
        items = api(task_path + f"/events?after={after}")
        for item in items:
            after = item["seq"]
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if args.desktop and item["kind"] in notices and shutil.which("notify-send"):
                subprocess.run(["notify-send", "Bot Team: " + item["kind"].replace("_", " "), item["task"]],
                               timeout=3, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if len(items) == 200:
            continue
        if args.once:
            return
        if task_path:
            task = api(task_path)
            if task["status"] == "completed":
                return
            if task["status"] in {"failed", "cancelled"}:
                raise ValueError(task["summary"] or task["status"])
            if args.until_input and task["status"] in {"blocked", "needs_review"}:
                return
        time.sleep(max(0.1, args.interval))


def parser():
    root = argparse.ArgumentParser(prog="botteam", description="Run and control the Director service")
    subs = root.add_subparsers(dest="command", required=True)
    daemon = subs.add_parser("serve")
    daemon.add_argument("--config", default="bots.yaml")
    daemon.add_argument("--data", default="data")
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", default=8042, type=int)
    daemon.add_argument("--offline", action="store_true")
    submit = subs.add_parser("submit")
    submit.add_argument("goal")
    submit.add_argument("--file", type=Path, action="append", default=[])
    submit.add_argument("--demo", action="store_true")
    subs.add_parser("health")
    listing = subs.add_parser("list")
    listing.add_argument("--limit", type=int, default=30)
    for command in ("show", "pause", "resume", "cancel", "retry"):
        subs.add_parser(command).add_argument("task")
    for command in ("approve", "reject"):
        decision = subs.add_parser(command)
        decision.add_argument("approval")
        decision.add_argument("--response", default="")
    for command in ("events", "watch"):
        logs = subs.add_parser(command)
        logs.add_argument("task", nargs="?")
        logs.add_argument("--after", type=int, default=0)
        logs.add_argument("--interval", type=float, default=1)
        logs.add_argument("--until-input", action="store_true")
        logs.add_argument("--desktop", action="store_true")
        logs.set_defaults(once=command == "events")
    artifact = subs.add_parser("artifact")
    artifact.add_argument("task")
    artifact.add_argument("path")
    artifact.add_argument("-o", "--output", type=Path)
    schedule = subs.add_parser("schedule")
    schedule.add_argument("goal")
    schedule.add_argument("--every", type=int, required=True, metavar="SECONDS")
    subs.add_parser("schedules")
    subs.add_parser("disable-schedule").add_argument("schedule")
    return root


def main():
    args = parser().parse_args()
    command = args.command
    if command == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        serve(args)
    elif command == "submit":
        if len({path.name for path in args.file}) != len(args.file):
            raise ValueError("Attachment basenames must be unique")
        attachments = {path.name: base64.b64encode(read_bytes(path)).decode() for path in args.file}
        display(api("/tasks", {"goal": args.goal, "mode": "demo" if args.demo else "live", "attachments": attachments}))
    elif command == "health":
        display(api("/healthz"))
    elif command == "list":
        display(api(f"/tasks?limit={args.limit}"))
    elif command in {"approve", "reject"}:
        display(api(f"/approvals/{quote(args.approval, safe='')}/{command}", {"response": args.response}))
    elif command == "schedule":
        display(api("/schedules", {"goal": args.goal, "interval_seconds": args.every}))
    elif command == "schedules":
        display(api("/schedules"))
    elif command == "disable-schedule":
        display(api(f"/schedules/{quote(args.schedule, safe='')}/disable", {}))
    else:
        task = "/tasks/" + quote(args.task, safe="") if args.task else ""
        if command in {"events", "watch"}:
            watch(args, task)
        elif command == "show":
            display(api(task))
        elif command == "artifact":
            content = api(task + "/artifacts/" + quote(args.path, safe="/"), raw=True)
            if args.output:
                args.output.write_bytes(content)
            else:
                sys.stdout.buffer.write(content)
        else:
            display(api(task + "/" + command, {}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"botteam: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

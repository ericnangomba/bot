import collections
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid

from .actions import Toolbox, Transient, bind_action, path_under, save_attachments, text_arg
from .models import CONTRACTS, Model
from .routing import BudgetExceeded, RateLimited
from .settings import EFFECTS, GATED, validate_plan
from .store import Store, encode, event

LOG = logging.getLogger("botteam")
TERMINAL = {"completed", "failed", "cancelled"}
MUTATING = GATED | {"write", "patch", "exec"}
MAX_GOAL = 7000


class Conflict(ValueError):
    pass


def fields(row, *names):
    return {name: row[name] for name in names}


def decode(row, *names):
    result = dict(row)
    for name in names:
        if result[name] is not None:
            result[name] = json.loads(result[name])
    return result


class Engine:
    def __init__(self, config, root):
        self.config = config
        self.policy = config["policy"]
        self.store = Store(root)
        self.root = self.store.root
        self.projects = self.root / "projects"
        self.projects.mkdir(exist_ok=True)
        self.bots = {bot["id"]: bot for bot in config["bots"]}
        self.capabilities = sorted({cap for profile in self.bots.values() for cap in profile["capabilities"]})
        self.tools = Toolbox(self.root, self.policy)
        self.model = Model(config, self.reserve)
        self.stop = threading.Event()
        self.heartbeat = 0.0
        self.thread = None
        self.lock_file = None

    def start(self):
        self.lock_file = (self.root / "daemon.lock").open("a")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            raise Conflict("Another director owns this state directory") from exc
        self.tools.sandbox_cleanup()
        with self.store.tx() as db:
            for step in db.execute("SELECT * FROM steps WHERE state IN ('assigned','in_progress')").fetchall():
                task, name = step["task"], step["id"]
                if step["effect"]:
                    message = "Daemon restarted during an external action. Check the destination for its outcome."
                    db.execute("UPDATE steps SET state='failed',result=? WHERE task=? AND id=?",
                               (encode({"summary": message}), task, name))
                    event(db, task, "effect_uncertain", {"message": message}, name)
                    self.fail_task(db, task, message)
                else:
                    db.execute("UPDATE steps SET state='queued' WHERE task=? AND id=?", (task, name))
                    event(db, task, "step_recovered", step=name)
                    self.refresh(db, task)
            db.execute("UPDATE tasks SET planning=0, finishing=0 WHERE planning=1 OR finishing=1")
        self.thread = threading.Thread(target=self.run, name="director")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        if self.lock_file:
            self.lock_file.close()

    @staticmethod
    def validate_goal(goal, mode):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL:
            raise ValueError(f"Goal must contain 1 to {MAX_GOAL} characters")
        if mode not in {"live", "demo"}:
            raise ValueError("Unknown task mode")

    def insert_task(self, db, goal, mode, task_id):
        now = time.time()
        db.execute("INSERT INTO tasks(id,goal,mode,status,created,updated) VALUES(?,?,?,?,?,?)",
                   (task_id, goal, mode, "queued", now, now))
        event(db, task_id, "task_submitted", {"mode": mode})

    def submit(self, goal, mode, attachments, telegram_origin=None):
        self.validate_goal(goal, mode)
        if telegram_origin:
            update_id, *scope = telegram_origin
            with self.store.tx(write=False) as db:
                existing = db.execute("SELECT task FROM telegram_origins WHERE update_id=?", (update_id,)).fetchone()
                if existing:
                    return existing["task"]
                drafts = db.execute("SELECT name,content FROM telegram_drafts WHERE user_id=? AND chat_id=? "
                                    "AND topic=?", scope)
                attachments = {**{row["name"]: row["content"] for row in drafts}, **attachments}
        task = uuid.uuid4().hex
        workspace = self.projects / task
        try:
            save_attachments(workspace, attachments)
            with self.store.tx() as db:
                self.insert_task(db, goal, mode, task)
                if telegram_origin:
                    db.execute("INSERT INTO telegram_origins VALUES(?,?,?,?,?)", (task, *telegram_origin))
                    db.execute("DELETE FROM telegram_drafts WHERE user_id=? AND chat_id=? AND topic=?", scope)
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        return task

    def schedule(self, db, goal, seconds):
        self.validate_goal(goal, "live")
        if type(seconds) is not int or not 60 <= seconds <= 31_536_000:
            raise ValueError("Interval must be an integer between 60 and 31536000 seconds")
        if self.model.offline:
            raise ValueError("Schedules require live mode")
        schedule = uuid.uuid4().hex
        db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,1)",
                   (schedule, goal, "live", seconds, time.time() + seconds))
        return schedule

    def fire_schedules(self):
        now = time.time()
        with self.store.tx() as db:
            schedules = db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_at<=? LIMIT 20", (now,)).fetchall()
            for row in schedules:
                task = uuid.uuid4().hex
                save_attachments(self.projects / task, {})
                self.insert_task(db, row["goal"], row["mode"], task)
                event(db, task, "schedule_fired", {"schedule": row["id"]})
                db.execute("UPDATE schedules SET next_at=? WHERE id=?", (now + row["interval_seconds"], row["id"]))

    def reserve(self, task, profile, alias, provider, provider_rpm, input_tokens, output_tokens):
        tokens = input_tokens + output_tokens
        cost = (input_tokens * profile["input_usd_m"] + output_tokens * profile["output_usd_m"]) / 1_000_000
        with self.store.tx() as db:
            row = self.task_row(db, task)
            if row["status"] in TERMINAL:
                raise Conflict("Task is no longer active")
            if row["paused"]:
                raise RateLimited(1)
            if row["tokens"] + tokens > self.policy["token_budget"] or row["cost"] + cost > self.policy["budget_usd"]:
                raise BudgetExceeded("Task model budget exhausted")
            now = time.time()
            for condition, parameters, limit in (
                ("", (), self.policy["model_rpm"]),
                ("AND provider=?", (provider,), provider_rpm)
            ):
                calls = db.execute("SELECT COUNT(*) AS count, MIN(at) AS oldest FROM model_calls WHERE at>? "
                                   + condition, (now - 60, *parameters)).fetchone()
                if calls["count"] >= limit:
                    raise RateLimited(max(0.2, calls["oldest"] + 60 - now))
            db.execute("DELETE FROM model_calls WHERE at<=?", (now - 60,))
            db.execute("INSERT INTO model_calls(provider,at) VALUES(?,?)", (provider, now))
            db.execute("UPDATE tasks SET tokens=tokens+?,cost=cost+? WHERE id=?", (tokens, cost, task))
            event(db, task, "model_reserved", {"model": profile["name"], "alias": alias, "provider": provider, "tokens": tokens, "usd": cost})

    @staticmethod
    def task_row(db, task):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            raise KeyError("Unknown task")
        return row

    def task_view(self, task):
        with self.store.tx(write=False) as db:
            status = dict(self.task_row(db, task))
            rows = db.execute("SELECT * FROM steps WHERE task=? ORDER BY rowid", (task,))
            status["steps"] = [decode(row, "deps", "result", "action", "observations") for row in rows]
            rows = db.execute("SELECT * FROM approvals WHERE task=? ORDER BY created", (task,))
            status["approvals"] = [decode(row, "action") for row in rows]
            rows = db.execute("SELECT * FROM runs WHERE task=? ORDER BY seq DESC LIMIT 100", (task,))
            status["runs"] = [decode(row, "result") for row in rows]
        status["artifacts"] = self.files(task)
        return status

    def files(self, task):
        workspace = self.projects / task
        return sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")
                      if path.is_file() and not path.is_symlink()
                      and all(not part.startswith(".") for part in path.relative_to(workspace).parts))

    @staticmethod
    def refresh(db, task):
        row = Engine.task_row(db, task)
        if row["status"] in TERMINAL:
            return
        states = [step["state"] for step in db.execute("SELECT state FROM steps WHERE task=?", (task,))]
        if row["paused"]:
            status = "paused"
        elif "blocked" in states:
            status = "blocked"
        elif "needs_review" in states:
            status = "needs_review"
        elif states or row["planning"]:
            status = "in_progress"
        else:
            status = "queued"
        db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (status, time.time(), task))

    @staticmethod
    def fail_task(db, task, summary):
        db.execute("UPDATE tasks SET status='failed',summary=?,updated=? WHERE id=? AND status NOT IN "
                   "('completed','cancelled','failed')", (summary[:8000], time.time(), task))
        db.execute("UPDATE approvals SET state='void' WHERE task=? AND state IN ('pending','approved')", (task,))
        event(db, task, "task_failed", {"summary": summary[:8000]})
        LOG.info("Task %s failed", task)

    def control(self, task, verb):
        with self.store.tx() as db:
            row = self.task_row(db, task)
            status = row["status"]
            if verb == "retry":
                if status != "failed":
                    raise Conflict("Only failed tasks can be retried")
                if row["planning"] or row["finishing"] or db.execute(
                        "SELECT 1 FROM steps WHERE task=? AND (state IN ('assigned','in_progress') OR effect=1)",
                        (task,)).fetchone():
                    raise Conflict("Wait for active work or reconcile an uncertain external effect")
                db.execute("UPDATE tasks SET status='queued',summary=NULL,planning=0,finishing=0,paused=0,"
                           "plan_attempts=0,ready=0,updated=? WHERE id=?", (time.time(), task))
                db.execute("UPDATE steps SET state='queued',attempts=0,ready=0,action=NULL,turns=0,result=NULL "
                           "WHERE task=? AND state!='completed'", (task,))
                self.refresh(db, task)
            elif status in TERMINAL:
                raise Conflict("Task is already terminal")
            elif verb == "pause":
                db.execute("UPDATE tasks SET paused=1 WHERE id=?", (task,))
                self.refresh(db, task)
            elif verb == "resume":
                db.execute("UPDATE tasks SET paused=0 WHERE id=?", (task,))
                self.refresh(db, task)
            elif verb == "cancel":
                db.execute("UPDATE tasks SET status='cancelled',summary='Cancelled by operator.',updated=? WHERE id=?",
                           (time.time(), task))
                db.execute("UPDATE approvals SET state='void' WHERE task=? AND state IN ('pending','approved')",
                           (task,))
            else:
                raise ValueError("Unknown task control")
            if verb == "cancel":
                LOG.info("Task %s cancelled", task)
            event(db, task, {"retry": "task_retried", "pause": "task_paused", "resume": "task_resumed",
                             "cancel": "task_cancelled"}[verb])
            return self.task_row(db, task)["status"]

    def approval(self, approval, decision, response):
        if decision not in {"approve", "reject"} or not isinstance(response, str) or len(response) > 3000:
            raise ValueError("Invalid approval decision or response")
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval,)).fetchone()
            if row is None:
                raise KeyError("Unknown approval")
            task, step = row["task"], row["step"]
            if row["state"] != "pending" or self.task_row(db, task)["status"] in TERMINAL:
                raise Conflict("Approval is no longer pending")
            action = json.loads(row["action"])
            if decision == "approve" and action["tool"] == "clarify" and not response.strip():
                raise ValueError("This approval requires an answer")
            state = "approved" if decision == "approve" else "rejected"
            db.execute("UPDATE approvals SET state=?,response=? WHERE id=?", (state, response, approval))
            event(db, task, "approval_" + state, {"approval": approval}, step)
            if decision == "reject":
                summary = "Operator rejected " + action["tool"] + (f": {response}" if response else ".")
                db.execute("UPDATE steps SET state='failed',result=?,action=NULL WHERE task=? AND id=?",
                           (encode({"summary": summary}), task, step))
                self.fail_task(db, task, summary)
            else:
                db.execute("UPDATE steps SET state='queued',ready=0 WHERE task=? AND id=?", (task, step))
                self.refresh(db, task)
            return task

    def run(self):
        running = {}
        next_schedule = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.policy["max_parallel"]) as pool:
            while not self.stop.is_set():
                self.heartbeat = time.monotonic()
                if time.monotonic() >= next_schedule:
                    self.fire_schedules()
                    next_schedule = time.monotonic() + 1
                for future in list(running):
                    if future.done():
                        running.pop(future)
                        future.result()  # Persistence errors stop the scheduler and make health fail.
                loads = collections.Counter(bot for bot in running.values() if bot != "director")
                while len(running) < self.policy["max_parallel"] and not self.stop.is_set():
                    claim = self.claim(loads)
                    if claim is None:
                        break
                    handler, arguments, bot = claim
                    running[pool.submit(handler, *arguments)] = bot
                    loads[bot] += 1
                self.stop.wait(0.15)

    def route(self, capability, loads, previous):
        eligible = [profile for profile in self.bots.values()
                    if capability in profile["capabilities"] and loads[profile["id"]] < profile["slots"]]
        if previous in self.bots and capability in self.bots[previous]["capabilities"]:
            return next((profile for profile in eligible if profile["id"] == previous), None)
        return min(eligible, key=lambda profile: (
            loads[profile["id"]] / profile["slots"],
            profile["price"], profile["id"]
        ), default=None)

    def claim(self, loads):
        now = time.time()
        with self.store.tx() as db:
            tasks = db.execute("SELECT * FROM tasks WHERE paused=0 AND status NOT IN "
                               "('completed','failed','cancelled') ORDER BY updated,created").fetchall()
            for task in tasks:
                task_id = task["id"]
                rows = db.execute("SELECT * FROM steps WHERE task=? ORDER BY rowid", (task_id,)).fetchall()
                if not rows and not task["planning"] and task["ready"] <= now:
                    db.execute("UPDATE tasks SET planning=1 WHERE id=?", (task_id,))
                    event(db, task_id, "planning_started")
                    self.refresh(db, task_id)
                    return self.plan, (dict(task),), "director"
                done = {row["id"] for row in rows if row["state"] == "completed"}
                if rows and len(done) == len(rows) and not task["finishing"] and task["ready"] <= now:
                    db.execute("UPDATE tasks SET finishing=1 WHERE id=?", (task_id,))
                    event(db, task_id, "director_review_started")
                    return self.finish, (dict(task), [dict(row) for row in rows]), "director"
                for row in rows:
                    if row["state"] != "queued" or row["ready"] > now or not set(json.loads(row["deps"])) <= done:
                        continue
                    if row["action"] and row["bot"] not in self.bots:
                        self.fail_task(db, task_id, f"Bot {row['bot']} was removed with an action pending")
                        break
                    profile = self.route(row["capability"], loads, row["bot"])
                    if profile is None:
                        if not any(row["capability"] in bot["capabilities"] for bot in self.bots.values()):
                            self.fail_task(db, task_id, f"No configured bot for {row['capability']}")
                            break
                        continue
                    bot, step = profile["id"], row["id"]
                    db.execute("UPDATE steps SET state='assigned',bot=? WHERE task=? AND id=?", (bot, task_id, step))
                    event(db, task_id, "step_assigned", {"bot": bot, "model": profile["model"]}, step)
                    self.refresh(db, task_id)
                    return self.turn, (dict(task), dict(row), profile), bot
        return None

    def plan(self, task):
        task_id = task["id"]
        try:
            context = {"goal": task["goal"], "mode": "demo" if self.model.offline else task["mode"],
                       "attachments": self.files(task_id)}
            plan = validate_plan(self.model.plan(task_id, context, self.capabilities), self.capabilities)
        except Exception as exc:
            self.director_error(task_id, "planning", exc)
            return
        with self.store.tx() as db:
            db.execute("UPDATE tasks SET planning=0,plan_attempts=0,ready=0 WHERE id=?", (task_id,))
            if self.task_row(db, task_id)["status"] in TERMINAL:
                return
            for item in plan:
                db.execute("INSERT INTO steps(task,id,capability,instruction,deps,review_of) VALUES(?,?,?,?,?,?)",
                           (task_id, item["id"], item["capability"], item["instruction"],
                            encode(item["depends_on"]), item.get("review_of")))
            event(db, task_id, "plan_created", {"steps": [item["id"] for item in plan]})
            self.refresh(db, task_id)

    def finish(self, task, rows):
        task_id = task["id"]
        try:
            steps = [{"id": row["id"], "result": json.loads(row["result"])} for row in rows]
            context = {"goal": task["goal"], "steps": steps}
            if len(encode(context).encode()) > self.policy["max_input_tokens"] // 2:
                for step in steps:
                    step["result"]["summary"] = step["result"]["summary"][:300]
                    step["result"]["artifacts"] = step["result"]["artifacts"][:2]
            result = self.model.finish(task_id, context)
            if isinstance(result, str):
                result = {"summary": result, "passed": True}
        except Exception as exc:
            self.director_error(task_id, "finishing", exc)
            return
        with self.store.tx() as db:
            db.execute("UPDATE tasks SET finishing=0 WHERE id=?", (task_id,))
            if self.task_row(db, task_id)["status"] in TERMINAL:
                return
            if not result["passed"]:
                self.fail_task(db, task_id, result["summary"])
                return
            db.execute("UPDATE tasks SET status='completed',summary=?,updated=? WHERE id=?",
                       (result["summary"], time.time(), task_id))
            event(db, task_id, "task_completed", {"summary": result["summary"]})
            LOG.info("Task %s completed", task_id)

    def director_error(self, task, phase, exc):
        with self.store.tx() as db:
            row = self.task_row(db, task)
            db.execute(f"UPDATE tasks SET {phase}=0 WHERE id=?", (task,))
            if row["status"] in TERMINAL:
                return
            if self.defer(db, task, None, row["plan_attempts"], exc):
                self.refresh(db, task)
                return
            self.fail_task(db, task, f"Director {phase} failed: {str(exc)[:600]}")

    def defer(self, db, task, step, attempts, exc):
        if not isinstance(exc, Transient):
            return False
        if isinstance(exc, RateLimited):
            delay = exc.delay
        else:
            attempts += 1
            if attempts >= self.policy["max_attempts"]:
                return False
            delay = min(60, 2 ** attempts)
        ready = time.time() + delay
        if step:
            db.execute("UPDATE steps SET state='queued',ready=?,attempts=? WHERE task=? AND id=?",
                       (ready, attempts, task, step))
        else:
            db.execute("UPDATE tasks SET ready=?,plan_attempts=? WHERE id=?", (ready, attempts, task))
        event(db, task, "retry_scheduled", {"in_seconds": round(delay, 2), "reason": str(exc)[:400]}, step)
        return True

    def allowed_tools(self, profile, capability):
        allowed = (set(profile["tools"]) | {"clarify"}) - set(self.policy["disabled_tools"])
        if capability != "ops":
            allowed -= GATED
        if self.policy["autonomy"] == "read_only":
            allowed -= MUTATING
        return allowed

    def context(self, task, row, profile):
        task_id, name = task["id"], row["id"]
        with self.store.tx(write=False) as db:
            dependency_ids = set(json.loads(row["deps"]))
            dependencies = {item["id"]: json.loads(item["result"])
                            for item in db.execute("SELECT id,result FROM steps WHERE task=?", (task_id,))
                            if item["id"] in dependency_ids}
            history = [decode(item, "result") for item in db.execute(
                "SELECT step,state,result FROM runs WHERE task=? AND bot=? ORDER BY seq DESC LIMIT 3",
                (task_id, profile["id"]))]
        observations = json.loads(row["observations"])
        allowed = self.allowed_tools(profile, row["capability"])
        context = {
            "goal": task["goal"], "step": name, "instruction": row["instruction"],
            "review_of": row["review_of"], "review_feedback": row["feedback"],
            "attachments": [path for path in self.files(task_id) if path.startswith("attachments/")],
            "dependencies": dependencies, "observations": observations,
            "own_files": [path for path in self.files(task_id) if path.startswith(f"steps/{name}/")],
            "bot_project_history": history,
            "tools": {tool: CONTRACTS[tool] for tool in sorted(allowed)},
            "destinations": {"fetch_hosts": self.policy["fetch_hosts"],
                             "integrations": sorted(self.policy["integrations"]),
                             "github_repo": self.policy["github_repo"]}
        }
        if not self.model.offline:
            bound = self.policy["max_input_tokens"] // 2
            while history and len(encode(context).encode()) > bound:
                history.pop()
            while len(observations) > 1 and len(encode(context).encode()) > bound:
                observations.pop(0)
            if len(encode(context).encode()) > bound and observations:
                last = observations[-1]
                if len(encode(last["result"]).encode()) > 3500:
                    last["result"] = {"preview": encode(last["result"])[:2500], "truncated": True}
            if len(encode(context).encode()) > bound:
                for result in dependencies.values():
                    result["summary"] = result["summary"][:400]
                    result["artifacts"] = result["artifacts"][:3]
        return context

    def turn(self, task, row, profile):
        task_id, name = task["id"], row["id"]
        with self.store.tx() as db:
            if self.stopped_before_claim(db, task_id, name):
                return
            db.execute("UPDATE steps SET state='in_progress' WHERE task=? AND id=?", (task_id, name))
            event(db, task_id, "step_in_progress", {"bot": profile["id"]}, name)
        try:
            if row["action"]:
                self.execute(task_id, row, profile)
            else:
                if row["turns"] >= self.policy["max_turns"]:
                    raise ValueError("Worker turn limit reached")
                reply = self.model.worker(task_id, profile, self.context(task, row, profile))
                if "tool" in reply:
                    self.propose(task_id, row, profile, reply)
                else:
                    self.complete(task_id, row, profile, reply)
        except Exception as exc:
            with self.store.tx() as db:
                current = db.execute("SELECT * FROM steps WHERE task=? AND id=?", (task_id, name)).fetchone()
                terminal = self.task_row(db, task_id)["status"] in TERMINAL
                if not terminal and not current["effect"] and self.defer(db, task_id, name, current["attempts"], exc):
                    self.refresh(db, task_id)
                    return
                message = str(exc)[:700] or type(exc).__name__
                if current["effect"]:
                    message = "External action outcome may be uncertain. Inspect its destination. " + message
                    event(db, task_id, "effect_uncertain", {"message": message}, name)
                db.execute("UPDATE steps SET state='failed',result=? WHERE task=? AND id=?",
                           (encode({"summary": message}), task_id, name))
                self.log_run(db, task_id, name, profile["id"], "failed", {"summary": message})
                if not terminal:
                    self.fail_task(db, task_id, f"{name} failed: {message}")

    def stopped_before_claim(self, db, task, step):
        current = self.task_row(db, task)
        if current["status"] in TERMINAL or current["paused"]:
            # Do not discard a previously approved action on pause.
            db.execute("UPDATE steps SET state='queued' WHERE task=? AND id=?", (task, step))
            return True
        return False

    def propose(self, task, row, profile, reply):
        name = reply["tool"]
        args = reply.get("args")
        if not isinstance(name, str) or name not in self.allowed_tools(profile, row["capability"]):
            raise ValueError("Worker requested a tool outside its policy")
        if not isinstance(args, dict) or len(encode(args).encode()) > 60000:
            raise ValueError("Invalid or oversized tool arguments")
        if name == "clarify":
            text_arg(args, "question", 1500)
        step = row["id"]
        action = bind_action(name, args, self.projects / task, self.policy)
        action["nonce"] = uuid.uuid4().hex
        with self.store.tx() as db:
            if self.task_row(db, task)["status"] in TERMINAL:
                db.execute("UPDATE steps SET state='queued' WHERE task=? AND id=?", (task, step))
                return
            db.execute("UPDATE steps SET action=?,state='queued',turns=turns+1,attempts=0,ready=0 "
                       "WHERE task=? AND id=?", (encode(action), task, step))
            event(db, task, "tool_proposed", {"tool": name, "bot": profile["id"]}, step)
            self.refresh(db, task)

    def execute(self, task, row, profile):
        step = row["id"]
        action = json.loads(row["action"])
        tool = action["tool"]
        if tool not in self.allowed_tools(profile, row["capability"]):
            raise ValueError("Tool is no longer allowed by policy")
        digest = hashlib.sha256(encode(action).encode()).hexdigest()
        gate = tool in GATED or tool == "clarify" or (
            self.policy["autonomy"] == "confirm_all" and tool in MUTATING)
        with self.store.tx() as db:
            if self.stopped_before_claim(db, task, step):
                return
            approval = db.execute("SELECT * FROM approvals WHERE task=? AND step=? AND digest=?",
                                  (task, step, digest)).fetchone()
            if gate and approval is None:
                approval_id = uuid.uuid4().hex
                db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,'pending',NULL,?)",
                           (approval_id, task, step, digest, encode(action), time.time()))
                db.execute("UPDATE steps SET state=? WHERE task=? AND id=?",
                           ("blocked" if tool == "clarify" else "needs_review", task, step))
                event(db, task, "approval_requested",
                      {"approval": approval_id, "tool": tool, "question": action["args"].get("question")}, step)
                self.refresh(db, task)
                LOG.info("Task %s needs input for %s, approval %s", task, tool, approval_id)
                return
            if tool == "pull_request" and not os.environ.get("BOTTEAM_GITHUB_TOKEN"):
                raise ValueError("BOTTEAM_GITHUB_TOKEN is required")
            if approval:
                if approval["state"] not in {"approved", "consumed"}:
                    raise Conflict("Action lacks a current approval")
                db.execute("UPDATE approvals SET state='consumed' WHERE id=?", (approval["id"],))
            if tool == "clarify":
                self.record_tool(db, task, row, action, {"answer": approval["response"]})
                return
            if tool == "schedule":
                args = action["args"]
                schedule = self.schedule(db, args.get("goal"), args.get("interval_seconds"))
                self.record_tool(db, task, row, action, {"schedule": schedule})
                return
            if tool in EFFECTS:
                if not approval or approval["state"] != "approved":
                    raise Conflict("External action approval has already been consumed")
                db.execute("UPDATE steps SET effect=1 WHERE task=? AND id=?", (task, step))
            event(db, task, "tool_started", {"tool": tool, "digest": digest}, step)
        try:
            result = self.tools.run(self.projects / task, step, action, digest)
        except (ValueError, OSError) as exc:
            if tool in EFFECTS:
                raise
            result = {"error": str(exc)[:700]}
        with self.store.tx() as db:
            self.record_tool(db, task, row, action, result)

    def record_tool(self, db, task, row, action, result):
        observations = json.loads(row["observations"])
        observations.append({"tool": action["tool"], "result": result})
        db.execute("UPDATE steps SET state='queued',action=NULL,effect=0,observations=?,attempts=0,ready=0 "
                   "WHERE task=? AND id=?", (encode(observations), task, row["id"]))
        event(db, task, "tool_finished", {"tool": action["tool"], "result": result}, row["id"])
        self.refresh(db, task)

    @staticmethod
    def log_run(db, task, step, bot, state, result):
        db.execute("INSERT INTO runs(task,step,bot,at,state,result) VALUES(?,?,?,?,?,?)",
                   (task, step, bot, time.time(), state, encode(result)))

    def complete(self, task, row, profile, reply):
        summary = text_arg(reply, "summary", 8000)
        artifacts = reply.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 12:
            raise ValueError("Invalid artifact list")
        for artifact in artifacts:
            if not path_under(self.projects / task, artifact).is_file():
                raise ValueError("Worker named an artifact that does not exist")
        name = row["id"]
        result = {"summary": summary, "artifacts": artifacts}
        review = row["review_of"] is not None
        if review and reply.get("verdict") not in {"pass", "fail"}:
            raise ValueError("Reviewer must return a pass or fail verdict")
        if review:
            result["verdict"] = reply["verdict"]
        with self.store.tx() as db:
            if self.task_row(db, task)["status"] in TERMINAL:
                db.execute("UPDATE steps SET state='queued' WHERE task=? AND id=?", (task, name))
                return
            if result.get("verdict") == "fail":
                self.log_run(db, task, name, profile["id"], "rejected", result)
                if row["repairs"] >= self.policy["max_repairs"]:
                    db.execute("UPDATE steps SET state='failed',result=? WHERE task=? AND id=?",
                               (encode(result), task, name))
                    self.fail_task(db, task, f"Review repair limit reached for {row['review_of']}. {summary}")
                    return
                db.execute("UPDATE steps SET state='queued',observations='[]',attempts=0,turns=0,result=NULL,"
                           "ready=0,repairs=repairs+1 WHERE task=? AND id=?", (task, name))
                db.execute("UPDATE steps SET state='queued',observations='[]',attempts=0,turns=0,result=NULL,"
                           "ready=0,feedback=? WHERE task=? AND id=?", (summary, task, row["review_of"]))
                event(db, task, "review_rejected", {"target": row["review_of"], "feedback": summary}, name)
                self.refresh(db, task)
                return
            db.execute("UPDATE steps SET state='completed',result=? WHERE task=? AND id=?",
                       (encode(result), task, name))
            self.log_run(db, task, name, profile["id"], "completed", result)
            event(db, task, "step_completed", {"bot": profile["id"], "result": result}, name)
            self.refresh(db, task)

import base64
import difflib
import hashlib
import json
import os
import shutil
import tarfile
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .settings import identifier
from .store import encode

MAX_FILE = 200_000
MAX_REPLY = 100_000


class Transient(Exception):
    """A retry may resolve the failure."""


class ProviderRequestError(ValueError):
    JSON_ERRORS = {
        "json_validate_failed", "json_schema_validation_failed",
        "response_format_unsupported", "unsupported_response_format",
    }
    KNOWN_CODES = JSON_ERRORS | {
        "context_length_exceeded", "model_decommissioned", "model_not_found",
        "invalid_model", "invalid_request_error",
    }

    def __init__(self, status, error):
        self.status = status
        code = error.get("code")
        self.code = code if isinstance(code, str) and code in self.KNOWN_CODES else None
        self.generic_request_error = error.get("type") == "invalid_request_error"
        self.json_mode_failure = self.code in self.JSON_ERRORS or error.get("param") == "response_format"
        detail = self.code
        if detail is None and self.json_mode_failure:
            detail = "response_format"
        elif detail is None and self.generic_request_error:
            detail = "invalid_request_error"
        super().__init__(f"HTTP {status}" + (f" ({detail})" if detail else ""))

    @classmethod
    def from_http_error(cls, exc):
        try:
            with exc:
                payload = json.loads(exc.read(MAX_REPLY))
            error = payload.get("error") if isinstance(payload, dict) else None
        except (ValueError, OSError):
            error = None
        # Provider messages and failed generations may echo task data. Keep only allowlisted metadata.
        return cls(exc.code, error if isinstance(error, dict) else {})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = build_opener(NoRedirect())
LOCAL_HTTP = build_opener(ProxyHandler({}), NoRedirect())


def request(url, timeout, headers=None, payload=None, local=False):
    headers = {"User-Agent": "BotTeam/0.1", **(headers or {})}
    data = encode(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    deadline = time.monotonic() + timeout
    try:
        opener = LOCAL_HTTP if local else HTTP
        with opener.open(Request(url, data=data, headers=headers), timeout=timeout) as response:
            content = bytearray()
            while len(content) < MAX_REPLY:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = response.read1(min(8192, MAX_REPLY - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            return response.status, content.decode("utf-8", "replace")
    except HTTPError as exc:
        if exc.code in {408, 425, 429} or exc.code >= 500:
            raise Transient(f"HTTP {exc.code}") from exc
        raise ProviderRequestError.from_http_error(exc) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise Transient(f"HTTP transport failure ({type(exc).__name__})") from exc


def path_under(root, value):
    if not isinstance(value, str) or not value or len(value) > 220 or "\\" in value:
        raise ValueError("Expected a relative workspace path")
    parts = PurePosixPath(value)
    if parts.is_absolute() or not parts.parts or any(part.startswith(".") for part in parts.parts):
        raise ValueError("Hidden paths and traversal are disabled")
    candidate = root
    for part in parts.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Symlinks are disabled")
    return candidate


def atomic_write(path, content, overwrite=True):
    if len(content) > MAX_FILE:
        raise ValueError("File exceeds workspace size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".work-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
        os.chmod(name, 0o644)
        if overwrite:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_bytes(path):
    with path.open("rb") as source:
        data = source.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("File exceeds workspace size limit")
    return data


def save_attachments(workspace, attachments):
    if not isinstance(attachments, dict) or len(attachments) > 12:
        raise ValueError("Supply up to 12 attachments")
    workspace.mkdir(parents=True, exist_ok=False)
    for name, data in attachments.items():
        if not isinstance(name, str) or "/" in name or not isinstance(data, str):
            raise ValueError("Attachments need basenames and Base64 content")
        atomic_write(path_under(workspace / "attachments", name),
                     base64.b64decode(data, validate=True), overwrite=False)


def https_url(url, allowed=None):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 for char in url):
        raise ValueError("Invalid HTTPS URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.port not in {None, 443} or parts.fragment
            or (allowed is not None and parts.hostname not in allowed)):
        raise ValueError("URL is outside the configured HTTPS destinations")
    return url


def text_arg(args, key, limit=2000):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {key}")
    return value


def target_for(name, args, policy):
    if name == "integration":
        key = text_arg(args, "name", 40)
        if key not in policy["integrations"]:
            raise ValueError("Integration is unavailable")
        if "payload" not in args or len(encode(args["payload"])) > 10000:
            raise ValueError("Integration needs a bounded JSON payload")
        return https_url(policy["integrations"][key])
    if name == "pull_request":
        repo = policy["github_repo"]
        parts = repo.split("/")
        if len(parts) != 2 or not all(p and all(c.isascii() and (c.isalnum() or c in "._-")
                                                for c in p) and p not in {".", ".."} for p in parts):
            raise ValueError("Configure a GitHub repository")
        for key, limit in (("title", 256), ("head", 200), ("base", 200)):
            text_arg(args, key, limit)
        if not isinstance(args.get("body", ""), str) or len(args.get("body", "")) > 8000:
            raise ValueError("Invalid pull request body")
        if ":" in args["head"] or ":" in args["base"]:
            raise ValueError("Only branches in the configured repository are supported")
        return f"https://api.github.com/repos/{repo}/pulls"
    return None


def bind_action(name, args, workspace, policy):
    action = {"tool": name, "args": args}
    destination = target_for(name, args, policy)
    if destination:
        action["destination"] = destination
    if name == "publish":
        source = path_under(workspace, args.get("path"))
        action["sha256"] = hashlib.sha256(read_bytes(source)).hexdigest()
    return action


class Toolbox:
    def __init__(self, root, policy):
        self.policy = policy
        self.label = "botteam=" + hashlib.sha256(str(root).encode()).hexdigest()[:20]
        self.docker = shutil.which("docker")

    def sandbox_ready(self):
        if not self.docker:
            return False
        try:
            check = subprocess.run([self.docker, "image", "inspect", self.policy["sandbox_image"]],
                                   capture_output=True, timeout=3, check=False)
            return check.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def sandbox_cleanup(self):
        if not self.docker:
            return
        try:
            result = subprocess.run([self.docker, "ps", "-aq", "--filter", f"label={self.label}"],
                                    capture_output=True, timeout=5, check=True, text=True)
            for container in result.stdout.split():
                self.remove_container(container)
        except (OSError, subprocess.SubprocessError):
            return

    def remove_container(self, name):
        try:
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return

    def sandbox(self, workspace, argv):
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or not all(isinstance(arg, str) and 1 <= len(arg) <= 1024 and "\0" not in arg for arg in argv)):
            raise ValueError("exec requires a nonempty argument array")
        if not self.sandbox_ready():
            raise ValueError("Docker sandbox image is unavailable")
        name = "botteam-" + uuid.uuid4().hex
        command = [
            self.docker, "run", "--rm", "--name", name, "--label", self.label, "--pull=never",
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user=65534:65534", "--pids-limit=64", "--memory=128m", "--cpus=0.5",
            "--ulimit=fsize=1048576:1048576", "--log-driver=none",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=32m,mode=1777", "--interactive",
            self.policy["sandbox_image"], *argv
        ]
        try:
            with tempfile.TemporaryFile() as inputs, tempfile.TemporaryFile() as log:
                self.archive_workspace(workspace, inputs)
                inputs.seek(0)
                with subprocess.Popen(command, stdin=inputs, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT) as process:
                    # Drain streaming output while keeping only the first 16 KB.
                    def drain():
                        for chunk in iter(lambda: process.stdout.read(8192), b""):
                            if log.tell() < 16000:
                                log.write(chunk[:16000 - log.tell()])
                    reader = threading.Thread(target=drain)
                    reader.start()
                    try:
                        code = process.wait(timeout=self.policy["tool_timeout_seconds"])
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise ValueError("Sandbox command timed out") from None
                    finally:
                        reader.join()
                log.seek(0)
                return {"exit_code": code, "output": log.read().decode("utf-8", "replace")}
        finally:
            self.remove_container(name)

    @staticmethod
    def archive_workspace(workspace, destination):
        total = 0
        count = 0
        with tarfile.open(fileobj=destination, mode="w") as archive:
            for path in sorted(workspace.rglob("*")):
                relative = path.relative_to(workspace)
                if any(part.startswith(".") for part in relative.parts) or path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as source:
                    size = os.fstat(source.fileno()).st_size
                    count += 1
                    total += size
                    if size > MAX_FILE or total > 16_000_000 or count > 400:
                        raise ValueError("Workspace is too large for the sandbox")
                    info = tarfile.TarInfo(relative.as_posix())
                    info.size = size
                    info.mode = 0o644
                    archive.addfile(info, source)

    def run(self, workspace, step, action, idempotency):
        name, args = action["tool"], action["args"]
        timeout = self.policy["tool_timeout_seconds"]
        own = workspace / "steps" / step
        if name == "read":
            path = path_under(workspace, args.get("path"))
            return {"path": args["path"], "text": read_bytes(path)[:16000].decode("utf-8", "replace")}
        if name == "write":
            path = path_under(own, args.get("path"))
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("write requires text content")
            atomic_write(path, content.encode())
            return {"artifact": path.relative_to(workspace).as_posix()}
        if name == "patch":
            old = path_under(workspace, args.get("original"))
            new = path_under(workspace, args.get("revised"))
            target = text_arg(args, "target", 200)
            path_under(workspace, target)
            original = read_bytes(old).decode().splitlines(keepends=True)
            revised = read_bytes(new).decode().splitlines(keepends=True)
            lines = difflib.unified_diff(original, revised, f"a/{target}", f"b/{target}")
            patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                            for line in lines)
            return self.run(workspace, step, {"tool": "write", "args": {
                "path": args.get("path"), "content": patch}}, idempotency)
        if name == "exec":
            return self.sandbox(workspace, args.get("argv"))
        if name in {"search", "fetch"}:
            if name == "fetch":
                url = https_url(args.get("url"), self.policy["fetch_hosts"])
                _, body = request(url, timeout)
                return {"url": url, "text": body[:16000]}
            query = text_arg(args, "query", 400)
            key = os.environ.get("BRAVE_SEARCH_API_KEY")
            if key:
                url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 5})
                _, body = request(url, timeout, {"X-Subscription-Token": key, "Accept": "application/json"})
                rows = json.loads(body).get("web", {}).get("results", [])
                return {"results": [{"title": row["title"], "url": row["url"],
                                     "snippet": row.get("description", "")[:500]} for row in rows[:5]]}
            url = "https://en.wikipedia.org/w/rest.php/v1/search/page?" + urlencode({"q": query, "limit": 5})
            _, body = request(url, timeout)
            return {"results": [{"title": row["title"], "url": "https://en.wikipedia.org/wiki/" + quote(row["key"]),
                                 "snippet": (row.get("excerpt") or "")[:500]}
                                for row in json.loads(body).get("pages", [])[:5]]}
        if name == "publish":
            source = path_under(workspace, args.get("path"))
            content = read_bytes(source)
            if hashlib.sha256(content).hexdigest() != action["sha256"]:
                raise ValueError("Artifact changed after approval")
            destination = workspace / "published" / step / source.name
            atomic_write(destination, content)
            return {"artifact": destination.relative_to(workspace).as_posix(), "sha256": action["sha256"]}
        if name in {"integration", "pull_request"}:
            url = target_for(name, args, self.policy)
            if action["destination"] != url:
                raise ValueError("Integration configuration changed after approval")
            headers = {"Idempotency-Key": idempotency, "Accept": "application/json"}
            if name == "pull_request":
                token = os.environ.get("BOTTEAM_GITHUB_TOKEN")
                if not token:
                    raise ValueError("BOTTEAM_GITHUB_TOKEN is required")
                headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
                payload = {key: args[key] for key in ("title", "head", "base")}
                payload.update({"body": args.get("body", ""), "draft": True})
            else:
                payload = args["payload"]
            status, body = request(url, timeout, headers, payload)
            if name == "pull_request":
                return {"status": status, "url": json.loads(body).get("html_url")}
            return {"status": status}
        raise ValueError("Unknown tool")

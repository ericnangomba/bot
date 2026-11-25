"""A bounded, human-gated GitHub issue workflow."""
import base64
import difflib
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from .actions import ProviderRequestError, atomic_write, path_under, read_bytes, request
from .store import encode

MODE = "github_issue"
DIGEST_MODE = "github_digest"
MANIFEST = "steps/checkout/manifest.json"
ISSUE = "steps/checkout/issue.md"
DIFF = "steps/review/proposed.diff"
TESTS = "steps/review/tests.txt"
TOOLS_BY_STEP = {"checkout": "github_checkout", "review": "github_verify", "draft": "github_draft"}
TOOLS = frozenset(TOOLS_BY_STEP.values())
SHA = re.compile(r"[0-9a-f]{40}\Z")
BUILD_CHANGES = "steps/build/changes"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("GitHub returned an invalid object ID")
    return value


def issue_goal(value):
    match = re.fullmatch(r"(?:GitHub issue )?#?([1-9][0-9]{0,9})", value.strip())
    if not match:
        raise ValueError("Use /issue NUMBER for the configured GitHub repository")
    return int(match.group(1))


class GitHubWorkflow:
    def __init__(self, config, toolbox):
        self.recipe = config.get("github_workflow")
        self.policy = config["policy"]
        self.toolbox = toolbox
        self.repo = self.policy["github_repo"]
        if not self.recipe:
            return
        if (not isinstance(self.recipe, dict) or not re.fullmatch(
                r"[A-Za-z0-9_-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*", self.repo)):
            raise ValueError("GitHub workflow needs one configured repository")
        base = self.recipe.get("base")
        if (not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9_/-]{1,120}", base)
                or any(not part for part in base.split("/"))):
            raise ValueError("GitHub workflow needs a valid base branch")
        paths, editable, tests = (self.recipe.get(key) for key in ("paths", "editable_paths", "test_paths"))
        for group in (paths, editable, tests):
            if not isinstance(group, list) or not 1 <= len(group) <= 40:
                raise ValueError("Configure 1 to 40 exact repository paths per GitHub file list")
            for name in group:
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", name)
                        or any(not part or part.startswith(".") for part in name.split("/"))):
                    raise ValueError("Unsafe GitHub workflow path")
            if len(group) != len(set(group)):
                raise ValueError("GitHub workflow paths must be unique")
        if not set(editable + tests) <= set(paths) or set(editable) & set(tests):
            raise ValueError("Editable and protected test paths must be disjoint checkout paths")
        argv = self.recipe.get("test_argv")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not 1 <= len(arg) <= 1024 or "\0" in arg for arg in argv)):
            raise ValueError("Configure the GitHub workflow sandbox test command")

    def preflight(self):
        if not self.recipe or not os.environ.get("BOTTEAM_GITHUB_TOKEN"):
            raise ValueError("Configure github_workflow and BOTTEAM_GITHUB_TOKEN first")
        if self.policy["autonomy"] == "read_only":
            raise ValueError("This workflow requires sandbox execution and a gated draft PR")

    def plan(self):
        self.preflight()
        return {"steps": [
            {"id": "checkout", "capability": "research", "depends_on": [],
             "instruction": "Read the GitHub issue and fetch the configured files at a pinned commit."},
            {"id": "build", "capability": "build", "depends_on": ["checkout"],
             "instruction": "Read steps/checkout/issue.md and the relevant source files. Implement only the benign "
                            "change requested by the issue. Use write to save the edit to an allowed changes path. "
                            "For a small edit, give the exact old text and its replacement. "
                            "Finish after write returns an artifact. The workflow runs sandbox tests and review."},
            {"id": "review", "capability": "review", "depends_on": ["checkout", "build"], "review_of": "build",
             "instruction": "Read the issue, steps/review/proposed.diff and steps/review/tests.txt. Check that "
                            "the tested diff meets the issue and review it independently. Report pass or fail."},
            {"id": "draft", "capability": "ops", "depends_on": ["review"],
             "instruction": "After approval, create a new branch and open the draft PR for the reviewed patch."},
        ]}

    def next_reply(self, workspace, task, step, review=()):
        name = step["id"]
        if name not in TOOLS_BY_STEP:
            return None
        tool = TOOLS_BY_STEP[name]
        results = [item["result"] for item in json.loads(step["observations"]) if item["tool"] == tool]
        if results:
            result = results[-1]
            if name == "review":
                if result.get("exit_code") != 0:
                    return {"verdict": "fail", "summary": result.get("error") or
                            "Configured sandbox tests failed. Inspect steps/review/tests.txt.",
                            "artifacts": result.get("artifacts", [])}
                return None
            return result
        if name == "draft":
            passing = next((item["result"] for item in reversed(review) if item["tool"] == "github_verify"), {})
            args = self.draft_args(workspace, passing)
        elif name == "checkout":
            args = {"issue": issue_goal(task["goal"])}
        else:
            args = {}
        return {"tool": tool, "args": args}

    def run(self, workspace, action):
        tool, args = action["tool"], action["args"]
        if tool == "github_checkout":
            return self.checkout(workspace, args["issue"])
        if tool == "github_verify":
            return self.verify(workspace)
        return self.draft(workspace, args)

    def get(self, endpoint, payload=None):
        token = os.environ.get("BOTTEAM_GITHUB_TOKEN")
        if not token:
            raise ValueError("BOTTEAM_GITHUB_TOKEN is required")
        _, body = request(
            f"https://api.github.com/repos/{self.repo}/" + endpoint,
            self.policy["tool_timeout_seconds"],
            {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"},
            payload,
        )
        return json.loads(body)

    def digest_preflight(self):
        if (not isinstance(self.repo, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_][A-Za-z0-9_.-]*", self.repo)
                or not os.environ.get("BOTTEAM_GITHUB_TOKEN")):
            raise ValueError("The daily digest needs a configured GitHub repository and token")

    def issue_digest(self):
        self.digest_preflight()
        issues = []
        # The repository issues endpoint includes PRs. Scan a bounded number of recent results.
        for page in range(1, 5):
            rows = self.get("issues?" + urlencode({
                "state": "open", "sort": "updated", "direction": "desc", "per_page": 5, "page": page,
            }))
            if not isinstance(rows, list):
                raise ValueError("GitHub did not return an issue list")
            issues.extend(item for item in rows if "pull_request" not in item)
            if len(issues) >= 10 or len(rows) < 5:
                break
        url = f"https://github.com/{self.repo}/issues"
        lines = [f"Daily GitHub digest · {self.repo}", ""]
        if not issues:
            lines.append("No open issues in the recent results." if len(rows) == 5 else "No open issues.")
        for issue in issues[:10]:
            number = issue["number"]
            if type(number) is not int or number < 1:
                raise ValueError("GitHub returned an invalid issue number")
            title = " ".join(issue["title"].split())[:140]
            lines.append(f"#{number} {title}\n{url}/{number}")
        lines.extend(("", f"All open issues: {url}?q=is%3Aissue+is%3Aopen"))
        return "\n".join(lines)

    def branch_sha(self):
        return sha(self.get("git/ref/heads/" + quote(self.recipe["base"], safe="/"))["object"]["sha"])

    def checkout(self, workspace, number):
        self.preflight()
        issue = self.get(f"issues/{number}")
        if issue.get("state") != "open" or "pull_request" in issue:
            raise ValueError("The GitHub issue must be open")
        base = self.branch_sha()
        tree = sha(self.get(f"git/commits/{base}")["tree"]["sha"])
        entries = self.get(f"git/trees/{tree}?recursive=1")
        if entries.get("truncated"):
            raise ValueError("The GitHub source tree exceeds the workflow checkout limit")
        indexed = {item["path"]: item for item in entries["tree"]}
        files = {}
        for name in self.recipe["paths"]:
            item = indexed.get(name, {})
            if item.get("type") != "blob" or item.get("mode") not in {"100644", "100755"}:
                raise ValueError(f"Configured path is not a regular file: {name}")
            if type(item.get("size")) is not int or item["size"] > 50_000:
                raise ValueError(f"Configured source file exceeds 50 KB: {name}")
            blob = self.get(f"git/blobs/{sha(item['sha'])}")
            if blob.get("encoding") != "base64":
                raise ValueError("GitHub returned an unsupported blob encoding")
            data = base64.b64decode("".join(blob["content"].split()), validate=True)
            if len(data) != item["size"] or b"\0" in data:
                raise ValueError(f"Invalid text file in repository snapshot: {name}")
            data.decode("utf-8")
            atomic_write(path_under(workspace / "steps/checkout/source", name), data)
            files[name] = {"sha256": digest(data), "mode": item["mode"]}
        title = (issue.get("title") or "")[:180].replace("\n", " ")
        text = f"# GitHub issue #{number}\n\n{title}\n\n" + (issue.get("body") or "")[:12000]
        atomic_write(workspace / ISSUE, text.encode())
        manifest = {"repo": self.repo, "base": self.recipe["base"], "commit": base, "tree": tree, "issue": number,
                    "title": title, "files": files, "editable_paths": self.recipe["editable_paths"],
                    "commit_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "recipe_sha256": digest(encode(self.recipe).encode())}
        atomic_write(workspace / MANIFEST, encode(manifest).encode())
        return {"summary": f"Checked out issue #{number} at {base[:12]}. Read the issue and manifest.",
                "artifacts": [ISSUE, MANIFEST]}

    def build_write_args(self, workspace, args):
        paths = {f"changes/{name}": name for name in self.recipe["editable_paths"]}
        path = args.get("path")
        if not isinstance(path, str) or path not in paths:
            raise ValueError("Use an allowed write path: " + ", ".join(paths)[:500])
        name = paths[path]
        original = path_under(workspace / "steps/checkout/source", name)
        staged = path_under(workspace / BUILD_CHANGES, name)
        before = read_bytes(staged if staged.exists() else original).decode("utf-8")
        if "old" in args or "new" in args:
            old, new = args.get("old"), args.get("new")
            if "content" in args or not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError("Supply old and new text for an edit, or content for a full replacement")
            start = before.find(old)
            if start < 0 or before.find(old, start + 1) >= 0:
                raise ValueError("The old text must match exactly once. Read the current file and include nearby lines.")
            content = before[:start] + new + before[start + len(old):]
        else:
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("write requires text content or an exact old/new edit")
        if content == before or "\0" in content:
            raise ValueError("The proposed edit must change the file and remain plain text")
        return {"path": path, "content": content}

    def build_artifacts(self, workspace, observations):
        if not any(item["tool"] == "write" and item["result"].get("artifact") for item in observations):
            raise ValueError("The builder must use write to save a changed file before completing this attempt")
        _, _, changes = self.source_and_changes(workspace)
        return [f"{BUILD_CHANGES}/{name}" for name in changes]

    def source_and_changes(self, workspace):
        manifest = json.loads(read_bytes(workspace / MANIFEST))
        if manifest["repo"] != self.repo or manifest["recipe_sha256"] != digest(encode(self.recipe).encode()):
            raise ValueError("GitHub workflow configuration changed after checkout")
        source = {}
        for name, metadata in manifest["files"].items():
            data = read_bytes(path_under(workspace / "steps/checkout/source", name))
            if digest(data) != metadata["sha256"]:
                raise ValueError("The checked-out source snapshot changed")
            source[name] = data
        changes = {}
        root = workspace / BUILD_CHANGES
        for file in sorted(root.rglob("*")):
            if file.is_symlink():
                raise ValueError("Symlinks are not allowed in the proposed changes")
            if not file.is_file():
                continue
            name = file.relative_to(root).as_posix()
            if name not in manifest["editable_paths"]:
                raise ValueError(f"Change is outside the configured editable paths: {name}")
            data = read_bytes(path_under(root, name))
            data.decode("utf-8")
            if b"\0" in data:
                raise ValueError(f"Change is not a text file: {name}")
            if data != source[name]:
                changes[name] = data
        if not changes:
            raise ValueError("The builder saved no changes under its changes directory")
        return manifest, source, changes

    def verify(self, workspace):
        manifest, source, changes = self.source_and_changes(workspace)
        patch = []
        for name, revised in changes.items():
            lines = difflib.unified_diff(source[name].decode().splitlines(keepends=True),
                                         revised.decode().splitlines(keepends=True),
                                         f"a/{name}", f"b/{name}")
            patch.extend(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        atomic_write(workspace / DIFF, "".join(patch).encode())
        with tempfile.TemporaryDirectory(prefix="botteam-repo-") as directory:
            root = Path(directory)
            for name, data in (source | changes).items():
                atomic_write(path_under(root, name), data)
            result = self.toolbox.sandbox(root, self.recipe["test_argv"])
        note = f"Sandbox exit code: {result['exit_code']}\n" + result["output"]
        atomic_write(workspace / TESTS, note.encode())
        return {"exit_code": result["exit_code"], "patch_sha256": self.patch_id(manifest, changes),
                "artifacts": [DIFF, TESTS]}

    @staticmethod
    def patch_id(manifest, changes):
        data = {"repo": manifest["repo"], "commit": manifest["commit"], "issue": manifest["issue"],
                "files": {name: digest(content) for name, content in changes.items()},
                "recipe_sha256": manifest["recipe_sha256"]}
        return digest(encode(data).encode())

    def draft_args(self, workspace, review):
        manifest, _, changes = self.source_and_changes(workspace)
        patch = self.patch_id(manifest, changes)
        if review.get("exit_code") != 0 or review.get("patch_sha256") != patch:
            raise ValueError("The current patch does not match the passing sandbox run")
        return {"repo": self.repo, "base": manifest["base"], "base_sha": manifest["commit"],
                "issue": manifest["issue"], "branch": f"botteam/issue-{manifest['issue']}-{workspace.name[:12]}",
                "patch_sha256": patch, "files": list(changes),
                "diff": DIFF, "diff_sha256": digest(read_bytes(workspace / DIFF)),
                "tests": TESTS, "tests_sha256": digest(read_bytes(workspace / TESTS))}

    def head_ref(self, branch):
        try:
            response = self.get("git/ref/heads/" + quote(branch, safe="/"))
        except ProviderRequestError as exc:
            if exc.status == 404:
                return None
            raise
        return sha(response["object"]["sha"])

    def existing_draft(self, args, commit):
        owner = self.repo.split("/", 1)[0]
        query = urlencode({"state": "all", "head": f"{owner}:{args['branch']}", "base": args["base"]})
        pulls = self.get("pulls?" + query)
        if not pulls:
            return None
        if (len(pulls) != 1 or pulls[0]["head"]["sha"] != commit
                or pulls[0]["state"] != "open" or pulls[0].get("draft") is not True):
            raise ValueError("An existing pull request does not match the approved open draft")
        return pulls[0]

    def draft(self, workspace, args):
        manifest, _, changes = self.source_and_changes(workspace)
        if self.draft_args(workspace, {"exit_code": 0, "patch_sha256": args["patch_sha256"]}) != args:
            raise ValueError("GitHub draft arguments changed after approval")
        current = self.head_ref(args["branch"])
        moved = self.branch_sha() != manifest["commit"]
        if not current and moved:
            raise ValueError("The base branch moved after checkout. Submit the issue again for a fresh review.")
        tree = sha(self.get("git/trees", {"base_tree": manifest["tree"], "tree": [
            {"path": name, "type": "blob", "mode": manifest["files"][name]["mode"], "content": data.decode("utf-8")}
            for name, data in changes.items()
        ]})["sha"])
        identity = {"name": "Bot Team", "email": "botteam@users.noreply.github.com",
                    "date": manifest["commit_date"]}
        commit = sha(self.get("git/commits", {"message": f"Implement GitHub issue #{args['issue']}",
                                             "tree": tree, "parents": [args["base_sha"]],
                                             "author": identity, "committer": identity})["sha"])
        if current is None:
            try:
                self.get("git/refs", {"ref": "refs/heads/" + args["branch"], "sha": commit})
                current = commit
            except ProviderRequestError as exc:
                if exc.status != 422:
                    raise
                current = self.head_ref(args["branch"])
                if current is None:
                    raise
        if current != commit:
            raise ValueError("The workflow branch already exists with a different commit")
        response = self.existing_draft(args, commit)
        if response is None:
            if moved:
                raise ValueError("The base branch moved after checkout. Submit the issue again for a fresh review.")
            payload = {"head": args["branch"], "base": args["base"], "draft": True,
                       "title": f"Issue #{args['issue']}: {manifest['title']}",
                       "body": f"Refs #{args['issue']}\n\nSandbox tests passed and the patch was reviewed.\n"
                               f"Base commit: `{args['base_sha']}`\n"
                               f"Reviewed patch SHA-256: `{args['patch_sha256']}`"}
            try:
                response = self.get("pulls", payload)
            except ProviderRequestError as exc:
                if exc.status != 422:
                    raise
                response = self.existing_draft(args, commit)
                if response is None:
                    raise
        number = response.get("number")
        if type(number) is not int or number < 1 or response.get("draft") is not True:
            raise ValueError("GitHub did not confirm a draft pull request")
        link = f"https://github.com/{self.repo}/pull/{number}"
        return {"summary": f"Draft PR: {link}\nSandbox tests passed and a reviewer accepted the patch.",
                "artifacts": [DIFF, TESTS]}

"""Behaviour tests against a real throwaway PostgreSQL, a scripted Claude and an in-memory GitHub.

Run: .venv/bin/python -I -m unittest discover -s tests -v
"""

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pgserver
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_tasks  # noqa: E402
import store  # noqa: E402
import worker  # noqa: E402

HOST = ""
SERVER = None
UID = 1
CONFIG = {
    "model": "claude-sonnet-5-5", "api_version": "2026-10-21", "max_tokens": 1536,
    "max_rounds": 4, "daily_requests": 1000,
}


def setUpModule() -> None:
    global HOST, SERVER
    SERVER = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    HOST = parse_qs(urlparse(SERVER.get_uri()).query)["host"][0]


def tearDownModule() -> None:
    SERVER.cleanup()


class Crash(BaseException):
    """Stands in for SIGKILL: escapes every `except Exception` in the code under test."""


def sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def tool_use(name: str, inputs: dict, use_id: str) -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": inputs}


def reply(content: list, stop: str = "tool_use") -> dict:
    return {"content": content, "stop_reason": stop, "usage": {"input_tokens": 100, "output_tokens": 20}}


class Claude:
    """Scripted Messages API: returns the queued responses in order and records every request."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list = []

    def __call__(self, version: str, request: dict) -> dict:
        self.requests.append(json.loads(json.dumps(request)))
        if not self.responses:
            raise AssertionError("Claude called more times than scripted")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return copy.deepcopy(item)


def patch_responses() -> list:
    return [
        reply([tool_use("list_files", {"query": ""}, "a1"), tool_use("read_file", {"path": "README.md"}, "a2")]),
        reply([tool_use("submit_patch", {
            "summary": "Fix typo", "edits": [{"path": "README.md", "old": "wrold", "new": "world"}],
        }, "b1")]),
    ]


class FakeGitHub:
    """Enough of the REST API for github_tasks, keyed by the repo-relative path it requests."""

    def __init__(self):
        self.base = sha("base")
        self.tree = sha("tree")
        self.files = {"README.md": "Hello wrold\n", "docs/guide.md": "# Guide\n\nSome text.\n"}
        self.blobs = {sha("blob:" + path): text for path, text in self.files.items()}
        self.refs = {"main": self.base}
        self.commits = {self.base: {"parents": [], "tree": self.tree, "author": UID}}
        self.issues = {1: self.issue(1)}
        self.pulls: list = []
        self.calls: list = []
        self.fail: dict = {}
        self.crash_after = None
        self.counter = 0

    @staticmethod
    def issue(number: int, author: int = UID) -> dict:
        return {
            "number": number, "state": "open", "title": f"Issue {number}", "body": "Fix the typo in README.",
            "labels": [{"name": "agent-ready"}], "updated_at": "2026-11-01T00:00:00Z",
            "user": {"id": author}, "assignee": None, "assignees": [], "locked": False,
        }

    def new_sha(self) -> str:
        self.counter += 1
        return sha(f"object-{self.counter}")

    def handle(self, gh, method: str, path: str, params=None, data=None):
        self.calls.append((method, path))
        for (fail_method, prefix), status in self.fail.items():
            if method == fail_method and path.startswith(prefix):
                raise github_tasks.GitHubError(status)
        result = self.route(method, path, params or {}, data)
        if self.crash_after and self.crash_after == (method, path):
            self.crash_after = None
            raise Crash()
        return result

    def route(self, method: str, path: str, params: dict, data):
        page = int(params.get("page", 1))
        if method == "GET":
            if path == "/user":
                return {"id": UID, "type": "User"}
            if path == "":
                return {"owner": {"id": UID, "type": "User"}, "full_name": "me/repo", "fork": False,
                        "archived": False, "disabled": False, "has_issues": True, "default_branch": "main"}
            if path in ("/collaborators", "/contributors"):
                return [{"id": UID}]
            if path == "/pulls":
                head = params.get("head")
                pulls = [p for p in self.pulls if head is None or "me:" + p["head"]["ref"] == head]
                return pulls if page == 1 else []
            if path == "/branches":
                return [{"name": name, "commit": {"sha": value}} for name, value in self.refs.items()]
            if path == "/commits":
                return self.history(params["sha"]) if page == 1 else []
            if path == "/issues":
                return [i for i in self.issues.values() if i["state"] == "open"] if page == 1 else []
            if path.startswith("/issues/"):
                return self.issues[int(path.rsplit("/", 1)[1])]
            if path.startswith("/git/commits/"):
                return {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}}
            if path.startswith("/git/trees/"):
                return {"truncated": False, "tree": [
                    {"path": p, "sha": sha("blob:" + p), "type": "blob", "mode": "100644", "size": len(t)}
                    for p, t in self.files.items()
                ]}
            if path.startswith("/git/blobs/"):
                blob = path.rsplit("/", 1)[1]
                return {"encoding": "base64", "sha": blob,
                        "content": base64.b64encode(self.blobs[blob].encode()).decode()}
            if path.startswith("/git/ref/heads/"):
                name = path[len("/git/ref/heads/"):]
                if name not in self.refs:
                    raise github_tasks.GitHubError(404)
                return {"object": {"sha": self.refs[name]}}
        if method == "POST":
            if path == "/git/trees":
                return {"sha": self.new_sha()}
            if path == "/git/commits":
                commit = self.new_sha()
                self.commits[commit] = {"parents": data["parents"], "tree": data["tree"], "author": UID}
                return {"sha": commit}
            if path == "/git/refs":
                name = data["ref"][len("refs/heads/"):]
                if name in self.refs:
                    raise github_tasks.GitHubError(422)
                self.refs[name] = data["sha"]
                return {"ref": data["ref"]}
            if path == "/pulls":
                if any(p["head"]["ref"] == data["head"] for p in self.pulls):
                    raise github_tasks.GitHubError(422)
                pull = {"number": len(self.pulls) + 1, "head": {"ref": data["head"]}, "body": data["body"],
                        "user": {"id": UID}, "draft": data["draft"]}
                self.pulls.append(pull)
                return pull
        raise AssertionError(f"unexpected GitHub call {method} {path}")

    def history(self, head: str) -> list:
        out, current = [], head
        while current:
            commit = self.commits[current]
            out.append({"sha": current, "author": {"id": commit["author"]}, "commit": {"message": "change"}})
            current = commit["parents"][0] if commit["parents"] else None
        return out


class Base(unittest.TestCase):
    def setUp(self):
        admin = psycopg2.connect(dbname="postgres", user="postgres", host=HOST)
        admin.autocommit = True
        admin.cursor().execute("DROP DATABASE IF EXISTS t WITH (FORCE)")
        admin.cursor().execute("CREATE DATABASE t")
        admin.close()
        self.conn = store.connect(f"dbname=t user=postgres host={HOST}")
        self.addCleanup(self.conn.close)
        self.db = self.conn.cursor()
        store.init_schema(self.db)

    def claim(self) -> dict:
        event = store.claim(self.db, store.now())
        self.assertIsNotNone(event)
        return event

    def serve_once(self, call) -> dict:
        """Mirror of one iteration of worker.serve's loop body."""
        event = self.claim()
        try:
            run = github_tasks.run_event if event["kind"] == "github" else worker.run_event
            run(self.db, event, CONFIG, call, lambda: False)
        except worker.QuotaExhausted:
            store.postpone_for_quota(self.db, event, store.now())
        except worker.ApiError as exc:
            delay = None if exc.retry_after is None else max(exc.retry_after, 15 * 2 ** (event["attempts"] - 1))
            with store.transaction(self.db):
                store.record_failure(self.db, event, str(exc), store.now(), delay)
        return self.event(event["id"])

    def event(self, event_id: int) -> dict:
        return self.db.execute("SELECT * FROM events WHERE id = %s", (event_id,)).fetchone()

    def outbox(self, kind: str = "reply") -> list:
        return [r["body"] for r in self.db.execute("SELECT * FROM outbox WHERE kind = %s ORDER BY id", (kind,))]


class GitHubBase(Base):
    def setUp(self):
        super().setUp()
        self.gh = FakeGitHub()
        original = github_tasks.GitHub.request
        github_tasks.GitHub.request = lambda gh, method, path, params=None, data=None: self.gh.handle(
            gh, method, path, params, data)
        self.addCleanup(setattr, github_tasks.GitHub, "request", original)
        env = {"ALWAYSON_GITHUB_REPO": "me/repo", "ALWAYSON_GITHUB_TOKEN": "github_pat_test",
               "ALWAYSON_GITHUB_PATHS": "README.md,docs/"}
        for key, value in env.items():
            self.addCleanup(os.environ.pop, key, None)
            os.environ[key] = value
        os.environ.pop("CREDENTIALS_DIRECTORY", None)

    def enqueue(self, mode: str = "apply") -> int:
        return store.enqueue(self.db, "github", "github", mode, store.now())

    def first_issue_number(self, claude: Claude) -> int:
        return json.loads(claude.requests[0]["messages"][0]["content"])["number"]


class GitHubFlowTest(GitHubBase):
    def test_preview_returns_diff_and_records_nothing(self):
        claude = Claude(patch_responses())
        self.enqueue("preview")
        event = self.serve_once(claude)
        self.assertEqual(event["status"], "done")
        self.assertIn("+Hello world", self.outbox()[0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) AS n FROM github_issues").fetchone()["n"], 0)
        self.assertEqual(self.gh.pulls, [])

    def test_request_shape(self):
        claude = Claude(patch_responses())
        self.enqueue("preview")
        self.serve_once(claude)
        self.assertEqual(claude.requests[0]["tool_choice"], {"type": "any"})

    def test_apply_publishes_one_draft_pr(self):
        claude = Claude(patch_responses())
        self.enqueue()
        event = self.serve_once(claude)
        self.assertEqual(event["status"], "done")
        self.assertEqual(len(self.gh.pulls), 1)
        self.assertTrue(self.gh.pulls[0]["draft"])
        row = self.db.execute("SELECT * FROM github_issues").fetchone()
        self.assertEqual((row["issue"], row["status"]), (1, "published"))

    def test_kill_after_each_write_still_yields_one_pr(self):
        for point in [("POST", "/git/trees"), ("POST", "/git/commits"), ("POST", "/git/refs"), ("POST", "/pulls")]:
            with self.subTest(point=point):
                self.setUp()
                self.gh.crash_after = point
                claude = Claude(patch_responses())
                event_id = self.enqueue()
                with self.assertRaises(Crash):
                    self.serve_once(claude)
                store.recover(self.db, store.now())
                event = self.serve_once(claude)
                self.assertEqual(event["status"], "done")
                self.assertLessEqual(event["attempts"], store.MAX_ATTEMPTS)
                self.assertEqual(len(self.gh.pulls), 1)
                self.assertEqual([r for r in self.gh.refs if r.startswith("alwayson/")], ["alwayson/issue-1"])
                self.assertEqual(len(self.outbox()), 1)
                self.assertEqual(self.event(event_id)["status"], "done")
                running = self.db.execute("SELECT COUNT(*) AS n FROM events WHERE status = 'running'").fetchone()
                self.assertEqual(running["n"], 0)

    def test_issue_that_keeps_failing_is_reselected(self):
        self.gh.issues[2] = FakeGitHub.issue(2)
        picked = []
        for _ in range(3):
            claude = Claude([reply([tool_use("list_files", {"query": ""}, f"x{i}")]) for i in range(CONFIG["max_rounds"])])
            self.enqueue("preview")
            event = self.serve_once(claude)
            self.assertEqual(event["status"], "failed")
            picked.append(self.first_issue_number(claude))
        self.assertEqual(picked, [1, 1, 1])

    def test_max_tokens_fails_the_event(self):
        claude = Claude([reply([], "max_tokens")])
        self.enqueue("preview")
        self.assertEqual(self.serve_once(claude)["status"], "failed")

    def test_expired_token_looks_like_a_skip(self):
        self.gh.fail[("GET", "/user")] = 401
        self.enqueue()
        event = self.serve_once(Claude([]))
        self.assertEqual(event["status"], "done")
        self.assertIn("跳过", self.outbox()[0])

    def test_issue_from_another_account_is_processed(self):
        self.gh.issues = {5: FakeGitHub.issue(5, author=2)}
        claude = Claude(patch_responses())
        self.enqueue("preview")
        self.serve_once(claude)
        self.assertEqual(self.first_issue_number(claude), 5)


class ChatTest(Base):
    def test_tool_loop_saves_progress_and_reply(self):
        claude = Claude([
            reply([tool_use("save_progress", {"summary": "step 1 done"}, "p1")]),
            reply([{"type": "text", "text": "好的"}], "end_turn"),
        ])
        store.enqueue(self.db, "user", "demo", "hi", store.now())
        event = self.serve_once(claude)
        self.assertEqual(event["status"], "done")
        self.assertEqual(self.outbox(), ["好的"])
        self.assertEqual(self.db.execute("SELECT value FROM progress").fetchone()["value"], "step 1 done")

    def test_thinking_block_survives_checkpoint_resume(self):
        first = [{"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"},
                 tool_use("save_progress", {"summary": "x"}, "p1")]
        store.enqueue(self.db, "user", "demo", "hi", store.now())
        with self.assertRaises(Crash):
            self.serve_once(Claude([reply(first), Crash()]))
        store.recover(self.db, store.now())
        claude = Claude([reply([{"type": "text", "text": "done"}], "end_turn")])
        self.serve_once(claude)
        self.assertEqual(claude.requests[0]["messages"][1], {"role": "assistant", "content": first})


class StoreTest(Base):
    def test_overdue_timer_fires_once(self):
        start = store.now() - 600
        with store.transaction(self.db):
            store.add_timer(self.db, "s", "ping", start, 60)
        store.tick(self.db, store.now())
        store.tick(self.db, store.now())
        self.assertEqual(self.db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"], 1)
        self.assertGreater(self.db.execute("SELECT due_at FROM timers").fetchone()["due_at"], store.now())

    def test_recover_requeues_running_event(self):
        store.enqueue(self.db, "user", "demo", "hi", store.now())
        event = self.claim()
        store.recover(self.db, store.now())
        row = self.event(event["id"])
        self.assertEqual((row["status"], row["attempts"]), ("queued", 1))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch

from achievements import BOOTSTRAP, Publisher, make_plan, statuses, verify_local_bundle
from audit import BadgeParser, collect, profile_badges
from cleanup import cleanup
from github_client import APIError, GitHub, SafetyError, parse_response, redact
from storage import digest, load, save


def initial_audit():
    return {"observed_at": "2026-10-15T00:00:00Z", "achievements": {
        name: {"status": "SKIPPED" if name in ("Quickdraw", "Starstruck") else "UNKNOWN",
               "tier": "UNKNOWN", "evidence": "test"}
        for name in ("Quickdraw", "Starstruck", "YOLO", "Pull Shark", "Pair Extraordinaire", "Galaxy Brain", "Public Sponsor")},
        "publication_target": {"repo": "deuxdoom/github-badge", "branches": [], "branches_complete": True,
            "default_branch": "main", "push": True, "rulesets": [], "private": False}}


class FakeGitHub:
    """Stateful remote, including mutations, for real orchestration and resume tests."""
    def __init__(self):
        self.repo = {"full_name": "deuxdoom/github-badge", "default_branch": "main", "private": False,
                     "permissions": {"push": True}, "allow_merge_commit": True}
        self.refs = {}
        self.commits = {}
        self.readme = None
        self.pr = None
        self.rules = []
        self.reviews = []
        self.protected = False
        self.checks = {"total_count": 0, "check_runs": []}
        self.status = {"total_count": 0, "state": "pending"}
        self.writes = []
        self.fail_after = None
        self.login = "deuxdoom"

    def assert_account(self):
        if self.login != "deuxdoom":
            raise SafetyError("account mismatch")

    def get(self, endpoint):
        path = endpoint.split("?")[0]
        suffix = path.removeprefix("repos/deuxdoom/github-badge")
        if suffix == "":
            return deepcopy(self.repo)
        if suffix in ("/rulesets", "/rules/branches/main"):
            return deepcopy(self.rules)
        if suffix == "/branches":
            return [{"name": branch} for branch in self.refs]
        if suffix == "/branches/main":
            return {"protected": self.protected, "commit": {"sha": self.refs["main"]}}
        if suffix.startswith("/git/ref/heads/"):
            branch = suffix.removeprefix("/git/ref/heads/")
            if branch not in self.refs:
                raise APIError(404, endpoint)
            return {"object": {"sha": self.refs[branch]}}
        if suffix.startswith("/git/commits/"):
            return deepcopy(self.commits[suffix.rsplit("/", 1)[1]])
        if suffix == "/contents/README.md":
            return {"content": self.readme}
        if suffix == "/pulls":
            if "state=open" in endpoint and self.pr and self.pr["merged"]:
                return []
            return [deepcopy(self.pr)] if self.pr else []
        if suffix == "/pulls/1":
            return deepcopy(self.pr)
        if suffix == "/pulls/1/reviews":
            return deepcopy(self.reviews)
        if suffix.endswith("/check-runs"):
            return deepcopy(self.checks)
        if suffix.endswith("/status"):
            return deepcopy(self.status)
        raise AssertionError("Unexpected GET " + endpoint)

    def optional(self, endpoint, *, statuses=(404,)):
        try:
            return self.get(endpoint)
        except APIError as exc:
            if exc.status in statuses:
                return None
            raise

    def pages(self, endpoint, **kwargs):
        return self.get(endpoint), True

    def write(self, method, endpoint, payload=None):
        self.assert_account()
        self.writes.append((method, endpoint))
        suffix = endpoint.removeprefix("repos/deuxdoom/github-badge")
        if suffix == "/contents/README.md":
            self.readme = payload["content"]
            self.refs["main"] = "base"
            self.commits["base"] = {"sha": "base", "message": payload["message"], "parents": [],
                "tree": {"sha": "base-tree"}, "author": {"name": "deuxdoom", "email": "test@users.noreply.github.com"}}
            result = {"commit": {"sha": "base"}}
        elif suffix == "/git/trees":
            result = {"sha": "source-tree"}
        elif suffix == "/git/commits":
            self.commits["source"] = deepcopy(payload)
            result = {"sha": "source"}
        elif suffix == "/git/refs":
            self.refs[payload["ref"].removeprefix("refs/heads/")] = payload["sha"]
            result = {"object": {"sha": payload["sha"]}}
        elif suffix == "/pulls":
            self.pr = {"number": 1, "body": payload["body"], "user": {"login": "deuxdoom"},
                "head": {"sha": "source", "ref": payload["head"], "repo": deepcopy(self.repo)},
                "base": {"ref": "main"}, "merged": False, "state": "open", "mergeable": True,
                "mergeable_state": "clean", "html_url": "https://github.com/deuxdoom/github-badge/pull/1"}
            result = deepcopy(self.pr)
        elif suffix == "/pulls/1/merge":
            self.pr.update(merged=True, merge_commit_sha="merge", state="closed")
            self.refs["main"] = "merge"
            result = {"merged": True}
        elif method == "DELETE" and suffix.startswith("/git/refs/heads/"):
            del self.refs[suffix.removeprefix("/git/refs/heads/")]
            result = None
        else:
            raise AssertionError("Unexpected write " + endpoint)
        if self.fail_after == suffix:
            self.fail_after = None
            raise APIError(503, endpoint, uncertain=True)
        return result


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.audit = initial_audit()
        self.plan = make_plan(self.audit)
        self.state = {"approved_digest": digest(self.plan), "branch": self.plan["branch"]}
        self.remote = FakeGitHub()
        self.snapshots = []
        self.publisher = Publisher(self.remote, self.plan, self.state,
                                   persist=lambda state: self.snapshots.append(deepcopy(state)))

    def test_full_workflow_and_second_run_do_not_duplicate(self):
        self.publisher.execute()
        cleanup(self.publisher)
        before = list(self.remote.writes)
        self.publisher.execute()
        cleanup(self.publisher)
        self.assertEqual(self.remote.writes, before)
        self.assertEqual(sum(path.endswith("/pulls") for _, path in before), 1)
        self.assertTrue(self.state["merged"])
        self.assertTrue(self.state["branch_cleaned"])
        self.assertEqual(list(self.remote.refs), ["main"])

    def test_observable_writes_recover_after_response_loss(self):
        for endpoint in ("/contents/README.md", "/git/refs", "/pulls", "/pulls/1/merge"):
            with self.subTest(endpoint=endpoint):
                self.setUp()
                self.remote.fail_after = endpoint
                with self.assertRaises(APIError):
                    self.publisher.execute()
                self.publisher.execute()
                cleanup(self.publisher)
                self.assertEqual(sum(path.endswith(endpoint) for _, path in self.remote.writes), 1)

    def test_immutable_objects_recover_deterministically(self):
        for endpoint in ("/git/trees", "/git/commits"):
            with self.subTest(endpoint=endpoint):
                self.setUp()
                self.remote.fail_after = endpoint
                with self.assertRaises(APIError):
                    self.publisher.execute()
                self.publisher.execute()
                self.assertTrue(self.state["merged"])

    def test_cleanup_recovers_lost_delete_response(self):
        self.publisher.execute()
        self.remote.fail_after = "/git/refs/heads/" + self.plan["branch"]
        with self.assertRaises(APIError):
            cleanup(self.publisher)
        cleanup(self.publisher)
        self.assertTrue(self.state["branch_cleaned"])

    def test_ambiguous_unobservable_pr_is_not_repeated(self):
        self.remote.fail_after = "/pulls"
        with self.assertRaises(APIError):
            self.publisher.execute()
        self.remote.pr = None
        count = len(self.remote.writes)
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertEqual(len(self.remote.writes), count)

    def test_unapproved_plan_is_refused(self):
        with self.assertRaises(SafetyError):
            Publisher(self.remote, self.plan, {}, persist=lambda _: None)

    def test_changed_source_invalidates_bundle(self):
        verify_local_bundle(self.plan)
        self.plan["payload"][0]["content"] += "modified"
        with self.assertRaises(SafetyError):
            verify_local_bundle(self.plan)

    def test_protected_branch_is_not_merged(self):
        self.remote.protected = True
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.state.get("merged", False))

    def test_active_ruleset_is_refused_before_writes(self):
        self.remote.rules = [{"enforcement": "active"}]
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.remote.writes)

    def test_existing_repo_is_not_overwritten(self):
        self.remote.refs["main"] = "old"
        self.remote.commits["old"] = {"message": "unrelated", "parents": []}
        self.remote.readme = "unrelated"
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.remote.writes)

    def test_cleanup_preserves_branch_with_new_work(self):
        self.publisher.execute()
        self.remote.refs[self.plan["branch"]] = "someone-else"
        with self.assertRaises(SafetyError):
            cleanup(self.publisher)
        self.assertIn(self.plan["branch"], self.remote.refs)

    def test_cleanup_preserves_unmerged_branch(self):
        self.publisher.execute()
        self.remote.pr["merged"] = False
        with self.assertRaises(SafetyError):
            cleanup(self.publisher)
        self.assertIn(self.plan["branch"], self.remote.refs)

    def test_failed_ci_is_not_merged(self):
        self.remote.checks = {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]}
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.state.get("merged", False))

    def test_added_review_stops_automatic_merge(self):
        self.remote.reviews = [{"state": "COMMENTED"}]
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.state.get("merged", False))

    def test_account_mismatch_stops_before_writes(self):
        self.remote.login = "other"
        with self.assertRaises(SafetyError):
            self.publisher.execute()
        self.assertFalse(self.remote.writes)

    def test_tampered_merged_pr_is_not_counted_as_success(self):
        self.publisher.execute()
        self.remote.pr["body"] = "unrelated"
        with self.assertRaises(SafetyError):
            self.publisher.execute()

    def test_other_open_pr_preserves_source_branch(self):
        self.publisher.execute()
        original_get = self.remote.get
        def get(endpoint):
            return [{"number": 2}] if "pulls?state=open" in endpoint else original_get(endpoint)
        self.remote.get = get
        with self.assertRaises(SafetyError):
            cleanup(self.publisher)
        self.assertIn(self.plan["branch"], self.remote.refs)

    def test_blocked_prerequisites_and_earned_skips(self):
        rows = {row["achievement"]: row for row in statuses(self.audit, {})}
        self.assertEqual(rows["Quickdraw"]["status"], "SKIPPED")
        self.assertEqual(rows["Starstruck"]["status"], "SKIPPED")
        for name in ("Pair Extraordinaire", "Galaxy Brain", "Public Sponsor"):
            self.assertEqual(rows[name]["status"], "BLOCKED")
        self.audit["achievements"]["Pair Extraordinaire"]["status"] = "VERIFIED"
        self.assertEqual(statuses(self.audit, {})[4]["status"], "VERIFIED")

    def test_merge_does_not_claim_badge(self):
        rows = statuses(self.audit, {"merged": True, "pr_url": "https://github.com/example"})
        self.assertEqual(rows[2]["status"], "PENDING")
        self.assertEqual(rows[3]["status"], "PENDING")

    def test_confirmed_earned_goals_do_not_generate_a_plan(self):
        plan = make_plan(self.audit, {"user_confirmed_earned": ["YOLO", "Pull Shark"]})
        self.assertTrue(any(reason.startswith("ALREADY_EARNED") for reason in plan["blocked"]))

    def test_config_cannot_change_account_or_repo(self):
        for config in ({"account": "other"}, {"repo": "deuxdoom/PICVIUM"}):
            with self.assertRaises(SafetyError):
                make_plan(self.audit, config)


class TransportTests(unittest.TestCase):
    def response(self, status=200, data=None, headers="", returncode=None):
        return subprocess.CompletedProcess([], int(status >= 400) if returncode is None else returncode,
            f"HTTP/2.0 {status} Response\n{headers}\n" + json.dumps(data), "")

    def test_parse_headers(self):
        status, headers, body = parse_response("HTTP/2.0 200 OK\r\nX-Test: value\r\n\r\n{}")
        self.assertEqual((status, headers["x-test"], body), (200, "value", "{}"))

    def test_dry_run_mutations_denied_without_subprocess(self):
        calls = []
        client = GitHub(runner=lambda *args, **kw: calls.append(args))
        for endpoint in ("repos/deuxdoom/github-badge/pulls", "repos/deuxdoom/PICVIUM/pulls"):
            with self.assertRaises(SafetyError):
                client.write("POST", endpoint, {})
        self.assertEqual(calls, [])

    def test_unsafe_endpoints_and_graphql_mutations_are_denied(self):
        client = GitHub(writable=True)
        for endpoint in ("repos/deuxdoom/PICVIUM/pulls", "repos/deuxdoom/github-badge/issues",
                         "repos/deuxdoom/github-badge/git/refs/heads/main"):
            with self.assertRaises(SafetyError):
                client.write("DELETE", endpoint)
        with self.assertRaises(SafetyError):
            client.graphql("mutation { addDiscussionComment }")

    def test_reads_retry_5xx_with_backoff(self):
        results = iter([self.response(503, {}), self.response(502, {}), self.response(200, {"ok": True})])
        sleeps = []
        client = GitHub(runner=lambda *args, **kw: next(results), sleeper=sleeps.append)
        self.assertEqual(client.get("user"), {"ok": True})
        self.assertEqual(sleeps, [1, 2])

    def test_retry_after_is_respected(self):
        results = iter([self.response(429, {}, "Retry-After: 60\n"), self.response(200, {})])
        sleeps = []
        client = GitHub(runner=lambda *args, **kw: next(results), sleeper=sleeps.append)
        client.get("rate_limit")
        self.assertEqual(sleeps, [60])

    def test_long_rate_limit_wait_is_deferred(self):
        sleeps = []
        client = GitHub(runner=lambda *args, **kw: self.response(429, {}, "Retry-After: 120\n"),
                        sleeper=sleeps.append)
        with self.assertRaises(APIError):
            client.get("rate_limit")
        self.assertEqual(sleeps, [])

    def test_retry_attempt_count_is_bounded(self):
        calls = []
        def runner(*args, **kwargs):
            calls.append(args)
            return self.response(503, {})
        client = GitHub(runner=runner, sleeper=lambda _: None, max_retries=3)
        with self.assertRaises(APIError):
            client.get("user")
        self.assertEqual(len(calls), 4)

    def test_writes_never_blindly_retry(self):
        calls = []
        def runner(args, **kw):
            calls.append(args)
            return self.response(200, {"login": "deuxdoom"}) if "user" in args else self.response(503, {})
        client = GitHub(writable=True, runner=runner, sleeper=lambda _: None)
        with self.assertRaises(APIError) as caught:
            client.write("POST", "repos/deuxdoom/github-badge/pulls", {"title": "test"})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(sum("POST" in args for args in calls), 1)

    def test_account_is_checked_at_each_write(self):
        calls = []
        def runner(args, **kw):
            calls.append(args)
            return self.response(200, {"login": "other"})
        client = GitHub(writable=True, runner=runner)
        with self.assertRaises(SafetyError):
            client.write("POST", "repos/deuxdoom/github-badge/pulls", {})
        self.assertEqual(len(calls), 1)

    def test_credentials_and_emails_are_redacted(self):
        text = redact("ghp_secret123456 user@example.com\nCookie: private\nhttps://hooks.slack.com/services/private")
        for secret in ("secret123456", "user@example.com", "Cookie: private", "/services/private"):
            self.assertNotIn(secret, text)


class AuditTests(unittest.TestCase):
    def test_profile_parser_uses_only_explicit_github_images(self):
        parser = BadgeParser()
        parser.feed('<img src="https://github.githubassets.com/assets/pull-shark-silver--light.png" alt="Achievement: Pull Shark">'
                    '<p>YOLO</p><img src="https://untrusted/x.png" alt="Achievement: YOLO">')
        self.assertEqual(parser.found["Pull Shark"]["tier"], "SILVER")
        self.assertNotIn("YOLO", parser.found)

    def test_empty_profile_is_not_proof_of_no_badges(self):
        observed = profile_badges(lambda: "<html>hidden</html>")
        self.assertFalse(observed["complete"])
        self.assertEqual(observed["badges"], {})

    def test_profile_fetch_failure_is_unknown(self):
        def fail():
            raise OSError("test")
        self.assertEqual(profile_badges(fail)["badges"], {})

    def test_audit_fails_on_wrong_account_before_profile_fetch(self):
        remote = FakeGitHub()
        remote.login = "other"
        with self.assertRaises(SafetyError):
            collect(remote, fetcher=lambda: self.fail("profile should not be fetched"))

    def test_atomic_checkpoint_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save(path, {"unicode": "한글", "progress": 1})
            save(path, {"unicode": "한글", "progress": 2})
            self.assertEqual(load(path)["progress"], 2)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


class EntryPointTests(unittest.TestCase):
    def test_dry_run_and_unapproved_execute_have_no_mutations(self):
        import run
        import storage
        audit = initial_audit()
        audit["rate_limit"] = {"core": {"remaining": 1000}}
        remote = FakeGitHub()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(run, "ROOT", root), patch.object(run, "STATE", root / ".state"),
                  patch.object(storage, "STATE", root / ".state"),
                  patch.object(run, "collect", return_value=audit), patch.object(run, "tests_pass"),
                  patch("sys.stdin", io.StringIO()), contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(run.main(["--dry-run"], client_factory=lambda: remote), 0)
                with self.assertRaises(SafetyError):
                    run.main(["--execute"], client_factory=lambda: remote)
                self.assertFalse((root / ".state" / "checkpoint.json").exists())
        self.assertEqual(remote.writes, [])


if __name__ == "__main__":
    unittest.main()

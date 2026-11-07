"""Automatic dispatch, exact revision checks, and data-only README validation."""

import copy
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_curation import BASE, ITEM, NOW, c
import readme_check as r


class API:
    def __init__(self):
        self.pr = {"number": 1, "state": "open", "body": c.MARKER,
                   "user": {"type": "Bot", "login": "github-actions[bot]"},
                   "head": {"ref": c.ENTRY_BRANCH_PREFIX + "a" * 16, "sha": "1" * 40,
                            "repo": {"full_name": "list/repo"}}}
        self.pulls = [self.pr]
        self.statuses = []
        self.reads, self.writes = [], []
        self.source = BASE + ITEM
        self.dispatch_error = None

    def get(self, path):
        if path == "/repos/list/repo":
            return {"default_branch": "main"}
        if path == "/repos/list/repo/pulls/1":
            return copy.deepcopy(self.pr)
        if path.endswith("/status"):
            return {"statuses": copy.deepcopy(self.statuses)}
        raise AssertionError(path)

    def pages(self, path):
        assert path == "/repos/list/repo/pulls?state=open&base=main"
        return copy.deepcopy(self.pulls)

    def text(self, repo, path, ref):
        self.reads.append((repo, path, ref))
        if isinstance(self.source, Exception):
            raise self.source
        return self.source

    def request(self, path, method, data):
        self.writes.append((path, method, copy.deepcopy(data)))
        if "/statuses/" in path:
            self.statuses.insert(0, dict(data, created_at=c.iso(NOW)))
        elif path.endswith("/dispatches") and self.dispatch_error:
            raise self.dispatch_error


class ReadmeCheckTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def dispatch(self):
        with patch.object(r, "utcnow", return_value=NOW):
            r.dispatch(self.api, "list/repo")

    def test_bot_dispatch_locks_default_workflow_to_exact_pr_revision(self):
        self.dispatch()
        path, method, data = self.api.writes[-1]
        self.assertEqual((path, method), ("/repos/list/repo/actions/workflows/test_readme.yml/dispatches", "POST"))
        self.assertEqual(data, {"ref": "main", "inputs": {"pr_number": "1", "head_sha": "1" * 40}})
        self.assertEqual(self.api.statuses[0]["state"], "pending")
        self.dispatch()
        self.assertEqual(len(self.api.writes), 2, "Queued check must not be dispatched twice")

    def test_passed_and_failed_checks_do_not_repeat_for_unchanged_heads(self):
        for state in ("success", "failure"):
            with self.subTest(state=state):
                self.api.statuses = [{"context": r.CONTEXT, "state": state,
                                      "description": f"Result [{r.signature()}]"}]
                self.dispatch()
                self.assertEqual(self.api.writes, [])

    def test_changed_validator_rules_automatically_recheck_cached_results(self):
        self.api.statuses = [{"context": r.CONTEXT, "state": "failure",
                              "description": "Old validator [outdated]"}]
        self.dispatch()
        self.assertEqual(len(self.api.writes), 2)
        self.assertTrue(self.api.statuses[0]["description"].endswith(f"[{r.signature()}]"))

    def test_stale_pending_and_error_checks_can_retry(self):
        for state in ("pending", "error"):
            with self.subTest(state=state):
                self.api.writes.clear()
                self.api.statuses = [{"context": r.CONTEXT, "state": state,
                                      "description": f"Queued [{r.signature()}]",
                                      "created_at": c.iso(NOW - timedelta(minutes=21))}]
                self.dispatch()
                self.assertEqual(len(self.api.writes), 2)

    def test_dispatch_error_is_reported_and_can_be_retried(self):
        self.api.dispatch_error = c.Unavailable("Cannot dispatch", 503)
        with self.assertRaises(c.Unavailable):
            self.dispatch()
        self.assertEqual(self.api.statuses[0]["state"], "error")
        self.api.dispatch_error = None
        self.dispatch()
        self.assertEqual(self.api.statuses[0]["state"], "pending")

    def test_only_recognized_same_repository_bot_proposals_are_dispatched(self):
        for variant in ("human", "fork", "unmarked", "unreserved"):
            with self.subTest(variant=variant):
                api = API()
                if variant == "human":
                    api.pr["user"] = {"type": "User", "login": "contributor"}
                elif variant == "fork":
                    api.pr["head"]["repo"]["full_name"] = "fork/repo"
                elif variant == "unmarked":
                    api.pr["body"] = "Unrecognized proposal"
                else:
                    api.pr["head"]["ref"] = "untrusted"
                r.dispatch(api, "list/repo")
                self.assertEqual(api.writes, [])

    def test_check_reads_only_markdown_at_the_exact_head_and_reports_success(self):
        self.api.pr["head"]["repo"]["full_name"] = "contributor/fork"
        with patch.object(r, "lint") as lint:
            r.check(self.api, "list/repo", 1, "1" * 40)
        self.assertEqual(self.api.reads, [("contributor/fork", "_README.md", "1" * 40)])
        lint.assert_called_once_with(BASE + ITEM)
        self.assertEqual([row["state"] for row in reversed(self.api.statuses)], ["pending", "success"])

    def test_closed_or_superseded_pr_does_not_publish_success(self):
        for closed in (False, True):
            with self.subTest(closed=closed), patch.object(r, "lint") as lint:
                self.api.pr["state"] = "closed" if closed else "open"
                r.check(self.api, "list/repo", 1, "2" * 40)
                lint.assert_not_called()
                self.assertEqual(self.api.writes, [])
                self.assertEqual(self.api.reads, [])

    def test_lint_failure_is_a_failure_and_network_failure_is_inconclusive(self):
        with patch.object(r, "lint", side_effect=subprocess.CalledProcessError(1, ["npx"])):
            with self.assertRaises(subprocess.CalledProcessError):
                r.check(self.api, "list/repo", 1)
        self.assertEqual(self.api.statuses[0]["state"], "failure")
        self.api.source = c.Unavailable("Source unavailable", 503)
        with self.assertRaises(c.Unavailable):
            r.check(self.api, "list/repo", 1)
        self.assertEqual(self.api.statuses[0]["state"], "error")

    def test_linter_receives_generated_data_in_trusted_repo_context_without_tokens(self):
        def run(args, **kwargs):
            self.assertEqual(args[:3], ["npx", "--yes", r.LINTER])
            self.assertEqual(Path(args[3]).read_text(), "generated Markdown")
            self.assertEqual(Path(args[3]).parent, r.ROOT)
            self.assertRegex(Path(args[3]).name, r"^readme-check-[0-9a-f]{32}\.md$")
            self.assertEqual(kwargs["cwd"], r.ROOT)
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("NODE_AUTH_TOKEN", kwargs["env"])
            self.assertTrue(kwargs["check"])

        with patch.dict(os.environ, GH_TOKEN="secret", GITHUB_TOKEN="secret", NODE_AUTH_TOKEN="secret"):
            with patch.object(r, "render_readme", return_value="generated Markdown") as generate:
                with patch.object(r.subprocess, "run", side_effect=run):
                    r.lint("Submitted Markdown, never a script or npm configuration")
                generate.assert_called_once_with("Submitted Markdown, never a script or npm configuration")

    def test_invalid_inputs_are_rejected_before_reading_a_pr(self):
        for number, sha in (("1/../../other", None), (0, None), (1, "main")):
            with self.subTest(number=number, sha=sha), self.assertRaises(ValueError):
                r.check(self.api, "list/repo", number, sha)
        self.assertFalse(self.api.reads or self.api.writes)


if __name__ == "__main__":
    unittest.main()

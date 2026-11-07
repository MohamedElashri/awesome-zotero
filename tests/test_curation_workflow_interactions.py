"""Exercise ownership and state transitions across weekly/event/daily workflows."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_curation as fixtures
from test_curation import BASE, BODY, ITEM, NOW, POLICY, FakeAPI, c


def cleanup_pr():
    pr = copy.deepcopy(FakeAPI().pr)
    pr["head"].update(ref=c.CLEANUP_BRANCH, repo={"full_name": "list/repo"})
    pr["user"] = {"type": "Bot", "login": "github-actions[bot]"}
    pr["body"] = c.MARKER + "\nAwaiting recheck: previous removal finding"
    return pr


class MaintenanceStatusOwnershipTests(unittest.TestCase):
    def test_ready_cleanup_pr_preserves_each_weekly_decision_without_new_writes(self):
        for state in ("pending", "error", "failure", "success"):
            with self.subTest(state=state):
                api = FakeAPI()
                api.pr = cleanup_pr()
                api.before, api.after = BASE + ITEM, BASE
                c.status(api, "list/repo", "head", "curation/admission", state, "Weekly decision")
                expected = copy.deepcopy(api.writes)
                api.pages = Mock(side_effect=AssertionError("Admission must not inspect or approve the cleanup PR"))
                with patch.object(c, "utcnow", return_value=NOW):
                    c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
                self.assertEqual(api.writes, expected)

    def test_duplicate_fork_pr_cannot_overwrite_status_on_the_same_bot_commit(self):
        api = FakeAPI()
        api.before, api.after = BASE + ITEM, BASE
        api.pr["head"].update(ref="duplicate-cleanup", repo={"full_name": "contributor/repo"})
        api.pr["labels"] = [{"name": POLICY["override_label"]}]
        get = api.get
        api.get = lambda path: {"statuses": [{"context": "curation/maintenance", "state": "pending"}]} if path.endswith("/status") else get(path)
        c.status(api, "list/repo", "head", "curation/admission", "pending", "Weekly decision")
        expected = copy.deepcopy(api.writes)
        c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
        self.assertEqual(api.writes, expected)

    def test_published_cleanup_head_is_protected_before_its_status_exists(self):
        api = FakeAPI()
        get = api.get
        api.get = lambda path: {"object": {"sha": "head"}} if "/git/ref/heads/" in path else get(path)
        c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
        self.assertEqual(api.writes, [])

    def test_maintenance_claim_published_during_inspection_prevents_final_success(self):
        api = FakeAPI()
        request = api.request

        def fresh(path, method="GET", data=None):
            if method == "GET" and path.endswith("/status"):
                return {"statuses": [{"context": "curation/maintenance", "state": "pending"}]}
            return request(path, method, data)

        api.request = fresh
        c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
        self.assertFalse(any(data.get("state") == "success" for _, _, data in api.writes))

    def test_daily_selection_excludes_duplicate_prs_with_maintenance_owned_commits(self):
        api = FakeAPI()
        api.pages = Mock(return_value=[copy.deepcopy(api.pr)])
        api.get = Mock(return_value={"statuses": [{"context": "curation/maintenance", "state": "pending"}]})
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [])

    def test_admission_override_cannot_waive_maintenance_verification(self):
        api = FakeAPI()
        api.pr = cleanup_pr()
        api.pr["labels"] = [{"name": POLICY["override_label"]}]
        c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
        self.assertEqual(api.writes, [])

    def test_daily_selection_leaves_cleanup_to_the_weekly_owner(self):
        api = FakeAPI()
        cleanup = cleanup_pr()
        ordinary = copy.deepcopy(FakeAPI().pr)
        ordinary["number"], ordinary["head"]["sha"] = 13, "ordinary"
        api.pages = Mock(return_value=[cleanup, ordinary])
        api.get = Mock(return_value={"statuses": [{"context": "curation/admission", "state": "pending"}]})
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [13])
        api.get.assert_called_once_with("/repos/list/repo/commits/ordinary/status")

    def test_fork_branch_body_marker_or_human_author_cannot_claim_cleanup_role(self):
        for field in ("fork", "other-branch", "human"):
            with self.subTest(field=field):
                api = FakeAPI()
                api.pr = cleanup_pr()
                api.pr["body"] = BODY + "\n" + c.MARKER
                if field == "fork":
                    api.pr["head"]["repo"]["full_name"] = "contributor/repo"
                elif field == "other-branch":
                    api.pr["head"]["ref"] = "feature/untrusted"
                else:
                    api.pr["user"] = {"type": "User", "login": "contributor"}
                api.meta["created_at"] = c.iso(NOW)
                with patch.object(c, "utcnow", return_value=NOW):
                    c.admission(api, "list/repo", {"pull_request": {"number": 12}}, POLICY)
                self.assertTrue(any(data == {"state": "closed"} for _, _, data in api.writes))

    def test_weekly_failure_then_admission_event_then_daily_selection_remains_pending(self):
        case = fixtures.WeeklyIntegrationTests()
        case.setUp()
        case.api.last_commit = "2020-01-01T00:00:00Z"
        case.api.releases = []
        case.run_weekly()
        case.api.failure = c.Unavailable("Transient metadata failure", 503)
        case.run_weekly()
        api = FakeAPI()
        api.pr = copy.deepcopy(case.api.open_pulls[0])
        api.before, api.after = case.api.source, BASE
        text = api.text
        api.text = lambda repo, path, ref: json.dumps(case.api.state) if path == c.STATE_FILE else text(repo, path, ref)
        api.writes = case.api.writes
        get, request = api.get, api.request
        pull_path = "/repos/list/repo/pulls/50"
        api.get = lambda path: copy.deepcopy(api.pr) if path == pull_path else get(path)
        api.request = lambda path, method="GET", data=None: copy.deepcopy(api.pr) if path == pull_path and method == "GET" else request(path, method, data)
        with patch.object(c, "utcnow", return_value=NOW), patch.object(c, "verify_cleanup_tree", return_value="base"):
            c.admission(api, "list/repo", {"pull_request": {"number": 50}}, POLICY)
        api.pages = Mock(return_value=[copy.deepcopy(api.pr)])
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [])
        statuses = [data for path, _, data in api.writes if "/statuses/" in path and data.get("context") == "curation/admission"]
        self.assertEqual(statuses[-1]["state"], "pending")
        self.assertIn("Awaiting recheck", case.api.open_pulls[0]["body"])


if __name__ == "__main__":
    unittest.main()

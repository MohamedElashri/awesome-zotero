"""Single-entry diffs, migration, repeat runs, and automatic base refresh."""

import copy
import unittest
from unittest.mock import patch

import test_curation as fixtures
from test_curation import BASE, ITEM, NOW, POLICY, c


OTHER = "- [Other](https://github.com/owner/other) - Another useful Zotero tool.\n"


class PerEntryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WeeklyIntegrationTests()
        self.case.setUp()
        self.api = self.case.api

    def run_weekly(self, **kwargs):
        self.case.run_weekly(**kwargs)

    def stale(self):
        self.api.last_commit = "2020-01-01T00:00:00Z"
        self.api.releases = []

    def files(self, pr):
        tree = self.api.trees[self.api.commits[pr["head"]["sha"]]["tree"]["sha"]]
        return {row["path"]: row["content"] for row in tree["tree"]}

    def legacy(self):
        pr = {"number": 49, "state": "open", "draft": True,
              "body": c.MARKER + "\n[Tool](https://github.com/owner/tool)",
              "user": {"type": "Bot", "login": "github-actions[bot]"},
              "head": {"ref": c.CLEANUP_BRANCH, "sha": "legacy", "repo": {"full_name": "list/repo"}}}
        self.api.open_pulls.append(pr)
        return pr

    def test_two_removals_have_independent_source_and_generated_readme(self):
        self.api.source += OTHER
        self.stale()
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 2)
        for pr in self.api.open_pulls:
            files = self.files(pr)
            removed, retained = (ITEM, OTHER) if pr["title"] == "Remove item: Tool" else (OTHER, ITEM)
            self.assertNotIn(removed, files[c.SOURCE])
            self.assertIn(retained, files[c.SOURCE])
            self.assertEqual(files["README.md"], c.render_readme(files[c.SOURCE]))
            self.assertEqual(len(c.entries(files[c.SOURCE])), 1)
            self.assertTrue(pr["draft"])
            self.assertTrue(c.is_cleanup_pr("list/repo", pr))
            self.assertTrue(c.is_entry_branch(pr["head"]["ref"]))
            self.assertEqual(pr["body"].count("| Remove |"), 1)

    def test_url_fix_is_separate_from_removal(self):
        self.api.source += OTHER
        get = self.api.get

        def metadata(path):
            if path == "/repos/owner/tool":
                return dict(self.api.meta, archived=True)
            if path == "/repos/owner/other":
                return dict(self.api.meta, full_name="moved/other")
            return get(path)

        self.api.get = metadata
        self.run_weekly()
        self.assertEqual({pr["title"] for pr in self.api.open_pulls}, {"Remove item: Tool", "Fix item: Other"})
        fix = next(pr for pr in self.api.open_pulls if pr["title"].startswith("Fix"))
        self.assertIn(ITEM, self.files(fix)[c.SOURCE])
        self.assertIn("https://github.com/moved/other", self.files(fix)[c.SOURCE])
        self.assertNotIn("| Remove |", fix["body"])

    def test_identical_duplicate_rows_still_get_one_removal_per_pr(self):
        self.api.source += ITEM
        self.stale()
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 2)
        self.assertEqual(len({pr["head"]["ref"] for pr in self.api.open_pulls}), 2)
        for pr in self.api.open_pulls:
            self.assertEqual(self.files(pr)[c.SOURCE], BASE + ITEM)

    def test_declined_proposal_is_not_recreated_for_the_same_evidence(self):
        self.stale()
        self.run_weekly()
        self.api.open_pulls[0]["state"] = "closed"
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 1)
        self.assertEqual(self.api.writes, [])

    def test_changed_evidence_can_open_a_new_proposal_after_decline(self):
        self.stale()
        self.run_weekly()
        self.api.open_pulls[0]["state"] = "closed"
        self.api.meta["archived"] = True
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 2)
        self.assertEqual(self.api.open_pulls[1]["state"], "open")

    def test_reintroduced_entry_can_be_proposed_after_an_earlier_merge(self):
        self.stale()
        self.run_weekly()
        self.api.open_pulls[0].update(state="closed", merged_at=c.iso(NOW))
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 2)
        self.assertEqual(self.api.open_pulls[1]["state"], "open")

    def test_merge_one_refreshes_remaining_pr_against_main_without_project_reads(self):
        self.api.source += OTHER
        self.stale()
        self.run_weekly()
        merged, remaining = self.api.open_pulls
        old_head = remaining["head"]["sha"]
        self.api.source = self.files(merged)[c.SOURCE]
        merged["state"] = "closed"
        merged["merged_at"] = c.iso(NOW)
        self.api.branches["main"] = "base-2"
        self.api.commits["base-2"] = {"message": "Merged first removal", "tree": {"sha": "base-tree-2"}}
        saved = copy.deepcopy(self.api.state)
        self.api.failure = c.Unavailable("Project inspection must not run", 503)
        with patch.object(c, "maintenance", side_effect=AssertionError("Refresh must not inspect projects")):
            self.run_weekly(refresh=True)
        self.assertNotEqual(remaining["head"]["sha"], old_head)
        parents = self.api.commits[remaining["head"]["sha"]]["parents"]
        self.assertIn({"sha": "base-2"}, parents)
        self.assertEqual(self.files(remaining)[c.SOURCE], BASE)
        self.assertEqual(self.api.state, saved)
        self.assertEqual(len(self.api.open_pulls), 2)

    def test_refresh_does_not_reopen_a_declined_proposal(self):
        self.stale()
        self.run_weekly()
        self.api.open_pulls[0]["state"] = "closed"
        self.run_weekly(refresh=True)
        self.assertEqual(len(self.api.open_pulls), 1)
        self.assertEqual(self.api.writes, [])

    def test_refresh_closes_proposal_when_target_row_has_been_edited(self):
        self.stale()
        self.run_weekly()
        self.api.source = self.api.source.replace("A useful", "A different")
        self.run_weekly(refresh=True)
        self.assertEqual(self.api.open_pulls[0]["state"], "closed")
        self.assertFalse(any("/git/" in path for path, _, _ in self.api.writes))

    def test_refresh_keeps_unknown_findings_pending(self):
        self.stale()
        self.run_weekly()
        self.api.failure = c.Unavailable("Network failure", 503)
        self.run_weekly()
        self.run_weekly(refresh=True)
        statuses = [data for path, _, data in self.api.writes if "/statuses/" in path]
        self.assertTrue(statuses)
        self.assertEqual({row["state"] for row in statuses}, {"pending"})

    def test_refresh_requires_recheck_after_policy_change(self):
        self.stale()
        self.run_weekly()
        with patch.dict(POLICY, inactive_days=730):
            self.run_weekly(refresh=True)
        statuses = [data for path, _, data in self.api.writes if "/statuses/" in path]
        self.assertEqual({row["state"] for row in statuses}, {"pending"})

    def test_refresh_closes_newly_exempt_inactivity_proposal(self):
        self.stale()
        self.run_weekly()
        with patch.dict(POLICY, inactivity_exemptions={"owner/tool": "Still useful"}):
            self.run_weekly(refresh=True)
        self.assertEqual(self.api.open_pulls[0]["state"], "closed")

    def test_retained_url_fix_stays_pending_when_project_recheck_fails(self):
        get = self.api.get
        self.api.get = lambda path: dict(self.api.meta, full_name="moved/tool") if path == "/repos/owner/tool" else get(path)
        self.run_weekly()
        self.api.get = get
        self.api.failure = c.Unavailable("Network failure", 503)
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 1)
        self.assertEqual(self.api.open_pulls[0]["state"], "open")
        statuses = [data for path, _, data in self.api.writes if "/statuses/" in path]
        self.assertEqual({row["state"] for row in statuses}, {"pending"})

    def test_refresh_without_cached_proposals_leaves_legacy_pr_untouched(self):
        self.legacy()
        self.run_weekly(refresh=True)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.open_pulls[0]["state"], "open")

    def test_legacy_closes_only_after_all_individual_replacements_exist(self):
        legacy = self.legacy()
        self.api.source = BASE + "".join(ITEM.replace("Tool]", f"Tool {i}]") for i in range(14))
        self.stale()
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 11)
        self.assertEqual(legacy["state"], "open")
        self.assertLess(self.api.write_count, c.MAX_WRITES)
        self.run_weekly(refresh=True)
        self.assertEqual(len(self.api.open_pulls), 11)
        self.assertEqual(legacy["state"], "open")
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 15)
        self.assertEqual(legacy["state"], "closed")
        self.assertIn("Superseded", legacy["body"])
        self.assertLess(self.api.write_count, c.MAX_WRITES)
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 15)

    def test_individual_branch_gap_is_protected_when_legacy_branch_is_absent(self):
        branch = c.entry_branch(c.entry_record(c.entries(ITEM)[0], 0))

        def read(path):
            if path.endswith("/status"):
                return {"statuses": []}
            if "/matching-refs/" in path:
                return [{"ref": "refs/heads/" + branch, "object": {"sha": "protected"}}]
            raise c.Unavailable("No legacy branch", 404)

        self.api.get = read
        self.assertTrue(c.maintenance_owns_commit(self.api, "list/repo", "protected"))
        self.assertFalse(c.maintenance_owns_commit(self.api, "list/repo", "ordinary"))


if __name__ == "__main__":
    unittest.main()

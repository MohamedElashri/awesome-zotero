"""Normal branch updates must not lose maintenance approval or block the queue."""

import copy
import subprocess
import unittest
from unittest.mock import patch

import test_curation as fixtures
from test_curation import BASE, ITEM, POLICY, c


OTHER = "- [Other](https://github.com/owner/other) - Another useful Zotero tool.\n"


class BranchUpdateTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WeeklyIntegrationTests()
        self.case.setUp()
        self.api = self.case.api
        self.api.last_commit = "2020-01-01T00:00:00Z"
        self.api.releases = []

    def create(self, two=False):
        if two:
            self.api.source += OTHER
        self.case.run_weekly()
        return self.api.open_pulls[0]

    def update_branch(self, pr):
        old = pr["head"]["sha"]
        self.api.commits["human-merge"] = {
            "message": "Merge branch 'main' into the cleanup branch",
            "tree": copy.deepcopy(self.api.commits[old]["tree"]),
            "parents": [{"sha": old}, {"sha": self.api.branches["main"]}]}
        self.api.branches[pr["head"]["ref"]] = "human-merge"
        pr["head"]["sha"] = "human-merge"
        return "human-merge"

    def advance_main(self):
        self.api.branches["main"] = "base-2"
        self.api.commits["base-2"] = {"message": "Trusted main change", "tree": {"sha": "base-tree-2"}}
        for pr in self.api.open_pulls:
            pr["base"]["sha"] = "base-2"

    def alter_head(self, pr, files):
        sha = pr["head"]["sha"]
        old_tree = self.api.commits[sha]["tree"]["sha"]
        rows = {row["path"]: copy.deepcopy(row) for row in self.api.tree_rows[old_tree]}
        for name, content in files.items():
            rows[name] = {"path": name, "mode": "100644", "type": "blob", "sha": c.blob_sha(content)}
        tree = c.updated_tree_sha(list(rows.values()), {})
        self.api.tree_rows[tree] = list(rows.values())
        self.api.commits[sha]["tree"]["sha"] = tree

    def admission(self, pr):
        self.api.writes = []
        c.admission(self.api, "list/repo", {"pull_request": {"number": pr["number"]}}, POLICY)
        return [data for path, _, data in self.api.writes if "/statuses/" in path]

    def test_update_branch_merge_is_retained_when_it_exactly_matches_the_plan(self):
        pr = self.create()
        sha = self.update_branch(pr)
        self.case.run_weekly(refresh=True)
        self.assertEqual(pr["head"]["sha"], sha)
        self.assertFalse(any("/git/refs" in path for path, _, _ in self.api.writes))
        states = [data["state"] for path, _, data in self.api.writes if "/statuses/" in path]
        self.assertEqual(states, ["success", "success"])

    def test_merge_of_main_can_be_safely_refreshed_again_after_main_advances(self):
        pr = self.create()
        old = self.update_branch(pr)
        self.advance_main()
        self.case.run_weekly(refresh=True)
        new = pr["head"]["sha"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.api.commits[new]["parents"], [{"sha": "base-2"}, {"sha": old}])
        tree = self.api.trees[self.api.commits[new]["tree"]["sha"]]
        self.assertEqual(tree["base_tree"], "base-tree-2")
        self.assertTrue(all(not data.get("force") for _, _, data in self.api.writes))

    def test_unexpected_human_changes_are_preserved_while_other_prs_refresh(self):
        pr = self.create(two=True)
        old = self.update_branch(pr)
        self.alter_head(pr, {"unexpected.py": "Unexpected submitted code"})
        healthy = self.api.open_pulls[1]
        healthy_old = healthy["head"]["sha"]
        self.advance_main()
        with self.assertRaisesRegex(c.Unavailable, "Other cleanup proposals were processed"):
            self.case.run_weekly(refresh=True)
        self.assertEqual(pr["head"]["sha"], old)
        self.assertNotEqual(healthy["head"]["sha"], healthy_old)
        statuses = [(path, data) for path, _, data in self.api.writes if "/statuses/" in path]
        self.assertTrue(any(path.endswith(old) and data["state"] == "failure" for path, data in statuses))
        self.assertTrue(any(path.endswith(healthy["head"]["sha"]) and data["state"] == "success" for path, data in statuses))

    def test_admission_repairs_missing_required_status_after_update_branch(self):
        pr = self.create()
        self.update_branch(pr)
        statuses = self.admission(pr)
        self.assertEqual({row["context"] for row in statuses}, {"curation/maintenance", "curation/admission"})
        self.assertEqual({row["state"] for row in statuses}, {"success"})
        self.assertTrue(all("/statuses/" in path for path, _, _ in self.api.writes), "Validation must not write Git objects or PR content")

    def test_normal_bot_head_is_also_fully_validated_before_admission_success(self):
        pr = self.create()
        self.alter_head(pr, {"unexpected.txt": "Even a bot-looking commit must be checked"})
        self.assertEqual({row["state"] for row in self.admission(pr)}, {"failure"})

    def test_override_cannot_approve_extra_edits_or_inconclusive_findings(self):
        pr = self.create()
        self.update_branch(pr)
        pr["labels"] = [{"name": POLICY["override_label"]}]
        self.api.state["_cleanup"][pr["head"]["ref"]]["pending"] = True
        self.assertEqual({row["state"] for row in self.admission(pr)}, {"pending"})
        self.alter_head(pr, {"README.md": "Hand-edited generated output"})
        self.assertEqual({row["state"] for row in self.admission(pr)}, {"failure"})

    def test_missing_plan_and_changed_policy_never_publish_success(self):
        pr = self.create()
        self.api.state["_cleanup_policy"]["inactive_days"] += 1
        self.assertEqual({row["state"] for row in self.admission(pr)}, {"pending"})
        self.api.state = {}
        self.assertEqual({row["state"] for row in self.admission(pr)}, {"error"})

    def test_admission_does_not_publish_to_a_superseded_head(self):
        pr = self.create()

        def changed(*args):
            pr["head"]["sha"] = "newer-head"

        with patch.object(c, "verify_cleanup_tree", side_effect=changed):
            self.assertEqual(self.admission(pr), [])


class GitTreeHashTests(unittest.TestCase):
    def test_root_tree_hash_matches_native_git_including_modes_and_directory_order(self):
        readme, source = "Generated README\n", BASE + ITEM
        native_blob = lambda value: subprocess.check_output(["git", "hash-object", "--stdin"], input=value.encode()).decode().strip()
        readme_sha, source_sha = native_blob(readme), native_blob(source)
        rows = [{"path": "assets", "mode": "040000", "type": "tree", "sha": "b" * 40},
                {"path": "assets.txt", "mode": "100644", "type": "blob", "sha": "c" * 40},
                {"path": "link", "mode": "120000", "type": "blob", "sha": "d" * 40}]
        # Git sorts a directory as if its name ended with '/', after assets.txt.
        body = (b"100644 README.md\0" + bytes.fromhex(readme_sha) +
                b"100644 _README.md\0" + bytes.fromhex(source_sha) +
                b"100644 assets.txt\0" + bytes.fromhex("c" * 40) +
                b"40000 assets\0" + bytes.fromhex("b" * 40) +
                b"120000 link\0" + bytes.fromhex("d" * 40))
        expected = subprocess.check_output(["git", "hash-object", "-t", "tree", "--stdin"], input=body).decode().strip()
        self.assertEqual(c.updated_tree_sha(rows, {"README.md": readme, c.SOURCE: source}), expected)


if __name__ == "__main__":
    unittest.main()

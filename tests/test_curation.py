import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
spec = importlib.util.spec_from_file_location("curation", ROOT / ".github/scripts/curation.py")
c = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = c
spec.loader.exec_module(c)
POLICY = json.loads((ROOT / ".github/curation.json").read_text())
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
BASE = "# Awesome Zotero\n\n## Extensions\n"
ITEM = "- [Tool](https://github.com/owner/tool) - A useful Zotero plugin.\n"
WORDS = "Checked Tool v1.0.0 in Zotero 10.0.8 with a temporary library and verified the documented task completed successfully."
BODY = f"""Project URL: https://github.com/owner/tool
Project type: plugin
Documentation URL: https://github.com/owner/tool/blob/main/README.md
Installation URL: https://github.com/owner/tool/releases
Tested version: v1.0.0
Tested Zotero version: 10.0.8
Verification: {WORDS}
Distinct value: This plugin adds collection-specific automation missing from the built-in Zotero interface and the tools already listed here.
Data handling: The plugin processes metadata locally and does not send attachments or credentials to external services or delete library files.
AI-generated code: some
"""


class FakeAPI:
    def __init__(self):
        self.meta = {"full_name": "owner/tool", "created_at": "2025-01-01T00:00:00Z",
                     "default_branch": "main", "size": 20, "archived": False}
        self.last_commit = "2026-10-01T00:00:00Z"
        self.source_manifest = {"manifest_version": 2, "name": "Tool", "version": "1.0.0", "applications": {
            "zotero": {"id": "tool@example.org", "strict_min_version": "6.999", "strict_max_version": "10.*"}}}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", json.dumps(self.source_manifest))
            archive.writestr("bootstrap.js", "function startup() {}")
        self.asset_bytes = output.getvalue()
        self.releases = [{"tag_name": "v1.0.0", "published_at": "2026-10-01T00:00:00Z", "draft": False,
                          "prerelease": False, "assets": [{"id": 1, "name": "tool.xpi", "size": len(self.asset_bytes)}]}]
        self.readme_text = "Working Zotero installation and usage documentation. " * 20
        self.failure = None
        self.commit_failure = None
        self.link_result = True
        self.writes = []
        self.comments = []
        self.before = BASE
        self.after = BASE + ITEM
        self.pr = {"number": 12, "state": "open", "draft": False, "labels": [], "body": BODY,
                   "head": {"sha": "head"}, "base": {"sha": "base", "ref": "main"}}
        self.current = None
        self.files = [{"filename": "_README.md"}]

    def get(self, path):
        if "/git/matching-refs/" in path:
            return []
        if path == "/repos/list/repo/pulls/12":
            return copy.deepcopy(self.pr)
        if "/releases/tags/" in path:
            release = next((row for row in self.releases if row["tag_name"] == path.rsplit("/", 1)[1]), None)
            if release is None:
                raise c.Unavailable("No release", 404)
            return copy.deepcopy(release)
        if "/commits/" in path:
            return {"sha": "project-head"}
        if path.startswith("/repos/") and "/commits?" not in path:
            if self.failure:
                raise self.failure
            return copy.deepcopy(self.meta)
        if "/commits?" in path:
            if self.commit_failure:
                raise self.commit_failure
            return [{"commit": {"committer": {"date": self.last_commit}}}]
        raise AssertionError(f"Unexpected GET: {path}")

    def pages(self, path):
        if path.endswith("/releases"):
            return copy.deepcopy(self.releases)
        if path.endswith("/files"):
            return self.files
        if path.endswith("/comments"):
            return self.comments
        raise AssertionError(f"Unexpected pages: {path}")

    def readme(self, repo, ref):
        if isinstance(self.readme_text, Exception):
            raise self.readme_text
        return self.readme_text

    def link(self, url):
        if isinstance(self.link_result, Exception):
            raise self.link_result
        return self.link_result

    def text(self, repo, path, ref):
        if path in {"manifest.json", "src/manifest.json", "addon/manifest.json"}:
            if path == "manifest.json" and self.source_manifest:
                return json.dumps(self.source_manifest)
            raise c.Unavailable("No manifest", 404)
        return self.before if ref == "base" else self.after

    def asset(self, repo, asset, limit):
        return self.asset_bytes

    def zotero_version(self):
        return "10.0.8"

    def request(self, path, method="GET", data=None):
        if method == "GET" and path == "/repos/list/repo/pulls/12":
            return copy.deepcopy(self.current or self.pr)
        if method == "GET":
            return self.get(path)
        self.writes.append((path, method, data))
        return {"sha": "written"}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.policy = copy.deepcopy(POLICY)

    def plan(self, previous=None, now=NOW, source=BASE + ITEM):
        return c.maintenance(self.api, source, previous or {}, self.policy, now)

    def test_active_project_unchanged_and_xpi_recorded(self):
        source, state, findings, unknown = self.plan()
        self.assertEqual(source, BASE + ITEM)
        self.assertTrue(state["owner/tool"]["xpi_seen"])
        self.assertFalse(findings or unknown)

    def test_old_commit_but_recent_release_stays(self):
        self.api.last_commit = "2020-01-01T00:00:00Z"
        self.assertFalse(self.plan()[2])

    def test_recent_commit_without_releases_stays(self):
        self.api.releases = []
        self.assertFalse(self.plan()[2])

    def test_stale_project_removed_at_threshold(self):
        self.api.last_commit = c.iso(NOW - timedelta(days=365))
        self.api.releases = []
        source, _, findings, _ = self.plan()
        self.assertEqual(source, BASE)
        self.assertIn("365 day", findings[0][2])

    def test_inactivity_exception_persists_but_archived_still_removed(self):
        self.policy["inactivity_exemptions"]["owner/tool"] = "Finished and useful"
        self.api.last_commit = "2020-01-01T00:00:00Z"
        self.api.releases = []
        self.assertFalse(self.plan()[2])
        self.api.meta["archived"] = True
        self.assertEqual(self.plan()[0], BASE)

    def test_missing_requires_three_spaced_checks(self):
        self.api.failure = c.Unavailable("missing", 404)
        state = {}
        for week in range(3):
            source, state, findings, _ = self.plan(state, NOW + timedelta(days=7 * week))
            self.assertEqual(bool(findings), week == 2)
        self.assertEqual(source, BASE)
        self.assertEqual(state["owner/tool"]["project_missing"]["count"], 3)

    def test_manual_runs_cannot_accelerate_missing(self):
        self.api.failure = c.Unavailable("missing", 410)
        _, state, _, _ = self.plan()
        _, state, findings, _ = self.plan(state, NOW + timedelta(hours=1))
        self.assertEqual(state["owner/tool"]["project_missing"]["count"], 1)
        self.assertFalse(findings)

    def test_transient_failure_breaks_missing_streak(self):
        previous = {"owner/tool": {"project_missing": {"count": 2, "checked_at": c.iso(NOW - timedelta(days=7))}}}
        for error in [403, 429, 500, None]:
            with self.subTest(error=error):
                self.api.failure = c.Unavailable("uncertain", error)
                source, state, findings, unknown = self.plan(previous)
                self.assertEqual(source, BASE + ITEM)
                self.assertEqual(state, {})
                self.assertFalse(findings)
                self.assertTrue(unknown)

    def test_commit_endpoint_404_does_not_mean_project_missing(self):
        self.api.commit_failure = c.Unavailable("branch missing", 404)
        _, state, findings, unknown = self.plan()
        self.assertEqual(state, {})
        self.assertFalse(findings)
        self.assertTrue(unknown)

    def test_download_disappearance_requires_previous_asset_and_three_checks(self):
        _, state, _, _ = self.plan()
        self.api.releases = []
        for week in range(1, 4):
            _, state, findings, _ = self.plan(state, NOW + timedelta(days=7 * week))
            self.assertEqual(bool(findings), week == 3)
        self.assertIn(".xpi", findings[0][2])

    def test_no_releases_does_not_invent_missing_download(self):
        self.api.releases = []
        _, state, findings, _ = self.plan()
        self.assertEqual(state, {})
        self.assertFalse(findings)

    def test_normalized_duplicate_keeps_first_entry(self):
        second = ITEM.replace("owner/tool)", "OWNER/TOOL/)")
        source, _, findings, _ = self.plan(source=BASE + ITEM + second)
        self.assertEqual(source, BASE + ITEM)
        self.assertEqual(findings[0][2], "Duplicate project URL")

    def test_repository_transfer_updates_link(self):
        self.api.meta["full_name"] = "new-owner/tool"
        source, _, findings, _ = self.plan()
        self.assertIn("https://github.com/new-owner/tool", source)
        self.assertEqual(findings[0][1], "Update URL")

    def test_external_links_are_checked_without_activity_rules(self):
        source = BASE + ITEM.replace("https://github.com/owner/tool", "https://www.zotero.org/tool")
        self.assertEqual(self.plan(source=source)[0], source)
        self.api.link_result = False
        state = {}
        for week in range(3):
            result, state, _, _ = self.plan(state, NOW + timedelta(days=7 * week), source)
        self.assertEqual(result, BASE)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def verdict(self, body=BODY, before=BASE, after=BASE + ITEM):
        return c.admission_verdict(self.api, before, after, body, POLICY, NOW)

    def test_mature_documented_released_project_passes(self):
        result = self.verdict()
        self.assertEqual(result, c.Verdict())

    def test_repository_age_uses_two_calendar_months(self):
        self.api.meta["created_at"] = "2026-09-06T00:00:01Z"
        self.assertTrue(any("two" in row or "2 calendar months" in row for row in self.verdict().rejected))
        self.api.meta["created_at"] = "2026-09-06T00:00:00Z"
        self.assertFalse(self.verdict().rejected)

    def test_month_end_and_leap_year_boundaries(self):
        self.assertEqual(c.months_ago(datetime(2024, 4, 30), 2), datetime(2024, 2, 29))
        self.assertEqual(c.months_ago(datetime(2026, 1, 31), 2), datetime(2025, 11, 30))

    def test_majority_disclosure_rejected_without_style_detection(self):
        result = self.verdict(BODY.replace("AI-generated code: some", "AI-generated code: majority"))
        self.assertTrue(any("most project code" in row for row in result.rejected))
        self.api.readme_text += "\nAI-generated code: majority\n"
        self.assertTrue(any("README explicitly" in row for row in self.verdict().rejected))

    def test_agents_files_or_ai_features_are_not_proof(self):
        self.api.readme_text += "Uses AI APIs and AGENTS.md; all features were reviewed."
        self.assertEqual(self.verdict(), c.Verdict())

    def test_readme_examples_and_comments_are_not_authorship_disclosures(self):
        self.api.readme_text += "\n<!--\nAI-generated code: majority\n-->\n```text\nAI-generated code: majority\n```\n"
        self.assertEqual(self.verdict(), c.Verdict())

    def test_copy_pasted_evidence_requests_correction(self):
        fields = c.evidence_fields(BODY)
        body = BODY.replace(fields["Distinct value"], WORDS)
        self.assertTrue(any("copy-pasted" in row for row in self.verdict(body).fixable))

    def test_unrelated_project_without_zotero_purpose_is_rejected(self):
        self.api.readme_text = "A generic calculator with installation and usage instructions. " * 20
        result = self.verdict(after=BASE + ITEM.replace("Zotero ", ""))
        self.assertTrue(any("Zotero-specific" in row for row in result.fixable))

    def test_unknown_authorship_does_not_block_otherwise_valid_evidence(self):
        result = self.verdict(BODY.replace("AI-generated code: some", "AI-generated code: unknown"))
        self.assertFalse(result.rejected)
        self.assertFalse(result.uncertain or result.fixable)

    def test_missing_readme_or_placeholder_documentation_requests_correction(self):
        for readme in [c.Unavailable("no readme", 404), "# Tool\nComing soon!", "<!--" + "x" * 300 + "-->"]:
            with self.subTest(readme=readme):
                self.api.readme_text = readme
                self.assertTrue(any("README" in row for row in self.verdict().fixable))

    def test_no_stable_plugin_download_rejected_but_integration_allowed(self):
        self.api.releases[0]["prerelease"] = True
        self.assertTrue(any("stable" in row for row in self.verdict().rejected))
        self.api.releases[0]["prerelease"] = False
        self.api.releases[0]["assets"] = []
        self.api.source_manifest = None
        self.assertFalse(self.verdict(BODY.replace("Project type: plugin", "Project type: integration"), after=BASE + ITEM.replace("plugin", "integration")).rejected)

    def test_draft_releases_do_not_satisfy_plugin_gate(self):
        self.api.releases[0]["draft"] = True
        self.assertTrue(any("stable" in row for row in self.verdict().rejected))

    def test_empty_xpi_is_not_usable_release(self):
        self.api.releases[0]["assets"][0]["size"] = 0
        self.assertTrue(any(".xpi" in row for row in self.verdict().rejected))

    def test_missing_or_placeholder_evidence_rejected(self):
        for body in ["", BODY.replace(WORDS, "TODO"), BODY + "Project URL: https://example.com\n"]:
            with self.subTest(body=body):
                self.assertTrue(self.verdict(body).fixable)

    def test_template_comment_does_not_satisfy_evidence(self):
        self.assertTrue(self.verdict("<!--\n" + BODY + "\n-->").fixable)

    def test_duplicate_added_to_existing_project_rejected(self):
        result = self.verdict(before=BASE + ITEM, after=BASE + ITEM + ITEM)
        self.assertTrue(any("Duplicate" in row for row in result.rejected))

    def test_existing_duplicate_does_not_reject_unrelated_change(self):
        result = self.verdict(before=BASE + ITEM + ITEM, after=BASE + ITEM + ITEM + "\n")
        self.assertEqual(result, c.Verdict())

    def test_multiple_projects_rejected(self):
        self.assertTrue(any("one new project" in row for row in self.verdict(after=BASE + ITEM + ITEM.replace("owner/tool", "other/tool")).rejected))

    def test_non_github_project_stays_open_for_override(self):
        result = self.verdict(BODY.replace("https://github.com/owner/tool", "https://example.com/tool"),
                              after=BASE + ITEM.replace("https://github.com/owner/tool", "https://example.com/tool"))
        self.assertFalse(result.rejected)
        self.assertTrue(result.uncertain)

    def test_api_failure_never_accuses_or_closes_valid_submission(self):
        self.api.failure = c.Unavailable("rate limited", 403)
        result = self.verdict()
        self.assertFalse(result.rejected)
        self.assertTrue(result.uncertain)

    def test_removal_or_description_change_needs_no_admission_evidence(self):
        self.assertEqual(self.verdict("", before=BASE + ITEM, after=BASE), c.Verdict())
        self.assertEqual(self.verdict("", before=BASE + ITEM, after=BASE + ITEM.replace("useful", "helpful")), c.Verdict())

    def test_malformed_new_entry_cannot_bypass_gate(self):
        for line in [ITEM.replace(") - A", ") A"), ITEM.replace("- [", "* ["), "  " + ITEM]:
            with self.subTest(line=line):
                result = self.verdict(after=BASE + line)
                self.assertTrue(any("Description" in row for row in result.fixable))

    def test_metadata_canonical_name_catches_transferred_duplicate(self):
        before = BASE + ITEM.replace("owner/tool", "new/tool")
        self.api.meta["full_name"] = "new/tool"
        result = self.verdict(before=before, after=before + ITEM)
        self.assertTrue(any("already present" in row for row in result.rejected))


class RemoteActionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def run_admission(self):
        with patch.object(c, "utcnow", return_value=NOW):
            c.admission(self.api, "list/repo", {"pull_request": {"number": 12}}, POLICY)

    def test_rejection_comments_before_closing(self):
        self.api.meta["created_at"] = c.iso(NOW)
        self.run_admission()
        comment = next(i for i, row in enumerate(self.api.writes) if row[0].endswith("/comments"))
        closure = next(i for i, row in enumerate(self.api.writes) if row[2] == {"state": "closed"})
        self.assertLess(comment, closure)
        self.assertIn("calendar months", self.api.writes[comment][2]["body"])

    def test_network_uncertainty_leaves_pr_open(self):
        self.api.failure = c.Unavailable("network unavailable")
        self.run_admission()
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        self.assertTrue(any(data.get("state") == "error" for _, _, data in self.api.writes))

    def test_contributor_edit_in_flight_prevents_closure(self):
        self.api.meta["created_at"] = c.iso(NOW)
        self.api.current = copy.deepcopy(self.api.pr)
        self.api.current["body"] += "\nNew evidence"
        self.run_admission()
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_override_label_bypasses_automatic_closure(self):
        self.api.pr["labels"] = [{"name": "curation:override"}]
        self.api.pr["body"] = ""
        self.run_admission()
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0][2]["state"], "success")

    def test_draft_is_left_alone(self):
        self.api.pr["draft"] = True
        self.run_admission()
        self.assertEqual(self.api.writes, [])

    def test_existing_bot_comment_updated_without_spam(self):
        self.api.pr["body"] = ""
        self.api.comments = [{"id": 44, "body": c.MARKER, "user": {"type": "Bot"}}]
        self.run_admission()
        self.assertTrue(any(path.endswith("/comments/44") and method == "PATCH" for path, method, _ in self.api.writes))
        self.assertFalse(any(path.endswith("/comments") and method == "POST" for path, method, _ in self.api.writes))

    def test_corrected_submission_replaces_old_rejection_comment(self):
        self.api.comments = [{"id": 44, "body": c.MARKER + " Old rejection", "user": {"type": "Bot"}}]
        self.run_admission()
        update = next(data for path, _, data in self.api.writes if path.endswith("/comments/44"))
        self.assertIn("satisfies", update["body"])
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_generated_readme_only_additions_request_correction(self):
        self.api.files = [{"filename": "README.md"}]
        self.run_admission()
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        self.assertTrue(any(data.get("state") == "pending" for _, _, data in self.api.writes))

    def test_infrastructure_changes_do_not_require_new_project_evidence(self):
        self.api.files = [{"filename": ".github/scripts/curation.py"}]
        self.api.pr["body"] = ""
        self.run_admission()
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_cannot_write_nonreserved_branch(self):
        with self.assertRaises(ValueError):
            c.branch_commit(self.api, "list/repo", "main", "base", {"README.md": "x"})
        self.assertEqual(self.api.writes, [])

    def test_human_commit_on_reserved_branch_is_not_overwritten(self):
        self.api.get = lambda path: ({"object": {"sha": "human"}} if "/git/ref/" in path else {"message": "Human change"})
        with self.assertRaises(c.Unavailable):
            c.branch_commit(self.api, "list/repo", c.CLEANUP_BRANCH, "base", {"README.md": "x"})
        self.assertEqual(self.api.writes, [])


class SecurityTests(unittest.TestCase):
    def test_github_token_cannot_be_redirected_off_host(self):
        handler = c.SafeRedirect({"api.github.com"})
        for url in ["https://evil.example/api", "http://api.github.com/api", "https://api.github.com@evil.example/"]:
            with self.subTest(url=url), self.assertRaises(c.Unavailable):
                handler.redirect_request(None, None, 302, "", {}, url)

    def test_private_urls_are_inconclusive(self):
        api = c.GitHub("secret")
        for url in ["http://127.0.0.1/", "https://127.0.0.1/"]:
            with self.subTest(url=url), patch.object(c, "public_fetch", side_effect=c.UnsafeURL("Private host")), self.assertRaises(c.Unavailable):
                api.link(url)

    def test_repository_identity_cannot_escape_api_path(self):
        for url in ["https://github.com/../issues", "https://github.com@evil.example/a/b", "https://evil.example/a/b"]:
            self.assertIsNone(c.github_repo(url))

    def test_comment_evidence_cannot_ping_arbitrary_users(self):
        self.assertNotIn("@", c.markdown("@all | <script>\nnext"))
        self.assertNotIn("<script>", c.markdown("<script>"))

    def test_large_api_pages_retry_at_a_smaller_size(self):
        api = c.GitHub("unused")
        paths = []

        def get(path):
            paths.append(path)
            if "per_page=100&" in path:
                raise c.Unavailable("API response too large to inspect safely")
            return list(range(10)) if path.endswith("&page=1") else [10]

        api.get = get
        self.assertEqual(api.pages("/releases"), list(range(11)))
        self.assertEqual(len(paths), 3)

    def test_truncated_pagination_is_never_reported_as_complete(self):
        api = c.GitHub("unused")
        api.get = lambda path: [None] * 100
        with self.assertRaises(c.Unavailable):
            api.pages("/releases")


class WeeklyAPI(FakeAPI):
    """In-memory GitHub boundary: records generated trees, branches, and PRs."""

    def __init__(self):
        super().__init__()
        self.source = BASE + ITEM
        self.state = None
        self.branches = {"main": "base"}
        self.commits = {"base": {"message": "Existing source", "tree": {"sha": "base-tree"}}}
        self.trees = {}
        self.open_pulls = []

    @property
    def write_count(self):
        return len(self.writes)

    def get(self, path):
        if path == "/repos/list/repo":
            return {"default_branch": "main"}
        if "/git/ref/heads/" in path:
            branch = path.split("/git/ref/heads/")[1]
            if branch not in self.branches:
                raise c.Unavailable("Missing ref", 404)
            return {"object": {"sha": self.branches[branch]}}
        if "/git/commits/" in path:
            return self.commits[path.rsplit("/", 1)[1]]
        return super().get(path)

    def text(self, repo, path, ref):
        if ref == c.STATE_BRANCH:
            if self.state is None:
                raise c.Unavailable("Missing state", 404)
            return json.dumps(self.state)
        self.assert_source_request(path, ref)
        return self.source

    def assert_source_request(self, path, ref):
        if (path, ref) != ("_README.md", self.branches["main"]):
            raise AssertionError(f"Unexpected source request: {path}, {ref}")

    def pages(self, path):
        if "/pulls?" in path:
            return copy.deepcopy(self.open_pulls)
        return super().pages(path)

    def request(self, path, method="GET", data=None):
        self.writes.append((path, method, copy.deepcopy(data)))
        if path.endswith("/git/trees"):
            sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.trees[sha] = data
            return {"sha": sha}
        if path.endswith("/git/commits"):
            sha = f"commit-{len(self.commits)}"
            self.commits[sha] = {"message": data["message"], "tree": {"sha": data["tree"]},
                                 "parents": [{"sha": parent} for parent in data["parents"]]}
            return {"sha": sha}
        if path.endswith("/git/refs"):
            branch = data["ref"].removeprefix("refs/heads/")
            self.branches[branch] = data["sha"]
            self.save_state(branch, data["sha"])
            return {}
        if "/git/refs/heads/" in path:
            branch = path.split("/git/refs/heads/")[1]
            self.branches[branch] = data["sha"]
            self.save_state(branch, data["sha"])
            for pr in self.open_pulls:
                if pr["head"]["ref"] == branch:
                    pr["head"]["sha"] = data["sha"]
            return {}
        if path.endswith("/pulls"):
            number = 50 + len(self.open_pulls)
            self.open_pulls.append(dict(data, number=number, state="open",
                                       user={"type": "Bot", "login": "github-actions[bot]"},
                                       head={"ref": data["head"], "sha": self.branches[data["head"]],
                                             "repo": {"full_name": "list/repo"}},
                                       base={"ref": data["base"], "sha": self.branches[data["base"]]}))
            return {"number": number}
        if "/pulls/" in path:
            next(pr for pr in self.open_pulls if pr["number"] == int(path.rsplit("/", 1)[1])).update(data)
            return {}
        if "/statuses/" in path:
            return {}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def save_state(self, branch, sha):
        if branch == c.STATE_BRANCH:
            tree = self.trees[self.commits[sha]["tree"]["sha"]]
            self.state = json.loads(next(row["content"] for row in tree["tree"] if row["path"] == c.STATE_FILE))


class WeeklyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.api = WeeklyAPI()

    def run_weekly(self, dry_run=False, refresh=False):
        self.api.writes = []
        with patch.object(c.subprocess, "check_output", wraps=c.subprocess.check_output) as command:
            # Let the actual trusted generator run; substitute only the checkout SHA.
            wrapped = command._mock_wraps
            command.side_effect = lambda args, **kwargs: self.api.branches["main"] + "\n" if args[:2] == ["git", "rev-parse"] else wrapped(args, **kwargs)
            with patch.object(c, "utcnow", return_value=NOW), patch("sys.stdout", new=io.StringIO()):
                c.weekly(self.api, "list/repo", POLICY, dry_run, refresh)

    def test_dry_run_has_no_remote_writes_even_when_state_and_cleanup_change(self):
        self.api.source += ITEM
        self.run_weekly(dry_run=True)
        self.assertEqual(self.api.writes, [])

    def test_clean_list_does_not_create_a_pr(self):
        self.api.releases = []
        self.run_weekly()
        self.assertEqual(self.api.writes, [])

    def test_cleanup_opens_ready_for_review_with_source_and_generated_readme(self):
        self.api.source += ITEM
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 1)
        self.assertFalse(self.api.open_pulls[0]["draft"])
        tree = self.api.trees[self.api.commits[self.api.open_pulls[0]["head"]["sha"]]["tree"]["sha"]]
        files = {row["path"]: row["content"] for row in tree["tree"]}
        self.assertEqual(files["_README.md"], BASE + ITEM)
        self.assertIn("github/last-commit/owner/tool", files["README.md"])
        self.assertEqual(set(files), {"_README.md", "README.md"})
        self.assertTrue(all(not data.get("force") for _, _, data in self.api.writes if isinstance(data, dict)))
        self.assertTrue(any(data.get("context") == "curation/admission" and data.get("state") == "success"
                            for _, _, data in self.api.writes if isinstance(data, dict)))

    def test_next_run_updates_existing_pr_without_duplicate_commits(self):
        self.api.source += ITEM
        self.run_weekly()
        first = self.api.open_pulls[0]["head"]["sha"]
        self.api.state = {"owner/tool": {"xpi_seen": True}}
        self.run_weekly()
        self.assertEqual(len(self.api.open_pulls), 1)
        self.assertEqual(self.api.open_pulls[0]["head"]["sha"], first)

    def test_recovered_project_closes_obsolete_proposal(self):
        self.api.last_commit = "2020-01-01T00:00:00Z"
        self.api.releases = []
        self.run_weekly()
        self.api.last_commit = "2026-10-01T00:00:00Z"
        self.run_weekly()
        self.assertEqual(self.api.open_pulls[0]["state"], "closed")
        self.assertIn("No cleanup changes remain", self.api.open_pulls[0]["body"])

    def test_current_default_tree_is_used_when_updating_existing_bot_branch(self):
        self.api.source += ITEM
        self.run_weekly()
        self.api.commits["base"]["tree"]["sha"] = "new-default-tree"
        self.run_weekly()
        tree = self.api.trees[self.api.commits[self.api.open_pulls[0]["head"]["sha"]]["tree"]["sha"]]
        self.assertEqual(tree["base_tree"], "new-default-tree")
        self.assertEqual(len(self.api.open_pulls), 1)

    def test_no_edits_to_unrecognized_cleanup_pr(self):
        self.api.open_pulls = [{"number": 50, "state": "open", "body": "Human work",
                               "head": {"ref": c.CLEANUP_BRANCH, "repo": {"full_name": "list/repo"}}}]
        self.api.releases = []
        with self.assertRaises(c.Unavailable):
            self.run_weekly()
        self.assertEqual(self.api.writes, [])


if __name__ == "__main__":
    unittest.main()

"""Regressions from the critical review; all GitHub mutations stay in memory."""

import copy
from datetime import timedelta
from http.client import IncompleteRead
import hashlib
import io
import json
import socket
import ssl
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
import zipfile

import test_curation as fixtures
from test_curation import BASE, BODY, ITEM, NOW, POLICY, FakeAPI, c
import curation_support as support


def archive_bytes(manifest=None, files=None):
    manifest = copy.deepcopy(manifest or FakeAPI().source_manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in (files if files is not None else {
            "manifest.json": json.dumps(manifest), "bootstrap.js": "function startup() {}"}).items():
            archive.writestr(name, value)
    return output.getvalue()


class AdmissionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def verdict(self, after=BASE + ITEM, body=BODY, before=BASE):
        return c.admission_verdict(self.api, before, after, body, POLICY, NOW)

    def test_unsupported_markdown_is_never_a_successful_empty_submission(self):
        for entry in ["- [Tool][tool]\n\n[tool]: https://github.com/owner/tool\n",
                      "- [Tool](<https://github.com/owner/tool>) - Description.\n",
                      "1. [Tool](https://github.com/owner/tool) - Description.\n",
                      '- <a href="https://github.com/owner/tool">Tool</a>\n',
                      "- **[Tool](https://github.com/owner/tool)** - Description.\n"]:
            with self.subTest(entry=entry):
                self.assertTrue(self.verdict(BASE + entry, "").fixable)

    def test_new_toc_anchor_is_not_a_project(self):
        self.assertEqual(self.verdict(BASE + "- [New section](#new-section)\n", ""), c.Verdict())

    def test_verified_url_move_does_not_need_admission_fields(self):
        self.api.meta["full_name"] = "new-owner/tool"
        result = self.verdict(BASE + ITEM.replace("owner/tool", "new-owner/tool"), "", BASE + ITEM)
        self.assertEqual(result, c.Verdict())

    def test_unrelated_replacement_does_need_admission_evidence(self):
        original = self.api.get

        def get(path):
            if path == "/repos/old-owner/tool":
                return {"full_name": "old-owner/tool"}
            return original(path)

        self.api.get = get
        result = self.verdict(BASE + ITEM, "", BASE + ITEM.replace("owner/tool", "old-owner/tool"))
        self.assertTrue(result.fixable)

    def test_unresolved_old_url_is_inconclusive_not_a_false_closure(self):
        self.api.failure = c.Unavailable("rate limited", 403)
        result = self.verdict(BASE + ITEM.replace("owner/tool", "new-owner/tool"), "", BASE + ITEM)
        self.assertTrue(result.uncertain)
        self.assertFalse(result.rejected or result.fixable)

    def test_plugin_type_is_detected_independently_of_declaration(self):
        self.api.releases[0]["assets"] = []
        result = self.verdict(body=BODY.replace("Project type: plugin", "Project type: integration"))
        self.assertTrue(any(".xpi" in reason for reason in result.rejected))

    def test_xpi_in_tested_release_is_inspected_when_recent_releases_have_none(self):
        self.api.source_manifest = None
        latest = dict(self.api.releases[0], tag_name="v2.0.0", assets=[])
        self.api.release_pages = lambda repo: iter([[latest]])
        self.api.asset_bytes = b"x"
        self.api.releases[0]["assets"][0]["size"] = 1
        self.api.asset = Mock(return_value=b"x")
        result = self.verdict(after=BASE + ITEM.replace("plugin", "integration"),
                              body=BODY.replace("Project type: plugin", "Project type: integration"))
        self.api.asset.assert_called_once()
        self.assertTrue(any("artifact verification failed" in row for row in result.rejected))

    def test_valid_xpi_in_older_tested_release_records_verified_digest(self):
        self.api.source_manifest = None
        latest = dict(self.api.releases[0], tag_name="v2.0.0", assets=[])
        self.api.release_pages = lambda repo: iter([[latest]])
        result = self.verdict(after=BASE + ITEM.replace("plugin", "integration"),
                              body=BODY.replace("Project type: plugin", "Project type: integration"))
        self.assertEqual(result, c.Verdict())
        self.assertTrue(any("SHA-256" in row for row in result.verified))

    def test_empty_xpi_in_tested_release_cannot_claim_non_plugin_exemption(self):
        self.api.source_manifest = None
        latest = dict(self.api.releases[0], tag_name="v2.0.0", assets=[])
        self.api.release_pages = lambda repo: iter([[latest]])
        self.api.releases[0]["assets"][0]["size"] = 0
        result = self.verdict(after=BASE + ITEM.replace("plugin", "integration"),
                              body=BODY.replace("Project type: plugin", "Project type: integration"))
        self.assertTrue(any("no nonempty .xpi" in row for row in result.rejected))

    def test_source_manifest_detects_plugin_even_without_description_hint(self):
        self.api.releases[0]["assets"] = []
        result = self.verdict(after=BASE + ITEM.replace("plugin", "integration"),
                              body=BODY.replace("Project type: plugin", "Project type: integration"))
        self.assertTrue(any(".xpi" in reason for reason in result.rejected))

    def test_one_byte_xpi_is_rejected(self):
        self.api.asset_bytes = b"x"
        self.api.releases[0]["assets"][0]["size"] = 1
        self.assertTrue(any("artifact verification failed" in row for row in self.verdict().rejected))

    def test_archive_identity_is_compared_with_source(self):
        manifest = copy.deepcopy(self.api.source_manifest)
        manifest["applications"]["zotero"]["id"] = "different@example.org"
        self.api.asset_bytes = archive_bytes(manifest)
        self.assertTrue(any("plugin ID" in row for row in self.verdict().rejected))

    def test_valid_artifact_records_hash_id_version_and_current_compatibility(self):
        result = self.verdict()
        self.assertEqual(result, c.Verdict())
        self.assertTrue(any("SHA-256" in row and "current Zotero 10.0.8" in row and "tool@example.org" in row for row in result.verified))

    def test_current_zotero_outside_manifest_is_rejected(self):
        manifest = copy.deepcopy(self.api.source_manifest)
        manifest["applications"]["zotero"]["strict_max_version"] = "9.*"
        self.api.asset_bytes = archive_bytes(manifest)
        self.assertTrue(any("compatibility with Zotero 10.0.8" in row for row in self.verdict().rejected))

    def test_unavailable_current_zotero_does_not_accuse_project(self):
        self.api.zotero_version = Mock(side_effect=c.Unavailable("Official site unavailable"))
        result = self.verdict()
        self.assertFalse(result.rejected)
        self.assertTrue(result.uncertain)

    def test_unavailable_download_is_inconclusive(self):
        self.api.asset = Mock(side_effect=c.Unavailable("Download timed out"))
        result = self.verdict()
        self.assertFalse(result.rejected)
        self.assertTrue(result.uncertain)

    def test_unknown_provenance_does_not_override_verified_quality(self):
        result = self.verdict(body=BODY.replace("AI-generated code: some", "AI-generated code: unknown"))
        self.assertEqual(result, c.Verdict())
        self.assertTrue(result.verified)

    def test_plain_explicit_ai_disclosure_is_not_ignored(self):
        self.api.readme_text += "\nAll of the source code in this project was generated by AI.\n"
        result = self.verdict(body=BODY.replace("AI-generated code: some", "AI-generated code: none"))
        self.assertTrue(any("README explicitly" in row for row in result.rejected))

    def test_negative_or_quoted_ai_statement_is_not_a_disclosure(self):
        for statement in ["Not all code was generated by AI.", "> All code was generated by AI.",
                          "```\nAll code was generated by AI.\n```", "~~~\nAll code was generated by AI.\n~~~",
                          "    All code was generated by AI."]:
            with self.subTest(statement=statement):
                self.api.readme_text = "Working Zotero documentation. " * 20 + "\n" + statement
                self.assertEqual(self.verdict(), c.Verdict())

    def test_short_specific_answers_do_not_fail_arbitrary_word_threshold(self):
        fields = c.evidence_fields(BODY)
        body = BODY.replace(fields["Data handling"], "Local processing; no uploads or deletion.")
        self.assertEqual(self.verdict(body=body), c.Verdict())

    def test_verification_must_name_the_tested_versions(self):
        result = self.verdict(body=BODY.replace("Checked Tool v1.0.0 in Zotero 10.0.8", "Checked the latest version"))
        self.assertTrue(any("Verification must" in row for row in result.fixable))

    def test_malformed_or_private_evidence_urls_request_correction(self):
        for url in ["https://[broken", "https://127.0.0.1/private", "https://localhost/docs"]:
            with self.subTest(url=url):
                result = self.verdict(body=BODY.replace("https://github.com/owner/tool/blob/main/README.md", url))
                self.assertTrue(any("well-formed public HTTPS" in row for row in result.fixable))

    def test_unsupported_version_bound_stays_inconclusive(self):
        manifest = copy.deepcopy(self.api.source_manifest)
        manifest["applications"]["zotero"]["strict_min_version"] = "10.0-beta.1"
        self.api.asset_bytes = archive_bytes(manifest)
        result = self.verdict()
        self.assertTrue(result.uncertain)
        self.assertFalse(result.rejected)

    def prepare_published_override(self, digest=None, version="1.0.0"):
        manifest = copy.deepcopy(self.api.source_manifest)
        settings = manifest["applications"]["zotero"]
        settings["strict_max_version"] = "9.*"
        settings["update_url"] = "https://docs.example.org/updates.json"
        self.api.asset_bytes = archive_bytes(manifest)
        digest = digest or "sha256:" + hashlib.sha256(self.api.asset_bytes).hexdigest()
        self.api.public_json = Mock(return_value={"addons": {settings["id"]: {"updates": [{
            "version": version, "update_hash": digest,
            "applications": {"zotero": {"strict_max_version": "10.*"}}}]}}})

    def test_published_compatibility_override_for_exact_artifact_passes(self):
        self.prepare_published_override()
        result = self.verdict()
        self.assertEqual(result, c.Verdict())
        self.assertTrue(any("published update manifest" in row for row in result.verified))

    def test_override_for_other_artifact_or_version_is_not_accepted(self):
        for digest, version in [("sha256:wrong", "1.0.0"), (None, "1.0.1")]:
            with self.subTest(digest=digest, version=version):
                self.prepare_published_override(digest, version)
                result = self.verdict()
                self.assertTrue(result.uncertain)
                self.assertFalse(result.rejected)


class CorrectionWindowTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def run_at(self, now=NOW):
        with patch.object(c, "utcnow", return_value=now):
            c.admission(self.api, "list/repo", {"pull_request": {"number": 12}}, POLICY)

    def remember_comment(self):
        body = next(data["body"] for path, _, data in reversed(self.api.writes)
                    if path.endswith("/comments") or path.endswith("/comments/44"))
        self.api.comments = [{"id": 44, "body": body, "user": {"type": "Bot", "login": "github-actions[bot]"}}]
        self.api.writes.clear()

    def test_missing_fields_get_a_window_then_close_after_deadline(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        self.remember_comment()
        self.run_at(NOW + timedelta(hours=47))
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        self.api.writes.clear()
        self.run_at(NOW + timedelta(hours=49))
        self.assertTrue(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_body_and_head_edits_do_not_restart_the_clock(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.remember_comment()
        self.api.pr["body"] = "Still unfinished"
        self.api.pr["head"]["sha"] = "new-head"
        self.run_at(NOW + timedelta(hours=49))
        self.assertTrue(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_fixed_submission_passes_and_clears_deadline(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.remember_comment()
        self.api.pr["body"] = BODY
        self.run_at(NOW + timedelta(hours=12))
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        updated = next(data["body"] for path, _, data in self.api.writes if path.endswith("/comments/44"))
        self.assertNotIn("correction-deadline", updated)

    def test_inconclusive_file_scan_preserves_original_deadline_until_recovery(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.remember_comment()
        pages = self.api.pages

        def unavailable(path):
            if path.endswith("/files"):
                raise c.Unavailable("Temporary file-list outage", 503)
            return pages(path)

        self.api.pages = unavailable
        self.run_at(NOW + timedelta(hours=24))
        self.remember_comment()
        self.assertIn(c.iso(NOW + timedelta(hours=48)), self.api.comments[0]["body"])
        self.api.pages = pages
        self.run_at(NOW + timedelta(hours=49))
        self.assertTrue(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        body = next(data["body"] for path, _, data in self.api.writes if path.endswith("/comments/44"))
        self.assertNotIn(c.iso(NOW + timedelta(hours=97)), body)

    def test_corrected_submission_is_not_closed_when_recovery_is_after_deadline(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.remember_comment()
        self.api.pr["body"] = BODY
        pages = self.api.pages

        def unavailable(path):
            if path.endswith("/files"):
                raise c.Unavailable("Temporary file-list outage", 503)
            return pages(path)

        self.api.pages = unavailable
        self.run_at(NOW + timedelta(hours=49))
        self.remember_comment()
        self.assertIn("correction-deadline", self.api.comments[0]["body"])
        self.api.pages = pages
        self.run_at(NOW + timedelta(hours=50))
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))
        body = next(data["body"] for path, _, data in self.api.writes if path.endswith("/comments/44"))
        self.assertNotIn("correction-deadline", body)

    def test_explicit_admission_override_clears_ordinary_submission_deadline(self):
        self.api.pr["body"] = ""
        self.run_at()
        self.remember_comment()
        self.api.pr["labels"] = [{"name": POLICY["override_label"]}]
        self.run_at(NOW + timedelta(hours=49))
        body = next(data["body"] for path, _, data in self.api.writes if path.endswith("/comments/44"))
        self.assertNotIn("correction-deadline", body)
        self.assertFalse(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_generated_readme_and_generator_touch_do_not_bypass_gate(self):
        self.api.files = [{"filename": "README.md"}, {"filename": "generate_readme.py"}]
        self.run_at()
        self.remember_comment()
        self.run_at(NOW + timedelta(hours=49))
        self.assertTrue(any(data == {"state": "closed"} for _, _, data in self.api.writes))

    def test_touching_source_does_not_hide_an_unlisted_rendered_entry(self):
        self.api.files = [{"filename": "README.md"}, {"filename": "_README.md"}]
        original = self.api.text
        self.api.text = lambda repo, path, ref: BASE if path == "_README.md" else original(repo, path, ref)
        self.run_at()
        body = next(data["body"] for path, _, data in self.api.writes if path.endswith("/comments"))
        self.assertIn("entry absent from its source", body)

    def test_young_repository_is_closed_immediately_without_archive_download(self):
        self.api.meta["created_at"] = c.iso(NOW)
        self.api.asset = Mock(side_effect=AssertionError("Should not download rejected projects"))
        self.run_at()
        self.assertTrue(any(data == {"state": "closed"} for _, _, data in self.api.writes))


class ArtifactSafetyTests(unittest.TestCase):
    def test_missing_bootstrap_invalid_json_traversal_and_duplicate_manifests(self):
        for files in [{"manifest.json": "{}"}, {"manifest.json": "bad", "bootstrap.js": "x"},
                      {"manifest.json": "{}", "bootstrap.js": "x", "../escape": "bad"}]:
            with self.subTest(files=files), self.assertRaises(support.InvalidArtifact):
                support.inspect_xpi(archive_bytes(files=files), "10.0.8", "10.0.8", 26214400)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("./manifest.json", "{}")
            archive.writestr("bootstrap.js", "x")
        with self.assertRaises(support.InvalidArtifact):
            support.inspect_xpi(output.getvalue(), "10.0.8", "10.0.8", 26214400)

    def test_compressed_large_manifest_is_rejected_before_decompression(self):
        content = archive_bytes(files={"manifest.json": "x" * 200000, "bootstrap.js": "x"})
        with self.assertRaises(support.InvalidArtifact):
            support.inspect_xpi(content, "10.0.8", "10.0.8", 26214400)

    def test_numerical_compatibility_comparison(self):
        self.assertTrue(support.compatible("10.0.8", "6.999", "10.*"))
        self.assertFalse(support.compatible("10.0.8", "6.999", "9.*"))
        self.assertFalse(support.compatible("10.0.8", "10.0.9", "10.*"))


class URLAndBudgetTests(unittest.TestCase):
    def test_slash_ref_and_percent_encoded_document_are_resolved_once(self):
        api = c.GitHub("unused")
        expected = "/repos/owner/tool/contents/My%20Guide.md?ref=resolved"
        calls = []

        def get(path):
            calls.append(path)
            if path == "/repos/owner/tool":
                return {"full_name": "owner/tool"}
            if path == "/repos/owner/tool/commits/docs%2Fmain":
                return {"sha": "resolved"}
            if path == expected:
                return {"type": "file"}
            raise c.Unavailable("Missing", 404)

        api.get = get
        self.assertTrue(api.link("https://github.com/owner/tool/blob/docs/main/My%20Guide.md"))
        self.assertIn(expected, calls)

    def test_nonexistent_ref_is_inconclusive_not_a_missing_document(self):
        api = c.GitHub("unused")
        api.get = lambda path: {"full_name": "owner/tool"} if path == "/repos/owner/tool" else (_ for _ in ()).throw(c.Unavailable("No ref", 404))
        with self.assertRaises(c.Unavailable) as result:
            api.link("https://github.com/owner/tool/blob/missing/README.md")
        self.assertIsNone(result.exception.status)

    def test_read_and_project_budgets_reserve_reporting_capacity(self):
        api = c.GitHub("unused", read_limit=3, project_limit=1)
        with api.project_budget():
            api.read_budget()
            with self.assertRaises(c.Unavailable):
                api.read_budget()
        api.read_budget()
        api.read_budget()
        with self.assertRaises(c.Unavailable):
            api.read_budget()
        api.reporting = True
        api.read_budget()
        self.assertEqual(api.read_count, 4)

    def test_tiny_pages_are_bounded_without_two_thousand_requests(self):
        api = c.GitHub("unused")
        api.get = Mock(return_value=[{"id": 1}])
        with self.assertRaises(c.Unavailable):
            api.pages("/releases", per_page=1)
        self.assertEqual(api.get.call_count, 12)

    def test_slow_inspection_stops_and_preserves_reporting(self):
        with patch.object(c.time, "monotonic", return_value=0):
            api = c.GitHub("unused")
            with api.project_budget():
                with patch.object(c.time, "monotonic", return_value=91), self.assertRaises(c.Unavailable):
                    api.read_budget()
        with patch.object(c.time, "monotonic", return_value=481), self.assertRaises(c.Unavailable):
            api.read_budget()
        api.reporting = True
        with patch.object(c.time, "monotonic", return_value=481):
            api.read_budget()
        self.assertEqual(api.read_count, 1)

    def test_asset_host_allowlist_is_checked_before_a_connection(self):
        with patch.object(support, "build_opener") as opener:
            with self.assertRaises(support.UnsafeURL):
                support.public_fetch("https://evil.example/asset", 100,
                                     allowed_hosts={"release-assets.githubusercontent.com"})
            opener.return_value.open.assert_not_called()

    def test_recent_activity_stops_release_history_scanning(self):
        api = FakeAPI()

        def pages(repo):
            yield api.releases
            raise AssertionError("Should stop after the first sufficient page")

        api.release_pages = pages
        last, _, _ = c.activity(api, "owner/tool", api.meta, NOW, need_download=True)
        self.assertEqual(last.year, 2026)

    def test_private_ipv4_ipv6_or_mixed_dns_is_rejected_before_connection(self):
        for addresses in [["127.0.0.1"], ["::1"], ["93.184.216.34", "169.254.169.254"]]:
            rows = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
            with self.subTest(addresses=addresses), patch.object(support.socket, "getaddrinfo", return_value=rows), patch.object(support.socket, "socket") as connect:
                with self.assertRaises(support.UnsafeURL):
                    support.PublicHTTPSConnection("docs.example", timeout=2).connect()
                connect.assert_not_called()

    def test_public_connection_uses_pinned_ip_and_original_tls_hostname(self):
        endpoint = ("93.184.216.34", 443)
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", endpoint)]
        connection = support.PublicHTTPSConnection("docs.example", timeout=2, context=ssl.create_default_context())
        connection._context = Mock()
        with patch.object(support.socket, "getaddrinfo", return_value=rows), patch.object(support.socket, "socket") as socket_factory:
            connection.connect()
            socket_factory.return_value.connect.assert_called_once_with(endpoint)
            connection._context.wrap_socket.assert_called_once_with(socket_factory.return_value, server_hostname="docs.example")

    def test_custom_public_docs_are_checked_without_github_credentials(self):
        api = c.GitHub("secret")
        with patch.object(c, "public_fetch", return_value=b"") as fetch:
            self.assertTrue(api.link("https://docs.example.org/install"))
            self.assertEqual(fetch.call_args.args[0], "https://docs.example.org/install")
            self.assertNotIn("secret", repr(fetch.call_args))

    def test_asset_redirect_cannot_forward_the_github_token(self):
        api = c.GitHub("secret")
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.github.com/x", 302, "Redirect", {"Location": "https://release-assets.githubusercontent.com/asset"}, None)
        with patch.object(c, "build_opener", return_value=opener), patch.object(c, "public_fetch", return_value=b"zip") as fetch:
            self.assertEqual(api.asset("owner/tool", {"id": 1, "size": 3}, 100), b"zip")
            self.assertNotIn("secret", repr(fetch.call_args))
            self.assertIn("release-assets.githubusercontent.com", fetch.call_args.kwargs["allowed_hosts"])

    def test_truncated_asset_response_is_inconclusive(self):
        api = c.GitHub("secret")
        response = MagicMock()
        response.read.side_effect = IncompleteRead(b"partial")
        opener = Mock()
        opener.open.return_value = response
        with patch.object(c, "build_opener", return_value=opener), self.assertRaises(c.Unavailable):
            api.asset("owner/tool", {"id": 1, "size": 50}, 100)


class CleanupRecheckTests(unittest.TestCase):
    def test_inconclusive_scan_preserves_proposal_until_a_conclusive_recovery(self):
        case = fixtures.WeeklyIntegrationTests()
        case.setUp()
        case.api.last_commit = "2020-01-01T00:00:00Z"
        case.api.releases = []
        case.run_weekly()
        sha = case.api.branches[c.CLEANUP_BRANCH]
        case.api.failure = c.Unavailable("rate limit", 403)
        case.run_weekly()
        self.assertNotEqual(case.api.open_pulls[0].get("state"), "closed")
        self.assertEqual(case.api.branches[c.CLEANUP_BRANCH], sha)
        self.assertTrue(any(data.get("context") == "curation/admission" and data.get("state") == "pending" for _, _, data in case.api.writes))
        self.assertIn("Awaiting recheck", case.api.open_pulls[0]["body"])
        case.api.failure = None
        case.api.last_commit = "2026-10-01T00:00:00Z"
        case.run_weekly()
        self.assertEqual(case.api.open_pulls[0]["state"], "closed")


class DailyRecheckTests(unittest.TestCase):
    def test_oldest_attempts_first_and_drafts_or_passed_checks_excluded(self):
        api = FakeAPI()
        pulls = [{"number": n, "draft": n == 4, "head": {"sha": str(n)}} for n in range(1, 6)]
        api.pages = Mock(return_value=pulls)
        statuses = {"1": {"state": "pending", "created_at": "2026-11-05"},
                    "2": {"state": "error", "created_at": "2026-11-01"},
                    "3": {"state": "success", "created_at": "2026-11-01"},
                    "4": {"state": "pending", "created_at": "2026-10-01"},
                    "5": {"state": "failure", "created_at": "2026-11-02"}}
        api.get = lambda path: {"statuses": [dict(statuses[path.split("/")[-2]], context="curation/admission")]}
        policy = dict(POLICY, maximum_daily_rechecks=2)
        self.assertEqual(c.pending_admission(api, "list/repo", policy), [2, 5])
        self.assertFalse(api.writes)

    def test_failed_closure_is_retried_while_closed_prs_are_not_reopened(self):
        api = FakeAPI()
        api.pages = Mock(return_value=[{"number": 12, "draft": False, "head": {"sha": "head"}}])
        api.get = Mock(return_value={"statuses": [{"context": "curation/admission", "state": "failure", "created_at": "2026-11-01"}]})
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [12])
        api.pages.return_value = []
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [])
        self.assertFalse(api.writes)

    def test_missing_status_gets_a_first_inspection(self):
        api = FakeAPI()
        api.pages = Mock(return_value=[{"number": 12, "draft": False, "head": {"sha": "head"}}])
        api.get = Mock(return_value={"statuses": []})
        self.assertEqual(c.pending_admission(api, "list/repo", POLICY), [12])


if __name__ == "__main__":
    unittest.main()

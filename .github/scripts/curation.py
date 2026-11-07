#!/usr/bin/env python3
"""Deterministic curation; stdlib only. Untrusted projects are never executed."""

import base64
import calendar
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import html
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from curation_support import (CompatibilityMismatch, InvalidArtifact, NoRedirect, UnsupportedCompatibility,
                              UnsafeURL, inspect_xpi, public_fetch, public_url)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "_README.md"
STATE_BRANCH = "automation/curation-state"
CLEANUP_BRANCH = "automation/weekly-curation"
ENTRY_BRANCH_PREFIX = "automation/curation-entry-"
STATE_FILE = ".github/curation-state.json"
MARKER = "<!-- awesome-zotero:curation -->"
COMMIT_PREFIX = "automation: curation"
ENTRY = re.compile(r"^- \[([^\]]+)\]\((https?://[^\s)]+)\)\s+-\s+(.+)$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_BYTES = 2_000_000
MAX_WRITES = 120


def utcnow():
    return datetime.now(timezone.utc)


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def months_ago(now, months):
    year, month = divmod(now.year * 12 + now.month - 1 - months, 12)
    month += 1
    return now.replace(year=year, month=month,
                       day=min(now.day, calendar.monthrange(year, month)[1]))


def markdown(value):
    return html.escape(str(value)).replace("@", "&#64;").replace("|", "&#124;").replace("\n", " ")


def github_repo(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    name = "/".join(parts[:2]).removesuffix(".git")
    return name if REPO.fullmatch(name) and all(p not in {".", ".."} for p in name.split("/")) else None


def key(url):
    repo = github_repo(url)
    return repo.lower() if repo else url.rstrip("/")


@dataclass
class Item:
    index: int
    name: str
    url: str
    description: str


def entries(text):
    return [Item(i, *match.groups()) for i, line in enumerate(text.splitlines())
            if (match := ENTRY.fullmatch(line))]


class Unavailable(Exception):
    """Missing or inconclusive external evidence, not a policy violation."""

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


class CleanupMismatch(Unavailable):
    """A cleanup head contains changes beyond its trusted saved proposal."""


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc.lower() not in self.hosts:
            raise Unavailable("Redirect outside the permitted HTTPS hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token, read_limit=600, project_limit=24):
        self.token = token
        self.cache = {}
        self.read_limit, self.project_limit = read_limit, project_limit
        self.read_count, self.write_count, self.project_start = 0, 0, None
        self.reporting = False
        self.started, self.project_started = time.monotonic(), None

    def read_budget(self):
        if not self.reporting and (time.monotonic() - self.started > 480 or
                                  self.project_started is not None and time.monotonic() - self.project_started > 90):
            raise Unavailable("Inspection time budget exhausted; awaiting recheck")
        if self.read_count >= self.read_limit + (100 if self.reporting else 0):
            raise Unavailable("Read request budget exhausted; writes are reserved for reporting")
        if not self.reporting and self.project_start is not None and self.read_count - self.project_start >= self.project_limit:
            raise Unavailable("Per-project request budget exhausted; awaiting recheck")
        self.read_count += 1

    @contextmanager
    def project_budget(self):
        previous, previous_time = self.project_start, self.project_started
        self.project_start = self.read_count
        self.project_started = time.monotonic()
        try:
            yield
        finally:
            self.project_start = previous
            self.project_started = previous_time

    def request(self, path, method="GET", data=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a GitHub API path")
        if method == "GET":
            self.read_budget()
        else:
            if self.write_count >= MAX_WRITES:
                raise Unavailable("Write request budget exhausted")
            self.write_count += 1
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "awesome-zotero-curation"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = None if data is None else json.dumps(data).encode()
        request = Request("https://api.github.com" + path, data=payload, headers=headers, method=method)
        try:
            with build_opener(SafeRedirect({"api.github.com"})).open(request, timeout=25) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise Unavailable("API response too large to inspect safely")
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raise Unavailable(f"GitHub API returned HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError, HTTPException, json.JSONDecodeError) as exc:
            raise Unavailable("GitHub API temporarily unavailable") from exc

    def get(self, path):
        if path not in self.cache:
            self.cache[path] = self.request(path)
        return self.cache[path]

    def iter_pages(self, path, per_page=100, max_pages=12):
        page, attempts = 1, 0
        while attempts < max_pages:
            attempts += 1
            try:
                rows = self.get(path + ("&" if "?" in path else "?") + urlencode({"per_page": per_page, "page": page}))
            except Unavailable as exc:
                if str(exc) == "API response too large to inspect safely" and per_page > 1:
                    # Restart at the smaller size; callers must tolerate repeated rows.
                    per_page, page = max(1, per_page // 10), 1
                    continue
                raise
            yield rows
            if len(rows) < per_page:
                return
            page += 1
        raise Unavailable("Pagination limit reached; evidence is incomplete")

    def pages(self, path, per_page=100):
        result, seen = [], set()
        for rows in self.iter_pages(path, per_page):
            for row in rows:
                identity = json.dumps(row, sort_keys=True)
                if identity not in seen:
                    seen.add(identity)
                    result.append(row)
        return result

    def release_pages(self, repo):
        return self.iter_pages(f"/repos/{repo}/releases", per_page=10, max_pages=6)

    def asset(self, repo, asset, limit):
        if asset.get("size", 0) > limit:
            raise Unavailable("Release asset exceeds the configured download limit")
        identifier = asset.get("id")
        if not isinstance(identifier, int):
            raise Unavailable("Release asset lacks an inspectable GitHub asset ID")
        self.read_budget()
        headers = {"Accept": "application/octet-stream", "User-Agent": "awesome-zotero-curation"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"https://api.github.com/repos/{repo}/releases/assets/{identifier}", headers=headers)
        try:
            try:
                response = build_opener(NoRedirect()).open(request, timeout=25)
            except HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308} or not exc.headers.get("Location"):
                    raise
                location = exc.headers["Location"]
                exc.close()
                return public_fetch(location, limit, self.read_budget, allowed_hosts={
                    "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                    "github-releases.githubusercontent.com"})
            with response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise Unavailable("Release asset exceeds the configured download limit")
                return raw
        except (HTTPError, URLError, OSError, UnsafeURL, HTTPException) as exc:
            raise Unavailable("Release asset could not be downloaded safely") from exc

    def zotero_version(self):
        if "zotero_version" not in self.cache:
            try:
                raw = public_fetch("https://www.zotero.org/support/changelog", MAX_BYTES, self.read_budget)
                match = re.search(r"Changes in\s+(\d+\.\d+(?:\.\d+){0,2})", raw.decode("utf-8"))
                if not match:
                    raise Unavailable("Official Zotero release version could not be identified")
                self.cache["zotero_version"] = match[1]
            except (HTTPError, URLError, OSError, UnicodeError, UnsafeURL, HTTPException) as exc:
                raise Unavailable("Official Zotero version temporarily unavailable") from exc
        return self.cache["zotero_version"]

    def public_json(self, url):
        try:
            return json.loads(public_fetch(url, MAX_BYTES, self.read_budget))
        except (HTTPError, URLError, OSError, UnsafeURL, HTTPException, ValueError) as exc:
            raise Unavailable("Published update manifest could not be read safely") from exc

    def text(self, repo, path, ref):
        data = self.get(f"/repos/{repo}/contents/{quote(path, safe='/')}?{urlencode({'ref': ref})}")
        if not isinstance(data, dict) or data.get("encoding") != "base64" or data.get("size", 0) > MAX_BYTES:
            raise Unavailable("File is not a supported UTF-8 text document")
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise Unavailable("File is not a supported UTF-8 text document") from exc

    def readme(self, repo, ref):
        data = self.get(f"/repos/{repo}/readme?{urlencode({'ref': ref})}")
        if data.get("encoding") != "base64":
            raise Unavailable("README is not inspectable")
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise Unavailable("README is not UTF-8") from exc

    def link(self, url):
        """True/False for accessible/missing, Unavailable for uncertain results."""
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise Unavailable("Malformed HTTPS URL") from exc
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise Unavailable("Only public HTTPS evidence links are supported")
        if parsed.netloc.lower() == "github.com":
            repo = github_repo(url)
            if not repo:
                raise Unavailable("Expected a GitHub project link")
            self.get(f"/repos/{repo}")
            parts = unquote(parsed.path).strip("/").split("/")
            # Use fixed API endpoints, never fetch contributor-supplied download URLs.
            if len(parts) == 2:
                return True
            if parts[2] in {"blob", "tree"} and len(parts) >= 5:
                tail = parts[3:]
                if len(tail) > 9:
                    raise Unavailable("Documentation path is too ambiguous to resolve automatically")
                for split in range(len(tail) - 1, 0, -1):
                    ref, path = "/".join(tail[:split]), "/".join(tail[split:])
                    try:
                        commit = self.get(f"/repos/{repo}/commits/{quote(ref, safe='')}")
                    except Unavailable as exc:
                        if exc.status == 404:
                            continue
                        raise
                    try:
                        self.get(f"/repos/{repo}/contents/{quote(path, safe='/')}?"
                                 + urlencode({"ref": commit["sha"]}))
                        return True
                    except Unavailable as exc:
                        if exc.status == 404:
                            return False
                        raise
                raise Unavailable("Documentation ref could not be resolved; this is not proof the document is missing")
            if parts[2] == "releases":
                suffix = parts[3:]
                if not suffix:
                    return True
                if suffix == ["latest"]:
                    self.get(f"/repos/{repo}/releases/latest")
                    return True
                if len(suffix) >= 2 and suffix[0] in {"tag", "download"}:
                    tag = "/".join(suffix[1:-1] if suffix[0] == "download" else suffix[1:])
                    release = self.get(f"/repos/{repo}/releases/tags/{quote(tag, safe='')}")
                    if suffix[0] == "download":
                        return any(a["name"] == suffix[-1] for a in release.get("assets", []))
                    return True
            raise Unavailable("This GitHub evidence URL needs maintainer review")
        try:
            public_fetch(url, 0, self.read_budget, probe=True)
            return True
        except HTTPError as exc:
            if exc.code in {404, 410}:
                return False
            raise Unavailable(f"Link check returned HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError, UnsafeURL, HTTPException) as exc:
            raise Unavailable("Link check temporarily unavailable") from exc


def activity(api, repo, meta, now=None, need_download=False, inactive_days=365):
    rows = api.get(f"/repos/{repo}/commits?" + urlencode({"sha": meta["default_branch"], "per_page": 1}))
    commits = [date(rows[0]["commit"]["committer"]["date"])] if rows else []
    dates, releases = commits[:], []
    pages = api.release_pages(repo) if hasattr(api, "release_pages") else [api.pages(f"/repos/{repo}/releases")]
    for page in pages:
        published = [r for r in page if not r.get("draft") and r.get("published_at")]
        releases.extend(published)
        dates.extend(date(r["published_at"]) for r in published)
        active = dates and now is not None and now - max(dates) < timedelta(days=inactive_days)
        has_download = any(a.get("name", "").lower().endswith(".xpi") and a.get("size", 0) > 0
                           for release in releases for a in release.get("assets", []))
        if active and (not need_download or has_download):
            break
    if not dates:
        raise Unavailable("No inspectable commit or release history")
    return max(dates), rows, releases


def missing_count(previous, now, policy):
    if previous and now - date(previous["checked_at"]) < timedelta(days=policy["missing_check_interval_days"]):
        return previous
    return {"count": previous.get("count", 0) + 1,
            "checked_at": iso(now)} if previous else {"count": 1, "checked_at": iso(now)}


def maintenance(api, source, previous, policy, now):
    """Plan only; no remote writes. Missing observations are persisted separately."""
    lines = source.splitlines(keepends=True)
    seen, observations, findings, unknown = set(), {}, [], []
    for item in entries(source):
        identity = key(item.url)
        reason, replacement = None, None
        if identity in seen:
            reason = "Duplicate project URL"
        else:
            seen.add(identity)
            saved = previous.get(identity, {})
            observation = {"xpi_seen": True} if saved.get("xpi_seen") else {}
            project_found = False
            uncertain = False
            try:
                with api.project_budget() if hasattr(api, "project_budget") else nullcontext():
                    repo = github_repo(item.url)
                    if repo:
                        meta = api.get(f"/repos/{repo}")
                        project_found = True
                        if meta.get("archived") or meta.get("disabled"):
                            reason = "Repository archived or disabled"
                        else:
                            last, _, releases = activity(api, repo, meta, now, bool(saved.get("xpi_seen")), policy["inactive_days"])
                            if any(a.get("name", "").lower().endswith(".xpi") and a.get("size", 0) > 0
                                   for r in releases for a in r.get("assets", [])):
                                observation["xpi_seen"] = True
                            elif saved.get("xpi_seen"):
                                counter = missing_count(saved.get("download_missing"), now, policy)
                                observation["download_missing"] = counter
                                if counter["count"] >= policy["missing_checks"]:
                                    reason = f"Previously available .xpi downloads missing across {counter['count']} spaced checks"
                            if now - last >= timedelta(days=policy["inactive_days"]) and identity not in policy["inactivity_exemptions"]:
                                reason = f"No default-branch commits or published releases since {last.date()} ({policy['inactive_days']} day policy)"
                        if not reason and meta["full_name"].lower() != repo.lower():
                            replacement = "https://github.com/" + meta["full_name"]
                    elif not api.link(item.url):
                        raise Unavailable("Project link missing", 404)
            except Unavailable as exc:
                if exc.status in {404, 410} and not project_found:
                    counter = missing_count(saved.get("project_missing"), now, policy)
                    observation["project_missing"] = counter
                    if counter["count"] >= policy["missing_checks"]:
                        reason = f"Project missing across {counter['count']} spaced checks"
                else:
                    # An uncertain run breaks the missing streak rather than deleting an entry.
                    observation = {"xpi_seen": True} if saved.get("xpi_seen") else {}
                    unknown.append(f"{identity}: {exc}")
                    uncertain = True
            if reason or replacement:
                observation["confirmed"] = {"reason": reason, "replacement": replacement}
            elif saved.get("confirmed") and (uncertain or observation.get("project_missing")):
                confirmed = saved["confirmed"]
                # A deliberate policy exception can withdraw an inactivity finding.
                exempt = identity in policy["inactivity_exemptions"] and (confirmed.get("reason") or "").startswith("No default-branch")
                if not exempt:
                    observation["confirmed"] = confirmed
                    reason = "Awaiting recheck: " + confirmed["reason"] if confirmed.get("reason") else None
                    replacement = confirmed.get("replacement")
                    unknown.append(f"{identity}: retaining the previous confirmed proposal until a conclusive recheck")
            if observation:
                observations[identity] = observation
        if reason:
            lines[item.index] = ""
            findings.append((item, "Remove", reason))
        elif replacement:
            lines[item.index] = lines[item.index].replace(item.url, replacement, 1)
            findings.append((item, "Update URL", replacement))
    return "".join(lines), observations, findings, unknown


def render_readme(source):
    # The generator comes from the trusted checkout, never the PR head.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / SOURCE
        path.write_text(source)
        return subprocess.check_output([sys.executable, str(ROOT / "generate_readme.py"), str(path)], text=True)


def branch_commit(api, repo, branch, base_sha, files, verify_existing=None):
    """Write only reserved automation branches, atomically, without force pushes."""
    if branch not in {STATE_BRANCH, CLEANUP_BRANCH} and not is_entry_branch(branch):
        raise ValueError("Not an automation branch")
    try:
        old = api.get(f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
        parent = api.get(f"/repos/{repo}/git/commits/{old}")
        if not parent["message"].startswith(COMMIT_PREFIX):
            if verify_existing is None:
                raise CleanupMismatch(f"Reserved branch {branch} contains a non-automation commit; refusing to overwrite")
            verify_existing(old, parent)
    except Unavailable as exc:
        if exc.status != 404:
            raise
        old = None
    base = api.get(f"/repos/{repo}/git/commits/{base_sha}")
    tree = api.request(f"/repos/{repo}/git/trees", "POST", {
        "base_tree": base["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                 for path, content in files.items()]})
    if (old and tree["sha"] == parent["tree"]["sha"]
            and base_sha in {row["sha"] for row in parent.get("parents", [])}):
        return old
    commit = api.request(f"/repos/{repo}/git/commits", "POST", {
        "message": f"{COMMIT_PREFIX} ({branch.rsplit('/', 1)[-1]})",
        "tree": tree["sha"], "parents": list(dict.fromkeys([sha for sha in [base_sha, old] if sha]))})
    if old:
        api.request(f"/repos/{repo}/git/refs/heads/{branch}", "PATCH", {"sha": commit["sha"], "force": False})
    else:
        api.request(f"/repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    return commit["sha"]


def status(api, repo, sha, context, state, description):
    api.request(f"/repos/{repo}/statuses/{sha}", "POST", {
        "state": state, "context": context, "description": description[:140],
        "target_url": f"https://github.com/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"})


def is_entry_branch(branch):
    return bool(re.fullmatch(re.escape(ENTRY_BRANCH_PREFIX) + r"[0-9a-f]{16}", branch or ""))


def entry_record(item, occurrence):
    return dict(name=item.name, url=item.url, description=item.description, occurrence=occurrence)


def entry_branch(record):
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]
    return ENTRY_BRANCH_PREFIX + digest


def cleanup_plans(source, findings, unknown):
    records, counts = {}, Counter()
    for item in entries(source):
        signature = (item.name, item.url, item.description)
        records[item.index] = entry_record(item, counts[signature])
        counts[signature] += 1
    return {entry_branch(records[item.index]): {
        "item": records[item.index], "action": action, "reason": reason,
        "pending": any(row.startswith(key(item.url) + ": ") for row in unknown)
    } for item, action, reason in findings}


def planned_item(source, record):
    matches = [item for item in entries(source)
               if (item.name, item.url, item.description) ==
               (record["name"], record["url"], record["description"])]
    return matches[record["occurrence"]] if len(matches) > record["occurrence"] else None


def cleanup_files(source, plan):
    item = planned_item(source, plan["item"])
    if item is None:
        raise CleanupMismatch("The proposed entry is absent or edited; awaiting maintenance recheck")
    lines = source.splitlines(keepends=True)
    if plan["action"] == "Remove":
        lines[item.index] = ""
    elif plan["action"] == "Update URL":
        lines[item.index] = lines[item.index].replace(item.url, plan["reason"], 1)
    else:
        raise CleanupMismatch("Unrecognized maintenance action")
    updated = "".join(lines)
    generated = render_readme(updated)
    if not any(line.startswith("# Awesome Zotero") for line in generated.splitlines()) or len(entries(updated)) > len(entries(source)):
        raise ValueError("Generated README failed cleanup validation")
    return {SOURCE: updated, "README.md": generated}


def blob_sha(content):
    data = content.encode("utf-8")
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def updated_tree_sha(rows, files):
    """Compute Git's complete root-tree ID without writing objects or refs."""
    tree = {row["path"]: dict(row) for row in rows}
    for path, content in files.items():
        if "/" in path:
            raise ValueError("Cleanup validation only supports root files")
        tree[path] = {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha(content)}
    ordered = sorted(tree.values(), key=lambda row: row["path"].encode() + (b"/" if row["type"] == "tree" else b""))
    data = b"".join(row["mode"].lstrip("0").encode() + b" " + row["path"].encode() + b"\0" + bytes.fromhex(row["sha"])
                    for row in ordered)
    return hashlib.sha1(f"tree {len(data)}\0".encode() + data).hexdigest()


def verify_cleanup_tree(api, repo, base, sha, plan, commit=None):
    """Allow an Update-branch merge only if its entire tree is the saved edit.

    Use the common ancestor on main so intervening trusted main changes are
    retained. Extra files, altered entries, or hand-edited README output fail.
    No PR code is executed and no Git objects or branches are written.
    """
    comparison = api.get(f"/repos/{repo}/compare/{base}...{sha}")
    ancestor = comparison["merge_base_commit"]["sha"]
    source = api.text(repo, SOURCE, ancestor)
    files = cleanup_files(source, plan)
    original = api.get(f"/repos/{repo}/git/commits/{ancestor}")
    tree = api.get(f"/repos/{repo}/git/trees/{original['tree']['sha']}")
    if tree.get("truncated"):
        raise Unavailable("Base tree is incomplete; awaiting maintenance recheck")
    commit = commit or api.get(f"/repos/{repo}/git/commits/{sha}")
    if updated_tree_sha(tree["tree"], files) != commit["tree"]["sha"]:
        raise CleanupMismatch("Cleanup tree differs from the saved single-entry proposal; preserving branch for review")
    return ancestor


def cleanup_report(repo, default, findings, unknown):
    body = MARKER + "\n\nWeekly curation under [the maintenance policy](https://github.com/" + repo + "/blob/" + default + "/CONTRIBUTING.md#curation-and-automated-checks).\n\n"
    body += "| Project | Action | Evidence |\n| --- | --- | --- |\n"
    for item, action, reason in findings:
        body += f"| [{markdown(item.name)}]({item.url}) | {action} | {markdown(reason)} |\n"
    body += "\nGenerated README is included. Merge requires maintainer review.\n"
    if unknown:
        body += "\nChecks left inconclusive (these do not cause removals):\n" + "\n".join(f"- {markdown(row)}" for row in unknown)
    return body


def proposal_marker(plan):
    # Rechecks of the same evidence must not reopen a maintainer's declined PR.
    reason = re.sub(r"across \d+ spaced checks", "across repeated spaced checks",
                    plan["reason"].removeprefix("Awaiting recheck: "))
    evidence = dict(plan, pending=False, reason=reason)
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return "<!-- awesome-zotero:proposal:" + digest + " -->"


def reconcile_cleanup(api, repo, default, base, source, plans, pulls, unknown, refresh=False):
    opened = {pr["head"]["ref"]: pr for pr in pulls if pr["state"] == "open"}
    deferred, created, errors = 0, 0, []
    # Refresh existing proposals first. Bound new notifications and API mutations.
    for branch, plan in sorted(plans.items(), key=lambda row: row[0] not in opened):
        pr = opened.get(branch)
        if not pr and any(prior["head"]["ref"] == branch and prior["state"] == "closed"
                          and not prior.get("merged_at")
                          and proposal_marker(plan) in (prior.get("body") or "")
                          and "<!-- awesome-zotero:obsolete -->" not in (prior.get("body") or "")
                          for prior in pulls):
            continue
        if refresh and not pr:
            deferred += 1
            continue
        if (not pr and created >= 10) or api.write_count > MAX_WRITES - 12:
            deferred += 1
            continue
        item = planned_item(source, plan["item"])
        if item is None:
            continue
        try:
            files = cleanup_files(source, plan)
            sha = branch_commit(api, repo, branch, base, files,
                                verify_existing=lambda old, commit: verify_cleanup_tree(api, repo, base, old, plan, commit))
        except Unavailable as exc:
            errors.append(f"{branch}: {exc}")
            if pr:
                for context in ("curation/maintenance", "curation/admission"):
                    status(api, repo, pr["head"]["sha"], context,
                           "failure" if isinstance(exc, CleanupMismatch) else "error", str(exc))
            continue
        pending = plan["pending"]
        state = "pending" if pending else "success"
        status(api, repo, sha, "curation/maintenance", state, "Awaiting maintenance recheck" if pending else "Single-entry cleanup and README generation validated")
        status(api, repo, sha, "curation/admission", state, "Awaiting maintenance recheck" if pending else "Maintenance-only changes; no new projects introduced")
        body = cleanup_report(repo, default, [(item, plan["action"], plan["reason"])],
                              ["Awaiting a conclusive weekly recheck for this entry."] if pending else [])
        body += "\n" + proposal_marker(plan) + "\n"
        payload = {"title": ("Remove item: " if plan["action"] == "Remove" else "Fix item: ") + item.name,
                   "body": body}
        if pr:
            if any(pr.get(field) != value for field, value in payload.items()):
                api.request(f"/repos/{repo}/pulls/{pr['number']}", "PATCH", payload)
        else:
            api.request(f"/repos/{repo}/pulls", "POST", dict(payload, head=branch, base=default, draft=False))
            created += 1
    for branch, pr in opened.items():
        if branch in plans or api.write_count > MAX_WRITES - 3:
            continue
        if branch == CLEANUP_BRANCH:
            # Keep the legacy aggregate until every replacement is accounted for.
            represented = {key(plan["item"]["url"]) for plan in plans.values()}
            unresolved = [row for row in unknown if row.split(": ", 1)[0] not in represented
                          and row.split(": ", 1)[0].lower() in (pr.get("body") or "").lower()]
            if deferred or errors or unresolved:
                status(api, repo, pr["head"]["sha"], "curation/admission", "pending", "Awaiting individual replacement proposals or conclusive recheck")
                continue
            note = "Superseded by individual entry proposals; no combined cleanup will be merged."
        else:
            note = "No cleanup changes remain for this entry; closing this obsolete proposal."
        api.request(f"/repos/{repo}/pulls/{pr['number']}", "PATCH", {
            "state": "closed", "body": (pr.get("body") or "") + "\n\n<!-- awesome-zotero:obsolete -->\n" + note + "\n"})
    if deferred:
        note = f"Deferred {deferred} entry proposals to the next write run (notification/API budget)."
        print(note)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write("\n\n" + note + "\n")
    if errors:
        raise Unavailable("Other cleanup proposals were processed; branches needing review/retry: " + "; ".join(errors))


def weekly(api, repo, policy, dry_run, refresh=False):
    meta = api.get(f"/repos/{repo}")
    default = meta["default_branch"]
    base = api.get(f"/repos/{repo}/git/ref/heads/{quote(default, safe='/')}")["object"]["sha"]
    # Ensure generation uses the same trusted base revision we inspected.
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if checkout != base and not dry_run:
        raise Unavailable("Default branch changed after checkout; rerun against the latest revision")
    source = api.text(repo, SOURCE, base)
    try:
        previous = json.loads(api.text(repo, STATE_FILE, STATE_BRANCH))
    except Unavailable as exc:
        if exc.status != 404:
            raise
        previous = {}
    if refresh and "_cleanup" not in previous:
        print("No saved individual proposals to refresh; run weekly curation first.")
        return
    if refresh:
        # Reapply only saved findings to exact, unchanged entry rows. No project
        # inspections, missing counters, or new proposals on each main update.
        plans = {branch: dict(plan, pending=plan["pending"] or previous.get("_cleanup_policy") != policy)
                 for branch, plan in previous.get("_cleanup", {}).items()
                 if planned_item(source, plan["item"]) is not None
                 and not (key(plan["item"]["url"]) in policy["inactivity_exemptions"]
                          and plan["reason"].removeprefix("Awaiting recheck: ").startswith("No default-branch"))}
        observations = previous
        findings = [(planned_item(source, plan["item"]), plan["action"], plan["reason"]) for plan in plans.values()]
        unknown = [key(plan["item"]["url"]) + ": Awaiting a conclusive weekly recheck"
                   for plan in plans.values() if plan["pending"]]
    else:
        _, observations, findings, unknown = maintenance(api, source, previous, policy, utcnow())
        plans = cleanup_plans(source, findings, unknown)
        if plans:
            observations["_cleanup"] = plans
            observations["_cleanup_policy"] = policy
    body = cleanup_report(repo, default, findings, unknown)
    print(body)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(body)
    if dry_run:
        return
    api.reporting = True
    pulls = [pr for pr in api.pages(f"/repos/{repo}/pulls?" + urlencode({"state": "all", "base": default}))
             if (pr.get("head", {}).get("repo") or {}).get("full_name", "").casefold() == repo.casefold()
             and (pr["head"].get("ref") == CLEANUP_BRANCH or is_entry_branch(pr["head"].get("ref")))]
    if any(pr["state"] == "open" and (MARKER not in (pr.get("body") or "") or not is_cleanup_pr(repo, pr)) for pr in pulls):
        raise Unavailable("Cleanup branch has an unrecognized PR; refusing to modify it")
    if observations != previous:
        branch_commit(api, repo, STATE_BRANCH, base, {STATE_FILE: json.dumps(observations, indent=2) + "\n"})
    reconcile_cleanup(api, repo, default, base, source, plans, pulls, unknown, refresh)


@dataclass
class Verdict:
    rejected: list = field(default_factory=list)
    uncertain: list = field(default_factory=list)
    fixable: list = field(default_factory=list)
    verified: list = field(default_factory=list, compare=False)


FIELDS = {"Project URL", "Project type", "Documentation URL", "Installation URL",
          "Tested version", "Tested Zotero version", "Verification", "Distinct value", "Data handling", "AI-generated code"}


def evidence_fields(body):
    # Remove template instructions/comments so examples cannot satisfy required evidence.
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    result = {}
    for line in body.splitlines():
        match = re.fullmatch(r"([A-Za-z -]+):\s*(.*?)\s*", line)
        if match and match[1] in FIELDS:
            if match[1] in result:
                result[match[1]] = ""  # Ambiguous declarations must be corrected.
            else:
                result[match[1]] = match[2]
    return result


def majority_disclosure(text):
    # Deliberately narrow: do not infer authorship from style, agents files, or commits.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    plain, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        if not re.match(r"^(?: {4}|\t|\s*>)", line):
            plain.append(line)
    text = "\n".join(plain)
    declaration = r"AI-generated code:\s*majority"
    explicit = r"(?:all|most|the majority) (?:of )?(?:the )?(?:source )?code(?: in this project)? (?:was|is|has been) (?:generated|written) by (?:AI|an? LLM|ChatGPT|Claude|GitHub Copilot)"
    return bool(re.search(r"(?im)^\s*(?:" + declaration + "|" + explicit + r")[.!\s]*$", text))


def inspect_project(api, item, evidence, policy, now, verdict, existing):
    repo = github_repo(item.url)
    if not repo:
        verdict.uncertain.append("Non-GitHub project: repository age and release evidence need maintainer review")
        return
    try:
        with api.project_budget() if hasattr(api, "project_budget") else nullcontext():
            _inspect_project(api, repo, item, evidence, policy, now, verdict, existing)
    except Unavailable as exc:
        verdict.uncertain.append(f"Cannot complete project inspection: {exc}")


def verify_plugin(api, content, asset, tested_zotero, current_zotero, limit):
    try:
        return inspect_xpi(content, tested_zotero, current_zotero, limit)
    except CompatibilityMismatch as exc:
        settings = exc.manifest["applications"]["zotero"]
        update_url = settings.get("update_url")
        if not update_url:
            raise
        updates = api.public_json(update_url)
        try:
            rows = updates["addons"][settings["id"]]["updates"]
            for row in rows:
                if row.get("version") != exc.manifest["version"]:
                    continue
                digest = row.get("update_hash")
                same_asset = (digest == "sha256:" + exc.digest if digest else
                              bool(asset.get("browser_download_url")) and row.get("update_link") == asset["browser_download_url"])
                if not same_asset:
                    continue
                override = row.get("applications", {}).get("zotero", {})
                override = {name: override[name] for name in ("strict_min_version", "strict_max_version") if name in override}
                result = inspect_xpi(content, tested_zotero, current_zotero, limit, overrides=override)
                result["published_override"] = True
                return result
        except (KeyError, TypeError, AttributeError, InvalidArtifact) as error:
            raise Unavailable("Published update manifest did not establish compatibility for this exact artifact") from error
        raise Unavailable("Published update manifest did not identify this exact plugin version and artifact")


def _inspect_project(api, repo, item, evidence, policy, now, verdict, existing):
    meta = api.get(f"/repos/{repo}")
    if meta["full_name"].lower() in existing:
        verdict.rejected.append("Project resolves to a repository already present in the list")
    if date(meta["created_at"]) > months_ago(now, policy["minimum_age_months"]):
        verdict.rejected.append(f"Repository created {meta['created_at'][:10]}; projects must be at least {policy['minimum_age_months']} calendar months old")
    if meta.get("archived") or meta.get("disabled"):
        verdict.rejected.append("Project repository is archived or disabled")
    if meta.get("size") == 0:
        verdict.rejected.append("Project reports zero repository size")
    if verdict.rejected:
        return
    branch = meta["default_branch"]
    readme = ""
    try:
        readme = api.readme(repo, branch)
        text = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
        if len(re.sub(r"\s", "", text)) < policy["minimum_readme_characters"]:
            verdict.fixable.append(f"Project README lacks the minimum documentation ({policy['minimum_readme_characters']} non-whitespace characters)")
        if not re.search(r"\bzotero\b", text + " " + item.description, re.I):
            verdict.fixable.append("Neither the project README nor its entry documents a Zotero-specific purpose")
        if majority_disclosure(readme):
            verdict.rejected.append("Project README explicitly declares that most or all of its code was generated by AI")
    except Unavailable as exc:
        if exc.status == 404:
            verdict.fixable.append("Project has no accessible README")
        else:
            raise
    last, _, releases = activity(api, repo, meta, now, inactive_days=policy["inactive_days"])
    if now - last >= timedelta(days=policy["inactive_days"]):
        verdict.rejected.append(f"Project has no commits or published releases within {policy['inactive_days']} days")
    source_id, source_plugin = None, False
    for path in ("manifest.json", "src/manifest.json", "addon/manifest.json"):
        try:
            manifest = json.loads(api.text(repo, path, branch))
            if isinstance(manifest, dict):
                settings = manifest.get("applications", {}).get("zotero")
                if isinstance(settings, dict):
                    source_plugin = True
                    identifier = settings.get("id")
                    if isinstance(identifier, str) and not re.search(r"[{$}]|__", identifier):
                        source_id = identifier or source_id
                    break
        except Unavailable as exc:
            if exc.status != 404:
                raise
        except (ValueError, TypeError, AttributeError):
            continue
    plugin = (evidence.get("Project type") == "plugin" or source_plugin or
              any(a.get("name", "").lower().endswith(".xpi") for r in releases for a in r.get("assets", [])) or
              re.search(r"\bzotero (?:plug-?in|add-?on)\b|\b(?:plug-?in|add-?on) for zotero\b", item.description, re.I))
    tested = evidence.get("Tested version", "")
    tested_zotero = evidence.get("Tested Zotero version", "")
    release = None
    if tested and not tested.startswith("<"):
        try:
            if not plugin and re.fullmatch(r"commit:[a-fA-F0-9]{40}", tested):
                api.get(f"/repos/{repo}/commits/{tested[7:]}")
                verdict.verified.append(f"Tested project revision: {tested}")
            else:
                release = api.get(f"/repos/{repo}/releases/tags/{quote(tested, safe='')}")
                if release.get("draft") or release.get("prerelease") or not release.get("published_at"):
                    verdict.rejected.append("Tested version must identify a published stable release")
                    release = None
                else:
                    verdict.verified.append(f"Tested project release: {tested}; tested Zotero version: {tested_zotero}")
        except Unavailable as exc:
            if exc.status == 404:
                verdict.fixable.append("Tested version does not identify an accessible release or full commit SHA in this project")
            else:
                raise
    # The declared release is authoritative evidence too. It may be older than
    # the recent activity snapshot and contain a plugin that initial hints missed.
    if release:
        plugin = plugin or any(asset.get("name", "").lower().endswith(".xpi")
                               for asset in release.get("assets", []))
    if plugin and release:
        candidates = [a for a in release.get("assets", []) if a.get("name", "").lower().endswith(".xpi") and a.get("size", 0) > 0]
        if not candidates:
            verdict.rejected.append("Plugin has no nonempty .xpi asset in the declared tested release")
        elif re.fullmatch(r"\d+(?:\.\d+){1,3}", tested_zotero):
            current_zotero = api.zotero_version()
            errors, unavailable = [], []
            for asset in candidates[:3]:
                try:
                    inspected = verify_plugin(api, api.asset(repo, asset, policy["maximum_asset_bytes"]), asset,
                                              tested_zotero, current_zotero, policy["maximum_asset_bytes"])
                    if source_id and source_id != inspected["id"]:
                        raise InvalidArtifact("Release plugin ID differs from the project's source manifest")
                    verdict.verified.append(f"Release {tested}, asset {asset['name']}: plugin {inspected['id']} version {inspected['version']}; declared Zotero range {inspected['minimum']}–{inspected['maximum']}; current Zotero {current_zotero}; SHA-256 {inspected['sha256']}")
                    if inspected.get("published_override"):
                        verdict.verified.append("Compatibility verified through a published update manifest matching this plugin version and artifact")
                    break
                except InvalidArtifact as exc:
                    errors.append(str(exc))
                except Unavailable as exc:
                    unavailable.append(str(exc))
                except UnsupportedCompatibility as exc:
                    unavailable.append(str(exc))
            else:
                if unavailable or len(candidates) > 3:
                    verdict.uncertain.append("Plugin artifact inspection incomplete: " + "; ".join(unavailable or ["asset inspection limit reached"]))
                else:
                    verdict.rejected.append("Plugin artifact verification failed: " + "; ".join(errors))
    for name in ("Documentation URL", "Installation URL"):
        if evidence.get(name, "").startswith("https://"):
            try:
                if not api.link(evidence[name]):
                    verdict.fixable.append(f"{name} points to a missing document or download")
            except Unavailable as exc:
                if exc.status in {404, 410}:
                    verdict.fixable.append(f"{name} points to a missing document or download")
                else:
                    verdict.uncertain.append(f"{name}: {exc}")


def format_errors(before, after):
    old_lines = set(before.splitlines())
    errors = []
    for line in after.splitlines():
        if line in old_lines or ENTRY.fullmatch(line):
            continue
        # Fail closed on alternate Markdown/HTML links, including reference links.
        candidate = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+.*(?:\[[^\]]+\]|https?://|<a\b)", line, re.I)
        reference = re.match(r"^\s*\[[^\]]+\]:\s*<?https?://", line)
        anchor = re.fullmatch(r"\s*- \[[^\]]+\]\(#[^\s)]+\)\s*", line)
        if (candidate and not anchor) or reference or re.search(r"<a\s+[^>]*href\s*=", line, re.I):
            errors.append("New project entries must use '- [Name](https://...) - Description'; reference, HTML, and alternate list formats are not supported")
    return errors


def new_projects(api, old, new, verdict):
    old_keys = {key(item.url) for item in old}
    new_keys = {key(item.url) for item in new}
    added = []
    for item in new:
        if key(item.url) in old_keys:
            continue
        candidates = [prior for prior in old if key(prior.url) not in new_keys or prior.name.casefold() == item.name.casefold()]
        repo = github_repo(item.url)
        if not candidates or not repo:
            added.append(item)
            continue
        try:
            with api.project_budget() if hasattr(api, "project_budget") else nullcontext():
                target = api.get(f"/repos/{repo}")["full_name"].lower()
                matched = False
                unresolved = False
                for prior in candidates:
                    previous_repo = github_repo(prior.url)
                    if not previous_repo:
                        continue
                    try:
                        same_project = api.get(f"/repos/{previous_repo}")["full_name"].lower() == target
                        matched |= same_project
                        if same_project and key(prior.url) in new_keys:
                            verdict.rejected.append("Project resolves to a repository already present in the list")
                    except Unavailable:
                        unresolved = True
                if not matched:
                    if unresolved:
                        verdict.uncertain.append("Could not resolve a possible existing-project URL correction; awaiting recheck")
                    else:
                        added.append(item)
        except Unavailable as exc:
            verdict.uncertain.append(f"Could not determine whether the URL change introduces a new project: {exc}")
    return added


def admission_verdict(api, before, after, body, policy, now):
    verdict = Verdict()
    old, new = entries(before), entries(after)
    old_counts = Counter(key(item.url) for item in old)
    new_counts = Counter(key(item.url) for item in new)
    added = new_projects(api, old, new, verdict)
    for identity, count in new_counts.items():
        if count > max(1, old_counts[identity]):
            verdict.rejected.append(f"Duplicate entry: {identity}")
    verdict.fixable.extend(format_errors(before, after))
    if not added:
        return verdict
    if len(added) > 1:
        verdict.rejected.append("Submit one new project per PR")
        return verdict
    evidence = evidence_fields(body)
    for name in sorted(FIELDS):
        if not evidence.get(name) or evidence[name].startswith("<"):
            verdict.fixable.append(f"Missing admission evidence: {name}")
    item = added[0]
    if not re.fullmatch(r"https://\S+", evidence.get("Project URL", "")):
        verdict.fixable.append("Project URL must be a plain HTTPS URL")
    if key(evidence.get("Project URL", "")) != key(item.url):
        verdict.fixable.append("Project URL declaration must match the new entry")
    if evidence.get("Project type") not in {"plugin", "integration", "theme", "developer-tool"}:
        verdict.fixable.append("Project type must be plugin, integration, theme, or developer-tool")
    for name in ("Documentation URL", "Installation URL"):
        if not re.fullmatch(r"https://\S+", evidence.get(name, "")):
            verdict.fixable.append(f"{name} must be a plain HTTPS URL")
    for name in ("Project URL", "Documentation URL", "Installation URL"):
        try:
            public_url(evidence.get(name, ""))
        except UnsafeURL:
            verdict.fixable.append(f"{name} must be a well-formed public HTTPS URL on port 443")
    for name in ("Verification", "Distinct value", "Data handling"):
        value = evidence.get(name, "")
        if not value.strip() or value.startswith("<") or re.search(r"(?i)\b(?:todo|tbd|fill this|replace this)\b", value):
            verdict.fixable.append(f"{name} must supply concrete evidence without template placeholders")
    explanations = [evidence.get(name, "").strip().lower() for name in ("Verification", "Distinct value", "Data handling")]
    if all(explanations) and len(set(explanations)) != len(explanations):
        verdict.fixable.append("Verification, Distinct value, and Data handling must provide different evidence; identical copy-pasted answers do not satisfy the template")
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", evidence.get("Tested Zotero version", "")):
        verdict.fixable.append("Tested Zotero version must be an exact numeric version, for example 10.0.8")
    for name in ("Tested version", "Tested Zotero version"):
        value = evidence.get(name, "")
        if value and not value.startswith("<") and value.casefold() not in evidence.get("Verification", "").casefold():
            verdict.fixable.append(f"Verification must describe the outcome for the declared {name}")
    disclosure = evidence.get("AI-generated code", "").lower()
    if disclosure == "majority":
        verdict.rejected.append("Submission declares that most project code is AI-generated; this list does not accept majority-generated projects")
    elif disclosure not in {"none", "some", "unknown"}:
        verdict.fixable.append("AI-generated code must be none, some, majority, or unknown")
    inspect_project(api, item, evidence, policy, now, verdict, old_counts)
    return verdict


def is_cleanup_pr(repo, pr):
    """Recognize the repository's own Actions-authored reserved cleanup PRs.

    Admission validates their saved proposal instead of new-project evidence.
    A fork, marker, or branch name alone cannot claim this role, and admission
    overrides cannot waive maintenance validation.
    """
    head = pr.get("head") or {}
    origin = head.get("repo") or {}
    author = pr.get("user") or {}
    return ((head.get("ref") == CLEANUP_BRANCH or is_entry_branch(head.get("ref")))
            and (origin.get("full_name") or "").casefold() == repo.casefold()
            and author.get("type") == "Bot"
            and author.get("login") == "github-actions[bot]")


def maintenance_owns_commit(api, repo, sha, fresh=False):
    """Statuses belong to commits, so duplicate/fork PRs cannot overwrite them."""
    read = api.request if fresh else api.get
    statuses = read(f"/repos/{repo}/commits/{sha}/status").get("statuses", [])
    if any(row["context"] == "curation/maintenance" for row in statuses):
        return True
    try:
        reference = read(f"/repos/{repo}/git/ref/heads/{CLEANUP_BRANCH}")
    except Unavailable as exc:
        if exc.status != 404:
            raise
        reference = {}
    # Protect the short interval between branch publication and status creation.
    if reference.get("object", {}).get("sha") == sha:
        return True
    references = read(f"/repos/{repo}/git/matching-refs/heads/{ENTRY_BRANCH_PREFIX}")
    return any(is_entry_branch(row.get("ref", "").removeprefix("refs/heads/"))
               and row.get("object", {}).get("sha") == sha for row in references)


def admission_ownership_unavailable(api, repo, sha, error):
    status(api, repo, sha, "curation/admission", "error",
           "Maintenance ownership unavailable; awaiting recheck: " + str(error))


def cleanup_admission(api, repo, pr, policy):
    """Repair the required status after a maintainer uses Update branch."""
    api.reporting = True
    sha, branch = pr["head"]["sha"], pr["head"]["ref"]
    try:
        saved = json.loads(api.text(repo, STATE_FILE, STATE_BRANCH))
        plan = saved.get("_cleanup", {}).get(branch)
        if not plan or entry_branch(plan["item"]) != branch:
            raise Unavailable("No matching saved cleanup proposal; awaiting weekly recheck")
        verify_cleanup_tree(api, repo, pr["base"]["sha"], sha, plan)
        pending = (plan["pending"] or saved.get("_cleanup_policy") != policy or
                   key(plan["item"]["url"]) in policy["inactivity_exemptions"] and
                   plan["reason"].removeprefix("Awaiting recheck: ").startswith("No default-branch"))
        state = "pending" if pending else "success"
        description = "Awaiting maintenance recheck" if pending else "Exact saved cleanup and generated README validated"
    except CleanupMismatch as exc:
        state, description = "failure", str(exc)
    except (Unavailable, ValueError, KeyError, TypeError) as exc:
        state, description = "error", "Maintenance validation unavailable; awaiting recheck: " + str(exc)
    # Do not publish a verdict for a head that changed while it was inspected.
    current = api.request(f"/repos/{repo}/pulls/{pr['number']}")
    if current["state"] != "open" or current.get("draft") or current["head"]["sha"] != sha or not is_cleanup_pr(repo, current):
        return
    for context in ("curation/maintenance", "curation/admission"):
        status(api, repo, sha, context, state, description)


def admission(api, repo, event, policy):
    api.reporting = False
    number = event["pull_request"]["number"]
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    if pr["state"] != "open" or pr.get("draft"):
        return
    if is_cleanup_pr(repo, pr):
        if is_entry_branch(pr["head"].get("ref")):
            cleanup_admission(api, repo, pr, policy)
        return
    sha = pr["head"]["sha"]
    try:
        if maintenance_owns_commit(api, repo, sha):
            return
    except Unavailable as exc:
        admission_ownership_unavailable(api, repo, sha, exc)
        return
    overridden = any(label["name"] == policy["override_label"] for label in pr.get("labels", []))
    if overridden:
        status(api, repo, sha, "curation/admission", "success", "Maintainer applied curation override")
        update_admission_comment(api, repo, number,
                                 MARKER + "\n\nA maintainer applied the admission override. Other repository checks still apply.",
                                 create=False, clear_deadline=True)
        return
    status(api, repo, sha, "curation/admission", "pending", "Checking new project evidence")
    try:
        files = api.pages(f"/repos/{repo}/pulls/{number}/files")
        names = {f["filename"] for f in files}
        if SOURCE in names or "README.md" in names:
            before = api.text(repo, SOURCE, pr["base"]["sha"])
            # GitHub exposes fork commit objects in the base repository. Never check them out.
            after = api.text(repo, SOURCE, sha) if SOURCE in names else before
            verdict = admission_verdict(api, before, after, pr.get("body"), policy, utcnow())
            if "README.md" in names:
                old_readme = api.text(repo, "README.md", pr["base"]["sha"])
                new_readme = api.text(repo, "README.md", sha)
                verdict.fixable.extend(format_errors(old_readme, new_readme))
                allowed = Counter(key(item.url) for item in entries(after))
                rendered = Counter(key(item.url) for item in entries(new_readme))
                if rendered - allowed:
                    verdict.fixable.append("New entries must be submitted in _README.md; README.md contains an entry absent from its source")
        else:
            verdict = Verdict()
    except Unavailable as exc:
        verdict = Verdict(uncertain=[str(exc)])
    api.reporting = True
    # A contributor may have updated their PR while API requests were in flight.
    current = api.request(f"/repos/{repo}/pulls/{number}")
    if (current["head"]["sha"] != sha or current.get("body") != pr.get("body")
            or current["state"] != "open"
            or current.get("draft")
            or is_cleanup_pr(repo, current)
            or any(label["name"] == policy["override_label"] for label in current.get("labels", []))):
        return
    try:
        if maintenance_owns_commit(api, repo, sha, fresh=True):
            return
    except Unavailable as exc:
        admission_ownership_unavailable(api, repo, sha, exc)
        return
    if not verdict.rejected and not verdict.uncertain and not verdict.fixable:
        status(api, repo, sha, "curation/admission", "success", "Automated admission criteria satisfied; editorial review still required")
        body = MARKER + "\n\nThe submission satisfies the automated admission criteria. Maintainer review is still required.\n\n"
        body += "\n".join(f"- {markdown(row)}" for row in verdict.verified)
        body += "\n\nArtifact checks verify structure and declared compatibility, not working behavior. AI provenance is self-declared; no percentage is inferred."
        update_admission_comment(api, repo, number, body, create=bool(verdict.verified), clear_deadline=True)
        return
    deadline = None
    if verdict.fixable and not verdict.rejected:
        comments = api.pages(f"/repos/{repo}/issues/{number}/comments")
        existing = bot_comment(comments)
        match = re.search(r"<!-- awesome-zotero:correction-deadline ([\dTZ:+.\-]+) -->", existing["body"]) if existing else None
        deadline = date(match[1]) if match else utcnow() + timedelta(hours=policy["correction_hours"])
    rejected = bool(verdict.rejected) or bool(deadline and utcnow() >= deadline)
    body = MARKER + "\n\n" + ("This PR is being closed because it does not meet this list's admission policy:\n\n" if rejected
                                 else "This PR remains open while the following checks are resolved:\n\n")
    body += "\n".join(f"- {markdown(reason)}" for reason in dict.fromkeys(verdict.rejected))
    if verdict.fixable:
        body += "\n\nCorrections required:\n" + "\n".join(f"- {markdown(reason)}" for reason in dict.fromkeys(verdict.fixable))
    if deadline:
        body += f"\n\nCorrection deadline: **{iso(deadline)}**. The bot rechecks edits immediately and open submissions daily; unresolved corrections are closed at the first recheck after this deadline.\n<!-- awesome-zotero:correction-deadline {iso(deadline)} -->"
    if verdict.uncertain:
        body += "\n\nInconclusive checks (not closure reasons):\n" + "\n".join(f"- {markdown(reason)}" for reason in dict.fromkeys(verdict.uncertain))
    next_step = ("Correct the reasons above and ask a maintainer to reopen, or resubmit once eligible. " if rejected else
                 "Update this PR to address the corrections; the bot rechecks edits and open submissions automatically. " if verdict.fixable else
                 "The bot will retry these inconclusive checks automatically. ")
    body += ("\n\nSee [the admission policy](https://github.com/" + repo + "/blob/" + pr["base"]["ref"]
             + "/CONTRIBUTING.md#curation-and-automated-checks). " + next_step
             + "A maintainer can apply `" + policy["override_label"] + "` for a documented exception. "
             "AI authorship is not inferred from coding style, commit volume, or contributor count.")
    update_admission_comment(api, repo, number, body)
    state = "failure" if rejected else "pending" if verdict.fixable else "error"
    status(api, repo, sha, "curation/admission", state,
           "Admission policy not satisfied" if rejected else "Corrections requested; awaiting recheck" if verdict.fixable else "Evidence unavailable; awaiting recheck")
    if rejected:
        # Comment is published before closing, so every automatic closure has its explanation.
        api.request(f"/repos/{repo}/pulls/{number}", "PATCH", {"state": "closed"})


def bot_comment(comments):
    return next((comment for comment in comments if MARKER in comment["body"]
                 and comment["user"]["type"] == "Bot"
                 and comment["user"].get("login", "github-actions[bot]") == "github-actions[bot]"), None)


def update_admission_comment(api, repo, number, body, create=True, clear_deadline=False):
    comments = api.pages(f"/repos/{repo}/issues/{number}/comments")
    existing = bot_comment(comments)
    if existing:
        # An inconclusive check can replace the visible report without erasing
        # persistent state. Only a conclusive pass or explicit override clears it.
        deadline = re.search(r"<!-- awesome-zotero:correction-deadline ([\dTZ:+.\-]+) -->", existing["body"])
        if deadline and "<!-- awesome-zotero:correction-deadline " not in body and not clear_deadline:
            body += f"\n\nExisting correction deadline: **{markdown(deadline[1])}**. Inconclusive checks alone do not cause closure.\n" + deadline[0]
        if existing["body"] != body:
            api.request(f"/repos/{repo}/issues/comments/{existing['id']}", "PATCH", {"body": body})
    elif create:
        api.request(f"/repos/{repo}/issues/{number}/comments", "POST", {"body": body})


def pending_admission(api, repo, policy):
    """Oldest attempted inspections first, so a daily cap cannot starve later PRs."""
    pending = []
    for pr in api.pages(f"/repos/{repo}/pulls?state=open"):
        if pr.get("draft") or is_cleanup_pr(repo, pr):
            continue
        statuses = api.get(f"/repos/{repo}/commits/{pr['head']['sha']}/status")["statuses"]
        if any(row["context"] == "curation/maintenance" for row in statuses):
            continue
        last = next((row for row in statuses if row["context"] == "curation/admission"), None)
        if not last or last["state"] in {"error", "pending", "failure"}:
            pending.append((last.get("created_at", "") if last else "", pr["number"]))
    return [number for _, number in sorted(pending)[:policy["maximum_daily_rechecks"]]]


def main():
    policy = json.loads((ROOT / ".github/curation.json").read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    if not REPO.fullmatch(repo):
        raise ValueError("Invalid workflow repository")
    api = GitHub(os.environ.get("GH_TOKEN"), policy["maximum_read_requests"], policy["maximum_project_requests"])
    if sys.argv[1:] == ["weekly"]:
        dry_run = os.environ.get("CURATION_DRY_RUN", "true").lower() != "false"
        weekly(api, repo, policy, dry_run)
    elif sys.argv[1:] == ["refresh"]:
        weekly(api, repo, policy, False, refresh=True)
    elif sys.argv[1:] == ["pending"]:
        numbers = pending_admission(api, repo, policy)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("numbers=" + json.dumps(numbers) + "\n")
    elif sys.argv[1:] == ["admission"]:
        event = {"pull_request": {"number": int(os.environ["CURATION_PR_NUMBER"])}} if os.environ.get("CURATION_PR_NUMBER") else json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        admission(api, repo, event, policy)
    else:
        raise SystemExit("Usage: curation.py weekly|refresh|pending|admission")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Deterministic curation; stdlib only. Untrusted projects are never executed."""

import base64
import calendar
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "_README.md"
STATE_BRANCH = "automation/curation-state"
CLEANUP_BRANCH = "automation/weekly-curation"
STATE_FILE = ".github/curation-state.json"
MARKER = "<!-- awesome-zotero:curation -->"
COMMIT_PREFIX = "automation: curation"
ENTRY = re.compile(r"^- \[([^\]]+)\]\((https?://[^\s)]+)\)\s+-\s+(.+)$")
ANY_ENTRY = re.compile(r"^\s*[-*+]\s+\[([^\]]+)\]\((https?://[^\s)]+)\)(.*)$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_BYTES = 2_000_000


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
    parsed = urlsplit(url)
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


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc.lower() not in self.hosts:
            raise Unavailable("Redirect outside the permitted HTTPS hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token, external_hosts):
        self.token = token
        self.external_hosts = set(external_hosts)
        self.cache = {}

    def request(self, path, method="GET", data=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a GitHub API path")
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
        except (URLError, TimeoutError, OSError) as exc:
            raise Unavailable("GitHub API temporarily unavailable") from exc

    def get(self, path):
        if path not in self.cache:
            self.cache[path] = self.request(path)
        return self.cache[path]

    def pages(self, path, per_page=100):
        result = []
        # Bound resource use; never silently treat a truncated response as complete.
        for page in range(1, 2000 // per_page + 1):
            try:
                rows = self.get(path + ("&" if "?" in path else "?") + urlencode({"per_page": per_page, "page": page}))
            except Unavailable as exc:
                if str(exc) == "API response too large to inspect safely" and per_page > 1:
                    return self.pages(path, max(1, per_page // 10))
                raise
            result.extend(rows)
            if len(rows) < per_page:
                return result
        raise Unavailable("Pagination limit reached; evidence is incomplete")

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
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise Unavailable("Only public HTTPS evidence links are supported")
        if parsed.netloc.lower() == "github.com":
            repo = github_repo(url)
            if not repo:
                raise Unavailable("Expected a GitHub project link")
            self.get(f"/repos/{repo}")
            parts = parsed.path.strip("/").split("/")
            # Use fixed API endpoints, never fetch contributor-supplied download URLs.
            if len(parts) == 2:
                return True
            if parts[2] in {"blob", "tree"} and len(parts) >= 5:
                self.get(f"/repos/{repo}/contents/{quote('/'.join(parts[4:]), safe='/')}?"
                         + urlencode({"ref": parts[3]}))
                return True
            if parts[2] == "releases":
                suffix = parts[3:]
                if not suffix:
                    return True
                if suffix == ["latest"]:
                    self.get(f"/repos/{repo}/releases/latest")
                    return True
                if len(suffix) >= 2 and suffix[0] in {"tag", "download"}:
                    release = self.get(f"/repos/{repo}/releases/tags/{quote(suffix[1], safe='')}")
                    if suffix[0] == "download":
                        return len(suffix) == 3 and any(a["name"] == suffix[2] for a in release.get("assets", []))
                    return True
            raise Unavailable("This GitHub evidence URL needs maintainer review")
        if parsed.netloc.lower() not in self.external_hosts:
            raise Unavailable("Evidence host is not in external_link_hosts")
        request = Request(url, headers={"User-Agent": "awesome-zotero-curation"})
        try:
            # No GitHub token is ever attached to external requests.
            with build_opener(SafeRedirect(self.external_hosts)).open(request, timeout=25):
                return True
        except HTTPError as exc:
            if exc.code in {404, 410}:
                return False
            raise Unavailable(f"Link check returned HTTP {exc.code}", exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise Unavailable("Link check temporarily unavailable") from exc


def published_releases(api, repo):
    return [r for r in api.pages(f"/repos/{repo}/releases") if not r.get("draft") and r.get("published_at")]


def activity(api, repo, meta):
    rows = api.get(f"/repos/{repo}/commits?" + urlencode({"sha": meta["default_branch"], "per_page": 1}))
    releases = published_releases(api, repo)
    commits = [date(rows[0]["commit"]["committer"]["date"])] if rows else []
    dates = commits + [date(r["published_at"]) for r in releases]
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
            try:
                repo = github_repo(item.url)
                if repo:
                    meta = api.get(f"/repos/{repo}")
                    project_found = True
                    if meta.get("archived") or meta.get("disabled"):
                        reason = "Repository archived or disabled"
                    else:
                        last, _, releases = activity(api, repo, meta)
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


def branch_commit(api, repo, branch, base_sha, files):
    """Write only reserved automation branches, atomically, without force pushes."""
    if branch not in {STATE_BRANCH, CLEANUP_BRANCH}:
        raise ValueError("Not an automation branch")
    try:
        old = api.get(f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
        parent = api.get(f"/repos/{repo}/git/commits/{old}")
        if not parent["message"].startswith(COMMIT_PREFIX):
            raise Unavailable(f"Reserved branch {branch} contains a non-automation commit; refusing to overwrite")
    except Unavailable as exc:
        if exc.status != 404:
            raise
        old = None
    base = api.get(f"/repos/{repo}/git/commits/{base_sha}")
    tree = api.request(f"/repos/{repo}/git/trees", "POST", {
        "base_tree": base["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                 for path, content in files.items()]})
    if old and tree["sha"] == parent["tree"]["sha"]:
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


def weekly(api, repo, policy, dry_run):
    meta = api.get(f"/repos/{repo}")
    default = meta["default_branch"]
    base = api.get(f"/repos/{repo}/git/ref/heads/{quote(default, safe='/')}")["object"]["sha"]
    # Ensure generation uses the same trusted base revision we inspected.
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if checkout != base:
        raise Unavailable("Default branch changed after checkout; rerun against the latest revision")
    source = api.text(repo, SOURCE, base)
    try:
        previous = json.loads(api.text(repo, STATE_FILE, STATE_BRANCH))
    except Unavailable as exc:
        if exc.status != 404:
            raise
        previous = {}
    updated, observations, findings, unknown = maintenance(api, source, previous, policy, utcnow())
    body = MARKER + "\n\nWeekly curation under [the maintenance policy](https://github.com/" + repo + "/blob/" + default + "/.github/CURATION.md).\n\n"
    body += "| Project | Action | Evidence |\n| --- | --- | --- |\n"
    for item, action, reason in findings:
        body += f"| [{markdown(item.name)}]({item.url}) | {action} | {markdown(reason)} |\n"
    body += "\nGenerated README is included. No projects are executed. Merge requires maintainer review.\n"
    if unknown:
        body += "\nChecks left inconclusive (these do not cause removals):\n" + "\n".join(f"- {markdown(row)}" for row in unknown)
    print(body)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(body)
    if dry_run:
        return
    if observations != previous:
        branch_commit(api, repo, STATE_BRANCH, base, {STATE_FILE: json.dumps(observations, indent=2) + "\n"})
    pulls = api.pages(f"/repos/{repo}/pulls?" + urlencode({"state": "open", "head": f"{repo.split('/')[0]}:{CLEANUP_BRANCH}", "base": default}))
    if any(MARKER not in (pr.get("body") or "") for pr in pulls):
        raise Unavailable("Cleanup branch has an unrecognized PR; refusing to modify it")
    if updated == source:
        for pr in pulls:
            api.request(f"/repos/{repo}/pulls/{pr['number']}", "PATCH", {"state": "closed", "body": body + "\nNo cleanup changes remain; closing this obsolete proposal.\n"})
        return
    generated = render_readme(updated)
    if not any(line.startswith("# Awesome Zotero") for line in generated.splitlines()) or len(entries(updated)) > len(entries(source)):
        raise ValueError("Generated README failed cleanup validation")
    sha = branch_commit(api, repo, CLEANUP_BRANCH, base, {SOURCE: updated, "README.md": generated})
    status(api, repo, sha, "curation/maintenance", "success", "Rules tested; cleanup and README generation validated")
    status(api, repo, sha, "curation/admission", "success", "Maintenance-only changes; no new projects introduced")
    payload = {"title": "Maintain curated entries: remove stale projects and fix links", "body": body}
    if pulls:
        api.request(f"/repos/{repo}/pulls/{pulls[0]['number']}", "PATCH", payload)
    else:
        api.request(f"/repos/{repo}/pulls", "POST", dict(payload, head=CLEANUP_BRANCH, base=default, draft=True))


@dataclass
class Verdict:
    rejected: list = field(default_factory=list)
    uncertain: list = field(default_factory=list)


FIELDS = {"Project URL", "Project type", "Documentation URL", "Installation URL",
          "Verification", "Distinct value", "Data handling", "AI-generated code"}


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
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    return bool(re.search(r"(?im)^\s*AI-generated code:\s*majority\s*$", text))


def inspect_project(api, item, evidence, policy, now, verdict, existing):
    repo = github_repo(item.url)
    if not repo:
        verdict.uncertain.append("Non-GitHub project: repository age and release evidence need maintainer review")
        return
    try:
        meta = api.get(f"/repos/{repo}")
        if meta["full_name"].lower() in existing:
            verdict.rejected.append("Project resolves to a repository already present in the list")
        if date(meta["created_at"]) > months_ago(now, policy["minimum_age_months"]):
            verdict.rejected.append(f"Repository created {meta['created_at'][:10]}; projects must be at least {policy['minimum_age_months']} calendar months old")
        if meta.get("archived") or meta.get("disabled"):
            verdict.rejected.append("Project repository is archived or disabled")
        if meta.get("size") == 0:
            verdict.rejected.append("Project repository is empty")
        branch = meta["default_branch"]
        try:
            readme = api.readme(repo, branch)
            text = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
            if len(re.sub(r"\s", "", text)) < policy["minimum_readme_characters"]:
                verdict.rejected.append(f"Project README lacks the minimum documentation ({policy['minimum_readme_characters']} non-whitespace characters)")
            if not re.search(r"\bzotero\b", text + " " + item.description, re.I):
                verdict.rejected.append("Neither the project README nor its entry documents a Zotero-specific purpose")
            if majority_disclosure(readme):
                verdict.rejected.append("Project README explicitly declares 'AI-generated code: majority'")
        except Unavailable as exc:
            if exc.status == 404:
                verdict.rejected.append("Project has no accessible README")
            else:
                raise
        last, _, releases = activity(api, repo, meta)
        if now - last >= timedelta(days=policy["inactive_days"]):
            verdict.rejected.append(f"Project has no commits or published releases within {policy['inactive_days']} days")
        if evidence.get("Project type") == "plugin":
            usable = any(not r.get("prerelease") and any(
                a.get("name", "").lower().endswith(".xpi") and a.get("size", 0) > 0
                for a in r.get("assets", [])) for r in releases)
            if not usable:
                verdict.rejected.append("Plugin has no published stable release containing a nonempty .xpi download")
        for name in ("Documentation URL", "Installation URL"):
            if evidence.get(name, "").startswith("https://"):
                try:
                    if not api.link(evidence[name]):
                        verdict.rejected.append(f"{name} points to a missing document or download")
                except Unavailable as exc:
                    if exc.status in {404, 410}:
                        verdict.rejected.append(f"{name} points to a missing document or download")
                    else:
                        verdict.uncertain.append(f"{name}: {exc}")
    except Unavailable as exc:
        # API failure, including a private/missing submitted repository, is not proof of low effort.
        verdict.uncertain.append(f"Cannot complete project inspection: {exc}")


def admission_verdict(api, before, after, body, policy, now):
    verdict = Verdict()
    old, new = entries(before), entries(after)
    old_counts = Counter(key(item.url) for item in old)
    new_counts = Counter(key(item.url) for item in new)
    added = [item for item in new if key(item.url) not in old_counts]
    for identity, count in new_counts.items():
        if count > max(1, old_counts[identity]):
            verdict.rejected.append(f"Duplicate entry: {identity}")
    # Catch new malformed entries instead of allowing alternate formatting to bypass the gate.
    old_lines = set(before.splitlines())
    for line in after.splitlines():
        if line not in old_lines and ANY_ENTRY.fullmatch(line) and not ENTRY.fullmatch(line):
            verdict.rejected.append("New project entries must use '- [Name](https://...) - Description'")
    if not added:
        return verdict
    if len(added) > 1:
        verdict.rejected.append("Submit one new project per PR")
        return verdict
    evidence = evidence_fields(body)
    for name in sorted(FIELDS):
        if not evidence.get(name) or evidence[name].startswith("<"):
            verdict.rejected.append(f"Missing admission evidence: {name}")
    item = added[0]
    if not re.fullmatch(r"https://\S+", evidence.get("Project URL", "")):
        verdict.rejected.append("Project URL must be a plain HTTPS URL")
    if key(evidence.get("Project URL", "")) != key(item.url):
        verdict.rejected.append("Project URL declaration must match the new entry")
    if evidence.get("Project type") not in {"plugin", "integration", "theme", "developer-tool"}:
        verdict.rejected.append("Project type must be plugin, integration, theme, or developer-tool")
    for name in ("Documentation URL", "Installation URL"):
        if not re.fullmatch(r"https://\S+", evidence.get(name, "")):
            verdict.rejected.append(f"{name} must be a plain HTTPS URL")
    for name in ("Verification", "Distinct value", "Data handling"):
        value = evidence.get(name, "")
        if len(value.split()) < 12 or re.search(r"(?i)\b(?:todo|tbd|fill this|replace this)\b", value):
            verdict.rejected.append(f"{name} must supply at least 12 words of concrete evidence, without template placeholders")
    explanations = [evidence.get(name, "").strip().lower() for name in ("Verification", "Distinct value", "Data handling")]
    if all(explanations) and len(set(explanations)) != len(explanations):
        verdict.rejected.append("Verification, Distinct value, and Data handling must provide different evidence; identical copy-pasted answers do not satisfy the template")
    disclosure = evidence.get("AI-generated code", "").lower()
    if disclosure == "majority":
        verdict.rejected.append("Submission declares that most project code is AI-generated; this list does not accept majority-generated projects")
    elif disclosure == "unknown":
        verdict.uncertain.append("AI code provenance is declared unknown; maintainer decision required")
    elif disclosure not in {"none", "some"}:
        verdict.rejected.append("AI-generated code must be none, some, majority, or unknown")
    inspect_project(api, item, evidence, policy, now, verdict, old_counts)
    return verdict


def admission(api, repo, event, policy):
    number = event["pull_request"]["number"]
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    if pr["state"] != "open" or pr.get("draft"):
        return
    sha = pr["head"]["sha"]
    overridden = any(label["name"] == policy["override_label"] for label in pr.get("labels", []))
    if overridden:
        status(api, repo, sha, "curation/admission", "success", "Maintainer applied curation override")
        return
    status(api, repo, sha, "curation/admission", "pending", "Checking new project evidence")
    try:
        files = api.pages(f"/repos/{repo}/pulls/{number}/files")
        names = {f["filename"] for f in files}
        if SOURCE in names:
            before = api.text(repo, SOURCE, pr["base"]["sha"])
            # GitHub exposes fork commit objects in the base repository. Never check them out.
            after = api.text(repo, SOURCE, sha)
            verdict = admission_verdict(api, before, after, pr.get("body"), policy, utcnow())
        elif "README.md" in names and "generate_readme.py" not in names:
            before = api.text(repo, "README.md", pr["base"]["sha"])
            after = api.text(repo, "README.md", sha)
            old = Counter(key(item.url) for item in entries(before))
            new = Counter(key(item.url) for item in entries(after))
            verdict = Verdict(rejected=["New entries must be submitted in _README.md; README.md is generated"] if new - old else [])
        else:
            verdict = Verdict()
    except Unavailable as exc:
        verdict = Verdict(uncertain=[str(exc)])
    # A contributor may have updated their PR while API requests were in flight.
    current = api.request(f"/repos/{repo}/pulls/{number}")
    if (current["head"]["sha"] != sha or current.get("body") != pr.get("body")
            or current["state"] != "open"
            or current.get("draft")
            or any(label["name"] == policy["override_label"] for label in current.get("labels", []))):
        return
    if not verdict.rejected and not verdict.uncertain:
        status(api, repo, sha, "curation/admission", "success", "Automated admission criteria satisfied; editorial review still required")
        update_admission_comment(api, repo, number, MARKER + "\n\nThe updated submission satisfies the automated admission criteria. Maintainer review is still required.", create=False)
        return
    rejected = bool(verdict.rejected)
    body = MARKER + "\n\n" + ("This PR is being closed because it does not meet this list's admission policy:\n\n" if rejected
                                 else "Automated inspection could not reach a decision. This PR remains open:\n\n")
    body += "\n".join(f"- {markdown(reason)}" for reason in dict.fromkeys(verdict.rejected))
    if verdict.uncertain:
        body += "\n\nInconclusive checks (not closure reasons):\n" + "\n".join(f"- {markdown(reason)}" for reason in dict.fromkeys(verdict.uncertain))
    body += ("\n\nSee [the admission policy](https://github.com/" + repo + "/blob/" + pr["base"]["ref"]
             + "/.github/CURATION.md). Correct the reasons above and ask a maintainer to reopen, or resubmit once eligible. "
             "A maintainer can apply `" + policy["override_label"] + "` for a documented exception. "
             "AI authorship is not inferred from coding style, commit volume, or contributor count.")
    update_admission_comment(api, repo, number, body)
    status(api, repo, sha, "curation/admission", "failure" if rejected else "error",
           "Admission policy not satisfied" if rejected else "Evidence unavailable; retry or request maintainer override")
    if rejected:
        # Comment is published before closing, so every automatic closure has its explanation.
        api.request(f"/repos/{repo}/pulls/{number}", "PATCH", {"state": "closed"})


def update_admission_comment(api, repo, number, body, create=True):
    comments = api.pages(f"/repos/{repo}/issues/{number}/comments")
    existing = next((comment for comment in comments if MARKER in comment["body"] and comment["user"]["type"] == "Bot"), None)
    if existing:
        if existing["body"] != body:
            api.request(f"/repos/{repo}/issues/comments/{existing['id']}", "PATCH", {"body": body})
    elif create:
        api.request(f"/repos/{repo}/issues/{number}/comments", "POST", {"body": body})


def retry_admission(api, repo, policy):
    """Retry transient/inconclusive inspections weekly without reopening rejected PRs."""
    for pr in api.pages(f"/repos/{repo}/pulls?state=open"):
        if pr.get("draft"):
            continue
        statuses = api.get(f"/repos/{repo}/commits/{pr['head']['sha']}/status")["statuses"]
        last = next((row for row in statuses if row["context"] == "curation/admission"), None)
        if last and last["state"] in {"error", "pending"}:
            admission(api, repo, {"pull_request": {"number": pr["number"]}}, policy)


def main():
    policy = json.loads((ROOT / ".github/curation.json").read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    if not REPO.fullmatch(repo):
        raise ValueError("Invalid workflow repository")
    api = GitHub(os.environ.get("GH_TOKEN"), policy["external_link_hosts"])
    if sys.argv[1:] == ["weekly"]:
        dry_run = os.environ.get("CURATION_DRY_RUN", "true").lower() != "false"
        weekly(api, repo, policy, dry_run)
        if not dry_run:
            retry_admission(api, repo, policy)
    elif sys.argv[1:] == ["admission"]:
        admission(api, repo, json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()), policy)
    else:
        raise SystemExit("Usage: curation.py weekly|admission")


if __name__ == "__main__":
    main()

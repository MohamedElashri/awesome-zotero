#!/usr/bin/env python3
"""Check PR Markdown as data with the trusted generator; never execute PR code."""

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from curation import (GitHub, MARKER, REPO, ROOT, SOURCE, Unavailable, date,
                      is_cleanup_pr, render_readme, status, utcnow)


CONTEXT = "curation/readme"
WORKFLOW = "test_readme.yml"
LINTER = "awesome-lint@2.3.0"
SHA = re.compile(r"[0-9a-f]{40}")


def signature():
    """Invalidate cached verdicts when trusted generation/validation rules change."""
    return hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "generate_readme.py").read_bytes()).hexdigest()[:12]


def publish(api, repo, sha, state, description):
    status(api, repo, sha, CONTEXT, state, description + f" [{signature()}]")


def dispatch(api, repo):
    """Explicit dispatch runs automatically even for GITHUB_TOKEN-created PRs."""
    default = api.get(f"/repos/{repo}")["default_branch"]
    queued = 0
    for pr in api.pages(f"/repos/{repo}/pulls?state=open&base={default}"):
        if not is_cleanup_pr(repo, pr) or MARKER not in (pr.get("body") or ""):
            continue
        sha = pr["head"]["sha"]
        statuses = api.get(f"/repos/{repo}/commits/{sha}/status").get("statuses", [])
        previous = next((row for row in statuses if row["context"] == CONTEXT), None)
        if previous and previous.get("description", "").endswith(f"[{signature()}]") and (
                         previous["state"] in {"success", "failure"} or
                         previous["state"] == "pending" and
                         utcnow() - date(previous["created_at"]) < timedelta(minutes=20)):
            continue
        publish(api, repo, sha, "pending", "README generation and lint queued automatically")
        try:
            api.request(f"/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches", "POST", {
                "ref": default, "inputs": {"pr_number": str(pr["number"]), "head_sha": sha}})
        except Unavailable:
            publish(api, repo, sha, "error", "README check dispatch failed; awaiting retry")
            raise
        print(f"Dispatched README check for PR #{pr['number']} at {sha}")
        queued += 1
        if queued >= 20:
            break


def lint(source):
    # Both the generator and npm invocation come from the trusted checkout.
    # Candidate files, npm configuration, and scripts are never checked out.
    # Keep the trusted Git/CONTRIBUTING context needed by awesome-lint without
    # overwriting README.md or loading any configuration from the PR head.
    generated = render_readme(source)
    path = ROOT / f"readme-check-{uuid.uuid4().hex}.md"
    document = path.open("x", encoding="utf-8")
    try:
        with document:
            document.write(generated)
        environment = {name: value for name, value in os.environ.items()
                       if name not in {"GH_TOKEN", "GITHUB_TOKEN", "NODE_AUTH_TOKEN"}}
        subprocess.run(["npx", "--yes", LINTER, str(path)], cwd=ROOT,
                       env=environment, check=True, timeout=300)
    finally:
        path.unlink()


def check(api, repo, number=None, expected_sha=None):
    if number is None:
        lint((ROOT / SOURCE).read_text())
        return
    if not str(number).isdigit() or int(number) <= 0 or expected_sha and not SHA.fullmatch(expected_sha):
        raise ValueError("Invalid PR number or head SHA")
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    if pr["state"] != "open" or expected_sha and pr["head"]["sha"] != expected_sha:
        print("PR closed or head changed; skipping the superseded README check")
        return
    sha = pr["head"]["sha"]
    origin = (pr["head"].get("repo") or {}).get("full_name", "")
    if not SHA.fullmatch(sha) or not REPO.fullmatch(origin):
        raise ValueError("PR head is unavailable or malformed")
    publish(api, repo, sha, "pending", "Checking PR Markdown with the trusted README generator")
    try:
        source = api.text(origin, SOURCE, sha)
        lint(source)
    except subprocess.CalledProcessError:
        publish(api, repo, sha, "failure", "README generation or awesome-lint failed; see run logs")
        raise
    except (Unavailable, OSError, subprocess.TimeoutExpired):
        publish(api, repo, sha, "error", "README inspection could not complete; see run logs")
        raise
    publish(api, repo, sha, "success", "README generated and awesome-lint passed")


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    if not REPO.fullmatch(repo):
        raise ValueError("Invalid workflow repository")
    api = GitHub(os.environ.get("GH_TOKEN"))
    if sys.argv[1:] == ["dispatch"]:
        dispatch(api, repo)
    elif sys.argv[1:] == ["check"]:
        number, sha = os.environ.get("README_PR_NUMBER"), os.environ.get("README_HEAD_SHA")
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target":
            pr = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["pull_request"]
            number, sha = pr["number"], pr["head"]["sha"]
        check(api, repo, number or None, sha or None)
    else:
        raise SystemExit("Usage: readme_check.py dispatch|check")


if __name__ == "__main__":
    main()

# Automated curation

This is a selective recommendation list. Passing automated checks makes a project
eligible for editorial review; it does not guarantee inclusion. Stars, forks,
contributor counts, and commit volume are not quality requirements.

## Weekly maintenance

Every Monday at 06:23 UTC, the bot checks `_README.md` and maintains a single draft
PR on `automation/weekly-curation`. It proposes removal when:

- Both the latest default-branch commit and the latest published release are at
  least 365 days old. Projects without releases use their default-branch commit.
- A project is archived or disabled.
- A project link returns 404 or 410 in three spaced checks.
- A project previously published `.xpi` assets, but none remain across three
  spaced checks. This verifies assets exposed by GitHub's release API, not an
  installation or download of the plugin.
- The same project URL appears more than once (the first occurrence is kept).

Repository transfers produce URL corrections. Missing checks must be at least six
days apart, so repeated manual runs cannot accelerate removal. A successful check
or an inconclusive check resets the missing streak. Timeouts, rate limits, 403s,
server errors, unsupported URL hosts, or unavailable commit/release history never
cause removals. A published release, including a prerelease, counts as activity.
This is deliberately a date rule: the bot does not judge commit substance or
claim that inactivity proves a project is broken.

The bot regenerates `README.md`, runs its rule tests, and includes evidence in the
PR. A recovered project is withdrawn from the next update. If nothing needs
changing, no PR is created, and an obsolete cleanup PR is closed. Nothing is
automatically merged.

Missing-link counters and previously observed downloads are stored on the reserved
`automation/curation-state` branch. This avoids weekly state-only PRs. Do not edit
either automation branch; the bot refuses to write over a non-automation commit.
Changes to the source list or exceptions belong on the default branch.

For a quiet project that remains useful, add its lowercase `owner/repo` to
`inactivity_exemptions` in `.github/curation.json`, with an explanatory string.
For example: `"owner/project": "Finished tool; verified useful on supported Zotero"`.
This exemption persists until removed and only suppresses inactivity removals.
External projects can have their HTTPS hosts added to `external_link_hosts`;
their link availability is checked, but GitHub activity rules do not apply.

## New-project admission

Ready-for-review PRs introducing new source-list entries are checked automatically.
Draft PRs are left alone until marked ready. Fixes, removals, and infrastructure
changes do not require new-project evidence. Pre-existing duplicate entries do not
cause unrelated submissions to be closed.

The bot comments with exact reasons and closes a PR for any of these violations:

- Repository created less than **two calendar months** ago (not a star or commit
  threshold, and not an estimate based on the project's README).
- Duplicate project, including an extra duplicate of an existing entry, or more
  than one unrelated new project in the same PR.
- Archived, disabled, or empty repository, or no activity within 365 days.
- Missing README or fewer than 200 non-whitespace characters of documentation.
- A declared plugin without a published stable release containing a nonempty
  `.xpi` asset. Integrations, themes, and developer tools need installation evidence
  but are not required to publish `.xpi` files.
- Missing required evidence, missing documentation or installation targets,
  placeholder or identical copy-pasted responses, incorrectly formatted new
  entries, or additions made only to the generated README.
- No documented Zotero-specific purpose in either the entry or project README.
- An explicit `AI-generated code: majority` declaration in the PR evidence or
  the project's README. “Majority” means more than half of the project code.

The new-project evidence block in the PR template must contain one plain value per
line. Verification, distinct value, and data handling each need at least 12 words.
Describe a real manual check with its version and outcome, the practical difference
from existing tools, and what data leaves Zotero (or explain why none does).
Length checks screen empty submissions; they cannot verify the truth of claims.

AI provenance declarations are `none`, `some`, `majority`, or `unknown`. AI-assisted
development is allowed when the project meets the bar. The bot does not infer the
percentage of generated code from style, an `AGENTS.md`, AI-related features,
commit messages, or a single contributor. `unknown` keeps the PR open with an
inconclusive admission status; it is not an accusation of AI authorship.

GitHub failures, non-GitHub projects, and unsupported evidence URLs keep the PR open
with an inconclusive status. Supported GitHub evidence links include repository
roots, `blob`/`tree` documents, and release/tag/download pages. Branch names with
slashes and custom documentation hosts may need an override. The bot never
executes submitted projects or contributor code. It cannot establish working
behavior or compatibility through metadata alone. Editorial inclusion still
requires maintainer approval.

Open PRs with pending or inconclusive admission statuses are retried by the weekly
workflow. Rejected PRs are not automatically reopened. Fixed submissions update
the existing bot comment instead of adding a new comment on every run.

Correct a rejected submission and ask for reopening, or resubmit once eligible.
A maintainer can apply the `curation:override` label for an exception, then reopen
the PR. Keep a brief human explanation in the PR. This bypasses admission, not
other repository checks. Removing the label re-enables evaluation.

## Setup and operation

- Merge the workflow, script, policy, and tests together into the default branch.
- In Actions settings, enable **Allow GitHub Actions to create and approve pull
  requests**. The workflows use the built-in `GITHUB_TOKEN`; no paid API or extra
  secret is required. Organization settings may restrict this permission.
- Add `curation/admission` as a required status check for merging new submissions.
  A metadata pass is not an automated endorsement and never merges a PR.
- Reserve `automation/weekly-curation` and `automation/curation-state` for the bot.
  If rulesets restrict branch creation or commits, allow the maintenance workflow
  to write these branches without granting it default-branch bypass permissions.
- Use **Weekly curation → Run workflow** with `dry_run: true` to preview current
  findings. Manual runs default to dry-run; scheduled runs write their results.
  Dry runs never advance counters or modify branches, statuses, comments, or PRs.
- Run local checks with `python3 -m unittest discover -s tests -v`.

The weekly run validates its own generated changes and publishes
`curation/maintenance` and `curation/admission` on its commit. Do not rely on a token-created PR to trigger
another workflow automatically: GitHub may require approval for those runs.
The admission workflow uses `pull_request_target` solely with trusted base code
and reads submitted files as data through the API. It never checks out a PR head,
installs its dependencies, or interpolates PR text into a shell command.

If GitHub disables scheduled workflows on an inactive public repository, re-enable
the workflow in Actions. Scheduled delivery is managed by GitHub and may be delayed.

# Automated curation

This is a selective recommendation list. Automation screens submissions and
prepares maintenance changes; passing checks is not an endorsement or a guarantee
of working behavior. Stars, forks, contributor counts, and commit volume are not
quality requirements. Inclusion still requires a maintainer's editorial decision.

## Weekly maintenance

Every Monday at 06:23 UTC, the bot checks `_README.md` and maintains one draft PR
on `automation/weekly-curation`. It proposes removal for:

- No default-branch commits or published releases within 365 days. Projects
  without releases use their default-branch commit date.
- An archived or disabled repository.
- A project link returning 404 or 410 across three checks at least six days apart.
- Previously observed `.xpi` assets disappearing from published GitHub releases
  across three spaced checks. This checks release metadata, not installation.
- Duplicate project URLs, keeping the first occurrence.

Repository moves produce URL corrections. These are housekeeping signals, not
proof of poor quality or broken compatibility. A prerelease counts as activity.
Archived and quiet projects can still be useful; review the proposed changes
before merging. Nothing is automatically merged.

Missing-link counters, observed downloads, and confirmed findings persist on
`automation/curation-state`. An inconclusive run never establishes a new removal.
A previously confirmed removal stays in the same PR, marked **awaiting recheck**,
and its admission/maintenance statuses become pending. A conclusive recovery
withdraws it. If no changes remain after conclusive checks, the bot closes the
obsolete PR. Transient failures do not close and recreate the cleanup PR.
Missing streaks that have not yet established a removal restart after an
inconclusive check; manual runs cannot accelerate the six-day interval.

The bot regenerates `README.md` from the trusted generator and runs its rule tests.
It writes only its reserved branches and refuses to overwrite a branch whose
latest commit is not an automation commit. Do not edit these branches manually.
Source-list changes and policy exceptions belong on the default branch.

A quiet but useful project can receive a persistent inactivity exception in
`inactivity_exemptions` in `.github/curation.json`, keyed by lowercase `owner/repo`
and accompanied by a reason. This only suppresses inactivity removals. External
project URLs receive safe link checks; GitHub activity rules do not apply to them.

## New-project admission

Draft PRs are left alone until marked ready. Existing-project description changes,
removals, and verified repository URL moves do not need new-project evidence.
Unresolved URL moves stay inconclusive rather than being assumed to be new tools.
Pre-existing duplicates do not cause unrelated submissions to be closed.

Entries must use `- [Name](https://...) - Description` in `_README.md`. New
reference links, angle-bracket links, HTML links, and alternate list formats get
correction requests rather than silently bypassing checks. Entries added to the
generated `README.md` must exist in the proposed source, even when the PR also
changes the generator or touches `_README.md`.

Definite policy violations receive an explanation and immediate closure:

- Repository created less than **two calendar months** ago. This is a cooling-off
  period measured from GitHub repository creation, not proof of project maturity.
- Duplicate projects, or multiple unrelated new projects in one PR.
- An archived/disabled repository, reported zero repository size, or no inspected
  activity within 365 days.
- A declared tested release that is draft/prerelease, an absent plugin asset in
  that release, or a conclusively invalid plugin archive/compatibility declaration.
- An explicit majority-AI-generated declaration in the submission or project
  README. Recognized plain statements that most or all source code was generated
  by AI also count; code examples, quotations, and negative statements do not.

## Verifiable evidence and correction window

Missing fields, documentation problems, placeholder or identical copied answers,
and entry formatting errors get a **48-hour correction window**. The PR stays
open with a pending admission status. Edits trigger rechecks without restarting
that deadline. Daily checks close unresolved submissions at the first recheck
following the deadline; this is not a guarantee of closure exactly at hour 48.
A corrected submission updates the existing bot comment and clears its deadline.
Transient API/download failures alone never become closure reasons.

Supply the template's plain fields, once each:

- `Project URL` and `Project type`.
- `Documentation URL` and `Installation URL` pointing to accessible HTTPS targets.
- `Tested version`: an exact published stable release tag in this repository.
  Non-plugin integrations, themes, and developer tools may instead use
  `commit:` followed by a full 40-character commit SHA.
- `Tested Zotero version`: the exact numeric version used during verification.
- `Verification`: the actual check and outcome, naming both declared versions.
- `Distinct value` and `Data handling`: specific explanations of the benefit and
  relevant uploads, permissions, costs, and destructive operations (or their absence).
- `AI-generated code`: `none`, `some`, `majority`, or `unknown`.

The 200-character README floor screens placeholders; it does not measure quality.
There is no arbitrary minimum word count for explanation fields. Verified versions
and artifacts provide the stronger evidence. Claims of usefulness, real-world
usage, human review, and privacy still require editorial judgment.

## Plugin artifact inspection

Plugin classification is checked independently of the submitter's selected type:
source Zotero manifests, published `.xpi` assets, and an entry explicitly describing
a Zotero plugin/add-on trigger artifact verification. Three common source manifest
locations are inspected. This is bounded static inspection, not a general code
classifier; an undetected or deliberately concealed plugin still needs human review.

The bot downloads up to three `.xpi` candidates from the declared stable release,
through GitHub's asset API. It never installs, imports, or executes those files.
Archives must be valid ZIP files containing nonempty root `manifest.json` and
`bootstrap.js`, manifest version 2, a plugin name/version/ID, and Zotero minimum and
maximum compatibility bounds. When the source declares a literal plugin ID, it
must match the release artifact. The comment records the release, actual plugin
version and ID, declared compatibility range, and SHA-256 digest.

The range must include both the declared tested Zotero version and the current
stable version identified from Zotero's official changelog. An unavailable or
unrecognized changelog keeps inspection inconclusive. If an embedded range is
outdated, the bot can use the artifact's published update manifest when it matches
the exact plugin version and SHA-256 (or the same GitHub asset URL when no digest
is published). Unverifiable overrides and unsupported Mozilla version syntax stay
inconclusive. Range checks establish declared compatibility, not runtime behavior.

Downloads default to 25 MiB. ZIP inspection limits member count, expanded size,
and manifest size and rejects unsafe paths, symlinks, encrypted/duplicate entries,
and malformed metadata. Files are never extracted into the workspace.

## Provenance, links, and bounded requests

AI provenance is self-declared. The bot cannot estimate generated-code percentage
or detect dishonest declarations. `unknown` does not block otherwise valid evidence.
Coding style, AI-related functionality, `AGENTS.md`, a single contributor, and
commit volume are not authorship evidence. The majority rule is an explicit
provenance policy, not an effective substitute for artifact and quality checks.

Custom documentation hosts can be checked automatically. Public HTTPS requests
use port 443, validate every redirect, reject non-public DNS addresses, connect
to the validated address with hostname-verified TLS, ignore environment proxies,
and never carry the GitHub token. GitHub document links resolve slash-containing
refs and percent-encoded paths before declaring a target missing. Unsupported or
ambiguous URLs remain inconclusive.

Inspections default to 24 read requests per project and 600 per workflow invocation,
plus a small reporting reserve. Individual projects have a 90-second inspection
budget, and the inspection phase has an eight-minute budget, checked before new
requests. Pagination and archive candidates are bounded;
release history stops once enough activity/download evidence exists. Exhausted
budgets remain inconclusive. These limits do not coordinate every workflow's use
of GitHub's shared repository token allowance. Heavy histories may need an explicit
exception or a higher configured budget rather than an unsupported removal claim.

## Setup and operation

- Merge workflows, scripts, policy, and tests together into the default branch.
- Enable **Allow GitHub Actions to create and approve pull requests** in Actions
  settings. The workflows use `GITHUB_TOKEN`; no paid API or extra secret is needed.
- Require `curation/admission` for merging. The weekly bot publishes this status
  itself so its maintenance PR does not depend on another token-triggered workflow.
- Reserve `automation/weekly-curation` and `automation/curation-state` for the bot.
  Allow writes to those branches where rulesets require it, without granting a
  default-branch bypass.
- Weekly curation's manual dispatch defaults to `dry_run: true`; it never advances
  counters or modifies remote branches, PRs, statuses, or comments. It can preview
  local branch code against the latest source without changing GitHub.
- Daily admission rechecks run at 07:43 UTC. Up to ten ready PRs with missing,
  pending, failed, or inconclusive admission statuses are selected, oldest attempts first.
  Rechecks share per-PR concurrency with event-triggered inspection. Closed PRs are
  not automatically reopened, and drafts are excluded.
- Correct a closed submission and request reopening, or resubmit when eligible.
  A maintainer can apply `curation:override` for a documented exception and reopen
  the PR. This bypasses admission, not other checks; removing it resumes evaluation.
- Run local checks with `python3 -m unittest discover -s tests -v`.

Privileged admission jobs use trusted base/default-branch code and read PR content
as data. They never check out a PR head or run a submitted project's code. GitHub
can delay scheduled runs and disable public-repository schedules after inactivity;
re-enable them in Actions if necessary. Automatic merging is never enabled here.

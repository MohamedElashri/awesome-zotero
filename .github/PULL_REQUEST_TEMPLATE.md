<!--
MANDATORY: Ordinary contributor PRs MUST follow this template. PRs missing required sections/checklists may be closed.
Ordinary PR content and repository changes must be prepared by the human contributor without AI or agent assistance. Explicit maintainer authorization and repository curation bots are exceptions. Do not use AI to prepare an ordinary submission.
-->

> [!IMPORTANT]
> Ordinary contributions must be written entirely by you, the human contributor. AI and coding-agent assistance is prohibited for the PR title, description, template responses, comments, review replies, commit messages, and repository changes. Human editing of AI-generated text does not make it acceptable. Explicitly authorized maintainer work and repository curation bots are exceptions and must not claim human authorship.

## Automated admission evidence (new projects only)

<!--
Keep each field on one plain line, with one occurrence of each field.
Replace placeholders with your own evidence. Remove this block for changes that
do not introduce a new project. Draft PRs are checked when marked ready.

Project type: plugin | integration | theme | developer-tool
AI-generated code: none | some | majority | unknown
Majority means more than half of project code. Do not guess if unknown.
Verification, Distinct value, and Data handling each need at least 12 words.
Projects must be at least two calendar months old. Plugins need a stable .xpi
release. Include supported GitHub documentation and installation URLs, or request
a maintainer exception for other hosts. See .github/CURATION.md for the rules.
-->

Project URL: <plain HTTPS URL matching the entry>
Project type: <choose one type>
Documentation URL: <plain HTTPS URL to working documentation>
Installation URL: <plain HTTPS URL to installation instructions or release>
Verification: <describe the version, test performed, and actual outcome>
Distinct value: <explain the practical benefit beyond Zotero and existing entries>
Data handling: <explain data sent externally, permissions, costs, and destructive operations or why none apply>
AI-generated code: <choose none, some, majority, or unknown>

## Types of Changes

> What types of changes does your PR introduce? Put an x in all boxes that apply

- [ ] New addition to list
- [ ] New category to list
- [ ] Fixing bug in existing item in list
- [ ] Removing item from list
- [ ] Changing structure (organization) of list

## Proposed Changes

### Why this change is needed

- Describe the problem and the expected outcome.
- If this PR is not a bugfix/addition, explain why the change is needed.

### Curation rationale

- Explain why this project belongs in a curated Zotero list.
- Link to working documentation for the submitted project.
- Explain how this project is different from existing entries, if similar tools already exist.
- Note whether the project is actively maintained, has releases, or has other evidence of real-world use.

### Tool quality

- Explain the project maturity: release status, maintenance status, and whether typical Zotero users can use it today.
- Describe the Zotero-specific functionality, not only the general technology used.
- If the submitted project was mostly AI-assisted or generated, explain what human design, review, testing, and maintenance happened afterward.
- Note any user-facing data, privacy, security, or permission considerations.

## PR Checklist

Put an x in the boxes once you've completed each step. You can also fill these out after creating the PR. If you're unsure about any of them, don't hesitate to ask. We're here to help! This is simply a reminder of what we are going to look for before merging your code.

- [ ] I have read the CONTRIBUTING guide
- [ ] I have explained why this PR is important
- [ ] I have added/updated necessary documentation (if appropriate)
- [ ] I have added tests or a clear validation plan (manual checks, commands run)
- [ ] I have explained why this item meets the curation bar for this list
- [ ] I have confirmed the submitted project has working documentation
- [ ] I have confirmed the submitted project is usable and not only a placeholder/demo repository
- [ ] I have described the submitted project's Zotero-specific value

### Human authorship checklist (ordinary contributions)

- [ ] I prepared this contribution without using an AI or coding agent to inspect, change, or otherwise work on this repository.
- [ ] I wrote the PR title, description, template responses, comments, review replies, commit messages, and repository text entirely myself.
- [ ] I did not use AI to draft, rewrite, paraphrase, translate, autocomplete, or polish any part of this contribution or its PR content.
- [ ] If the submitted project used AI during development, it still has clear documentation, maintainer ownership, and evidence of usefulness.
- [ ] If the submitted project is AI-assisted, it is not mostly unreviewed generated boilerplate or a low-effort vibe coded tool.
- [ ] This PR is not low-effort (clear rationale, focused scope, and concrete verification).
- [ ] I have confirmed this is not duplicate work (if fixing/adding items).

### Scope and quality checks

- [ ] I am submitting one PR per unrelated item/intent.
- [ ] New items are added in alphabetical order and in the correct category.
- [ ] For list or category changes, `_README.md` is updated (README is generated).

## Proposed Verification

- [ ] Manual verification performed (describe commands and outcomes below)
- [ ] Automated tests/checks run (if none, explain why)

## Further Comments

If this is a relatively large or complex change, describe why you chose this solution and any alternatives you considered.

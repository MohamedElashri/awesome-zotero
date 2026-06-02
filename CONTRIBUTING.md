Please note that this project is released with a [Contributor Code of Conduct](code-of-conduct.md). By participating in this project you agree to abide by its terms.

---

This repository is a curated list of Zotero plugins, integrations, and related tools. Inclusion is not automatic just because a project exists.

Contribution quality is expected by default. Using LLMs during development is fine, but every submitted entry must be useful, documented, Zotero-relevant, and human-reviewed before opening a PR.

## Required PR workflow

All pull requests MUST follow the exact template in [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md).  
PRs missing required sections or checklists will be closed.

- Each pull request should include a single, well-scoped addition or change.
- Use `_README.md` as your source of truth. `README.md` is generated from this file.
- Ensure the project entry does not duplicate existing entries.
- Add clear documentation for any functional or user-visible change.
- Add new entries in alphabetical order.
- Place items in the correct category, or propose a new category with a rationale.
- Use one PR per new item unless changes are tightly related.
- Format the PR title as `Add item: item name` or `Fix item: item name`.

## Curation policy

Newly created tools, experimental projects, and AI-generated projects are not accepted by default. A submitted project should have enough substance for readers to trust that it is useful.

Before submitting a new item, make sure it has:

- A clear Zotero-related purpose.
- Working documentation that explains what the tool does and how to use it.
- A public source or project page with enough detail to evaluate it.
- Evidence of maintenance, usability, or real-world usefulness.

Low-effort or speculative submissions may be closed even when the linked project is technically related to Zotero.

## Tool quality policy

This list does not accept tools that appear to be mostly generated output with little maintainer ownership. A tool may use AI during development, but it must still show care, usefulness, and ongoing responsibility from its maintainer.

Tools may be rejected if they appear to be:

- Vibe coded with no clear design choices, project scope, or maintainer understanding.
- Mostly generated boilerplate with minimal Zotero-specific functionality.
- Missing working documentation, setup instructions, screenshots, examples, or usage notes.
- Published only as a rough demo, placeholder repository, or one-off experiment.
- Duplicating existing tools without explaining a meaningful difference.
- Claiming features that are not implemented, documented, or verifiable.
- Unmaintained, unreleased, or not usable by typical Zotero users.
- Risky for users because permissions, data handling, privacy, or security implications are unclear.

New or AI-assisted tools can still be accepted when they demonstrate real value: clear Zotero integration, tested behavior, useful documentation, maintainer ownership, and enough maturity that readers can reasonably try or rely on them.

## AI-assistance policy

- AI-generated code and recommendations are allowed.
- Completely AI-generated or "vibe coded" tools are not accepted as list entries unless they meet the same curation bar as any other project.
- PRs that are entirely copy-paste AI output without meaningful curation, local reasoning, or manual review are not accepted.
- If AI was used, the author must:
  - review the proposed changes line-by-line,
  - adapt wording and implementation to the project’s conventions,
  - verify assumptions against project context.
- PR descriptions and comments must be authored/edited by a human contributor. Submitting unchanged, AI-generated PR text is not accepted and will result in a ban.
- Submissions that fail to demonstrate understanding of the changes (for example, adding a new item without proper documentation) are low-effort and will be closed.

## Minimum evidence expected in every PR

- What changed and why.
- Where it is documented (or why documentation is not needed).
- What was verified (manual test, smoke check, or automated test).
- If AI assisted, a short note describing what was delegated and what was manually validated.

Low-effort PRs are often rejected even if technically correct.

Thank you for your suggestions!
